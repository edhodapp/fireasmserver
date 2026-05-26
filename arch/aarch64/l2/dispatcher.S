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
// TX-specific poll budget. Pi 5 under Firecracker
// occasionally needs significantly more wall-clock time
// than 10M iters to see the device's first TX UsedRing.idx
// update — see the TX:TIMEOUT_DBG diagnostic landed in
// the prior commit, which confirmed `idx=0 exp=0` (device
// never acked) was the failure mode. Bumping the TX budget
// 5x (10M → 50M) gives the host's virtio-net thread more
// scheduling slack on the cold-cache first-TX path without
// pushing the outer tracer-bullet TIMEOUT (15s) over its
// limit — RX:TIMEOUT iterations still burn only the
// smaller POLL_BUDGET, so a no-traffic dispatch loop's
// total wall-clock cost is unchanged.
.equ TX_POLL_BUDGET,            50000000

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

// GUEST_MAC — the locally-administered unicast address this
// guest answers to. Must stay in sync with the same constants
// in arch/aarch64/platform/firecracker/boot.S (used for the
// tx_test_pkt canary's src MAC). Cross-file consistency is
// human discipline; each file asserts its own ETH-016 invariant.
.equ GUEST_MAC_BYTE_0,          0x02
.equ GUEST_MAC_BYTE_1,          0x00
.equ GUEST_MAC_BYTE_2,          0x00
.equ GUEST_MAC_BYTE_3,          0x00
.equ GUEST_MAC_BYTE_4,          0x00
.equ GUEST_MAC_BYTE_5,          0x01

// ETH-016 (IEEE 802.3 §4.1.2.1): outgoing source MAC MUST have
// the unicast bit clear (bit 0 of byte 0 = 0). The dispatcher
// uses GUEST_MAC as the source MAC for every outgoing frame
// it builds (ARP replies via arp_reply_template — and, when
// the TX API lands per docs/l2/TX_API.md, every queued L3 TX
// too). The build-time check catches a malformed-MAC edit
// before it ships.
.if GUEST_MAC_BYTE_0 & 1
.error "ETH-016: GUEST_MAC_BYTE_0 has multicast bit set"
.endif

// Derived LE encodings for the MAC filter's halfword/word
// compares. GNU as can't compute these from the .equ symbols
// above via bitwise OR (CLAUDE.md gotcha: .equ symbols can't
// combine via | with other .equ symbols — pre-compute as hex
// literals). Manual sync — if you change a GUEST_MAC_BYTE_N
// above, update the corresponding hex byte here. The ETH-016
// build-time check above guards byte 0's bit 0 specifically;
// cross-byte consistency between GUEST_MAC_BYTE_N and
// GUEST_MAC_LE_* is human-discipline.
//   LO32 = byte_0 | (byte_1 << 8) | (byte_2 << 16) | (byte_3 << 24)
//        = 0x02 | 0x00 | 0x00 | 0x00 = 0x00000002
//   HI16 = byte_4 | (byte_5 << 8)
//        = 0x00 | 0x0100 = 0x00000100
.equ GUEST_MAC_LE_LO32,         0x00000002
.equ GUEST_MAC_LE_HI16,         0x00000100

// TX API (phase b.3) — mirrors arch/aarch64/l2/tx_api.S and
// regions.yaml. Keep in sync.
.equ L2_TX_RING_MASK,           15
.equ L2_TX_RING_SIZE,           16
.equ L2_TX_HEADER_BYTES,        64
.equ L2_TX_SLOT_BYTES,          24
.equ L2_TX_SLOT_SEQ,            0
.equ L2_TX_SLOT_DST_MAC,        8
.equ L2_TX_SLOT_ETHERTYPE,      14
.equ L2_TX_SLOT_POOL_IDX,       16
.equ L2_TX_SLOT_PAYLOAD_LEN,    20
.equ L2_TX_PENDING_TAIL,        8
.equ L2_TX_POOL_NEXT_FREE,      8
.equ L2_TX_PAYLOAD_OFFSET,      26

// D068 working order item 6, phase 6.a/6.b: ARP cache state
// matching arch/aarch64/l2/arp_cache.S.
.equ ARP_STATE_REACHABLE,       2

.text
.global l2_dispatch
l2_dispatch:
    // AAPCS64 prologue + FSA-4 local frame.
    //
    // 144-byte frame layout (16-aligned per AAPCS64 §6.2.2):
    //   [sp +   0..7] : x29 (fp)
    //   [sp +   8..15]: x30 (lr)
    //   [sp +  16..79]: x20-x27 callee-saved pairs
    //   [sp +  80..87]: x28
    //   [sp +  88..91]: per-dispatch frame counter (u32; FSA-4)
    //   [sp +  92..95]: padding
    //   [sp +  96..99]: tx_next_avail stash (u32; FSA-4)
    //   [sp + 100..103]: padding
    //   [sp + 104..107]: unused (formerly arp_match flag,
    //                    removed in phase c.3 when the ARP
    //                    responder migrated to the TX API
    //                    queue path)
    //   [sp + 108..111]: sender_ip stash (FSA-4(A); the ARP
    //                    enqueue copies TPA from here)
    //   [sp + 112..117]: sender_mac stash (6 bytes; THA + dst
    //                    MAC for the ARP enqueue)
    //   [sp + 118..127]: padding
    //   [sp + 128..131]: TX API use_queue flag (b.3; 0/1)
    //   [sp + 132..135]: TX API pool_idx stash (b.3)
    //   [sp + 136..139]: TX desc.len stash (b.3; varies per request
    //                    when use_queue=1; canary + ARP-reply paths
    //                    set TX_TEST_PKT_LEN so the shared desc-write
    //                    site reads one slot)
    //   [sp + 140..143]: padding for 16-alignment
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
    sub     sp, sp, #144
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
    str     wzr, [sp, #128]                  // b.3: use_queue = 0
    mov     w11, #TX_TEST_PKT_LEN
    str     w11, [sp, #136]                  // b.3: desc_len default

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

    // --- ETH-003 / ETH-013: frame-size bounds (RX:DROP path) ---
    //
    // used_len (w21) includes the 12-byte virtio_net_hdr, so
    // the Ethernet wire range [60, 1518] becomes [72, 1530] in
    // virtio terms. Runt and oversize frames are dropped at L2
    // per docs/l2/REQUIREMENTS.md — the dispatcher emits an
    // RX:DROP marker, recycles the descriptor (the device must
    // get the buffer back regardless of whether we used it),
    // and re-enters the consume loop. ARP recognition + RX:FRAME
    // emit are skipped.
    //
    // Both bounds fit in 12-bit immediate cmp (max 4095), so no
    // movz scratch is needed.
    cmp     w21, #72
    b.lo    .Lrx_drop
    cmp     w21, #1530
    b.hi    .Lrx_drop

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

    // --- ETH-006/008, MAC-001..005: MAC filter ---
    //
    // Ethernet destination at buffer + 12 (past the
    // 12-byte virtio_net_hdr). Accept on:
    //   - multicast / broadcast (bit 0 of byte 0 set) — every
    //     multicast group address (e.g. IPv6 ff02::/16 mapping
    //     to 33:33:xx:xx:xx:xx) and broadcast (ff:ff:..) match
    //     this branch. Finer-grain multicast filtering (IGMP-
    //     aware joined groups) is out of scope until the
    //     project actually uses any.
    //   - unicast equal to GUEST_MAC (02:00:00:00:00:01).
    //
    // LE u32 of bytes 0-3 = 0x00000002, LE u16 of bytes 4-5 =
    // 0x0100. Both immediates fit `cmp` 12-bit imm12 (max 4095).
    //
    // Bit 0 of byte 0 wire-order = bit 0 of the LE-loaded u32,
    // so `tbnz w14, #0` is the multicast/broadcast check.
    //
    // Register: w14. Caller-saved scratch; verified dead at this
    // point. Liveness trace (grep `w14` in this file to confirm):
    //   - No prior w14 use in l2_dispatch's body — first write
    //     in the function is here in the MAC filter.
    //   - Next w14 access: the ARP recognition's EtherType /
    //     OPER / TPA ldrh's below (`ldrh w14, [x9, #24]` and
    //     friends) — all WRITES, so observing our value would
    //     not be possible even if it were live.
    // So clobbering w14 here is safe across every successor.
    // Reads from x9+N, never writes to x9 — buffer base stays
    // valid for hdr_len / num_buffers loads below.
    ldr     w14, [x9, #12]                        // dst MAC bytes 0-3
    tbnz    w14, #0, .Lmac_accept                 // multicast/broadcast → accept
    cmp     w14, #GUEST_MAC_LE_LO32               // GUEST_MAC bytes 0-3 LE
    b.ne    .Lrx_drop_mac
    ldrh    w14, [x9, #16]                        // dst MAC bytes 4-5
    cmp     w14, #GUEST_MAC_LE_HI16               // GUEST_MAC bytes 4-5 LE
    b.ne    .Lrx_drop_mac
.Lmac_accept:

    // --- ETH-015: source MAC unicast-bit check ---
    //
    // The source MAC field identifies the originating station,
    // which is by definition a unicast endpoint (IEEE 802.3-2018
    // §4.1.2.1). Frames with bit 0 of byte 0 = 1 (multicast or
    // broadcast source) are malformed and MUST be dropped.
    //
    // Source MAC at buffer + 18 (12 virtio_net_hdr + 6 dst MAC).
    // One byte-test is enough; we don't care about the rest of
    // the source MAC for this gate. w14 reuses the same register
    // as the MAC filter above — it's freshly written here, so
    // no liveness concern with the prior value.
    ldrb    w14, [x9, #18]                        // src MAC byte 0
    tbnz    w14, #0, .Lrx_drop_src

    // --- ETH-018: PAUSE frame drop ---
    //
    // Per IEEE 802.3x-1997 §31B.1, receivers that do not
    // implement link-level flow control MUST silently discard
    // MAC Control frames (EtherType 0x8808 = 0x0888 LE16).
    // We don't inspect the MAC Control opcode — any 0x8808
    // frame is discarded as a class. Acting on received
    // PAUSE (throttling our TX) stays deferred per D045.
    //
    // w14 reused as scratch (freshly loaded here; no liveness
    // with prior values). 0x0888 doesn't fit a 12-bit cmp imm,
    // so movz a comparison register from the wide constant.
    ldrh    w14, [x9, #24]                        // EtherType
    mov     w15, #0x0888                          // PAUSE BE = 0x0888 LE16
    cmp     w14, w15
    b.eq    .Lrx_drop_pause

    // Read virtio_net_hdr.hdr_len (u16 @ +2) and num_buffers (u16 @ +10).
    ldrh    w22, [x9, #2]                         // hdr_len
    ldrh    w23, [x9, #10]                        // num_buffers

    // Validate num_buffers == 1 under MVP no-MRG_RXBUF policy.
    cmp     w23, #1
    b.ne    .Lrx_num_bufs_fail

    // Defense-in-depth: virtio_net_hdr.flags + gso_type must
    // both be 0 under our VERSION_1-only feature policy (no
    // F_CSUM, no F_GUEST_CSUM, no F_GSO, no F_GUEST_TSO*, no
    // F_GUEST_UFO). A device that asserts them silently — or
    // a future feature negotiated without updating the
    // dispatcher — would have semantics propagate unchecked.
    // Single u16 load + cbnz covers both fields. New fail
    // label .Lrx_hdr_flags_fail mirrors the bad_id / num_bufs
    // shape. Mirrors x86_64 dispatcher.S.
    ldrh    w14, [x9, #0]                         // flags | (gso_type << 8)
    cbnz    w14, .Lrx_hdr_flags_fail

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
    // ARP frame. Branch on OPER: request (0x0100 LE) → existing
    // responder path; reply (0x0200 LE) → phase 6.b cache
    // insert. Other OPER values fall through to L3 (stub no-op).
    ldrh    w14, [x9, #32]                        // ARP OPER
    cmp     w14, #0x0200                          // reply?
    b.eq    .Larp_reply_path
    cmp     w14, #0x0100                          // request?
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

    adr     x1, arp_request_msg
    mov     w2, #(arp_request_msg_end - arp_request_msg)
    bl      emit_bytes

    // --- Phase (c): queue the ARP reply via the TX API ---
    //
    // Mirrors arch/x86_64/l2/dispatcher.S phase (c.1).
    // Producer side of the L2 TX API: alloc one pool buffer,
    // write the 28-byte ARP reply body at +PAYLOAD_OFFSET,
    // enqueue one request with dst = sender's MAC, etype =
    // 0x0806, payload_len = 28. The dispatcher's TX phase
    // queue-drain consumer (b.3) builds the wire frame and
    // submits to virtio.
    //
    // Caller-saved regs (x9-x18) are all available; sender_mac
    // and sender_ip stashes at [sp+112..117] and [sp+108..111]
    // are untouched by the TX API calls. x19-x28 are AAPCS64
    // callee-saved so the dispatcher's pinned state survives.
    // emit_bytes / emit_hex32 self-load UART_BASE per task #44,
    // so we don't need to preserve x0 across the call chain.
    bl      l2_tx_buffer_pool_alloc           // w0 = pool_idx
    cmn     w0, #1                            // == 0xFFFFFFFF (L2_TX_POOL_IDX_FULL = -1 as i32)
    b.eq    .Larp_no_enqueue
    mov     w14, w0                           // save idx (x14 caller-saved but not touched by buffer_addr)
    bl      l2_tx_buffer_addr                 // x0 = pool buffer base
    add     x9, x0, #L2_TX_PAYLOAD_OFFSET     // x9 = payload start

    // Write 28-byte ARP body at x9.
    mov     w10, #0x0100
    strh    w10, [x9, #0]                     // HTYPE 0x0001 BE
    mov     w10, #0x0008
    strh    w10, [x9, #2]                     // PTYPE 0x0800 BE
    mov     w10, #6
    strb    w10, [x9, #4]                     // HLEN
    mov     w10, #4
    strb    w10, [x9, #5]                     // PLEN
    mov     w10, #0x0200
    strh    w10, [x9, #6]                     // OPER 0x0002 BE (reply)
    mov     w10, #GUEST_MAC_LE_LO32           // 0x00000002
    str     w10, [x9, #8]                     // SHA bytes 0-3
    mov     w10, #GUEST_MAC_LE_HI16           // 0x00000100
    strh    w10, [x9, #12]                    // SHA bytes 4-5
    ldr     w10, =0x022AA8C0                  // SPA wire bytes c0 a8 2a 02 (LE u32)
    str     w10, [x9, #14]
    // THA from sender_mac stash.
    ldrh    w10, [sp, #112]
    strh    w10, [x9, #18]
    ldrh    w10, [sp, #114]
    strh    w10, [x9, #20]
    ldrh    w10, [sp, #116]
    strh    w10, [x9, #22]
    // TPA from sender_ip stash.
    ldr     w10, [sp, #108]
    str     w10, [x9, #24]

    // Enqueue.
    add     x0, sp, #112                      // dst MAC ptr = sender_mac stash
    mov     w1, #0x0608                       // etype 0x0806 BE
    mov     w2, w14                           // pool_idx
    mov     w3, #28                           // payload_len
    bl      l2_tx_enqueue
    // w0 = OK (0) or FULL (1); ignored — ARP requester retries
    // on its own if the reply doesn't arrive.

    adr     x1, arp_reply_msg
    mov     w2, #(arp_reply_msg_end - arp_reply_msg)
    bl      emit_bytes
    b       .Larp_no_enqueue

    // --- Phase 6.b: ARP REPLY → cache update ---
    //
    // Mirror of arch/x86_64/l2/dispatcher.S phase 6.b.
    // Stash SHA+SPA from the inbound reply into the same
    // slots the request path uses (request/reply are
    // mutually exclusive per dispatch iteration), then call
    // arp_cache_insert(w0=ip, x1=mac_ptr, w2=state=REACHABLE).
    // x19-x28 are AAPCS64 callee-saved so the dispatcher's
    // pinned state survives the call.
.Larp_reply_path:
    ldrh    w14, [x9, #50]                        // TPA bytes 0-1
    mov     w15, #0xA8C0
    cmp     w14, w15
    b.ne    .Lno_arp                              // reply not for us → L3 (no-op)
    ldrh    w14, [x9, #52]                        // TPA bytes 2-3
    mov     w15, #0x022A
    cmp     w14, w15
    b.ne    .Lno_arp

    // Stash SHA + SPA at the request path's slots.
    ldrh    w14, [x9, #34]
    strh    w14, [sp, #112]
    ldrh    w14, [x9, #36]
    strh    w14, [sp, #114]
    ldrh    w14, [x9, #38]
    strh    w14, [sp, #116]
    ldr     w14, [x9, #40]
    str     w14, [sp, #108]

    ldr     w0, [sp, #108]                        // ip arg
    add     x1, sp, #112                          // mac ptr arg
    mov     w2, #ARP_STATE_REACHABLE              // state
    bl      arp_cache_insert

    // Emit ARP:CACHE_INSERT ip=XXXXXXXX
    adr     x1, arp_cache_insert_msg
    mov     w2, #(arp_cache_insert_msg_end - arp_cache_insert_msg)
    bl      emit_bytes
    ldr     w5, [sp, #108]                        // SPA
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
    bl      emit_bytes

    // Fall through to .Larp_no_enqueue (skip L3 hand-off).
.Larp_no_enqueue:
    // ARP-for-us path done — skip the L3 hand-off (the
    // responder consumed this frame). Jump past the L3 call.
    b       .Lafter_l3
.Lno_arp:
    // --- L3 hand-off (item 5, D068 working order) ---
    //
    // Reached on accepted frames that were NOT ARP-for-us.
    // Call l3_rx_dispatch(x0 = frame_addr, w1 = wire_len)
    // where frame_addr skips the 12-byte virtio_net_hdr to
    // land on the Ethernet header, and wire_len is the
    // bytes from dst MAC through end of payload (= used_len
    // minus 12, which is what virtio reported minus the
    // virtio_net_hdr the device prepended).
    //
    // x21 (used_len) and w20 (id) are AAPCS64 callee-saved
    // and survive the L3 call automatically. x9 (RX buffer
    // ptr) is caller-saved — read it BEFORE the call to
    // compute frame_addr; after the call, x9 is dead in
    // this dispatch.
    add     x0, x9, #12                           // frame_addr = RX buf + virtio_net_hdr
    sub     w1, w21, #12                          // wire_len = used_len - 12
    bl      l3_rx_dispatch
.Lafter_l3:

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

    // --- TX API (b.3): peek + drain l2_tx_pending queue ---
    //
    // Vyukov MPSC consumer. Peek slot at consumer-only tail;
    // read seq with LDAR (acquire). If seq == tail + 1, drain
    // the entry: read fields, build wire frame in front of the
    // producer's payload in the pool buffer (consumer fills
    // virtio_net_hdr + Eth header at bytes 0..25), set x10 to
    // the pool buffer addr and [sp + 136] to the desc_len, then
    // jump to the shared TX submit. Vyukov seq release + pool
    // free run after TX:RECLAIMED in .Ltx_writeback.
    //
    // If seq != tail + 1, queue is empty for this round → fall
    // through to the existing arp_match / canary path unchanged.
    adrp    x10, __memreq_addr__l2_tx_pending
    add     x10, x10, :lo12:__memreq_addr__l2_tx_pending
    ldr     x10, [x10]                            // x10 = pending region base
    ldr     w11, [x10, #L2_TX_PENDING_TAIL]      // w11 = tail (consumer-owned)
    and     w12, w11, #L2_TX_RING_MASK           // w12 = slot
    mov     w13, #L2_TX_SLOT_BYTES
    umaddl  x13, w12, w13, xzr                   // x13 = slot * 24
    add     x14, x10, #L2_TX_HEADER_BYTES        // x14 = ring base
    add     x14, x14, x13                        // x14 = &ring[slot] (seq @ +0)
    ldar    w13, [x14]                            // w13 = published seq (acquire)
    add     w12, w11, #1                          // w12 = tail + 1
    cmp     w13, w12
    b.ne    .Ltx_queue_empty

    // Queue has request. Read entry fields BEFORE releasing the
    // slot — producer can overwrite once we bump seq for the
    // next round.
    mov     w13, #1
    str     w13, [sp, #128]                       // use_queue = 1
    ldr     w13, [x14, #L2_TX_SLOT_POOL_IDX]
    str     w13, [sp, #132]                       // pool_idx stash

    // desc_len = 12 + max(60, 14 + payload_len)
    ldrh    w12, [x14, #L2_TX_SLOT_PAYLOAD_LEN]
    add     w12, w12, #14                         // wire len without pad
    cmp     w12, #60
    b.hs    .Ltx_wire_ok
    mov     w12, #60
.Ltx_wire_ok:
    add     w12, w12, #12                         // +12 virtio_net_hdr
    str     w12, [sp, #136]                       // desc_len

    // Pool buffer addr: x10 = pool_base + HEADER + pool_idx * 2048.
    adrp    x10, __memreq_addr__l2_tx_buffer_pool
    add     x10, x10, :lo12:__memreq_addr__l2_tx_buffer_pool
    ldr     x10, [x10]
    add     x10, x10, #L2_TX_HEADER_BYTES
    ldr     w13, [sp, #132]                       // pool_idx
    add     x10, x10, x13, lsl #11                // x10 = pool buffer

    // Build wire frame headers in front of producer's payload.
    // Pool buffer is 4096-aligned (per regions.yaml); pool
    // buffer N at pool_base + 64 + N * 2048 = also 64-byte-
    // aligned. So [x10, #0] is aligned. But Device-typed
    // memory (bare-metal aarch64 without MMU) rejects
    // word stores that are not 4-byte-aligned within the
    // buffer. Word offsets that need to be aligned: +12
    // (dst MAC bytes 0-3 — aligned), +18 (src MAC bytes
    // 0-3 — UNALIGNED, 18 mod 4 = 2). Split the +18 store
    // into halfword pairs.
    stp     xzr, xzr, [x10, #0]
    //   +12..17 dst MAC from entry (12 mod 4 = 0, word OK)
    ldr     w13, [x14, #L2_TX_SLOT_DST_MAC]      // bytes 0-3
    str     w13, [x10, #12]
    ldrh    w13, [x14, #(L2_TX_SLOT_DST_MAC + 4)] // bytes 4-5
    strh    w13, [x10, #16]
    //   +18..23 src MAC = GUEST_MAC (18 mod 4 = 2 → split)
    mov     w13, #GUEST_MAC_LE_LO32              // 0x00000002
    strh    w13, [x10, #18]                      // bytes 0-1 (= 0x02 0x00)
    strh    wzr, [x10, #20]                      // bytes 2-3 (= 0x00 0x00)
    mov     w13, #GUEST_MAC_LE_HI16
    strh    w13, [x10, #22]
    //   +24..25 ethertype (24 mod 2 = 0, halfword OK)
    ldrh    w13, [x14, #L2_TX_SLOT_ETHERTYPE]
    strh    w13, [x10, #24]

    // Runt-frame zero pad: if (14 + payload_len) < 60, zero the
    // gap [26 + payload_len .. 11 + desc_len].
    ldrh    w13, [x14, #L2_TX_SLOT_PAYLOAD_LEN]
    add     w13, w13, #L2_TX_PAYLOAD_OFFSET      // pad start offset
    ldr     w12, [sp, #136]                       // desc_len = pad end offset (exclusive)
    cmp     w13, w12
    b.hs    .Ltx_no_pad
.Ltx_pad_loop:
    strb    wzr, [x10, x13]
    add     w13, w13, #1
    cmp     w13, w12
    b.lo    .Ltx_pad_loop
.Ltx_no_pad:

    b       .Larp_have_tx_addr

.Ltx_queue_empty:
    // Phase (c): the inline FSA-4(A) ARP-build path is gone.
    // ARP replies now flow through the TX API queue from the
    // RX recognition site; the queue-drain consumer above
    // handles them. Queue-empty therefore means there's
    // genuinely nothing to send, so submit the canary so the
    // TX path is always exercised end-to-end.
    adrp    x10, tx_test_pkt
    add     x10, x10, :lo12:tx_test_pkt           // runtime addr of canary
.Larp_have_tx_addr:
    add     x11, x19, #TX_QUEUE_OFF               // x11 = TX desc[0] base
    add     x11, x11, x16, lsl #4                 // x11 = TX desc[desc_idx] base (16 B/desc)
    str     x10, [x11, #0]                        // desc.addr (u64)
    ldr     w12, [sp, #136]                       // b.3: desc_len stash (varies for queue path)
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
    ldr     w9, =TX_POLL_BUDGET
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
    // Diagnostic emit on TIMEOUT (post-cursor-fix Pi flake
    // investigation): dump the device's last-visible
    // UsedRing.idx vs our expected shadow. Three outcomes:
    //   idx == exp → device never acked; kick reached it but
    //     no completion fired before the budget exhausted
    //     (H1 — host KVM scheduling jitter > poll budget
    //     wall-clock).
    //   idx > exp → device acked but our acquire loop didn't
    //     observe it (H3 — barrier/race; should never happen
    //     given ldarh is a load-acquire on inner-shareable
    //     Normal memory).
    //   idx < exp → device went backwards (impossible under
    //     virtio spec; would indicate a bug elsewhere).
    // w28 is AAPCS64 callee-saved so survives every emit
    // call. x11 and x19 are also safe (x19 is the RX virtq
    // base pin set in the dispatcher's prologue).
    adr     x1, tx_timeout_dbg_msg
    mov     w2, #(tx_timeout_dbg_msg_end - tx_timeout_dbg_msg)
    bl      emit_bytes
    // Re-derive &TX UsedRing.idx (x11 is caller-saved; was
    // clobbered by the emit chain above).
    add     x11, x19, #(TX_QUEUE_OFF + VIRTQ_OFF_USED)
    add     x11, x11, #2
    ldarh   w5, [x11]
    bl      emit_hex32
    adr     x1, tx_timeout_exp_msg
    mov     w2, #(tx_timeout_exp_msg_end - tx_timeout_exp_msg)
    bl      emit_bytes
    mov     w5, w28
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
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
    // --- TX API (b.3): pool free + Vyukov slot release ---
    //
    // If a queue request was drained this dispatch
    // ([sp + 128] = 1), the frame has now landed in
    // TX:RECLAIMED. Free the pool buffer (consumer-only inc on
    // next_free) and release the Vyukov slot by writing
    // seq[slot] = tail + 1 + RING_SIZE (release-store via STLR;
    // the next producer for that slot uses LDAR to see it).
    // Then bump tail.
    ldr     w11, [sp, #128]
    cbz     w11, .Ltx_no_pool_free
    // Pool free.
    adrp    x10, __memreq_addr__l2_tx_buffer_pool
    add     x10, x10, :lo12:__memreq_addr__l2_tx_buffer_pool
    ldr     x10, [x10]
    ldr     w11, [x10, #L2_TX_POOL_NEXT_FREE]
    add     w11, w11, #1
    str     w11, [x10, #L2_TX_POOL_NEXT_FREE]
    // Vyukov slot release.
    adrp    x10, __memreq_addr__l2_tx_pending
    add     x10, x10, :lo12:__memreq_addr__l2_tx_pending
    ldr     x10, [x10]
    ldr     w11, [x10, #L2_TX_PENDING_TAIL]     // w11 = tail
    and     w12, w11, #L2_TX_RING_MASK          // w12 = slot
    mov     w13, #L2_TX_SLOT_BYTES
    umaddl  x13, w12, w13, xzr                  // x13 = slot * 24
    add     x14, x10, #L2_TX_HEADER_BYTES       // x14 = ring base
    add     x14, x14, x13                       // x14 = &ring[slot] (seq @ +0)
    add     w12, w11, #(1 + L2_TX_RING_SIZE)    // w12 = tail + 1 + N
    stlr    w12, [x14]                           // release-store seq
    // Bump tail (consumer-only).
    add     w11, w11, #1
    str     w11, [x10, #L2_TX_PENDING_TAIL]
.Ltx_no_pool_free:

    // --- FSA-4: write back TX shadows to l2_state ---
    adrp    x10, __memreq_addr__l2_state
    add     x10, x10, :lo12:__memreq_addr__l2_state
    ldr     x10, [x10]
    ldr     w9, [sp, #96]                        // tx_next_avail (stashed)
    strh    w9, [x10, #L2_STATE_TX_NEXT_AVAIL]
    strh    w28, [x10, #L2_STATE_TX_USED_SHADOW]
    b       .Lok

.Lrx_drop:
    // --- ETH-003 / ETH-013: drop a malformed-size RX frame ---
    //
    // Branched here from .Lrx_consume_body when used_len (w21) is
    // outside the [72, 1530] virtio range (60..1518 wire bytes
    // per IEEE 802.3 / Virtio 1.2 §5.1.6.1). The descriptor is
    // returned to the device via the normal AvailRing path so we
    // don't leak buffers; only the per-frame consumer-side
    // processing (ARP recognition, RX:FRAME emit, num_bufs check)
    // is skipped.
    //
    // Defensive stash: w20 (id) and w21 (used_len) are AAPCS64
    // callee-saved AND emit_bytes/emit_hex32 today don't touch
    // x19-x28 either. But the recycle below depends on w20 being
    // correct, and silent corruption of the slot id would hand
    // the device a wrong descriptor index (then we leak / corrupt
    // the ring on the next consume). Save the pair on the stack
    // across the emit chain so a future helper-contract change
    // can't silently break the recycle. Mirrors the x86_64
    // [rsp + 12] discipline. stp pre-indexed keeps sp 16-aligned
    // for the bl emit_* calls.
    stp     x20, x21, [sp, #-16]!                // save id+used_len

    adr     x1, rx_drop_msg
    mov     w2, #(rx_drop_msg_end - rx_drop_msg)
    bl      emit_bytes
    ldr     w5, [sp, #8]                         // reload used_len
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #NEWLINE_MSG_LEN
    bl      emit_bytes

    ldp     x20, x21, [sp], #16                  // restore id+used_len
    b       .Lrx_drop_recycle

.Lrx_drop_mac:
    // --- ETH-006/008, MAC-001..005: drop a wrong-MAC RX frame ---
    //
    // Branched here from the MAC filter when dst MAC is unicast
    // and not GUEST_MAC. used_len (w21) and frame size are valid;
    // we just don't process frames that aren't for us.
    //
    // Defensive stash same as .Lrx_drop: emit_bytes today doesn't
    // touch x19-x28 but a future helper-contract change shouldn't
    // silently corrupt the slot id we hand back to the device.
    stp     x20, x21, [sp, #-16]!                // save id+used_len
    adr     x1, rx_drop_mac_msg
    mov     w2, #(rx_drop_mac_msg_end - rx_drop_mac_msg)
    bl      emit_bytes
    ldp     x20, x21, [sp], #16                  // restore id+used_len
    b       .Lrx_drop_recycle

.Lrx_drop_src:
    // --- ETH-015: drop a malformed-source-MAC RX frame ---
    //
    // Branched here from the source-MAC check when src MAC's
    // byte 0 bit 0 is set (multicast / broadcast source — invalid
    // per IEEE 802.3 §4.1.2.1). Frame is structurally well-formed
    // through the dst MAC filter; only the src field is wrong.
    stp     x20, x21, [sp, #-16]!                // save id+used_len
    adr     x1, rx_drop_src_msg
    mov     w2, #(rx_drop_src_msg_end - rx_drop_src_msg)
    bl      emit_bytes
    ldp     x20, x21, [sp], #16                  // restore id+used_len
    b       .Lrx_drop_recycle

.Lrx_drop_pause:
    // --- ETH-018: drop a PAUSE / MAC Control RX frame ---
    //
    // Branched here when EtherType = 0x8808 (MAC Control). The
    // frame passed every other gate (size, dst MAC, src MAC) —
    // we just don't act on flow-control frames per ETH-018's
    // silent-discard mandate. Counter (rx_pause_dropped) deferred
    // until stats infrastructure exists.
    stp     x20, x21, [sp, #-16]!                // save id+used_len
    adr     x1, rx_drop_pause_msg
    mov     w2, #(rx_drop_pause_msg_end - rx_drop_pause_msg)
    bl      emit_bytes
    ldp     x20, x21, [sp], #16                  // restore id+used_len
    // fall through to recycle

.Lrx_drop_recycle:
    // Shared recycle code reached by both .Lrx_drop (size bounds)
    // and .Lrx_drop_mac (MAC filter). Recycle: write id to
    // AvailRing.ring[next_avail & mask] so the device gets the
    // buffer back, then advance shadows + per-dispatch counter
    // and re-enter the consume loop. The drop counts toward
    // RX_FRAME_BUDGET because it consumed work the dispatcher
    // must do (read + recycle).
    and     w10, w24, w25
    add     x11, x19, #VIRTQ_OFF_AVAIL
    add     x11, x11, #4
    strh    w20, [x11, x10, lsl #1]

    add     w24, w24, #1
    add     w28, w28, #1
    ldr     w9, [sp, #88]
    add     w9, w9, #1
    str     w9, [sp, #88]

    b       .Lrx_consume_check

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

.Lrx_hdr_flags_fail:
    // Defense-in-depth: virtio_net_hdr.flags + gso_type
    // non-zero. w14 holds (flags | (gso_type << 8)) — dump
    // it in the marker so the diagnostic shows what bits
    // were set. Stash w14 across the emit chain (emit_bytes
    // / emit_hex32 don't formally preserve w14).
    mov     w20, w14
    adr     x1, rx_hdr_flags_msg
    mov     w2, #(rx_hdr_flags_msg_end - rx_hdr_flags_msg)
    bl      emit_bytes
    mov     w5, w20
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
    add     sp, sp, #144                          // b.3: 128 → 144 for TX API stash
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

rx_hdr_flags_msg:
    .ascii  "RX:FAIL hdr_flags="
rx_hdr_flags_msg_end:

rx_drop_msg:
    .ascii  "RX:DROP used_len="
rx_drop_msg_end:

rx_drop_mac_msg:
    .ascii  "RX:DROP mac\n"
rx_drop_mac_msg_end:

rx_drop_src_msg:
    .ascii  "RX:DROP src\n"
rx_drop_src_msg_end:

rx_drop_pause_msg:
    .ascii  "RX:DROP pause\n"
rx_drop_pause_msg_end:

rx_returned_msg:
    .ascii  "RX:RETURNED\n"
rx_returned_msg_end:

tx_submitted_msg:
    .ascii  "TX:SUBMITTED\n"
tx_submitted_msg_end:
tx_timeout_msg:
    .ascii  "TX:TIMEOUT\n"
tx_timeout_msg_end:
tx_timeout_dbg_msg:
    .ascii  "TX:TIMEOUT_DBG idx="
tx_timeout_dbg_msg_end:
tx_timeout_exp_msg:
    .ascii  " exp="
tx_timeout_exp_msg_end:
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
arp_cache_insert_msg:
    .ascii  "ARP:CACHE_INSERT ip="
arp_cache_insert_msg_end:

