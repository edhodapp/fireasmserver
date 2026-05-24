; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; L2 TX API (x86_64) — producer-facing send interface per
; docs/l2/TX_API.md. Companion to dispatcher.S — this file
; provides the functions an upper-layer (L3, the ARP
; responder once migrated, or test scaffolding) calls to
; queue a frame for transmission; the dispatcher.S TX
; consume phase drains the queue.
;
; Architecture overview:
;
;   PRODUCER (this file's API)
;       │
;       │  l2_tx_buffer_pool_alloc → idx
;       │  (caller fills buffer @ idx with payload bytes)
;       │  l2_tx_enqueue(dst, etype, idx, len)
;       ▼
;   l2_tx_pending ring (memreq region — Vyukov MPSC)
;       │
;       │  CONSUMER (dispatcher.S TX phase)
;       ▼
;   virtio TX desc[0] (existing)
;
; The Vyukov MPSC scheme uses per-slot sequence numbers:
;   - At init: seq[i] = i for i in 0..N-1.
;   - Producer claim: load head, slot = head & mask, read
;     seq[slot]; if seq == head, slot is ready for this
;     producer's round → CAS head → head+1 to claim.
;   - Producer publish: write entry fields, then release-
;     store seq[slot] = head + 1 (one ahead = "consumer
;     can read this round").
;   - Consumer read: read seq[slot] with acquire; if
;     seq == tail + 1, entry is ready. Read fields, then
;     release-store seq[slot] = tail + 1 + N (one round
;     ahead = "producer for round (tail/N + 1) can claim").
;
; The buffer pool uses a generation-counter ring (same
; shape as virtio descriptor rings): two monotonic counters
; (next_alloc + next_free) bracket the in-flight set;
; pool full when (next_alloc - next_free) >= POOL_SIZE.
; FIFO discipline matches the dispatcher's in-order TX
; consume + virtio in-order reclaim, so consumer naturally
; releases buffers in producer order.
;
; Symbol exports for callers (today: test scaffolding;
; tomorrow: L3 + the migrated ARP responder):
;   l2_tx_pending_init     — one-time pre-init of seq[]
;   l2_tx_buffer_pool_alloc → idx or RING_FULL
;   l2_tx_buffer_pool_free(idx)
;   l2_tx_enqueue(dst_mac_ptr, ethertype, pool_idx, payload_len)
;     → 0 on success, 1 on queue FULL

; ----- Constants (mirrored from regions.yaml + TX_API.md) -----

L2_TX_RING_SIZE        equ 16
L2_TX_RING_MASK        equ L2_TX_RING_SIZE - 1
L2_TX_POOL_SIZE        equ 16
L2_TX_POOL_MASK        equ L2_TX_POOL_SIZE - 1
L2_TX_BUFFER_SIZE      equ 2048
L2_TX_HEADER_BYTES     equ 64                  ; cache-line padding before ring slots / pool buffers

; l2_tx_pending header offsets.
L2_TX_PENDING_HEAD     equ 0                   ; atomic u32 — producer claim counter
L2_TX_PENDING_TAIL     equ 8                   ; u32 — consumer-only

; l2_tx_pending ring slot offsets (24 bytes per slot).
L2_TX_SLOT_BYTES       equ 24
L2_TX_SLOT_SEQ         equ 0                   ; u32 — Vyukov publication
L2_TX_SLOT_DST_MAC     equ 8                   ; 6 bytes
L2_TX_SLOT_ETHERTYPE   equ 14                  ; u16 (BE wire)
L2_TX_SLOT_POOL_IDX    equ 16                  ; u32
L2_TX_SLOT_PAYLOAD_LEN equ 20                  ; u16

; l2_tx_buffer_pool header offsets.
L2_TX_POOL_NEXT_ALLOC  equ 0                   ; atomic u32
L2_TX_POOL_NEXT_FREE   equ 8                   ; u32 (consumer-only)

; Return codes.
L2_TX_OK               equ 0
L2_TX_FULL             equ 1
L2_TX_POOL_IDX_FULL    equ 0xFFFFFFFF

extern __memreq_addr__l2_tx_pending
extern __memreq_addr__l2_tx_buffer_pool

global l2_tx_pending_init
global l2_tx_buffer_pool_alloc
global l2_tx_buffer_pool_free
global l2_tx_enqueue
global l2_tx_buffer_addr        ; helper: pool_idx → buffer base

[bits 64]
section .text

; ================================================================
; l2_tx_pending_init — one-time Vyukov seq[i] = i pre-init
; ================================================================
; Allocator zero-init leaves seq[0..N-1] = 0. The Vyukov scheme
; requires seq[i] = i at start. Must be called once after
; init_memory_layout returns and before any producer call.
;
;   In:  nothing.
;   Out: nothing.
;   Clobbers: rax, rcx, rdx.
;   Preserves: rbx, rbp, rsp, r12-r15.
l2_tx_pending_init:
    mov rax, [rel __memreq_addr__l2_tx_pending]
    add rax, L2_TX_HEADER_BYTES                  ; rax = ring base
    xor ecx, ecx                                 ; ecx = i
.loop:
    mov [rax + L2_TX_SLOT_SEQ], ecx              ; seq[i] = i
    add rax, L2_TX_SLOT_BYTES
    inc ecx
    cmp ecx, L2_TX_RING_SIZE
    jb .loop
    ret

; ================================================================
; l2_tx_buffer_pool_alloc — claim a free pool buffer
; ================================================================
;   In:  nothing.
;   Out: eax = pool index (0..POOL_SIZE-1) on success;
;        eax = L2_TX_POOL_IDX_FULL (0xFFFFFFFF) when pool empty.
;   Clobbers: ecx, edx, r8, r9, flags.
;
; Generation counter ring: next_alloc is atomically claimed via
; LOCK CMPXCHG; next_free is consumer-only. Pool full when
; (next_alloc - next_free) >= POOL_SIZE.
l2_tx_buffer_pool_alloc:
    mov r8, [rel __memreq_addr__l2_tx_buffer_pool]
.cas_retry:
    mov eax, [r8 + L2_TX_POOL_NEXT_ALLOC]        ; eax = expected (current head)
    mov edx, [r8 + L2_TX_POOL_NEXT_FREE]         ; edx = current free counter
    mov ecx, eax
    sub ecx, edx                                 ; ecx = in-flight
    cmp ecx, L2_TX_POOL_SIZE
    jae .full
    lea r9d, [eax + 1]                           ; r9d = head + 1
    ; LOCK CMPXCHG: if [next_alloc] == eax → [next_alloc] = r9d, ZF=1
    lock cmpxchg [r8 + L2_TX_POOL_NEXT_ALLOC], r9d
    jne .cas_retry
    ; Successfully claimed. Return idx = head & MASK.
    and eax, L2_TX_POOL_MASK
    ret
.full:
    mov eax, L2_TX_POOL_IDX_FULL
    ret

; ================================================================
; l2_tx_buffer_pool_free — release a pool buffer (consumer-only)
; ================================================================
;   In:  edi = pool_idx (the value the corresponding alloc
;        returned; ignored here — we trust FIFO order).
;   Out: nothing.
;   Clobbers: rax.
;
; Consumer-only: only the dispatcher's TX consume phase calls
; this, and it processes requests in ring order. No atomic
; needed — just bump next_free.
l2_tx_buffer_pool_free:
    mov rax, [rel __memreq_addr__l2_tx_buffer_pool]
    inc dword [rax + L2_TX_POOL_NEXT_FREE]
    ret

; ================================================================
; l2_tx_buffer_addr — resolve pool_idx → buffer base address
; ================================================================
;   In:  edi = pool_idx (must be 0..POOL_SIZE-1).
;   Out: rax = pool base + HEADER_BYTES + idx * BUFFER_SIZE.
;   Clobbers: rcx.
;
; Helper for callers that want to write payload bytes after a
; successful alloc, AND for the dispatcher's frame builder.
l2_tx_buffer_addr:
    mov rax, [rel __memreq_addr__l2_tx_buffer_pool]
    add rax, L2_TX_HEADER_BYTES
    mov ecx, edi
    shl ecx, 11                                  ; idx * 2048
    add rax, rcx
    ret

; ================================================================
; l2_tx_enqueue — producer claim + write + publish
; ================================================================
;   In:  rdi = pointer to 6-byte dst MAC
;        esi = ethertype (BE wire order; lower 16 bits used)
;        edx = pool_idx (u32; identifies the payload buffer)
;        ecx = payload_len (u16; lower 16 bits used)
;   Out: eax = L2_TX_OK (0) on success;
;        eax = L2_TX_FULL (1) when ring full.
;   Clobbers: rax, r8, r9, r10, r11, flags.
;
; Vyukov MPSC bounded queue claim-write-publish per
; docs/l2/TX_API.md §6.
l2_tx_enqueue:
    mov r8, [rel __memreq_addr__l2_tx_pending]
    lea r9, [r8 + L2_TX_HEADER_BYTES]            ; r9 = ring base
.cas_retry:
    mov eax, [r8 + L2_TX_PENDING_HEAD]           ; eax = current head
    mov r10d, eax
    and r10d, L2_TX_RING_MASK                    ; r10d = slot
    ; Compute &ring[slot] = ring_base + slot * 24. 24 isn't a
    ; valid `lea` scale (only 1/2/4/8), so use imul then add.
    ; r11d gets slot*24 then zero-extends to r11 for the 64-bit
    ; add against r9 (ring base, full 64-bit address).
    mov r11d, r10d
    imul r11d, r11d, L2_TX_SLOT_BYTES            ; r11d = slot * 24
    add r11, r9                                  ; r11 = &ring[slot]

    ; Read slot's current seq. Acquire under x86 TSO is free.
    mov r10d, [r11 + L2_TX_SLOT_SEQ]
    sub r10d, eax                                ; r10d = seq - head
    jl .full                                     ; seq < head → another round in flight (ring full from our perspective)
    jne .cas_retry                               ; seq > head → another producer mid-publish; retry
    ; seq == head → slot ready for this producer's round.
    ; Try to claim head → head + 1 via CAS.
    lea r10d, [eax + 1]
    lock cmpxchg [r8 + L2_TX_PENDING_HEAD], r10d
    jne .cas_retry

    ; Successfully claimed. Write entry fields.
    ; dst_mac: 6 bytes from [rdi]. Use mov dword + mov word.
    mov r10d, [rdi]                              ; bytes 0-3
    mov [r11 + L2_TX_SLOT_DST_MAC], r10d
    movzx r10d, word [rdi + 4]                   ; bytes 4-5
    mov [r11 + L2_TX_SLOT_DST_MAC + 4], r10w
    ; ethertype (u16)
    mov [r11 + L2_TX_SLOT_ETHERTYPE], si
    ; pool_idx (u32)
    mov [r11 + L2_TX_SLOT_POOL_IDX], edx
    ; payload_len (u16)
    mov [r11 + L2_TX_SLOT_PAYLOAD_LEN], cx

    ; Release-store seq = head + 1 (publish). Under x86 TSO,
    ; a regular store provides release ordering against prior
    ; stores; no fence needed.
    mov [r11 + L2_TX_SLOT_SEQ], r10d

    xor eax, eax                                 ; eax = L2_TX_OK (0)
    ret

.full:
    mov eax, L2_TX_FULL
    ret

section .note.GNU-stack noalloc noexec nowrite progbits
