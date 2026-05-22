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
//   - On return: w0 = 0 → caller branches to .Lflush (clean halt
//                — both success and wait-timeout paths land here;
//                pre-FSA-1 behavior, no return-code differentiation
//                between "consumed N frames" and "wait timed out"
//                yet, address when a caller cares);
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

// FSA-2: per-dispatch RX frame budget. Caps how many RX
// completions the multi-frame consume loop drains in a single
// l2_dispatch invocation. Per D043 ("bounded transitions") the
// dispatcher must not monopolize the CPU on a long burst of
// frames; 16 is small enough to keep transitions cooperative
// and large enough to absorb a typical interactive burst (one
// TCP window's worth of small frames). When the budget is hit
// and more frames remain, they stay in the UsedRing for the
// next dispatcher invocation. Tuneable as FSA-3+ data accrues.
.equ RX_FRAME_BUDGET,           16

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
    // Post-review (Gemini): w24 is hardcoded to VIRTQ_MAX_SIZE here
    // because boot.S populates exactly that many RX buffers before
    // calling the dispatcher (RX:POPULATED step publishes
    // AvailRing.idx = VIRTQ_MAX_SIZE). For reentrance, the
    // dispatcher must source RX next_avail from a persistent
    // location (matching the TX shadow plan below). Same FSA-4
    // work item; tracked together. Today's single-shot dispatch
    // path is unaffected.
    mov     w24, #VIRTQ_MAX_SIZE
    mov     w25, #(VIRTQ_MAX_SIZE - 1)
    add     x26, x19, #VIRTQ_OFF_USED
    mov     w28, #0                      // H2: RX used_shadow = 0

    // FSA-3: replace `dsb ishld + ldrh` with `ldarh` (load-acquire
    // halfword). LDARH is the canonical ARMv8 consumer-side
    // acquire idiom: any subsequent memory access in program
    // order is observed by the local PE AFTER the LDARH completes
    // its read.
    //
    // Diagnostic caveat: the prior `dsb ishld` pattern is also
    // load-ordering on inner-shareable Normal memory and *should*
    // have provided acquire-equivalent ordering against the
    // producer (Firecracker virtio-net VMM thread). Whether the
    // observed phantom-frame `num_bufs=0` race (~15-25% on Pi 5
    // under inbound traffic) was actually caused by missing
    // acquire ordering, by a different barrier-class defect, or
    // by speculative loads past `dsb ishld` on Cortex-A76 has
    // not been bottom-of-stack diagnosed. LDARH is the textbook
    // ARMv8 fix and is correct independent of which mechanism
    // was at fault; verification under sustained inbound traffic
    // (not exercised by the tap0-no-traffic tracer-bullet) is
    // still pending to confirm the race is closed.
    //
    // The yield stays — separately load-bearing for KVM VCPU
    // scheduling liveness under busy-wait, not for correctness;
    // see comment block before emit_bytes in boot.S.
    //
    // LDARH addressing mode is `[Xn|SP]` only (no offset), so
    // pre-compute &UsedRing.idx in x11 outside the loop.
    add     x11, x26, #2                 // x11 = &RX UsedRing.idx
    ldr     w9, =POLL_BUDGET
.Lrx_wait_loop:
    yield                                // KVM liveness; see comment above
    ldarh   w10, [x11]                   // load-acquire UsedRing.idx
    cmp     w10, w28
    b.ne    .Lrx_received
    subs    w9, w9, #1
    b.ne    .Lrx_wait_loop

    adr     x1, rx_timeout_msg
    mov     w2, #(rx_timeout_msg_end - rx_timeout_msg)
    bl      emit_bytes
    b       .Lok

.Lrx_received:
    // FSA-2: multi-frame consume loop. The wait loop above
    // established idx != shadow on entry, so the first iteration
    // proceeds unconditionally. Subsequent iterations re-enter
    // via .Lrx_consume_check (post-consume budget + queue probe).
    //
    // Per-frame work: read ring slot, bounds-check id, read
    // virtio_net_hdr, validate num_buffers, emit RX:FRAME, then
    // write the descriptor id into AvailRing.ring[next_avail & mask].
    // The AvailRing.idx publish + RX QueueNotify are BATCHED to
    // .Lrx_drained — one barrier + one idx update + one notify
    // covers all frames consumed this dispatch. Spec-compliant
    // (Virtio 1.2 §2.7.13.3: per-slot writes must precede the idx
    // update with a write barrier; nothing requires per-frame
    // notifies). Reduces dispatcher overhead under burst load.
    //
    // Frame budget: w28 doubles as the consumer shadow and the
    // per-dispatch frame counter. For FSA-1/FSA-2 these coincide
    // because the dispatcher initialises w28 to 0 at entry — the
    // dispatcher is called exactly once from boot.S today.
    //
    // CAUTION: FSA-2's multi-frame loop AMPLIFIES the conflation
    // hazard relative to FSA-1's single-shot path. If a future
    // caller invokes l2_dispatch a second time without first
    // making the shadow persistent, the wait loop will observe
    // idx > 0 immediately and the consume loop will re-read up
    // to RX_FRAME_BUDGET already-consumed UsedRing slots. The
    // FSA-3+ separation (distinct shadow vs counter) must land
    // before the dispatcher becomes re-entrant.
.Lrx_consume_body:
    // INVARIANT (FSA-3): every entry into .Lrx_consume_body —
    // both the fall-through from .Lrx_received AND the back-edge
    // from .Lrx_consume_check — is preceded by an LDARH load-
    // acquire of UsedRing.idx. Subsequent ring-slot, header, and
    // buffer reads here rely on that acquire for ordering against
    // the producer's release-direction stores. If a future edit
    // adds a third entry point into this block, it MUST establish
    // the same acquire on idx first.
    //
    // P2: ring slot via H2 consumer-shadow. mask = N-1 valid per
    // Virtio 1.2 §4.1.4.3 (VIRTQ_MAX_SIZE is a power of 2).
    //
    // FSA-3: no barrier needed around the slot read or buffer
    // read. The most recent UsedRing.idx observation already
    // provided acquire semantics, so all loads here see the
    // producer's released state. Removed two `dsb sy` pairs
    // (slot reads, buffer reads) that were redundant under the
    // LDARH-acquire model.
    and     w9, w28, w25                 // slot = used_shadow & mask
    add     x9, x26, x9, lsl #3          // entry_base = x26 + slot*8
    ldr     w20, [x9, #4]                // ring[slot].id
    ldr     w21, [x9, #8]                // ring[slot].len

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
    ldrh    w22, [x9, #2]                         // hdr_len
    ldrh    w23, [x9, #10]                        // num_buffers

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

    // --- VIO-R-006: per-frame AvailRing.ring[slot] = id ---
    // Idx publish + kick are batched at .Lrx_drained.
    and     w10, w24, w25                         // slot = next_avail & mask
    add     x11, x19, #VIRTQ_OFF_AVAIL
    add     x11, x11, #4                          // skip flags + idx
    strh    w20, [x11, x10, lsl #1]               // ring[slot] = id

    // Advance next_avail (producer counter for AvailRing) and the
    // RX consumer shadow (UsedRing read position).
    add     w24, w24, #1
    add     w28, w28, #1

.Lrx_consume_check:
    // Budget check first — bounded-transition invariant.
    cmp     w28, #RX_FRAME_BUDGET
    b.hs    .Lrx_drained

    // Queue probe — is there another completion already available?
    // No yield/wait here; this is a non-blocking drain.
    // FSA-3: LDARH for acquire. x11 is AAPCS64 caller-saved and
    // emit_bytes/emit_hex32 don't formally preserve it (their
    // contracts list x0, x5, x6, x7, x8, x30 only), so re-derive
    // &RX UsedRing.idx here rather than relying on incidental
    // preservation by the emit helpers. The 1-cycle `add` per
    // iteration is bounded by RX_FRAME_BUDGET = 16; hoisting into
    // a callee-saved register would require extending the
    // prologue save list and isn't worth the per-frame cycle
    // savings.
    add     x11, x26, #2                 // x11 = &RX UsedRing.idx
    ldarh   w10, [x11]
    cmp     w10, w28
    b.ne    .Lrx_consume_body
    // else: idx == shadow → queue drained, fall through

.Lrx_drained:
    // --- VIO-R-007: batched AvailRing.idx publish + RX kick ---
    //
    // Two barriers (was three pre-Gemini-review):
    //   #1 (before idx store) orders all per-slot AvailRing.ring
    //      writes from the consume loop against the idx publish.
    //      dsb sy retained rather than narrowed to dsb ishst —
    //      the second barrier needs full-system class anyway, so
    //      using ishst here would only shave one barrier's worth.
    //   #2 (between idx and notify) orders the normal-memory idx
    //      store against the Device-memory QueueNotify MMIO
    //      write. Per ARM ARM B2.3.7 the inner-shareable Normal-
    //      memory domain doesn't cover Device accesses, so full
    //      system dsb is the safe class across the Normal/Device
    //      boundary.
    //
    // Gemini (9) caught the third (post-notify) dsb sy as
    // unnecessary — no subsequent code depends on the notify
    // having completed, and the next op (emit_bytes) is on the
    // independent UART device with its own ordering.
    dsb     sy
    add     x11, x19, #VIRTQ_OFF_AVAIL
    strh    w24, [x11, #2]                        // AvailRing.idx = next_avail
    dsb     sy
    str     wzr, [x6, #VIRTIO_MMIO_QUEUE_NOTIFY]

    // Emit RX:RETURNED once per dispatch (after all frames consumed).
    adr     x1, rx_returned_msg
    mov     w2, #(rx_returned_msg_end - rx_returned_msg)
    bl      emit_bytes

    // --- VIO-T-002..005: submit one canary Ethernet frame on TX ---
    //
    // Post-review (Gemini): the prior code hardcoded `ring[0] = 0`
    // and `AvailRing.idx = 1`, baking in the single-shot
    // assumption. Restructured to compute both via parametric
    // shadows so the code shape is reentrant-ready even though
    // the persistent storage of those shadows is not yet wired:
    //
    //   w15 = tx_next_avail (producer counter for TX AvailRing).
    //         Currently initialised to 0 here. FSA-4 will source
    //         this from persistent storage (a memreq tx_state
    //         region or equivalent) so the value survives across
    //         dispatcher invocations — required for the
    //         dispatcher to become legitimately reentrant for TX.
    //   w16 = desc_idx (which TX descriptor slot to write).
    //         Currently fixed at 0 because TX_BUF_COUNT = 1 in
    //         pools.S — only one TX buffer exists. FSA-4 expands
    //         this with a free-list when TX gains multiple slots.
    //
    // For w15 = 0, w16 = 0 the byte effect is identical to the
    // prior hardcoded form: desc[0] gets the canary, ring[0] = 0,
    // AvailRing.idx = 1.
    mov     w15, #0                               // tx_next_avail (FSA-4: load from persistent)
    mov     w16, #0                               // desc_idx (FSA-4: from free-list)

    adrp    x10, tx_test_pkt
    add     x10, x10, :lo12:tx_test_pkt           // runtime addr
    add     x11, x19, #TX_QUEUE_OFF               // x11 = TX desc[0] base
    add     x11, x11, x16, lsl #4                 // x11 = TX desc[desc_idx] base (16 B/desc)
    str     x10, [x11, #0]                        // desc.addr (u64)
    mov     w12, #TX_TEST_PKT_LEN
    str     w12, [x11, #8]                        // desc.len
    strh    wzr, [x11, #12]                       // desc.flags = 0
    strh    wzr, [x11, #14]                       // desc.next  = 0

    add     x12, x19, #TX_QUEUE_OFF
    add     x12, x12, #VIRTQ_OFF_AVAIL            // x12 = TX AvailRing base
    and     w17, w15, w25                         // avail_slot = tx_next_avail & mask
    // Post-Gemini-review (MEDIUM): use x14 (AAPCS64 caller-saved
    // scratch) rather than x18 here. x18 is the "platform
    // register" reserved for platform-specific use under AAPCS64
    // §6.2; using it as general scratch is non-portable.
    add     x14, x12, #4                          // x14 = &ring[0]
    strh    w16, [x14, x17, lsl #1]               // ring[avail_slot] = desc_idx

    dsb     sy

    add     w15, w15, #1                          // tx_next_avail++
    strh    w15, [x12, #2]                        // AvailRing.idx = tx_next_avail

    dsb     sy

    // Kick TX (queue 1). Gemini (9): drop the trailing dsb sy
    // here — the upcoming .Ltx_wait_loop LDARH provides its own
    // acquire ordering on TX UsedRing.idx, and no other code
    // between this kick and the loop depends on the notify
    // having completed.
    mov     w13, #1
    str     w13, [x6, #VIRTIO_MMIO_QUEUE_NOTIFY]

    adr     x1, tx_submitted_msg
    mov     w2, #(tx_submitted_msg_end - tx_submitted_msg)
    bl      emit_bytes

    // --- VIO-T-006: poll TX Used Ring for completion ---
    add     x27, x19, #(TX_QUEUE_OFF + VIRTQ_OFF_USED)

    // H2: TX consumer-shadow restart (independent of RX queue).
    mov     w28, #0

    // FSA-3: load-acquire on TX UsedRing.idx — same rationale as
    // the RX wait loop above. Pre-compute &TX UsedRing.idx in x11.
    add     x11, x27, #2                 // x11 = &TX UsedRing.idx
    ldr     w9, =POLL_BUDGET
.Ltx_wait_loop:
    yield                                // KVM liveness
    ldarh   w10, [x11]                   // load-acquire TX UsedRing.idx
    cmp     w10, w28
    b.ne    .Ltx_reclaimed
    subs    w9, w9, #1
    b.ne    .Ltx_wait_loop

    adr     x1, tx_timeout_msg
    mov     w2, #(tx_timeout_msg_end - tx_timeout_msg)
    bl      emit_bytes
    b       .Lok

.Ltx_reclaimed:
    // FSA-3: no barrier needed — the .Ltx_wait_loop LDARH above
    // provided acquire semantics covering these slot reads.
    and     w9, w28, w25                 // slot = used_shadow & mask
    add     x9, x27, x9, lsl #3          // entry_base = x27 + slot*8
    ldr     w20, [x9, #4]                // ring[slot].id
    ldr     w21, [x9, #8]                // ring[slot].len

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
