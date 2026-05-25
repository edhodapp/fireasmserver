; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; ARP cache (x86_64) — D068 working order item 6, phase 6.a.
;
; Provides the table data structure + lookup/insert primitives.
; Hooked into the dispatcher's RX-side ARP REPLY recognition
; in phase 6.b; arp_resolve API + initiator land in 6.c-6.d;
; timer + state machine in 6.e; gratuitous ARP at boot in 6.f.
;
; Layout — see arch/x86_64/platform/firecracker/regions.yaml
; (`arp_cache` region) for the per-byte details. Summary:
;
;   header (64 B):
;     +0  u32 generation_counter (LRU clock, single-writer-bumped)
;     +4  u32 entries_used       (informational; not load-bearing yet)
;
;   16 × 32-byte entries starting at +64:
;     +0  u32 ip                       (BE wire bytes; 0 sentinel + state==FREE = empty slot)
;     +4  u8[6] mac
;    +10  u8 state                     (0=FREE, 1=INCOMPLETE, 2=REACHABLE, 3=STALE, 4=PROBE, 5=FAILED)
;    +11  u8 retry_count
;    +12  u32 last_access_gen          (LRU bookkeeping)
;    +16  u64 last_event_time_cycles   (phase 6.e)
;    +24  u64 next_action_time_cycles  (phase 6.e)
;
; Public API:
;
;   arp_cache_lookup(edi = ip_be)
;     → rax = pointer to 6-byte MAC if found (state != FREE),
;       0 otherwise. Bumps the entry's last_access_gen on hit
;       so insert's LRU eviction prefers least-recently-used.
;
;   arp_cache_insert(edi = ip_be, rsi = mac_ptr, edx = new_state)
;     Three-pass slot selection in precedence order:
;       Pass 1: existing IP match  → update in place.
;       Pass 2: any FREE slot      → take it.
;       Pass 3: smallest LRU gen   → evict.
;     One of the three always succeeds for a 16-slot cache.
;     Resets retry_count to 0; bumps + stamps lru_gen; does
;     NOT touch the timing fields (phase 6.e populates them).
;
; Calling convention: SysV. Lookup preserves rdi, rsi, rbx,
; rbp, r12-r15. Insert preserves the same set; uses rbx
; internally so push/pops it.

ARP_CACHE_ENTRIES         equ 16
ARP_CACHE_ENTRY_BYTES     equ 32
ARP_CACHE_HEADER_BYTES    equ 64

ARP_CACHE_HDR_GEN         equ 0

ARP_ENTRY_IP              equ 0
ARP_ENTRY_MAC             equ 4
ARP_ENTRY_STATE           equ 10
ARP_ENTRY_RETRY           equ 11
ARP_ENTRY_LRU_GEN         equ 12

ARP_STATE_FREE            equ 0

extern __memreq_addr__arp_cache

; TX API surface — see arch/x86_64/l2/tx_api.S. arp_send_request
; below allocates a pool buffer, writes the 28-byte ARP request
; body, and enqueues with broadcast dst + 0x0806 BE etype.
extern l2_tx_buffer_pool_alloc
extern l2_tx_buffer_addr
extern l2_tx_enqueue

; emit_bytes / emit_hex32 for the ARP:REQUEST_SENT marker.
extern emit_bytes
extern emit_hex32

L2_TX_POOL_IDX_FULL       equ 0xFFFFFFFF
L2_TX_PAYLOAD_OFFSET      equ 26

; GUEST_MAC + GUEST_IP wire constants. Mirrored from
; dispatcher.S; the cross-file consistency is human discipline
; (the dispatcher file's `arp_request_msg` and the inbound
; recognition's TPA constant both reflect the same source of
; truth — GUEST_MAC = 02:00:00:00:00:01, GUEST_IP =
; 192.168.42.2 → wire bytes c0 a8 2a 02).
GUEST_MAC_LE_LO32         equ 0x00000002
GUEST_MAC_LE_HI16         equ 0x00000100
GUEST_IP_WIRE_LE          equ 0x022AA8C0

global arp_cache_lookup
global arp_cache_insert
global arp_send_request

[bits 64]
section .text

; ================================================================
; arp_cache_lookup — find an entry by IP
; ================================================================
arp_cache_lookup:
    mov r8, [rel __memreq_addr__arp_cache]
    lea r9, [r8 + ARP_CACHE_HEADER_BYTES]              ; r9 = entries[0]
    xor ecx, ecx
.scan:
    mov eax, [r9 + ARP_ENTRY_IP]
    cmp eax, edi
    je  .candidate
.next:
    add r9, ARP_CACHE_ENTRY_BYTES
    inc ecx
    cmp ecx, ARP_CACHE_ENTRIES
    jb  .scan
    xor eax, eax                                       ; miss
    ret
.candidate:
    ; IP match — confirm slot is not FREE (allocator zero-init
    ; leaves every entry's ip=0; a lookup for ip=0 would spuriously
    ; hit a never-used slot). Defense costs one byte-cmp.
    cmp byte [r9 + ARP_ENTRY_STATE], ARP_STATE_FREE
    je  .next
    ; Hit. Bump generation counter + stamp this entry's LRU gen.
    mov eax, [r8 + ARP_CACHE_HDR_GEN]
    inc eax
    mov [r8 + ARP_CACHE_HDR_GEN], eax
    mov [r9 + ARP_ENTRY_LRU_GEN], eax
    lea rax, [r9 + ARP_ENTRY_MAC]
    ret

; ================================================================
; arp_cache_insert — insert or update an entry
; ================================================================
arp_cache_insert:
    push rbx                                           ; preserve caller's rbx
    mov ebx, edx                                       ; ebx = state arg (frees edx for walker)
    mov r8, [rel __memreq_addr__arp_cache]
    lea r9, [r8 + ARP_CACHE_HEADER_BYTES]              ; r9 = entries[0]

    ; --- Pass 1: existing IP match ---
    mov rdx, r9
    xor ecx, ecx
.pass1_loop:
    mov eax, [rdx + ARP_ENTRY_IP]
    cmp eax, edi
    je  .write                                          ; rdx points at matching slot
    add rdx, ARP_CACHE_ENTRY_BYTES
    inc ecx
    cmp ecx, ARP_CACHE_ENTRIES
    jb  .pass1_loop

    ; --- Pass 2: any FREE slot ---
    mov rdx, r9
    xor ecx, ecx
.pass2_loop:
    cmp byte [rdx + ARP_ENTRY_STATE], ARP_STATE_FREE
    je  .write
    add rdx, ARP_CACHE_ENTRY_BYTES
    inc ecx
    cmp ecx, ARP_CACHE_ENTRIES
    jb  .pass2_loop

    ; --- Pass 3: LRU eviction ---
    ; Track victim in rdx + best-seen gen in r10d. Initialise to
    ; entry 0 and scan from entry 1 upward.
    mov rdx, r9
    mov r10d, [r9 + ARP_ENTRY_LRU_GEN]
    lea r11, [r9 + ARP_CACHE_ENTRY_BYTES]              ; walker = entries[1]
    mov ecx, 1
.pass3_loop:
    mov eax, [r11 + ARP_ENTRY_LRU_GEN]
    cmp eax, r10d
    jae .pass3_next
    mov rdx, r11
    mov r10d, eax
.pass3_next:
    add r11, ARP_CACHE_ENTRY_BYTES
    inc ecx
    cmp ecx, ARP_CACHE_ENTRIES
    jb  .pass3_loop

.write:
    ; rdx = chosen slot. Write IP + MAC + state + reset retry.
    mov [rdx + ARP_ENTRY_IP], edi
    mov eax, [rsi]                                     ; MAC bytes 0-3
    mov [rdx + ARP_ENTRY_MAC], eax
    movzx eax, word [rsi + 4]                          ; MAC bytes 4-5
    mov [rdx + ARP_ENTRY_MAC + 4], ax
    mov [rdx + ARP_ENTRY_STATE], bl                    ; state byte
    mov byte [rdx + ARP_ENTRY_RETRY], 0
    ; Bump generation counter + stamp this entry's LRU.
    mov eax, [r8 + ARP_CACHE_HDR_GEN]
    inc eax
    mov [r8 + ARP_CACHE_HDR_GEN], eax
    mov [rdx + ARP_ENTRY_LRU_GEN], eax
    pop rbx
    ret

; ================================================================
; arp_send_request — send a broadcast ARP request
; ================================================================
;   In:  edi = target_ip_be (4-byte BE wire value in low 32 bits)
;   Out: eax = 0 on OK, 1 on pool/queue FULL
;   Clobbers: rax, rcx, rdx, rsi, rdi, r8-r11, flags.
;   Stack: 16-byte local frame (preserves alignment); stashes
;          target_ip across the TX API call chain.
;
; Builds the 28-byte ARP request body in the producer's pool
; buffer (SHA = GUEST_MAC, SPA = GUEST_IP, THA = 00...00,
; TPA = target_ip), then enqueues via the L2 TX API with
; broadcast dst MAC + 0x0806 BE ethertype. The dispatcher's
; TX queue-drain consumer (b.3) builds the wire frame and
; submits to virtio on the next iteration. Emits
; ARP:REQUEST_SENT ip=XXXXXXXX after a successful enqueue;
; emits ARP:REQUEST_FULL on pool/queue exhaustion (lets the
; observability surface distinguish the two failure modes).
;
; arp_resolve (phase 6.d) is the L3-side caller; phase 6.f
; will also call this with TPA = GUEST_IP for gratuitous
; ARP at boot.
arp_send_request:
    push rbp
    mov rbp, rsp
    sub rsp, 16                                        ; local frame
    mov [rsp + 0], edi                                 ; stash target_ip across calls

    call l2_tx_buffer_pool_alloc                       ; eax = pool_idx
    cmp eax, L2_TX_POOL_IDX_FULL
    je  .arp_send_full
    mov [rsp + 8], eax                                 ; stash pool_idx

    mov edi, eax
    call l2_tx_buffer_addr                             ; rax = pool buffer base
    add rax, L2_TX_PAYLOAD_OFFSET                      ; rax = payload start

    ; ARP request body (28 bytes).
    mov word  [rax + 0],  0x0100                       ; HTYPE 0x0001 BE
    mov word  [rax + 2],  0x0008                       ; PTYPE 0x0800 BE
    mov byte  [rax + 4],  6                            ; HLEN
    mov byte  [rax + 5],  4                            ; PLEN
    mov word  [rax + 6],  0x0100                       ; OPER 0x0001 BE (request)
    mov dword [rax + 8],  GUEST_MAC_LE_LO32            ; SHA bytes 0-3
    mov word  [rax + 12], GUEST_MAC_LE_HI16            ; SHA bytes 4-5
    mov dword [rax + 14], GUEST_IP_WIRE_LE             ; SPA
    mov dword [rax + 18], 0                            ; THA bytes 0-3 = 0
    mov word  [rax + 22], 0                            ; THA bytes 4-5 = 0
    mov ecx, [rsp + 0]                                 ; target_ip stashed at entry
    mov [rax + 24], ecx                                ; TPA

    ; Enqueue: dst = broadcast, etype = 0x0806 BE, pool_idx,
    ; payload_len = 28.
    lea rdi, [rel arp_broadcast_mac]
    mov esi, 0x0608                                    ; etype 0x0806 BE → LE u16 = 0x0608
    mov edx, [rsp + 8]                                 ; pool_idx
    mov ecx, 28                                        ; payload_len
    call l2_tx_enqueue
    test eax, eax
    jnz  .arp_send_full                                ; queue FULL

    ; Emit ARP:REQUEST_SENT ip=XXXXXXXX
    lea rsi, [rel arp_request_sent_msg]
    mov ecx, arp_request_sent_msg_len
    call emit_bytes
    mov eax, [rsp + 0]
    call emit_hex32
    lea rsi, [rel arp_send_newline]
    mov ecx, 1
    call emit_bytes

    xor eax, eax
    leave
    ret

.arp_send_full:
    lea rsi, [rel arp_request_full_msg]
    mov ecx, arp_request_full_msg_len
    call emit_bytes
    mov eax, 1
    leave
    ret

section .rodata
arp_broadcast_mac:
    db 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
arp_request_sent_msg:    db 'ARP:REQUEST_SENT ip='
arp_request_sent_msg_len equ $ - arp_request_sent_msg
arp_request_full_msg:    db 'ARP:REQUEST_FULL', 10
arp_request_full_msg_len equ $ - arp_request_full_msg
arp_send_newline:        db 10

section .note.GNU-stack noalloc noexec nowrite progbits
