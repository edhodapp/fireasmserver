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

// FSA-4: per-dispatch TX reclaim budget. Mirrors x86_64's
// TX_RECLAIM_BUDGET. Caps how many TX completions the new
// multi-frame TX reclaim loop drains in one dispatcher call.
.equ TX_RECLAIM_BUDGET,         16

// FSA-4: l2_state field offsets (mirror regions.yaml doc).
.equ L2_STATE_RX_NEXT_AVAIL,    0
.equ L2_STATE_RX_USED_SHADOW,   2
.equ L2_STATE_TX_NEXT_AVAIL,    4
.equ L2_STATE_TX_USED_SHADOW,   6

.text
.global l2_dispatch
l2_dispatch:
    // AAPCS64 prologue + FSA-4 local frame.
    //
    // 128-byte frame layout (16-aligned per AAPCS64 §6.2.2):
    //   [sp +   0..7] : x29 (fp)
    //   [sp +   8..15]: x30 (lr)
    //   [sp +  16..79]: x20-x27 callee-saved pairs
    //   [sp +  80..87]: x28
    //   [sp +  88..91]: per-dispatch frame counter (u32; FSA-4)
    //   [sp +  92..95]: padding
    //   [sp +  96..99]: tx_next_avail stash (u32; FSA-4)
    //   [sp + 100..103]: padding
    //   [sp + 104..107]: arp_match flag (u32; FSA-4(A))
    //   [sp + 108..111]: sender_ip stash (u32; FSA-4(A))
    //   [sp + 112..117]: sender_mac stash (6 bytes; FSA-4(A))
    //   [sp + 118..127]: padding for 16-alignment
    //
    // The per-dispatch counter, tx_next_avail stash, and ARP
    // recognition stash all live on the stack rather than in
    // registers because all the callee-saved registers (x19-x28)
    // are already spoken for and we don't want to grow the
    // prologue save list further.
    //
    // l2_state addressing note: this function re-derives
    // &l2_state via the `adrp + add + ldr` triple at four
    // sites (RX shadow load, RX shadow store, TX shadow load,
    // TX shadow store) rather than pinning the pointer in a
    // callee-saved register. Same register-pressure tradeoff:
    // pinning would require extending the prologue save list,
    // and 3 instructions × 4 sites = ~12 cycles of overhead per
    // dispatch is well under the cost of any single emit_bytes
    // call already on the path.
    sub     sp, sp, #128
    stp     x29, x30, [sp]
    stp     x20, x21, [sp, #16]
    stp     x22, x23, [sp, #32]
    stp     x24, x25, [sp, #48]
    stp     x26, x27, [sp, #64]
    str     x28, [sp, #80]
    mov     x29, sp

    // --- FSA-4: load persistent shadows from l2_state ---
    //
    // l2_state holds four u16 counters (regions.yaml docs the
    // layout). boot.S initialises rx_next_avail = VIRTQ_MAX_SIZE
    // after RX:POPULATED; the other three are zero-init by the
    // allocator. ldrh zero-extends, so w24/w28 range is
    // [0,65535] on entry — matching Virtio u16 idx wrap. The
    // strh-based writebacks at exit truncate to u16 again, so
    // persistent storage stays wrap-safe.
    adrp    x10, __memreq_addr__l2_state
    add     x10, x10, :lo12:__memreq_addr__l2_state
    ldr     x10, [x10]                       // x10 = l2_state base
    ldrh    w24, [x10, #L2_STATE_RX_NEXT_AVAIL]
    ldrh    w28, [x10, #L2_STATE_RX_USED_SHADOW]
    str     wzr, [sp, #88]                   // per-dispatch counter = 0
    str     wzr, [sp, #96]                   // tx_next_avail stash = 0 (TX phase fills)
    str     wzr, [sp, #104]                  // arp_match flag = 0 (set by ARP recognition)

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
    //   x24 = rx_next_avail (FSA-4: loaded from l2_state above;
    //         written back at .Lrx_drained).
    //   x25 = mask = N - 1; AvailRing slot = next_avail & mask.
    //   x26 = RX UsedRing base (x19 + VIRTQ_OFF_USED). Hoisted as
    //         a callee-saved pin because VIRTQ_OFF_USED = 0x2000
    //         puts the USED-header fields past the scaled
    //         LDRH/LDR imm12 windows from x19 directly.
    //   x27 = TX UsedRing base (x19 + TX_QUEUE_OFF + VIRTQ_OFF_USED).
    //   x28 = rx_used_shadow / tx_used_shadow (FSA-4: loaded from
    //         l2_state per phase; written back at phase exit).
    //
    // w24 and w28 already loaded from l2_state by the prologue;
    // only the constants (mask, UsedRing base) need init here.
    mov     w25, #(VIRTQ_MAX_SIZE - 1)
    add     x26, x19, #VIRTQ_OFF_USED

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
    // Post-Gemini-review (HIGH): w10 is u16 (zero-extended from
    // the 16-bit ldarh); w28 is a 32-bit counter that increments
    // uncapped. After 65,536 frames consumed across the lifetime
    // of the dispatcher's persistent shadow (FSA-4+), w28 would
    // exceed 65,535 while w10 wraps modulo 65,536 — a naive
    // `cmp w10, w28` would mis-detect "queue drained" as "new
    // frame available" forever. Fix: extended-register cmp with
    // UXTH masks w28 to its low 16 bits before compare, matching
    // the Virtio u16 wrap semantics. Latent today (single-shot
    // resets w28 to 0 every call; max 16 frames per dispatch).
    cmp     w10, w28, uxth
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

    // Buffer addr: load the rx_buffer pool base, then add id*2048.
    //
    // memreq indirection (codegen convention, easy to misread):
    // `__memreq_addr__rx_buffer` is an .equ ALIAS for the label
    // `__memreq_assigned__rx_buffer`, which marks an 8-byte
    // storage slot in .memreq holding the assigned runtime
    // address of the rx_buffer pool. So:
    //   adrp/add  → x9 = address of the storage slot
    //   ldr x9    → x9 = value at slot = the actual pool base
    // The ldr is REQUIRED — not a double-dereference bug. See
    // tooling/src/memreq_codegen/ for the codegen emitting the
    // `__memreq_addr__X equ __memreq_assigned__X` aliases.
    adrp    x9, __memreq_addr__rx_buffer
    add     x9, x9, :lo12:__memreq_addr__rx_buffer
    ldr     x9, [x9]                              // pool base (see comment above)
    lsl     x10, x20, #11                         // id * 2048
    add     x9, x9, x10                           // buffer addr

    // Read virtio_net_hdr.hdr_len (u16 @ +2) and num_buffers (u16 @ +10).
    ldrh    w22, [x9, #2]                         // hdr_len
    ldrh    w23, [x9, #10]                        // num_buffers

    // Validate num_buffers == 1 under MVP no-MRG_RXBUF policy.
    cmp     w23, #1
    b.ne    .Lrx_num_bufs_fail

    // --- FSA-4(A): ARP request recognition ---
    //
    // x9 still holds the buffer base. We inspect three fields
    // gated by short-circuit ne-checks:
    //   EtherType @ +24 = 0x0806 BE (= 0x0608 LE16)
    //   ARP OPER @ +32 = 0x0001 BE (= 0x0100 LE16) — request
    //   TPA      @ +50 = 192.168.42.2 BE → bytes C0 A8 2A 02
    //
    // All buffer offsets are 2-aligned but not 4-aligned at
    // +50/+52, so the TPA compare uses two ldrh halves rather
    // than a single ldr (MMU is disabled per boot.S, which means
    // strict Device-memory alignment applies to normal-memory
    // loads too; ldrh requires only 2-byte alignment).
    //
    // On match: stash sender MAC (SHA @ +34) and sender IP
    // (SPA @ +40) in the local frame, set arp_match = 1, emit
    // an ARP:REQUEST marker. The post-RX-drain code reads the
    // flag + stash to build the ARP reply frame in tx_arp_buf
    // and submit it on TX (replacing the canary).
    ldrh    w14, [x9, #24]                        // EtherType
    cmp     w14, #0x0608
    b.ne    .Lno_arp
    ldrh    w14, [x9, #32]                        // ARP OPER
    cmp     w14, #0x0100
    b.ne    .Lno_arp
    ldrh    w14, [x9, #50]                        // TPA bytes 0-1 (C0 A8 LE → 0xA8C0)
    mov     w15, #0xA8C0
    cmp     w14, w15
    b.ne    .Lno_arp
    ldrh    w14, [x9, #52]                        // TPA bytes 2-3 (2A 02 LE → 0x022A)
    mov     w15, #0x022A
    cmp     w14, w15
    b.ne    .Lno_arp

    // ARP match — stash SHA (6 bytes, 3× ldrh) and SPA (4 bytes).
    ldrh    w14, [x9, #34]                        // SHA bytes 0-1
    strh    w14, [sp, #112]
    ldrh    w14, [x9, #36]                        // SHA bytes 2-3
    strh    w14, [sp, #114]
    ldrh    w14, [x9, #38]                        // SHA bytes 4-5
    strh    w14, [sp, #116]
    ldr     w14, [x9, #40]                        // SPA (offset 40 is 8-aligned)
    str     w14, [sp, #108]
    mov     w14, #1
    str     w14, [sp, #104]                       // arp_match = 1

    adr     x1, arp_request_msg
    mov     w2, #(arp_request_msg_end - arp_request_msg)
    bl      emit_bytes

.Lno_arp:

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

    // Advance next_avail, consumer shadow, and the per-dispatch
    // frame counter. FSA-4 separated the counter from the shadow:
    // the shadow rides in w28 (persistent across calls, loaded
    // from l2_state), so it can be non-zero at function entry and
    // grow uncapped through the dispatch. The per-dispatch
    // counter at [sp + 88] starts at 0 every call and bounds the
    // consume loop via RX_FRAME_BUDGET.
    add     w24, w24, #1
    add     w28, w28, #1
    ldr     w9, [sp, #88]
    add     w9, w9, #1
    str     w9, [sp, #88]

.Lrx_consume_check:
    // Budget check first — uses the per-dispatch counter, not
    // the shadow (FSA-4 separation).
    ldr     w9, [sp, #88]
    cmp     w9, #RX_FRAME_BUDGET
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
    cmp     w10, w28, uxth               // u16 wrap-safe compare (see wait loop)
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

    // --- FSA-4: write back RX shadows to l2_state ---
    //
    // strh truncates the 32-bit w24/w28 to u16 on store, so even
    // if the in-register values have overflowed 16 bits during
    // this dispatch (e.g., w24 = 256+N after N frames consumed),
    // the persisted u16 wraps modulo 65536 correctly.
    adrp    x10, __memreq_addr__l2_state
    add     x10, x10, :lo12:__memreq_addr__l2_state
    ldr     x10, [x10]                            // x10 = l2_state base
    strh    w24, [x10, #L2_STATE_RX_NEXT_AVAIL]
    strh    w28, [x10, #L2_STATE_RX_USED_SHADOW]

    // --- VIO-T-002..005: submit one canary Ethernet frame on TX ---
    //
    // FSA-4: tx_next_avail loaded from l2_state. desc_idx still
    // fixed at 0 because TX_BUF_COUNT = 1; FSA-5 will introduce
    // a free-list when TX gains multiple buffer slots. For
    // tx_next_avail = 0 (first call) and desc_idx = 0 the byte
    // effect is identical to the FSA-3 hardcoded form.
    ldrh    w15, [x10, #L2_STATE_TX_NEXT_AVAIL]   // tx_next_avail (FSA-4: persistent)
    mov     w16, #0                               // desc_idx (FSA-5: from free-list)

    // --- FSA-4(A): pick TX buffer based on arp_match ---
    //
    // If an ARP request matched during RX consume, build a reply
    // frame in tx_arp_buf and submit that. Otherwise submit the
    // rodata canary (tx_test_pkt). Both are 72 bytes so desc.len
    // is the same either way.
    ldr     w13, [sp, #104]                       // arp_match
    cbz     w13, .Larp_use_canary

    // Build ARP reply in tx_arp_buf:
    //   1. Copy the 72-byte arp_reply_template (rodata) over.
    //      4× ldp/stp (64 B) + 1× ldr/str (8 B) = 72 B exactly.
    //      Template has GUEST_MAC as src + SHA and GUEST_IP as
    //      SPA pre-baked; dst MAC, THA, TPA are placeholder 0s.
    //   2. Patch dst MAC @ +12 from stashed sender MAC.
    //   3. Patch THA @ +44 from stashed sender MAC.
    //   4. Patch TPA @ +50 from stashed sender IP.
    adrp    x10, __memreq_addr__tx_arp_buf
    add     x10, x10, :lo12:__memreq_addr__tx_arp_buf
    ldr     x10, [x10]                            // x10 = tx_arp_buf base
    adrp    x11, arp_reply_template
    add     x11, x11, :lo12:arp_reply_template

    // Template copy: 9× 8-byte words via 4 ldp/stp + 1 ldr/str.
    ldp     x12, x13, [x11, #0]
    stp     x12, x13, [x10, #0]
    ldp     x12, x13, [x11, #16]
    stp     x12, x13, [x10, #16]
    ldp     x12, x13, [x11, #32]
    stp     x12, x13, [x10, #32]
    ldp     x12, x13, [x11, #48]
    stp     x12, x13, [x10, #48]
    ldr     x12, [x11, #64]
    str     x12, [x10, #64]

    // Patch dst MAC at +12 from sender_mac stash.
    ldrh    w12, [sp, #112]
    strh    w12, [x10, #12]
    ldrh    w12, [sp, #114]
    strh    w12, [x10, #14]
    ldrh    w12, [sp, #116]
    strh    w12, [x10, #16]

    // Patch THA at +44 from sender_mac stash.
    ldrh    w12, [sp, #112]
    strh    w12, [x10, #44]
    ldrh    w12, [sp, #114]
    strh    w12, [x10, #46]
    ldrh    w12, [sp, #116]
    strh    w12, [x10, #48]

    // Patch TPA at +50 from sender_ip stash (two halves; +50 is
    // 2-aligned only).
    ldrh    w12, [sp, #108]
    strh    w12, [x10, #50]
    ldrh    w12, [sp, #110]
    strh    w12, [x10, #52]

    // x10 stays = tx_arp_buf base for the desc.addr write below.

    adr     x1, arp_reply_msg
    mov     w2, #(arp_reply_msg_end - arp_reply_msg)
    bl      emit_bytes

    // emit_bytes preserves x10 per the helper's preserve list
    // (x0, x5, x6, x7, x8, x30 only — but the implementation
    // doesn't touch x9-x18 either). To stay robust against a
    // future emit_bytes that *does* clobber x10, re-derive
    // tx_arp_buf base here.
    adrp    x10, __memreq_addr__tx_arp_buf
    add     x10, x10, :lo12:__memreq_addr__tx_arp_buf
    ldr     x10, [x10]
    b       .Larp_have_tx_addr
.Larp_use_canary:
    adrp    x10, tx_test_pkt
    add     x10, x10, :lo12:tx_test_pkt           // runtime addr of canary

.Larp_have_tx_addr:
    add     x11, x19, #TX_QUEUE_OFF               // x11 = TX desc[0] base
    add     x11, x11, x16, lsl #4                 // x11 = TX desc[desc_idx] base (16 B/desc)
    str     x10, [x11, #0]                        // desc.addr (u64)
    mov     w12, #TX_TEST_PKT_LEN                 // = 72; identical for canary + ARP reply
    str     w12, [x11, #8]                        // desc.len
    strh    wzr, [x11, #12]                       // desc.flags = 0
    strh    wzr, [x11, #14]                       // desc.next  = 0

    add     x12, x19, #TX_QUEUE_OFF
    add     x12, x12, #VIRTQ_OFF_AVAIL            // x12 = TX AvailRing base
    and     w17, w15, w25                         // avail_slot = tx_next_avail & mask
    add     x14, x12, #4                          // x14 = &ring[0]
    strh    w16, [x14, x17, lsl #1]               // ring[avail_slot] = desc_idx

    dsb     sy

    add     w15, w15, #1                          // tx_next_avail++
    str     w15, [sp, #96]                        // stash for writeback (FSA-4)
    strh    w15, [x12, #2]                        // AvailRing.idx = tx_next_avail

    dsb     sy

    // Kick TX (queue 1).
    mov     w13, #1
    str     w13, [x6, #VIRTIO_MMIO_QUEUE_NOTIFY]

    adr     x1, tx_submitted_msg
    mov     w2, #(tx_submitted_msg_end - tx_submitted_msg)
    bl      emit_bytes

    // --- VIO-T-006: poll TX Used Ring + multi-frame reclaim ---
    //
    // FSA-4: tx_used_shadow loaded from l2_state (was reset to 0
    // each call pre-FSA-4). The wait loop polls for the FIRST
    // completion past the persistent shadow; once seen,
    // .Ltx_reclaim_body drains additional completions (analogous
    // to the RX multi-frame consume loop) up to TX_RECLAIM_BUDGET.
    add     x27, x19, #(TX_QUEUE_OFF + VIRTQ_OFF_USED)
    adrp    x10, __memreq_addr__l2_state
    add     x10, x10, :lo12:__memreq_addr__l2_state
    ldr     x10, [x10]
    ldrh    w28, [x10, #L2_STATE_TX_USED_SHADOW]
    str     wzr, [sp, #88]                       // reinit per-dispatch counter for TX

    add     x11, x27, #2                         // x11 = &TX UsedRing.idx
    ldr     w9, =POLL_BUDGET
.Ltx_wait_loop:
    yield                                        // KVM liveness
    ldarh   w10, [x11]                           // load-acquire TX UsedRing.idx
    cmp     w10, w28, uxth                       // u16 wrap-safe (see RX wait loop)
    b.ne    .Ltx_reclaim_body
    subs    w9, w9, #1
    b.ne    .Ltx_wait_loop

    adr     x1, tx_timeout_msg
    mov     w2, #(tx_timeout_msg_end - tx_timeout_msg)
    bl      emit_bytes
    b       .Ltx_writeback                        // writeback then clean halt

.Ltx_reclaim_body:
    // INVARIANT (FSA-3): every entry into .Ltx_reclaim_body —
    // both the fall-through from .Ltx_wait_loop and the back-edge
    // from .Ltx_reclaim_check — is preceded by an LDARH load-
    // acquire of TX UsedRing.idx. Subsequent ring-slot reads here
    // rely on that acquire for ordering against the producer
    // (Firecracker virtio-net VMM thread).
    and     w9, w28, w25                         // slot = tx_used_shadow & mask
    add     x9, x27, x9, lsl #3                  // entry_base = x27 + slot*8
    ldr     w20, [x9, #4]                        // ring[slot].id
    ldr     w21, [x9, #8]                        // ring[slot].len

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

    // Advance shadow + per-dispatch counter.
    add     w28, w28, #1                         // tx_used_shadow++
    ldr     w9, [sp, #88]
    add     w9, w9, #1
    str     w9, [sp, #88]

.Ltx_reclaim_check:
    // Budget check (per-dispatch counter; FSA-4 separated from shadow).
    ldr     w9, [sp, #88]
    cmp     w9, #TX_RECLAIM_BUDGET
    b.hs    .Ltx_writeback

    // Queue probe — non-blocking; drain more completions if any.
    add     x11, x27, #2
    ldarh   w10, [x11]
    cmp     w10, w28, uxth
    b.ne    .Ltx_reclaim_body
    // else: queue drained, fall through

.Ltx_writeback:
    // --- FSA-4: write back TX shadows to l2_state ---
    adrp    x10, __memreq_addr__l2_state
    add     x10, x10, :lo12:__memreq_addr__l2_state
    ldr     x10, [x10]
    ldr     w9, [sp, #96]                        // tx_next_avail (stashed)
    strh    w9, [x10, #L2_STATE_TX_NEXT_AVAIL]
    strh    w28, [x10, #L2_STATE_TX_USED_SHADOW]
    b       .Lok

.Lrx_bad_id_fail:
    // RX UsedRing.ring[slot].id is out of range. w20 = observed id.
    //
    // FSA-4 writeback intentionally skipped on validation-fail
    // paths: l2_dispatch returns w0 = 1 → caller sets
    // VIRTIO_STATUS_FAILED and halts the system. There is no
    // subsequent dispatch to consume the persisted shadow state,
    // so leaving l2_state at its on-entry values is correct
    // (any partial advance through .Lrx_consume_body is lost,
    // matching the device's own torn view of the failed frame).
    // Same logic applies to .Lrx_num_bufs_fail and
    // .Ltx_bad_id_fail below.
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
    add     sp, sp, #128                          // FSA-4/FSA-4(A): 128-byte frame
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

// FSA-4(A) ARP responder markers.
arp_request_msg:
    .ascii  "ARP:REQUEST\n"
arp_request_msg_end:
arp_reply_msg:
    .ascii  "ARP:REPLY\n"
arp_reply_msg_end:

// FSA-4(A) ARP reply template — 72 bytes, copied verbatim into
// tx_arp_buf then patched with dst MAC / THA / TPA from the
// stashed request info. Pre-baked fields: src MAC (GUEST_MAC),
// EtherType (ARP), HTYPE / PTYPE / HLEN / PLEN, OPER (reply),
// SHA (GUEST_MAC), SPA (GUEST_IP = 192.168.42.2).
//
// GUEST_MAC = 02:00:00:00:00:01 (locally-administered, matches
// the tx_test_pkt canary's src MAC for ARP-table consistency).
.align 4
arp_reply_template:
    // virtio_net_hdr (12 bytes zero)
    .byte   0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
    // Ethernet header
    .byte   0, 0, 0, 0, 0, 0                              // dst MAC (patched)
    .byte   0x02, 0x00, 0x00, 0x00, 0x00, 0x01           // src MAC = GUEST_MAC
    .byte   0x08, 0x06                                   // EtherType = ARP (BE)
    // ARP payload (28 bytes)
    .byte   0x00, 0x01                                   // HTYPE = Ethernet (BE)
    .byte   0x08, 0x00                                   // PTYPE = IPv4 (BE)
    .byte   0x06                                         // HLEN  = 6
    .byte   0x04                                         // PLEN  = 4
    .byte   0x00, 0x02                                   // OPER  = reply (BE)
    .byte   0x02, 0x00, 0x00, 0x00, 0x00, 0x01           // SHA   = GUEST_MAC
    .byte   0xC0, 0xA8, 0x2A, 0x02                       // SPA   = GUEST_IP (192.168.42.2)
    .byte   0, 0, 0, 0, 0, 0                              // THA   (patched)
    .byte   0, 0, 0, 0                                    // TPA   (patched)
    // Ethernet padding to 60-byte minimum frame (12+14+28 = 54;
    // need 12+60 = 72 total → 18 bytes of zero pad below).
    .byte   0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
arp_reply_template_end:
