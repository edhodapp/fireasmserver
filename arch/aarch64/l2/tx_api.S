// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// L2 TX API (aarch64) — producer-facing send interface per
// docs/l2/TX_API.md. Mirror of arch/x86_64/l2/tx_api.S.
//
// See the x86_64 file's header for the architecture overview
// (producer / ring / consumer / pool diagram and Vyukov MPSC
// claim-publish dance description). This file is the same
// API written with AArch64 primitives:
//
//   - LDAXR + STLXR for the CAS loop (load-exclusive
//     acquire / store-exclusive release)
//   - LDAR for the wait-for-seq acquire load
//   - STLR for the publish release store
//
// Symbol exports:
//   l2_tx_pending_init     — one-time pre-init of seq[]
//   l2_tx_buffer_pool_alloc → idx or RING_FULL
//   l2_tx_buffer_pool_free(idx)
//   l2_tx_enqueue(dst_mac_ptr, ethertype, pool_idx, payload_len)
//     → 0 on success, 1 on queue FULL
//   l2_tx_buffer_addr(pool_idx) → buffer base

// ----- Constants (mirrored from regions.yaml + TX_API.md) -----

.equ L2_TX_RING_SIZE,        16
.equ L2_TX_RING_MASK,        L2_TX_RING_SIZE - 1
.equ L2_TX_POOL_SIZE,        16
.equ L2_TX_POOL_MASK,        L2_TX_POOL_SIZE - 1
.equ L2_TX_BUFFER_SIZE,      2048
.equ L2_TX_HEADER_BYTES,     64

.equ L2_TX_PENDING_HEAD,     0
.equ L2_TX_PENDING_TAIL,     8

.equ L2_TX_SLOT_BYTES,       24
.equ L2_TX_SLOT_SEQ,         0
.equ L2_TX_SLOT_DST_MAC,     8
.equ L2_TX_SLOT_ETHERTYPE,   14
.equ L2_TX_SLOT_POOL_IDX,    16
.equ L2_TX_SLOT_PAYLOAD_LEN, 20

.equ L2_TX_POOL_NEXT_ALLOC,  0
.equ L2_TX_POOL_NEXT_FREE,   8

.equ L2_TX_OK,               0
.equ L2_TX_FULL,             1
// GNU as can't express 0xFFFFFFFF as a 32-bit signed .equ for
// mov immediate cleanly; consumers use `mov w0, #-1` to load it
// (zero-extension of -1 = 0xFFFFFFFF). Same value.
.equ L2_TX_POOL_IDX_FULL,    0xFFFFFFFF

// Pool buffer layout — the consumer builds the wire frame in
// front of the producer's payload:
//   +0   12 bytes virtio_net_hdr (zero-filled by consumer)
//   +12  6 bytes dst MAC          (from request)
//   +18  6 bytes src MAC = GUEST_MAC (consumer)
//   +24  2 bytes ethertype        (from request, BE wire)
//   +26  payload_len bytes        (PRODUCER writes here)
//   +26+plen+pad  zero-pad if needed to reach 60 wire / 72 virtio
.equ L2_TX_PAYLOAD_OFFSET,   26

.text
.global l2_tx_pending_init
.global l2_tx_buffer_pool_alloc
.global l2_tx_buffer_pool_free
.global l2_tx_enqueue
.global l2_tx_buffer_addr

// ================================================================
// l2_tx_pending_init — one-time Vyukov seq[i] = i pre-init
// ================================================================
//   In:  nothing.
//   Out: nothing.
//   Clobbers: x9, x10, w11.
//   Preserves: all callee-saved + x0..x8.
l2_tx_pending_init:
    adrp    x9, __memreq_addr__l2_tx_pending
    add     x9, x9, :lo12:__memreq_addr__l2_tx_pending
    ldr     x9, [x9]                              // x9 = pending region base
    add     x9, x9, #L2_TX_HEADER_BYTES           // x9 = ring base
    mov     w11, #0                               // w11 = i
.Lpending_init_loop:
    str     w11, [x9, #L2_TX_SLOT_SEQ]            // seq[i] = i
    add     x9, x9, #L2_TX_SLOT_BYTES
    add     w11, w11, #1
    cmp     w11, #L2_TX_RING_SIZE
    b.lo    .Lpending_init_loop
    ret

// ================================================================
// l2_tx_buffer_pool_alloc — claim a free pool buffer
// ================================================================
//   In:  nothing.
//   Out: w0 = pool index (0..POOL_SIZE-1) on success;
//        w0 = L2_TX_POOL_IDX_FULL when pool empty.
//   Clobbers: w0, x1, w2, w3, w4.
//
// Generation counter ring with LDAXR/STLXR CAS loop on
// next_alloc. next_free is consumer-only.
l2_tx_buffer_pool_alloc:
    adrp    x1, __memreq_addr__l2_tx_buffer_pool
    add     x1, x1, :lo12:__memreq_addr__l2_tx_buffer_pool
    ldr     x1, [x1]                              // x1 = pool base
    // LDAXR / STLXR only accept `[Xn]` addressing (no
    // immediate offset). next_alloc is at offset 0 of the
    // pool base, so x1 already points where the exclusive
    // ops need. next_free at offset 8 uses the regular ldr.
.Lalloc_retry:
    ldaxr   w0, [x1]                              // w0 = current next_alloc (@ +0)
    ldr     w2, [x1, #L2_TX_POOL_NEXT_FREE]      // w2 = current next_free
    sub     w3, w0, w2                            // w3 = in-flight
    cmp     w3, #L2_TX_POOL_SIZE
    b.hs    .Lalloc_full
    add     w4, w0, #1                            // w4 = next_alloc + 1
    stlxr   w3, w4, [x1]
    cbnz    w3, .Lalloc_retry                     // STLXR failed → retry
    and     w0, w0, #L2_TX_POOL_MASK              // return idx
    ret
.Lalloc_full:
    clrex                                         // clear monitor; no STLXR pending
    mov     w0, #-1                               // L2_TX_POOL_IDX_FULL
    ret

// ================================================================
// l2_tx_buffer_pool_free — release a pool buffer (consumer-only)
// ================================================================
//   In:  w0 = pool_idx (ignored — FIFO discipline trusts caller).
//   Out: nothing.
//   Clobbers: x9, w10.
l2_tx_buffer_pool_free:
    adrp    x9, __memreq_addr__l2_tx_buffer_pool
    add     x9, x9, :lo12:__memreq_addr__l2_tx_buffer_pool
    ldr     x9, [x9]
    ldr     w10, [x9, #L2_TX_POOL_NEXT_FREE]
    add     w10, w10, #1
    str     w10, [x9, #L2_TX_POOL_NEXT_FREE]
    ret

// ================================================================
// l2_tx_buffer_addr — resolve pool_idx → buffer base address
// ================================================================
//   In:  w0 = pool_idx (must be 0..POOL_SIZE-1).
//   Out: x0 = pool base + HEADER_BYTES + idx * BUFFER_SIZE.
//   Clobbers: x1, x9.
l2_tx_buffer_addr:
    adrp    x9, __memreq_addr__l2_tx_buffer_pool
    add     x9, x9, :lo12:__memreq_addr__l2_tx_buffer_pool
    ldr     x9, [x9]
    add     x9, x9, #L2_TX_HEADER_BYTES
    lsl     x1, x0, #11                           // idx * 2048
    add     x0, x9, x1
    ret

// ================================================================
// l2_tx_enqueue — producer claim + write + publish
// ================================================================
//   In:  x0 = pointer to 6-byte dst MAC
//        w1 = ethertype (BE wire order; lower 16 bits used)
//        w2 = pool_idx (u32)
//        w3 = payload_len (u16; lower 16 bits used)
//   Out: w0 = L2_TX_OK (0) on success; w0 = L2_TX_FULL (1)
//        when ring full.
//   Clobbers: w0, x9, x10, w11, w12, w13, w14, w15.
//
// Vyukov MPSC claim-write-publish per TX_API.md §6.
l2_tx_enqueue:
    adrp    x9, __memreq_addr__l2_tx_pending
    add     x9, x9, :lo12:__memreq_addr__l2_tx_pending
    ldr     x9, [x9]                              // x9 = pending region base
                                                  // (head is at offset 0)
    add     x10, x9, #L2_TX_HEADER_BYTES          // x10 = ring base
.Lenq_retry:
    ldaxr   w11, [x9]                             // w11 = current head (@ +0)
    and     w12, w11, #L2_TX_RING_MASK            // w12 = slot
    // Compute &ring[slot] = ring + slot * 24. AArch64 has no
    // *24 addressing mode; use mov + umaddl multiply-add.
    mov     w13, #L2_TX_SLOT_BYTES
    umaddl  x13, w12, w13, x10                    // x13 = ring + slot*24
                                                  // (seq is at offset 0 of slot)
    // Read slot's current seq with acquire (LDAR needs `[Xn]`).
    ldar    w14, [x13]
    sub     w15, w14, w11                         // w15 = seq - head
    cbnz    w15, .Lenq_seq_mismatch
    // seq == head — claim head → head+1.
    add     w14, w11, #1
    stlxr   w15, w14, [x9]
    cbnz    w15, .Lenq_retry                      // STLXR failed → retry
    // Successfully claimed. Write entry fields.
    // dst_mac: 6 bytes from [x0].
    ldr     w15, [x0]                             // bytes 0-3
    str     w15, [x13, #L2_TX_SLOT_DST_MAC]
    ldrh    w15, [x0, #4]                         // bytes 4-5
    strh    w15, [x13, #(L2_TX_SLOT_DST_MAC + 4)]
    // ethertype (u16)
    strh    w1, [x13, #L2_TX_SLOT_ETHERTYPE]
    // pool_idx (u32)
    str     w2, [x13, #L2_TX_SLOT_POOL_IDX]
    // payload_len (u16)
    strh    w3, [x13, #L2_TX_SLOT_PAYLOAD_LEN]
    // Release-store seq = head + 1 (publish; STLR needs `[Xn]`,
    // x13 already points at the slot's seq field).
    stlr    w14, [x13]
    mov     w0, #L2_TX_OK
    ret

.Lenq_seq_mismatch:
    // Determine whether ring is FULL (seq - head < 0 — another
    // round in flight, slot not ready) or another producer is
    // mid-publish (seq - head > 0 — slot has a newer round
    // already). FULL → bail; mid-publish → retry.
    // w15 = seq - head (computed above). Tested as a signed
    // 32-bit value: negative → FULL, positive → retry.
    clrex
    cmp     w15, #0
    b.lt    .Lenq_full
    b       .Lenq_retry

.Lenq_full:
    mov     w0, #L2_TX_FULL
    ret
