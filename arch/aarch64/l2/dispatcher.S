// SPDX-License-Identifier: AGPL-3.0-or-later
//
// L2 dispatcher — FSA-1 skeleton (single-shot wrapper around the
// existing RX/TX path). The earlier inline RX-wait / consume /
// return / TX-submit / wait / reclaim block in boot.S is extracted
// here behind a single AAPCS64 entry point, `l2_dispatch`.
//
// FSA framing (D012 / D043, Astier paper):
//   - The dispatcher is the engine. It picks events (today: a
//     hardcoded sequence of one RX consume + one TX submit) and
//     drives them to completion.
//   - Each event runs as a bounded transition with explicit
//     barriers around the device-shared rings.
//   - Buffer pools are sized statically (see pools.S) and never
//     allocated dynamically post-init.
//
// FSA-1 scope (intentionally narrow):
//   - Same external behavior as the prior inline code. One RX
//     completion consumed, one TX canary submitted + reclaimed,
//     same marker emissions.
//   - The dispatcher is still single-iteration. Multi-frame loop
//     semantics land with FSA-2 (task #16).
//
// Caller contract (from boot.S):
//   - On entry: x0 = UART base, x6 = VIRTIO_MMIO base, x19 = RX
//     virtq base (memreq hot-tier pin). All other registers free.
//   - On return: w0 = 0 → caller branches to .Lflush (clean halt);
//                w0 = 1 → caller branches to .Lset_failed
//                (set VIRTIO_STATUS_FAILED then halt).
//   - x19 is AAPCS64 callee-saved; preserved by ABI.
//   - x0 and x6 are caller-saved per AAPCS64 (x9-x15 are caller-
//     saved scratch, x0-x7 are arg/result registers). They survive
//     this function because (a) the body never writes x0 or x6
//     directly — x6 is only read for VIRTIO_MMIO_QUEUE_NOTIFY
//     stores, x0 is only read by emit_bytes/emit_hex32 — and
//     (b) both helpers explicitly preserve x0, x5, x6, x7, x8,
//     x30 per their documented contracts (see emit_bytes header
//     in boot.S). The dispatcher inherits that discipline.
//   - x20-x28, x29, x30 saved+restored by AAPCS64 callee
//     prologue/epilogue (this function is non-leaf — it calls
//     emit_bytes/emit_hex32).

// --- Virtio constants needed by the dispatcher ---
//
// DUPLICATED from arch/aarch64/platform/firecracker/boot.S. The
// values are spec-defined (Virtio 1.2 §4.2 for MMIO regs, §2.7
// for virtq layout) so drift is bounded — the constants do not
// change for the lifetime of the protocol version. Refactor into
// a shared include is a future cleanup (not FSA-1 scope).
.equ VIRTIO_MMIO_QUEUE_NOTIFY,  0x050
.equ VIRTQ_STRIDE,              0x3000
.equ VIRTQ_OFF_AVAIL,           0x1000
.equ VIRTQ_OFF_USED,            0x2000
.equ TX_QUEUE_OFF,              1 * VIRTQ_STRIDE
.equ VIRTQ_MAX_SIZE,            0x100
.equ POLL_BUDGET,               10000000

// Lengths of cross-file symbols (newline_msg, tx_test_pkt) live in
// boot.S so the `_end - _start` difference can't be evaluated at
// assembly time from this file. Pin the values as constants here.
// `tx_test_pkt` layout = 12 (virtio_net_hdr) + 14 (Ethernet) + 46
// (canary payload) = 72 bytes (Virtio 1.2 §5.1.6.1 + Ethernet
// minimum frame size). Keep in sync with the byte sequence in
// arch/aarch64/platform/firecracker/boot.S.
.equ NEWLINE_MSG_LEN,           1
.equ TX_TEST_PKT_LEN,           72

.text
.global l2_dispatch
l2_dispatch:
    // AAPCS64 prologue: save fp, lr, and the callee-saved
    // registers we modify (x20-x28). 96-byte frame = 12 * 8;
    // 11 regs + 1 padding slot keeps SP 16-byte aligned per
    // AAPCS64 §6.2.2.
    sub     sp, sp, #96
    stp     x29, x30, [sp]
    stp     x20, x21, [sp, #16]
    stp     x22, x23, [sp, #32]
    stp     x24, x25, [sp, #48]
    stp     x26, x27, [sp, #64]
    str     x28, [sp, #80]
    mov     x29, sp

    // --- VIO-R-003/004: poll RX Used Ring for first completion ---
    //
    //   x19 = RX virtq base (memreq hot-tier pin)
    //   x6  = VIRTIO_MMIO base
    //   x0  = UART base
    //   x20 = id from used ring (consumed across RX:FRAME emit,
    //         then reused across TX:RECLAIMED emit)
    //   x21 = used_len from used ring (RX then TX)
    //   x22 = hdr_len (RX only)
    //   x23 = num_buffers (RX only)
    //   x24 = next_avail shadow; initialised to N after RX
    //         populate published AvailRing.idx as N.
    //   x25 = mask = N - 1; AvailRing slot = next_avail & mask.
    //   x26 = RX UsedRing base (x19 + VIRTQ_OFF_USED). Hoisted as
    //         a callee-saved pin because VIRTQ_OFF_USED = 0x2000
    //         puts the USED-header fields past the scaled
    //         LDRH/LDR imm12 windows from x19 directly.
    //   x27 = TX UsedRing base (x19 + TX_QUEUE_OFF + VIRTQ_OFF_USED).
    //   x28 = H2 RX/TX consumer shadow.
    mov     w24, #VIRTQ_MAX_SIZE
    mov     w25, #(VIRTQ_MAX_SIZE - 1)
    add     x26, x19, #VIRTQ_OFF_USED
    mov     w28, #0                      // H2: RX used_shadow = 0

    ldr     w9, =POLL_BUDGET
.Lrx_wait_loop:
    yield                                // keep — see emit_bytes header
    // H5: dsb ishld — inner-shareable load-only ordering is the
    // minimum sufficient class for KVM-hosted virtio device
    // observers. See P4 doc block in boot.S for the full audit
    // trail (init paths stay on dsb sy).
    dsb     ishld
    ldrh    w10, [x26, #2]               // UsedRing.idx
    dsb     ishld
    cmp     w10, w28
    b.ne    .Lrx_received
    subs    w9, w9, #1
    b.ne    .Lrx_wait_loop

    adr     x1, rx_timeout_msg
    mov     w2, #(rx_timeout_msg_end - rx_timeout_msg)
    bl      emit_bytes
    b       .Lok

.Lrx_received:
    // P2: ring slot via H2 consumer-shadow. mask = N-1 valid per
    // Virtio 1.2 §4.1.4.3 (VIRTQ_MAX_SIZE is a power of 2).
    and     w9, w28, w25                 // slot = used_shadow & mask
    add     x9, x26, x9, lsl #3          // entry_base = x26 + slot*8
    dsb     sy
    ldr     w20, [x9, #4]                // ring[slot].id
    ldr     w21, [x9, #8]                // ring[slot].len
    dsb     sy

    // Bounds check id < VIRTQ_MAX_SIZE.
    cmp     w20, #VIRTQ_MAX_SIZE
    b.hs    .Lrx_bad_id_fail

    // Buffer addr: __memreq_addr__rx_buffer + id * 2048.
    adrp    x9, __memreq_addr__rx_buffer
    add     x9, x9, :lo12:__memreq_addr__rx_buffer
    ldr     x9, [x9]                              // pool base
    lsl     x10, x20, #11                         // id * 2048
    add     x9, x9, x10                           // buffer addr

    // Read virtio_net_hdr.hdr_len (u16 @ +2) and num_buffers (u16 @ +10).
    dsb     sy
    ldrh    w22, [x9, #2]                         // hdr_len
    ldrh    w23, [x9, #10]                        // num_buffers
    dsb     sy

    // Validate num_buffers == 1 under MVP no-MRG_RXBUF policy.
    cmp     w23, #1
    b.ne    .Lrx_num_bufs_fail

    // Emit: RX:FRAME id=... hdr_len=... num_bufs=... used_len=...
    adr     x1, rx_frame_prefix_msg
    mov     w2, #(rx_frame_prefix_msg_end - rx_frame_prefix_msg)
    bl      emit_bytes
    mov     w5, w20
    bl      emit_hex32

    adr     x1, rx_frame_hdr_len_msg
    mov     w2, #(rx_frame_hdr_len_msg_end - rx_frame_hdr_len_msg)
    bl      emit_bytes
    mov     w5, w22
    bl      emit_hex32

    adr     x1, rx_frame_num_bufs_msg
    mov     w2, #(rx_frame_num_bufs_msg_end - rx_frame_num_bufs_msg)
    bl      emit_bytes
    mov     w5, w23
    bl      emit_hex32

    adr     x1, rx_frame_used_len_msg
    mov     w2, #(rx_frame_used_len_msg_end - rx_frame_used_len_msg)
    bl      emit_bytes
    mov     w5, w21
    bl      emit_hex32

    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
    bl      emit_bytes

    // --- VIO-R-006/007: return consumed descriptor + notify ---
    and     w10, w24, w25                         // slot = x24 & mask
    add     x11, x19, #VIRTQ_OFF_AVAIL
    add     x11, x11, #4                          // skip flags + idx
    strh    w20, [x11, x10, lsl #1]               // ring[slot] = id

    dsb     sy

    add     w24, w24, #1
    add     x11, x19, #VIRTQ_OFF_AVAIL
    strh    w24, [x11, #2]

    dsb     sy

    // Kick RX (queue 0).
    str     wzr, [x6, #VIRTIO_MMIO_QUEUE_NOTIFY]
    dsb     sy

    // H2: advance used_shadow now that ring[0] has been consumed.
    add     w28, w28, #1

    adr     x1, rx_returned_msg
    mov     w2, #(rx_returned_msg_end - rx_returned_msg)
    bl      emit_bytes

    // --- VIO-T-002..005: submit one canary Ethernet frame on TX ---
    adrp    x10, tx_test_pkt
    add     x10, x10, :lo12:tx_test_pkt           // runtime addr
    add     x11, x19, #TX_QUEUE_OFF               // TX queue base
    str     x10, [x11, #0]                        // desc.addr (u64)
    mov     w12, #TX_TEST_PKT_LEN
    str     w12, [x11, #8]                        // desc.len
    strh    wzr, [x11, #12]                       // desc.flags = 0
    strh    wzr, [x11, #14]                       // desc.next  = 0

    add     x12, x11, #VIRTQ_OFF_AVAIL            // TX AvailRing base
    strh    wzr, [x12, #4]                        // ring[0] = 0

    dsb     sy

    mov     w13, #1
    strh    w13, [x12, #2]                        // AvailRing.idx = 1

    dsb     sy

    // Kick TX (queue 1).
    str     w13, [x6, #VIRTIO_MMIO_QUEUE_NOTIFY]
    dsb     sy

    adr     x1, tx_submitted_msg
    mov     w2, #(tx_submitted_msg_end - tx_submitted_msg)
    bl      emit_bytes

    // --- VIO-T-006: poll TX Used Ring for completion ---
    add     x27, x19, #(TX_QUEUE_OFF + VIRTQ_OFF_USED)

    // H2: TX consumer-shadow restart (independent of RX queue).
    mov     w28, #0

    ldr     w9, =POLL_BUDGET
.Ltx_wait_loop:
    yield
    dsb     ishld
    ldrh    w10, [x27, #2]               // TX UsedRing.idx
    dsb     ishld
    cmp     w10, w28
    b.ne    .Ltx_reclaimed
    subs    w9, w9, #1
    b.ne    .Ltx_wait_loop

    adr     x1, tx_timeout_msg
    mov     w2, #(tx_timeout_msg_end - tx_timeout_msg)
    bl      emit_bytes
    b       .Lok

.Ltx_reclaimed:
    and     w9, w28, w25                 // slot = used_shadow & mask
    add     x9, x27, x9, lsl #3          // entry_base = x27 + slot*8
    dsb     sy
    ldr     w20, [x9, #4]                // ring[slot].id
    ldr     w21, [x9, #8]                // ring[slot].len
    dsb     sy

    cmp     w20, #VIRTQ_MAX_SIZE
    b.hs    .Ltx_bad_id_fail

    // Emit: TX:RECLAIMED id=XXXXXXXX used_len=XXXXXXXX
    adr     x1, tx_reclaimed_prefix_msg
    mov     w2, #(tx_reclaimed_prefix_msg_end - tx_reclaimed_prefix_msg)
    bl      emit_bytes
    mov     w5, w20
    bl      emit_hex32

    adr     x1, tx_used_len_msg
    mov     w2, #(tx_used_len_msg_end - tx_used_len_msg)
    bl      emit_bytes
    mov     w5, w21
    bl      emit_hex32

    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
    bl      emit_bytes
    b       .Lok

.Lrx_bad_id_fail:
    // RX UsedRing.ring[slot].id is out of range. w20 = observed id.
    adr     x1, rx_bad_id_msg
    mov     w2, #(rx_bad_id_msg_end - rx_bad_id_msg)
    bl      emit_bytes
    mov     w5, w20
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
    bl      emit_bytes
    b       .Lfail

.Lrx_num_bufs_fail:
    // virtio_net_hdr.num_buffers != 1. w23 = observed value.
    adr     x1, rx_num_bufs_msg
    mov     w2, #(rx_num_bufs_msg_end - rx_num_bufs_msg)
    bl      emit_bytes
    mov     w5, w23
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
    bl      emit_bytes
    b       .Lfail

.Ltx_bad_id_fail:
    // TX UsedRing.ring[slot].id is out of range. w20 = observed id.
    adr     x1, tx_bad_id_msg
    mov     w2, #(tx_bad_id_msg_end - tx_bad_id_msg)
    bl      emit_bytes
    mov     w5, w20
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
    bl      emit_bytes
    b       .Lfail

.Lok:
    mov     w0, #0
    b       .Lepilogue

.Lfail:
    mov     w0, #1
    // fall through

.Lepilogue:
    ldr     x28, [sp, #80]
    ldp     x26, x27, [sp, #64]
    ldp     x24, x25, [sp, #48]
    ldp     x22, x23, [sp, #32]
    ldp     x20, x21, [sp, #16]
    ldp     x29, x30, [sp]
    add     sp, sp, #96
    ret

// --- Strings used by l2_dispatch (file-local .rodata) ---
.section .rodata
rx_timeout_msg:
    .ascii  "RX:TIMEOUT\n"
rx_timeout_msg_end:

rx_frame_prefix_msg:
    .ascii  "RX:FRAME id="
rx_frame_prefix_msg_end:
rx_frame_hdr_len_msg:
    .ascii  " hdr_len="
rx_frame_hdr_len_msg_end:
rx_frame_num_bufs_msg:
    .ascii  " num_bufs="
rx_frame_num_bufs_msg_end:
rx_frame_used_len_msg:
    .ascii  " used_len="
rx_frame_used_len_msg_end:

rx_bad_id_msg:
    .ascii  "RX:FAIL bad_id="
rx_bad_id_msg_end:
rx_num_bufs_msg:
    .ascii  "RX:FAIL num_bufs="
rx_num_bufs_msg_end:

rx_returned_msg:
    .ascii  "RX:RETURNED\n"
rx_returned_msg_end:

tx_submitted_msg:
    .ascii  "TX:SUBMITTED\n"
tx_submitted_msg_end:
tx_timeout_msg:
    .ascii  "TX:TIMEOUT\n"
tx_timeout_msg_end:
tx_reclaimed_prefix_msg:
    .ascii  "TX:RECLAIMED id="
tx_reclaimed_prefix_msg_end:
tx_used_len_msg:
    .ascii  " used_len="
tx_used_len_msg_end:
tx_bad_id_msg:
    .ascii  "TX:FAIL bad_id="
tx_bad_id_msg_end:
