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

global arp_cache_lookup
global arp_cache_insert

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

section .note.GNU-stack noalloc noexec nowrite progbits
