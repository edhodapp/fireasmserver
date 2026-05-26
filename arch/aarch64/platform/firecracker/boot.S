// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// aarch64 Firecracker guest stub. Emits the L2 tracer-bullet
// marker chain — matching x86_64's VIRTIO probe / init sequence
// slice by slice. This commit: READY + VIRTIO:OK (probe only).
// Follow-up commits will add VIO-001..009 init, VIO-R receive,
// VIO-T transmit.
//
// Image format: Linux arm64 Image (per the Firecracker aarch64 boot
// protocol — kernel file is a Linux Image header + code, loaded at
// RAM_BASE + text_offset).
//
// Firecracker v1.15.1 aarch64 layout
// (src/vmm/src/arch/aarch64/layout.rs + src/vmm/src/devices/legacy/serial.rs):
//   RAM base          = 0x8000_0000
//   text_off          = 0x0008_0000
//   load addr         = 0x8008_0000
//   MMIO32 start      = 0x4000_0000
//   boot device MMIO  = MMIO32_START          = 0x4000_0000
//   RTC MMIO          = boot_dev + MMIO_LEN   = 0x4000_1000
//   SERIAL MMIO       = RTC + MMIO_LEN        = 0x4000_2000
//   VIRTIO-MMIO #0    = SERIAL + MMIO_LEN     = 0x4000_3000   ← virtio-net
//   (MMIO_LEN = 0x1000)
//
// Firecracker attaches the network-interfaces[0] virtio-net device at
// the first post-SERIAL MMIO slot. Until a network-interface is
// configured in the Firecracker JSON, reads at 0x40003000 return 0
// (unpopulated MMIO slot).
//
// Despite arm64, Firecracker emulates an 8250/16550-compatible serial
// (via vm_superio::Serial), NOT PL011. Register map is the x86 COM1
// layout at a different base. Critical offsets used below:
//   THR/RBR    0x00   transmit-holding / receive-buffer (8-bit)
//   LSR        0x05   line status   bit 5 = THRE (Transmit Holding
//                                   Register Empty; 1 = ready-to-send)
//
// Note polarity vs. PL011: THRE == 1 means READY. tbz (branch if zero)
// is the wait-while-not-ready test, NOT tbnz.

// Constants — match the x86_64 boot.S naming for cross-arch cognitive
// continuity. Lower-case `virt` is the virtio-MMIO magic word.
.equ UART_BASE,         0x40002000
// TX API end-to-end test pre-bake — pool buffer payload start
// offset (12 B virtio_net_hdr + 14 B Eth header). Only used when
// TXAPI_PREBAKE is defined (see the pre-bake block before
// .Ldispatch_loop).
.equ L2_TX_PAYLOAD_OFFSET, 26
.equ VIRTIO_MMIO_BASE,  0x40003000
.equ VIRTIO_MAGIC_LO,   0x6976          // "vi" little-endian
.equ VIRTIO_MAGIC_HI,   0x7472          // "tr" little-endian

// virtio-MMIO register offsets (Virtio 1.2 §4.2.2, Table 4.1) —
// arch-neutral, identical to x86_64's equivalents.
.equ VIRTIO_MMIO_DEVICE_FEATURES,     0x010
.equ VIRTIO_MMIO_DEVICE_FEATURES_SEL, 0x014
.equ VIRTIO_MMIO_DRIVER_FEATURES,     0x020
.equ VIRTIO_MMIO_DRIVER_FEATURES_SEL, 0x024
.equ VIRTIO_MMIO_QUEUE_SEL,           0x030
.equ VIRTIO_MMIO_QUEUE_NUM_MAX,       0x034
.equ VIRTIO_MMIO_QUEUE_NUM,           0x038
.equ VIRTIO_MMIO_QUEUE_READY,         0x044
.equ VIRTIO_MMIO_QUEUE_NOTIFY,        0x050
.equ VIRTIO_MMIO_STATUS,              0x070
.equ VIRTIO_MMIO_QUEUE_DESC_LOW,      0x080
.equ VIRTIO_MMIO_QUEUE_DESC_HIGH,     0x084
.equ VIRTIO_MMIO_QUEUE_DRIVER_LOW,    0x090
.equ VIRTIO_MMIO_QUEUE_DRIVER_HIGH,   0x094
.equ VIRTIO_MMIO_QUEUE_DEVICE_LOW,    0x0A0
.equ VIRTIO_MMIO_QUEUE_DEVICE_HIGH,   0x0A4

// Virtqueue memory region (aarch64 RAM_BASE-relative; differs
// from x86_64 which places virtqueues at a flat 4 MiB). RAM_BASE
// = 0x80000000; image loads at 0x80080000; stack at 0x80200000;
// virtqueues at 0x80400000 leaves ~2 MiB headroom. 12 KiB per
// queue: page-aligned slots for descriptor table, available ring,
// used ring. Firecracker zero-inits guest RAM, so idx=0 across
// every ring at boot.
// Virtqueue layout — the base address is allocator-placed via the
// `virtq` region in regions.yaml (D066 step 5c, hot-tier); x19 is
// pinned to assigned_addr at kernel entry by memreq_pin_hot.inc.
// Queue addressing under the migration:
//   RX queue base = x19 + RX_QUEUE_OFF  (= x19)
//   TX queue base = x19 + TX_QUEUE_OFF  (= x19 + VIRTQ_STRIDE)
// Within each queue: + VIRTQ_OFF_{DESC,AVAIL,USED}.
.equ VIRTQ_STRIDE,      0x3000
.equ VIRTQ_OFF_DESC,    0x0000
.equ VIRTQ_OFF_AVAIL,   0x1000
.equ VIRTQ_OFF_USED,    0x2000
.equ RX_QUEUE_OFF,      0 * VIRTQ_STRIDE
.equ TX_QUEUE_OFF,      1 * VIRTQ_STRIDE

// Largest queue size this layout supports: 256 descriptors at
// 16 B each = 4096 B, filling the descriptor-table page. Device
// reporting QueueNumMax > 256 is spec-allowed; we clamp to what
// our allocated memory can back (Virtio 1.2 §4.1.4.3 permits
// any power-of-2 ≤ QueueNumMax).
.equ VIRTQ_MAX_SIZE,    0x100

// Wait budget for RX / TX Used Ring polls.
//
// Task #15 (2026-05-20): empirically dropped from 100M → 10M after
// investigation found the no-traffic wall clock on Pi 5 was
// 35-50s under KVM, dominated by `yield` + VMEXIT cost per
// iteration (the `yield` ahead of the dsb-ishld/ldrh pair is
// load-bearing: it provides timing slack that lets the host-side
// virtio device thread finish writing the buffer header before
// the guest reads it, preventing a producer-consumer race that
// surfaces as `RX:FAIL num_bufs=00000000`).
//
// 10M iterations × ~350 ns/iter under KVM = ~3.5 s wall clock —
// well below any reasonable frame-arrival latency, well above
// the per-byte UART emit cost. Parity with x86_64's 100M literal
// is dropped: aarch64-under-KVM has ~100× the per-iter cost, so
// equal iteration counts give vastly unequal wall clocks. Real
// parity is "wait long enough for any reasonable frame to
// arrive on each platform" — and that's wall-clock, not
// iteration-count, terms.
.equ POLL_BUDGET,       10000000

// Virtqueue descriptor flag bits (Virtio 1.2 §2.7.5.1).
.equ VIRTQ_DESC_F_NEXT,     1    // buffer continues via next field
.equ VIRTQ_DESC_F_WRITE,    2    // device may write (RX buffer)
.equ VIRTQ_DESC_F_INDIRECT, 4    // descriptor points at a chain

// RX buffer pool. 256 buffers × 2 KiB = 512 KiB. Allocator-placed
// via the `rx_buffer` region in regions.yaml (D066 step 5b);
// consumers load `__memreq_addr__rx_buffer` for the base address.
// The per-buffer size stays a .equ constant — protocol-level slot
// width, not a region.
.equ RX_BUFFER_SIZE,    0x800     // 2 KiB

// Device Status bit values (Virtio 1.2 §2.1). Bit-position numbering
// matches the REQUIREMENTS.md VIO table. Shared across arches.
.equ VIRTIO_STATUS_ACKNOWLEDGE,   1    // bit 0 — driver found the device
.equ VIRTIO_STATUS_DRIVER,        2    // bit 1 — driver knows how to use it
.equ VIRTIO_STATUS_DRIVER_OK,     4    // bit 2 — device is live (VIO-008)
.equ VIRTIO_STATUS_FEATURES_OK,   8    // bit 3 — feature subset accepted
.equ VIRTIO_STATUS_FAILED,        0x80 // bit 7 — driver has given up (VIO-009)

// Feature bits (Virtio 1.2 §6 — transport + device-specific). Bits
// are 64-wide; MMIO exposes them via two 32-bit pages selected by
// DeviceFeaturesSel / DriverFeaturesSel (0=bits 0-31, 1=bits 32-63).
// VIRTIO_F_VERSION_1 is bit 32 globally → bit 0 of the high page.
.equ VIRTIO_F_VERSION_1_HIBIT,    1    // bit 32 == 1 in the high page

// ================================================================
// Virtio feature negotiation policy
// ================================================================
// Mirrors arch/x86_64/platform/firecracker/boot.S — see that file
// for the full per-bit rationale. Three classes:
//
//   REQUIRED — must be offered by the device AND set in our
//              driver_features. Missing → fail at
//              .Lfeatures_fail_no_v1.
//   ACCEPTED — set in driver_features iff offered by device.
//   REJECTED — implicit (any bit not in REQUIRED or ACCEPTED).
//
// Negotiation formula:
//     driver_features = (offered & ACCEPTED) | REQUIRED
//
// Today's minimum-viable policy:
//   REQUIRED_LO = 0
//   REQUIRED_HI = VIRTIO_F_VERSION_1_HIBIT  (bit 32: modern virtio)
//   ACCEPTED_LO = 0   (every §5.1.3 net feature rejected — see
//                      x86_64 boot.S for the bit-by-bit rationale)
//   ACCEPTED_HI = VIRTIO_F_VERSION_1_HIBIT
//     (other high-page transport extensions — ACCESS_PLATFORM,
//      RING_PACKED, IN_ORDER, ORDER_PLATFORM, SR_IOV,
//      NOTIFICATION_DATA — all explicitly rejected today)
//
// Future expansion: adding a feature to ACCEPTED requires adding
// the consuming code that USES that feature. Don't expand the
// mask speculatively — every accepted bit is a wire-protocol
// contract we have to honor.
.equ VIRTIO_REQUIRED_FEATURES_LO,  0
.equ VIRTIO_REQUIRED_FEATURES_HI,  VIRTIO_F_VERSION_1_HIBIT
.equ VIRTIO_ACCEPTED_FEATURES_LO,  0
.equ VIRTIO_ACCEPTED_FEATURES_HI,  VIRTIO_F_VERSION_1_HIBIT

// Per-queue programming macro used by VIO-007 Slice 2. Selects
// the queue, reads QueueNumMax and clamps, programs physical
// addresses for descriptor / available / used rings, explicitly
// zeroes the avail + used ring headers (belt-and-suspenders vs
// non-zero RAM; Firecracker zero-inits but spec doesn't require
// it), toggles QueueReady to 1, reads back to confirm.
//
// Cross-arch parity with x86_64's `program_queue` macro. Fail
// label is a parameter per the macro-hygiene pattern adopted
// 2026-04-22 (Gemini LOW on x86_64 program_queue's implicit
// scope coupling).
//
// Parameters (GNU as macro args; queue base + offset compose
// at the call site, x19 is the memreq virtq pin per D066 Q-B):
//   \index        queue index (0 = RX, 1 = TX)
//   \queue_off    offset within the virtq region (RX_QUEUE_OFF /
//                 TX_QUEUE_OFF — 0 and VIRTQ_STRIDE respectively)
//   \fail_label   label to branch to on QueueReady read-back mismatch
//
// x19 carries the allocator-placed VIRTQ_BASE (< 4 GiB by
// design — guest RAM ceiling is 0x88000000) so the low 32
// bits in w19 are the full address; `add w7, w19, #imm` gives
// the 32-bit MMIO value, `add x10, x19, #imm` gives the
// 64-bit pointer for the ring-header zeros.
//
// Clobbers: w5, w7, x10, flags. Preserves x0, x6, x8, x19, x30.
.macro program_queue index, queue_off, fail_label
    // Select queue \index.
    mov     w5, #\index
    dsb     sy
    str     w5, [x6, #VIRTIO_MMIO_QUEUE_SEL]
    dsb     sy

    // Read QueueNumMax, clamp to VIRTQ_MAX_SIZE.
    ldr     w7, [x6, #VIRTIO_MMIO_QUEUE_NUM_MAX]
    dsb     sy
    cmp     w7, #VIRTQ_MAX_SIZE
    b.ls    .Lpq_size_ok_\@
    mov     w7, #VIRTQ_MAX_SIZE
.Lpq_size_ok_\@:

    // Write QueueNum (clamped size).
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_QUEUE_NUM]
    dsb     sy

    // Descriptor base = x19 + queue_off + VIRTQ_OFF_DESC, low 32
    // bits to QUEUE_DESC_LOW. All offsets are < 0x6000, well
    // within imm12-lsl-12 range, so `add w, w, #imm` encodes
    // as a single instruction.
    add     w7, w19, #(\queue_off + VIRTQ_OFF_DESC)
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_QUEUE_DESC_LOW]
    dsb     sy
    str     wzr, [x6, #VIRTIO_MMIO_QUEUE_DESC_HIGH]
    dsb     sy

    // Avail ring base.
    add     w7, w19, #(\queue_off + VIRTQ_OFF_AVAIL)
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_QUEUE_DRIVER_LOW]
    dsb     sy
    str     wzr, [x6, #VIRTIO_MMIO_QUEUE_DRIVER_HIGH]
    dsb     sy

    // Used ring base.
    add     w7, w19, #(\queue_off + VIRTQ_OFF_USED)
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_QUEUE_DEVICE_LOW]
    dsb     sy
    str     wzr, [x6, #VIRTIO_MMIO_QUEUE_DEVICE_HIGH]
    dsb     sy

    // Explicit ring-header zeroing before QueueReady = 1
    // (Avail header u16 flags + u16 idx; same for Used). Use the
    // 64-bit register form (`add x10, x19, #imm`) because we
    // dereference via `str wzr, [x10]`.
    add     x10, x19, #(\queue_off + VIRTQ_OFF_AVAIL)
    str     wzr, [x10]
    add     x10, x19, #(\queue_off + VIRTQ_OFF_USED)
    str     wzr, [x10]
    dsb     sy

    // Set QueueReady = 1.
    mov     w7, #1
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_QUEUE_READY]
    dsb     sy

    // Read-back verify.
    ldr     w7, [x6, #VIRTIO_MMIO_QUEUE_READY]
    dsb     sy
    cmp     w7, #1
    b.ne    \fail_label
.endm


.section .text, "ax"

// D066 step 5 — `.memreq` records emitted by `memreq-codegen` from
// arch/aarch64/platform/firecracker/regions.yaml. The included
// file pushes `.memreq` section, emits records, and pops back to
// the previously-active section so this `.text` is restored.
.include "memreq_records.inc"

.globl _start

_start:
    // ----- Linux arm64 Image header (64 bytes) -----
    b       _entry              // code0: branch past the header to entry
    // code1: in the EFI-stub variant this is a second valid instruction;
    // Firecracker doesn't EFI-boot, so 0 is fine.
    .long   0                   // code1: reserved (non-EFI boot path)
    .quad   0x80000             // text_offset: offset from RAM base
    .quad   _image_end - _start // image_size
    .quad   0                   // flags (4K pages, little-endian)
    .quad   0, 0, 0             // reserved
    .long   0x644D5241          // magic ("ARM\x64")
    .long   0                   // reserved (PE-COFF offset, unused)

_entry:
    // Per the Linux arm64 boot protocol, all vCPUs enter here. Park any
    // secondary CPU (any non-zero affinity) at the WFE loop — the
    // primary does the UART work alone to avoid interleaved writes.
    //
    // Mask covers all four affinity fields: Aff0 (bits 7:0), Aff1
    // (bits 15:8), Aff2 (bits 23:16), and Aff3 (bits 39:32) — the
    // mask explicitly skips the MT bit (24), the U bit (30), and
    // MPIDR's hardwired-to-1 reserved bit (31), which would
    // otherwise make every core appear non-zero and prevent the
    // boot core from passing the gate. The 0xFF00FFFFFF pattern is
    // not encodable as a single AND immediate (non-contiguous
    // 1-run), so the mask is loaded via the literal pool. H3:
    // widened from the prior 24-bit Aff0..Aff2 mask to cover Aff3,
    // which ARMv8.1+ hypervisors may populate for cluster/socket
    // hierarchy. x6 is unused at this point in `_entry` and is
    // free to clobber.
    mrs     x5, mpidr_el1
    ldr     x6, =0xFF00FFFFFF
    and     x5, x5, x6
    cbnz    x5, .Ldone

    // Linux arm64 boot protocol leaves sp undefined at EL1 entry,
    // so initialize it before any function that pushes a frame.
    // emit_hex32 does `stp x29, x30, [sp, #-32]!` on its VIRTIO-
    // fail path; an uninitialized sp would fault before emitting
    // the diagnostic — exactly when it matters most.
    //
    // SP points at the bootstrap stack reserved by the linker
    // script (D059 step 4.1: __bootstrap_stack_top — 4 KiB
    // scratch, 16-byte aligned, anchored 64 KiB past _image_end).
    // The bootstrap stack is consumed by init_memory_layout's
    // single allocator pass (D060); the same stack is reused by
    // downstream code (UART writers, VIRTIO init) until step 5
    // migrates it to a .memreq lifetime=stack region. Replaces
    // the previous hardcoded 0x80200000 anchor.
    //
    // CPU0-only by construction: the MPIDR park gate above filters
    // secondary cores to WFE before this SP setup. Multi-core wake
    // (post-step-5) gives each secondary its own .memreq-assigned
    // stack from the allocator's per-core table; the bootstrap
    // stack is never shared across cores.
    ldr     x5, =__bootstrap_stack_top
    mov     sp, x5

    // UART base in x0, preserved across emit_bytes / emit_hex32
    // calls AND across init_memory_layout (which preserves its
    // callers' state by contract). Set BEFORE the first emit so
    // READY lands before the allocator pass — cross-arch parity
    // with x86_64, whose PVH [bits 32] prologue emits READY before
    // mode-switching into kernel_main_64. On aarch64 there's no
    // mode-switch constraint, so the ordering is a free choice;
    // emit READY first to keep the rule "READY is the earliest
    // reachable marker" intact across both arches.
    movz    x0, #0x4000, lsl #16
    movk    x0, #0x2000              // x0 = UART_BASE

    // Emit "READY\n" — kernel image started executing. If
    // init_memory_layout ever hangs (overflow halt today, future
    // regression could deadlock), READY's prior emission marks
    // the boundary for the harness.
    adr     x1, ready_msg
    mov     w2, #(ready_msg_end - ready_msg)
    bl      emit_bytes

    // Run the init-time static memory allocator (D060 step 4.2).
    // Pre-bootstrap-stack-safe contract: callee may use SP it was
    // handed, must not assume a kernel boot stack is in place. With
    // an empty .memreq section (today) the call returns immediately;
    // the wire-up exists so the call site stops being a no-op the
    // moment a module declares a region (step 5). Symmetric to the
    // x86_64 platforms once D062's mode switch lands.
    bl      init_memory_layout

    // Hot-tier register pins emitted by `memreq-codegen` (D066 Q-B
    // aarch64 pool: x19-x25). Loaded AFTER init_memory_layout
    // populates each region's assigned_addr. x19 = virtq base.
    .include "memreq_pin_hot.inc"

    // D066 step 5d — switch SP from the linker-reserved bootstrap
    // stack to the allocator-placed kernel stack. The bootstrap
    // stack was only needed for init_memory_layout itself
    // (REQ MR-003); kstack_boot is lifetime=stack so the allocator
    // reverse-bumped it down from __ram_top per AL-002. AArch64
    // grows down — SP = assigned_addr + assigned_size.
    //
    // PC-relative loads (adrp + add) for the .memreq-internal
    // labels per the same reason as init_memory_layout: those
    // labels carry file-offset values, not runtime addresses.
    //
    // Safe to switch here: init_memory_layout's return already
    // consumed its bootstrap-stack frame, and we've not yet made
    // any post-allocator `bl` that would expect a return address
    // on the bootstrap stack (the memreq_pin_hot.inc above is
    // pure register loads).
    adrp    x10, __memreq_assigned__kstack_boot
    add     x10, x10, :lo12:__memreq_assigned__kstack_boot
    ldp     x11, x12, [x10]                  // x11=addr, x12=size
    add     x11, x11, x12
    mov     sp, x11

    // Emit "LAYOUT-OK\n" — D060 step 4.2 success marker. Reached only
    // when init_memory_layout returned cleanly; the function halts
    // internally on overflow per its contract. Tracer-bullet harness
    // can assert on this to know the allocator pass landed.
    adr     x1, layout_ok_msg
    mov     w2, #(layout_ok_msg_end - layout_ok_msg)
    bl      emit_bytes

    // --- TX API: one-time Vyukov ring pre-init ---
    //
    // The l2_tx_pending region holds the producer-facing send queue
    // (docs/l2/TX_API.md). The Vyukov MPSC discipline requires
    // seq[i] = i at start; the allocator's zero-init leaves all
    // seq fields at 0, which is correct only for slot 0. Walk the
    // ring once here to fix the other slots. After this point any
    // caller (test scaffolding today, L3 later) can call
    // l2_tx_enqueue safely. l2_tx_pending_init clobbers x9/x10/w11,
    // none of which are live across this point.
    bl      l2_tx_pending_init

    // ============================================================
    // P4 (H5 part 2) — init / notify path barrier policy.
    //
    // The init / probe / negotiation / queue-setup / notify
    // sequence below uses `dsb sy` throughout, in contrast to the
    // hot RX/TX wait loops (which H5 narrowed to `dsb ishld`) and
    // the UART `emit_bytes` poll (which H5 narrowed to `dsb nsh`).
    //
    // The asymmetry is INTENTIONAL. Init / notify path:
    //   - runs exactly once per boot;
    //   - has ~80 `dsb sy` instances total — ~4 µs aggregate cost
    //     at 2 GHz, swamped by the surrounding MMIO write latency
    //     (each MMIO write to virtio-mmio is a VMEXIT round-trip);
    //   - the strongest barrier class is the least-surprising
    //     choice for hand-off sequences that need both completion
    //     and cross-domain visibility to the host-side virtio
    //     device thread.
    //
    // A focused audit could narrow many of these to `dsb ish` or
    // remove the redundant pre-barriers (places where the prior
    // op is a register-only computation with no memory ordering
    // implication). The wall-clock win would be in the
    // single-digit microseconds; the churn would be ~80 surgical
    // edits + per-site analysis + three-reviewer attention. Not
    // worth the risk-budget trade today. Tracked for the future
    // if init-time perf ever becomes a real constraint.
    // ============================================================

    // --- VIRTIO probe ---
    // Read the 32-bit MagicValue register at VIRTIO_MMIO_BASE and
    // compare against "virt" little-endian (0x74726976). On a match
    // we know the MMIO slot is populated by a virtio device; on a
    // mismatch either the slot is empty (returns 0 on most VMMs)
    // or at the wrong address. Either way, dump the observed value
    // alongside the marker so a reader can tell the two apart.
    //
    // x86_64 uses mfence / lfence around the MMIO read for barrier
    // envelope discipline; aarch64's equivalent is DSB SY which
    // ensures the MMIO access completes before / after adjacent
    // memory ops. Device-nGnRnE memory (if MAIR were configured to
    // mark MMIO that way under an enabled MMU) would order back-to-
    // back accesses by ISA rule alone, making most DSBs redundant.
    // This stub runs with the MMU disabled, so memory attribute is
    // the architecture default — implementation-defined per the ARM
    // ARM (D5.2 / B2.7) but on Cortex-A76 under AArch64 EL1 the
    // default is Normal Non-Cacheable Outer-Shareable. Explicit
    // DSB SY is therefore load-bearing here, not vestigial.
    movz    x6, #0x4000, lsl #16
    movk    x6, #0x3000              // x6 = VIRTIO_MMIO_BASE
    dsb     sy
    ldr     w7, [x6]                 // MagicValue register
    dsb     sy

    // Build 0x74726976 into w8 via movz+movk.
    movz    w8, #VIRTIO_MAGIC_LO
    movk    w8, #VIRTIO_MAGIC_HI, lsl #16

    cmp     w7, w8
    b.ne    .Lvirtio_fail

    adr     x1, virtio_ok_msg
    mov     w2, #(virtio_ok_msg_end - virtio_ok_msg)
    bl      emit_bytes

    // --- VIO-001..003: Device-status init prefix ---
    // Virtio 1.2 §2.1.2 steps 1-3: reset (write 0 to Status),
    // RMW ACKNOWLEDGE, RMW DRIVER. §2.1 forbids direct clearing
    // of bits ("clearing can be done only by resetting the
    // device") — so ACK/DRIVER are read-modify-write with OR,
    // not blind stores. x6 still holds VIRTIO_MMIO_BASE from
    // the probe above; emit_bytes preserves it.
    //
    // Barrier discipline mirrors the VIRTIO probe: dsb sy
    // around each MMIO register access. Same rationale as the
    // probe block above — MMU is disabled here, so the memory
    // attribute is the AArch64 default (Normal NC OS on
    // Cortex-A76 EL1) rather than Device-nGnRnE; back-to-back
    // MMIO accesses are not auto-ordered by the ISA, and we
    // make the barrier explicit. A future kernel that brings
    // up the MMU and marks this MMIO region Device-nGnRnE via
    // MAIR would render most of these DSBs redundant.

    // VIO-001: reset device — STATUS := 0.
    dsb     sy
    str     wzr, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy

    // Virtio 1.2 §4.2.4: after writing 0 to Status the driver
    // MUST wait until a read returns 0 before re-initializing.
    // Firecracker's MMIO is synchronous today so the first
    // read succeeds, but polling matches the spec and surfaces
    // if a future VMM ever goes async. Bounded iteration count
    // survives a wedged device. H4: on timeout branch to
    // .Lreset_fail with a dedicated RESET:FAIL marker so the
    // diagnostic disambiguates "device never reset" from the
    // post-ACK / FEATURES_OK / DRIVER_OK status-mismatch path
    // handled by .Lstatus_fail.
    mov     w10, #0x100000
.Lreset_wait:
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    cbz     w7, .Lreset_done
    subs    w10, w10, #1
    b.ne    .Lreset_wait
    b       .Lreset_fail
.Lreset_done:

    // VIO-002: RMW ACKNOWLEDGE.
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    orr     w7, w7, #VIRTIO_STATUS_ACKNOWLEDGE
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy

    // VIO-003: RMW DRIVER.
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    orr     w7, w7, #VIRTIO_STATUS_DRIVER
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy

    // Confirm both bits stuck and nothing else crept in. Direct
    // equality compare (not a masked test) catches the FAILED
    // bit and any stray bit the device set mid-RMW — matches
    // the x86_64 discipline.
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    cmp     w7, #(VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER)
    b.ne    .Lstatus_fail

    adr     x1, status_driver_msg
    mov     w2, #(status_driver_msg_end - status_driver_msg)
    bl      emit_bytes

    // --- Feature negotiation (VIO-004, VIO-005, VIO-006) ---
    // Virtio 1.2 §2.1.2 steps 4-6. MVP policy: accept VERSION_1
    // only (high-page bit 0); everything else stays off.
    //
    // Register allocation across this block:
    //   w9 = offered dev_lo (the low-page features the device
    //        offers). Carried across until we either emit
    //        FEATURES:OK (discarded) or FEATURES:FAIL no-v1
    //        (dumped in hex). emit_bytes preserves x9;
    //        emit_hex32 clobbers x9 but by the time we call it
    //        for dev_lo we've already copied w9 into w5 for
    //        the hex render.
    //   w7 = scratch for MMIO reads + Status compare.
    //   x6 = VIRTIO_MMIO_BASE (still held from the probe).

    // VIO-004 part 1a: read dev_lo (low-page offered features).
    dsb     sy
    str     wzr, [x6, #VIRTIO_MMIO_DEVICE_FEATURES_SEL]   // SEL = 0
    dsb     sy
    ldr     w9, [x6, #VIRTIO_MMIO_DEVICE_FEATURES]        // dev_lo → w9
    dsb     sy

    // VIO-004 part 1b: read dev_hi (high-page offered features).
    mov     w7, #1
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_DEVICE_FEATURES_SEL]    // SEL = 1
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_DEVICE_FEATURES]        // dev_hi → w7
    dsb     sy

    // Verify every REQUIRED_HI bit is offered by the device.
    // The `and w10, w7, #MASK` masks off non-required bits, then
    // `cmp w10, #MASK` confirms every required bit survived.
    // With REQUIRED_HI = VIRTIO_F_VERSION_1_HIBIT today this is
    // equivalent to the prior `tst w7, #...` / `b.eq` form;
    // generalises when REQUIRED gains bits.
    // (Low-page required check omitted today because REQUIRED_LO
    // = 0 — every bit is satisfied vacuously. Add the symmetric
    // w9-based check when REQUIRED_LO is non-zero.)
    and     w10, w7, #VIRTIO_REQUIRED_FEATURES_HI
    cmp     w10, #VIRTIO_REQUIRED_FEATURES_HI
    b.ne    .Lfeatures_fail_no_v1

    // VIO-004 part 2: compute and write driver features.
    //     driver = (offered & ACCEPTED) | REQUIRED
    // w9 = offered_lo, w7 = offered_hi.
    //
    // Use register-form `and`/`orr` rather than immediate form
    // for both pages: ARMv8 logical-immediate encoding can't
    // represent #0 (no valid bitmask), so the LO path needs
    // a register-loaded zero. Same form on HI keeps the
    // sequence symmetric and grep-friendly.
    mov     w10, #VIRTIO_ACCEPTED_FEATURES_LO
    mov     w11, #VIRTIO_REQUIRED_FEATURES_LO
    and     w9, w9, w10
    orr     w9, w9, w11
    mov     w10, #VIRTIO_ACCEPTED_FEATURES_HI
    mov     w11, #VIRTIO_REQUIRED_FEATURES_HI
    and     w7, w7, w10
    orr     w7, w7, w11

    dsb     sy
    str     wzr, [x6, #VIRTIO_MMIO_DRIVER_FEATURES_SEL]   // SEL = 0
    dsb     sy
    str     w9, [x6, #VIRTIO_MMIO_DRIVER_FEATURES]        // driver_lo
    dsb     sy
    mov     w5, #1
    str     w5, [x6, #VIRTIO_MMIO_DRIVER_FEATURES_SEL]    // SEL = 1
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_DRIVER_FEATURES]        // driver_hi
    dsb     sy

    // VIO-005: RMW FEATURES_OK into Status.
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    orr     w7, w7, #VIRTIO_STATUS_FEATURES_OK
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy

    // VIO-006: re-read Status. MUST be ACK | DRIVER | FEATURES_OK.
    // Equality compare catches FAILED and any stray bit, same
    // discipline as VIO-003's cross-check.
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    cmp     w7, #(VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK)
    b.ne    .Lfeatures_fail_rejected

    adr     x1, features_ok_msg
    mov     w2, #(features_ok_msg_end - features_ok_msg)
    bl      emit_bytes

    // --- Virtqueue discovery + init (VIO-007) ---
    // Slice 1 (discovery): QueueSel + QueueNumMax for queues
    // 0 (RX) and 1 (TX). Either queue reporting max = 0 means
    // the device doesn't support it — fatal (MVP needs both).
    // Emit QUEUES:RX=XXXXXXXX TX=XXXXXXXX once both values are
    // read; values land in w7 / w8 which both helpers preserve.
    //
    // Slice 2 (init): program_queue macro twice, one per queue.
    // Each sets QueueNum, QueueDesc / QueueDriver / QueueDevice
    // physical addresses, zeroes ring headers, toggles
    // QueueReady, reads back. On read-back mismatch the macro
    // branches to .Lqueue_ready_fail.
    //
    // Emits QUEUES:READY on success.

    // --- Discovery ---
    // Select queue 0 (RX), read QueueNumMax → w7.
    mov     w5, #0
    dsb     sy
    str     w5, [x6, #VIRTIO_MMIO_QUEUE_SEL]
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_QUEUE_NUM_MAX]
    dsb     sy
    cbz     w7, .Lqueue_fail_rx

    // Select queue 1 (TX), read QueueNumMax → w8.
    mov     w5, #1
    dsb     sy
    str     w5, [x6, #VIRTIO_MMIO_QUEUE_SEL]
    dsb     sy
    ldr     w8, [x6, #VIRTIO_MMIO_QUEUE_NUM_MAX]
    dsb     sy
    cbz     w8, .Lqueue_fail_tx

    // Emit: QUEUES:RX=XXXXXXXX TX=XXXXXXXX\n
    adr     x1, queues_rx_prefix_msg
    mov     w2, #(queues_rx_prefix_msg_end - queues_rx_prefix_msg)
    bl      emit_bytes
    mov     w5, w7                       // RX max → hex
    bl      emit_hex32
    adr     x1, queues_tx_prefix_msg
    mov     w2, #(queues_tx_prefix_msg_end - queues_tx_prefix_msg)
    bl      emit_bytes
    mov     w5, w8                       // TX max → hex
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes

    // --- Init ---
    // GNU as doesn't process backslash-newline continuation
    // across macro invocations the way NASM does, so each
    // program_queue call sits on a single line.
    // x19 (memreq virtq pin) holds VIRTQ_BASE; the macro composes
    // per-queue desc/avail/used addresses as x19-relative offsets.
    program_queue 0, RX_QUEUE_OFF, .Lqueue_ready_fail
    program_queue 1, TX_QUEUE_OFF, .Lqueue_ready_fail

    adr     x1, queues_ready_msg
    mov     w2, #(queues_ready_msg_end - queues_ready_msg)
    bl      emit_bytes

    // --- VIO-008: set DRIVER_OK ---
    // Virtio 1.2 §2.1.2 step 8 — the device becomes "live"
    // after this write. Virtqueues are consumable; the device
    // can start filling the RX used-ring as soon as the driver
    // posts buffers (VIO-R-002 in a later slice). The live
    // transition here completes init cleanly.
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    orr     w7, w7, #VIRTIO_STATUS_DRIVER_OK
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy

    // Read-back: full expected state is
    //   ACK | DRIVER | FEATURES_OK | DRIVER_OK = 0x0F.
    // Equality compare catches DRIVER_OK-didn't-stick AND
    // any stray bit (including FAILED) that crept in during
    // init. Same discipline as VIO-003 / VIO-006.
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    cmp     w7, #(VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK | VIRTIO_STATUS_DRIVER_OK)
    b.ne    .Ldriver_ok_fail

    adr     x1, driver_ok_msg
    mov     w2, #(driver_ok_msg_end - driver_ok_msg)
    bl      emit_bytes

    // --- VIO-R-002: pre-populate RX queue with WRITE-flagged buffers ---
    // Virtio 1.2 §5.1.6.3 — the driver pre-populates RX before
    // the device has anything to deposit. DRIVER_OK just
    // asserted, so the device is live and starts consuming
    // these buffers as soon as AvailRing.idx advances + the
    // notify lands.
    //
    // All 256 RX slots get filled: each descriptor points at a
    // distinct 2 KiB buffer in the pool at RX_BUFFER_BASE.
    // No descriptor chaining (VIRTIO_NET_F_MRG_RXBUF not
    // negotiated under the VIO-F-001 MVP subset — every frame
    // must fit in one descriptor).
    //
    // Order per §2.7.13 / §2.7.11:
    //   1. Fill descriptor table entries
    //   2. Fill AvailRing.ring[0..N-1] with indices 0..N-1
    //   3. dsb — driver's writes drain before idx bump
    //   4. Publish AvailRing.idx = N
    //   5. dsb — idx bump drains before notify
    //   6. Write 0 to VIRTIO_MMIO_QUEUE_NOTIFY (kick RX)
    //
    // Register use inside the loop:
    //   x11 = VIRTQ_BASE (= RX_QUEUE_BASE since RX_QUEUE_OFF = 0),
    //         copied from the x19 memreq pin into x11 so the
    //         loop body and subsequent AvailRing publish keep
    //         the existing x11 addressing.
    //   x9  = __memreq_addr__rx_buffer (cold-tier lookup,
    //         loaded once outside the loop and added per-iter)
    //   x10 = loop counter i (0..255)
    //   x12 = descriptor entry pointer
    //   x13 = buffer physical address
    //   x14, x15 = scratch
    // None live across the subsequent emit_bytes call.
    mov     x11, x19
    // Cold-tier rx_buffer lookup. PC-relative load of the
    // __memreq_addr__rx_buffer symbol (file-offset domain on
    // aarch64 per the linker-script convention), then ldr to
    // pick up the allocator-written assigned_addr value. Hoisted
    // out of the loop; per-iter cost stays one add.
    adrp    x9, __memreq_addr__rx_buffer
    add     x9, x9, :lo12:__memreq_addr__rx_buffer
    ldr     x9, [x9]
    mov     x10, xzr

.Lrx_fill_loop:
    // desc[i] at x11 + VIRTQ_OFF_DESC + 16*i (VIRTQ_OFF_DESC = 0;
    // left implicit in the lsl + add below).
    lsl     x12, x10, #4
    add     x12, x11, x12

    // buffer_i = __memreq_addr__rx_buffer + 2048*i. x9 holds the
    // pre-loaded base; x14 is the per-iter byte offset.
    lsl     x14, x10, #11
    add     x13, x9, x14

    // addr_lo + addr_hi (pool is < 4 GiB so hi = 0).
    str     w13, [x12]
    str     wzr, [x12, #4]
    // len = 2048.
    mov     w14, #RX_BUFFER_SIZE
    str     w14, [x12, #8]
    // flags = VIRTQ_DESC_F_WRITE (device may write frame bytes).
    mov     w14, #VIRTQ_DESC_F_WRITE
    strh    w14, [x12, #12]
    // next = 0 (no chain).
    strh    wzr, [x12, #14]

    // AvailRing.ring[i] = i. Slot addr = RX_QUEUE_BASE +
    // VIRTQ_OFF_AVAIL + 4 (past flags + idx header) + 2*i.
    add     x14, x11, #VIRTQ_OFF_AVAIL
    add     x14, x14, #4
    strh    w10, [x14, x10, lsl #1]

    add     x10, x10, #1
    cmp     x10, #VIRTQ_MAX_SIZE
    b.lo    .Lrx_fill_loop

    // Drain descriptor/ring stores before idx bump.
    dsb     sy

    // AvailRing.idx = 256 at offset +2 of the avail region.
    add     x14, x11, #VIRTQ_OFF_AVAIL
    mov     w15, #VIRTQ_MAX_SIZE
    strh    w15, [x14, #2]

    // Drain idx bump before notify.
    dsb     sy

    // QueueNotify(0) — kicks the RX queue. Write-only register;
    // no read-back available.
    str     wzr, [x6, #VIRTIO_MMIO_QUEUE_NOTIFY]
    dsb     sy

    adr     x1, rx_populated_msg
    mov     w2, #(rx_populated_msg_end - rx_populated_msg)
    bl      emit_bytes

    // --- FSA-4: initialise l2_state.rx_next_avail ---
    //
    // Match the AvailRing.idx just published (= VIRTQ_MAX_SIZE,
    // since we filled all 256 descriptors and advertised them).
    // aarch64 hardcodes VIRTQ_MAX_SIZE for both the AvailRing.idx
    // publish above AND for this shadow init, so the two values
    // agree by construction — x86_64 uses r13w (runtime-clamped
    // queue size) in both places for the same reason.
    //
    // The other three shadows (rx_used_shadow, tx_next_avail,
    // tx_used_shadow) read as zero on first call because
    // Firecracker pre-zeros guest RAM on VM creation. The
    // allocator pass writes assigned_addr / assigned_size into
    // each memreq record but does NOT zero the backing storage,
    // so the pre-zeroed RAM is the load-bearing precondition.
    // Same assumption the virtqueue uninhabited-at-boot reasoning
    // relies on (per the comment block in regions.yaml).
    //
    // See arch/aarch64/.../regions.yaml's l2_state entry for the
    // layout.
    mov     w9, #VIRTQ_MAX_SIZE
    adrp    x10, __memreq_addr__l2_state
    add     x10, x10, :lo12:__memreq_addr__l2_state
    ldr     x10, [x10]                       // l2_state base address
    strh    w9, [x10, #0]                    // rx_next_avail = N
    dsb     sy

    // --- D068 item 6.f: gratuitous ARP at boot ---
    //
    // Announce our MAC binding for GUEST_IP (192.168.42.2
    // wire LE = 0x022AA8C0). arp_send_request uses SPA =
    // GUEST_IP in the request body, so TPA = GUEST_IP
    // yields SPA == TPA — the gratuitous-ARP shape per
    // RFC 5227 / Linux's arp_send_request boot behaviour.
    // movz+movk rather than `ldr w0, =literal` to keep the
    // literal pool out of the picture.
    movz    w0, #0xA8C0
    movk    w0, #0x022A, lsl #16
    bl      arp_send_request

    // --- FSA-1: hand off to L2 dispatcher ---
    //
    // Everything from this point through RX consume, TX submit, and
    // TX reclaim lives in arch/aarch64/l2/dispatcher.S behind the
    // single AAPCS64 entry point `l2_dispatch`. The function returns
    //   w0 = 0  → clean halt (success path or completion timeout)
    //   w0 = 1  → set VIRTIO_STATUS_FAILED then halt (validation fail)
    //
    // Entry contract: x0 = UART, x6 = VIRTIO_MMIO base, x19 = RX
    // virtq base. All preserved across the call by the conventional
    // memreq pin discipline that emit_bytes / emit_hex32 already
    // observe; AAPCS64 callee-saved set (x19-x28) preserved by
    // l2_dispatch's prologue/epilogue.
    // FSA-4(A): loop l2_dispatch so reentrance from FSA-4(B) is
    // actually exercised. 100 iterations gives the host enough
    // time to send an ARP request (or anything else) and observe
    // the reply land via tap0. Iteration count is a soft cap; if
    // we ever need open-ended dispatch the obvious tweak is to
    // turn this into a wfi/wfe-paced loop and gate exit on an
    // explicit shutdown signal.
    //
    // x20 holds the counter across `bl l2_dispatch` because
    // AAPCS64 callee-saved set covers it and the dispatcher
    // saves/restores x20 in its prologue/epilogue.
.ifdef TXAPI_PREBAKE
    // --- TX API end-to-end test pre-bake (phase b.4) ---
    //
    // Build-flag-gated: --defsym TXAPI_PREBAKE=1 turns this on.
    // OFF for production builds (Pi deploys, normal CD). ON for
    // the tooling/tests/integration/test_l2_tx_api.py aarch64
    // sibling test which sniffs the Pi's tap interface for the
    // resulting wire frame.
    //
    // Allocates one pool buffer, writes the ASCII payload
    // "TXAPI-TEST" at +PAYLOAD_OFFSET, enqueues one request
    // targeting broadcast MAC with the Local Experimental
    // ethertype 0x88B5. Dispatcher's TX phase drains it on the
    // first .Ldispatch_loop iteration and submits the wire
    // frame to virtio-net.
    //
    // x19 (VIRTQ_BASE pin), x6 (VIRTIO_MMIO base pin), and
    // x0 (UART base pin) must survive across the calls below.
    // Per AAPCS64, those are caller-saved EXCEPT x19 which is
    // callee-saved — the TX API functions don't reuse it, so
    // it's preserved automatically. x0/x6 are caller-saved and
    // ARE clobbered; re-derive after the calls.
    adr     x1, txapi_prebake_msg
    mov     w2, #(txapi_prebake_msg_end - txapi_prebake_msg)
    bl      emit_bytes
    bl      l2_tx_buffer_pool_alloc          // w0 = pool_idx
    // Save idx + restore UART base for later emit calls.
    mov     w21, w0                           // x21 = pool_idx (callee-saved)
    bl      l2_tx_buffer_addr                 // x0 = pool buffer base
    add     x0, x0, #L2_TX_PAYLOAD_OFFSET    // x0 = producer payload slot
    // ASCII bytes "TXAPI-TEST" — 10 bytes.
    // As LE 64-bit at +0: bytes 0..7 = T X A P I - T E
    //   = 0x4554_2D49_5041_5854
    ldr     x1, =0x45542D4950415854
    str     x1, [x0]
    mov     w1, #0x5453                       // bytes 8-9 = 'S' 'T'
    strh    w1, [x0, #8]
    // Re-derive UART base (x0 was clobbered above).
    movz    x0, #0x4000, lsl #16
    movk    x0, #0x2000              // x0 = UART_BASE
    // Enqueue: dst_mac_ptr, ethertype, pool_idx, payload_len.
    adr     x0, txapi_prebake_dst_mac         // tx_api uses x0 for dst ptr
    mov     w1, #0xB588                       // BE wire 0x88B5 (LE stored)
    mov     w2, w21                           // pool_idx
    mov     w3, #10                           // payload_len
    bl      l2_tx_enqueue

    // --- Phase 6.c: also exercise the ARP initiator. ---
    // Broadcast ARP request for HOST_DEFAULT_IP (192.168.42.1,
    // wire bytes c0 a8 2a 01 → LE u32 = 0x012AA8C0). Test at
    // tooling/tests/integration/test_arp_initiator.py asserts
    // the corresponding marker + tap0 capture.
    ldr     w0, =0x012AA8C0
    bl      arp_send_request

    // --- Phase 6.d: exercise arp_resolve (cache + initiator). ---
    // Different target IP from 6.c (192.168.42.99 → wire LE
    // 0x632AA8C0). Three steps: MISS-path resolve, direct
    // cache insert, HIT-path resolve. Test asserts
    // ARP:RESOLVE_PENDING then ARP:RESOLVE_OK in order.
    sub     sp, sp, #16                     // scratch for out-buf
    ldr     w0, =0x632AA8C0
    mov     x1, sp
    bl      arp_resolve                     // expect PENDING
    ldr     w0, =0x632AA8C0
    adr     x1, arp_resolve_test_fake_mac
    mov     w2, #2                          // ARP_STATE_REACHABLE
    bl      arp_cache_insert
    ldr     w0, =0x632AA8C0
    mov     x1, sp
    bl      arp_resolve                     // expect OK
    add     sp, sp, #16

    // --- Phase 6.e.2: INCOMPLETE entry that nobody replies to. ---
    // 192.168.42.100 (wire LE 0x642AA8C0). tap0's host won't
    // reply; tick eventually transitions INCOMPLETE → FAILED.
    sub     sp, sp, #16
    ldr     w0, =0x642AA8C0
    mov     x1, sp
    bl      arp_resolve                     // expect PENDING
    add     sp, sp, #16

    // Re-derive UART base for the dispatch loop's emit chains
    // (dispatcher caller-contract: x0 = UART base).
    movz    x0, #0x4000, lsl #16
    movk    x0, #0x2000              // x0 = UART_BASE
.endif
    // Dispatch loop iteration cap. Was 100 (Pi flake era).
    // Bumped to 200 to give the 6.e state machine's
    // per-entry transition chains enough runway.
    mov     x20, #200
.Ldispatch_loop:
    bl      l2_dispatch
    cbnz    w0, .Lset_failed
    // D068 item 6.e: drive ARP cache timer transitions.
    // Once per dispatcher iteration. Tick uses its own
    // stack frame + AAPCS64-callee-saved regs internally
    // (x19/x20/x21); the boot loop's x20 counter is
    // preserved across the call because tick's prologue
    // saves it.
    bl      arp_cache_tick
    subs    x20, x20, #1
    b.ne    .Ldispatch_loop
    b       .Lflush

.Ldriver_ok_fail:
    // w7 holds the observed Status. Emit DRIVER_OK:FAIL
    // status=<hex> and halt through the VIO-009 FAILED path.
    adr     x1, driver_ok_fail_msg
    mov     w2, #(driver_ok_fail_msg_end - driver_ok_fail_msg)
    bl      emit_bytes
    mov     w5, w7
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lqueue_fail_rx:
    adr     x1, queues_fail_rx_msg
    mov     w2, #(queues_fail_rx_msg_end - queues_fail_rx_msg)
    bl      emit_bytes
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lqueue_fail_tx:
    adr     x1, queues_fail_tx_msg
    mov     w2, #(queues_fail_tx_msg_end - queues_fail_tx_msg)
    bl      emit_bytes
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lqueue_ready_fail:
    adr     x1, queues_ready_fail_msg
    mov     w2, #(queues_ready_fail_msg_end - queues_ready_fail_msg)
    bl      emit_bytes
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lfeatures_fail_no_v1:
    // Device did not offer VIRTIO_F_VERSION_1. Dump both feature
    // pages for post-mortem. w9 = dev_lo, w7 = dev_hi.
    // Both emit_bytes calls preserve w7; first emit_hex32 call
    // clobbers x9, but by then we've already copied w9 → w5.
    adr     x1, features_no_v1_prefix_msg
    mov     w2, #(features_no_v1_prefix_msg_end - features_no_v1_prefix_msg)
    bl      emit_bytes
    mov     w5, w9                      // dev_lo → hex
    bl      emit_hex32
    adr     x1, features_hi_sep_msg
    mov     w2, #(features_hi_sep_msg_end - features_hi_sep_msg)
    bl      emit_bytes
    mov     w5, w7                      // dev_hi → hex
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lfeatures_fail_rejected:
    // Device cleared FEATURES_OK after we set it, OR set an
    // unexpected bit. w7 holds the observed status value.
    adr     x1, features_rejected_msg
    mov     w2, #(features_rejected_msg_end - features_rejected_msg)
    bl      emit_bytes
    mov     w5, w7                      // status → hex, post-emit_bytes
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lstatus_fail:
    // Emit: STATUS:FAIL status=XXXXXXXX\n. w7 holds the observed
    // Status. emit_bytes preserves w7; then render via emit_hex32.
    adr     x1, status_fail_msg
    mov     w2, #(status_fail_msg_end - status_fail_msg)
    bl      emit_bytes
    mov     w5, w7
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lreset_fail:
    // H4: bounded reset wait timed out. w7 holds the last observed
    // non-zero status value. Distinct marker so the diagnostic
    // disambiguates "device never reset" from the post-RMW status-
    // mismatch path handled by .Lstatus_fail above.
    adr     x1, reset_fail_msg
    mov     w2, #(reset_fail_msg_end - reset_fail_msg)
    bl      emit_bytes
    mov     w5, w7
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lset_failed

.Lvirtio_fail:
    // Emit "VIRTIO:FAIL magic=XXXXXXXX\n".
    // Falls through to .Lflush deliberately — a MagicValue
    // mismatch leaves the device's register layout unverified,
    // so a VIRTIO_STATUS_FAILED write through .Lset_failed
    // would be blind. The explicit `b .Lflush` below is load-
    // bearing because subsequent fail-path labels were inserted
    // between this point and .Lflush; without it the path would
    // fall into .Lrx_bad_id_fail, emit spurious "RX:FAIL bad_id="
    // with whatever happened to be in w20, then hit .Lset_failed
    // via the wrong door.
    adr     x1, virtio_fail_msg
    mov     w2, #(virtio_fail_msg_end - virtio_fail_msg)
    bl      emit_bytes
    mov     w5, w7                   // observed magic into w5 for emit_hex32
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes
    b       .Lflush

.Lflush:
    dsb     sy
    b       .Ldone

// VIO-009: signal the device that the driver has given up.
// Any fatal init error downstream of the VIRTIO probe routes
// here before halt. RMW preserves whatever Status bits are
// already set (ACK / DRIVER / FEATURES_OK in various
// combinations depending on which stage failed). Only entered
// after the probe matched — .Lvirtio_fail skips this step
// because a MagicValue mismatch leaves the device's register
// layout unverified and a Status write would be blind.
.Lset_failed:
    dsb     sy
    ldr     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    orr     w7, w7, #VIRTIO_STATUS_FAILED
    dsb     sy
    str     w7, [x6, #VIRTIO_MMIO_STATUS]
    dsb     sy
    // fall through to .Ldone

.Ldone:
    // WFE forever — Firecracker sees halt via MMIO quiescence.
    //
    // Placeholder for secondary_entry (post-step-5). Today: every
    // secondary core lands here from the MPIDR park gate at _entry
    // and stays in WFE until power-off. Post-step-5: this becomes
    // the PSCI_CPU_ON target — wakes here, reads MPIDR, indexes
    // into the allocator's per-core stack-base table, sets SP to
    // its own dedicated stack, then enters the per-core actor loop.
    wfe
    b       .Ldone


// ================================================================
// Helpers — UART writers
// ================================================================

// `yield` policy in busy-wait loops (RX wait, TX wait, emit_bytes).
//
// `yield` is the AArch64 SMT-hint instruction. On Cortex-A76 (no
// SMT) it's architecturally a NOP — but under KVM it can trigger
// guest-vCPU descheduling by the host scheduler, giving the host
// time to make progress on other work.
//
// Task #15 (2026-05-20) found that on Pi 5 / Firecracker / KVM,
// the yield in the RX wait loop is LOAD-BEARING beyond mere
// politeness: it provides timing slack that lets the host-side
// virtio device thread finish writing the buffer header BEFORE
// the guest's next ldrh observes UsedRing.idx and starts reading
// the buffer. Removing yield surfaces a producer-consumer race
// where the guest catches the device mid-write and reads garbage
// (`RX:FAIL num_bufs=00000000`).
//
// The dsb-ishld barriers narrowed in H5 order LOAD operations
// against subsequent loads on this PE; they don't make the host's
// remote stores propagate any faster. The yield's KVM-side
// descheduling effect IS what gives the producer-consumer
// pipeline time to drain.
//
// Keep all three yields. The no-traffic wall-clock optimization
// happens at the iteration-count level (POLL_BUDGET) instead.

// Cross-file symbols consumed by arch/aarch64/l2/dispatcher.S:
//   emit_bytes, emit_hex32 — UART write helpers
//   newline_msg            — single-byte "\n" string
//   tx_test_pkt            — canary TX frame (72 bytes)
.global emit_bytes
.global emit_hex32
.global newline_msg
.global tx_test_pkt

// emit_bytes: write w2 bytes from [x1] via UART.
//   In:      x1 = source pointer (advances by w2)
//            w2 = byte count (decremented to 0)
//   Clobbers w3, w4, w2, x1, x0 (rewritten to UART_BASE at
//            entry; preserved on exit since the function uses
//            it throughout, but callers should NOT depend on
//            x0 carrying any specific value across this call).
//   Preserves x5, x6, x7, x8, x30.
//
// History (2026-05-24): emit_bytes used to REQUIRE x0 =
// UART_BASE on entry AND was documented as preserving x0
// across the call. That turned x0 into a project-wide "do
// not clobber across emit_bytes" register and produced a
// real bug in the failpath stub — the aarch64 dispatcher's
// .Lfail path does `mov w0, #1` which zero-extends to x0 and
// clobbered UART_BASE for the post-dispatch emit chain.
// Symmetric with task #26's x86 COM1_PORT-into-emit fix.
// emit_bytes now derives UART_BASE from the assemble-time
// constant on every entry; callers no longer need to set or
// preserve x0.
emit_bytes:
    ldr     x0, =UART_BASE
    cbz     w2, .Lemit_done
.Lemit_loop:
    yield                                // see header comment block
    ldrb    w3, [x0, #0x05]             // LSR — THRE status
    dsb     nsh                          // H5: narrowed from dsb sy →
                                         // dsb nsh. UART MMIO is Device
                                         // memory; ARM ARM B2.3.7
                                         // disqualifies inner/outer-
                                         // shareable Normal-memory dmb
                                         // for Device-access ordering,
                                         // so the original dsb's
                                         // completion-wait stays.
                                         // Domain narrows from sy
                                         // (full system) to nsh (local
                                         // PE), which is the right
                                         // coverage for "this CPU's
                                         // UART accesses sequence with
                                         // each other" and removes the
                                         // cross-cluster-coordination
                                         // cost.
    tbz     w3, #5, .Lemit_loop
    ldrb    w4, [x1], #1
    strb    w4, [x0]                     // THR — byte out
    dsb     nsh                          // H5: same — THR write orders
                                         // against next LSR read.
    subs    w2, w2, #1
    b.ne    .Lemit_loop
.Lemit_done:
    ret

// emit_hex32: write w5 as 8 uppercase hex digits via UART at x0.
//   In:      x0 = UART base (preserved)
//            w5 = 32-bit value to render
//   Uses 8 bytes of stack scratch. Calls emit_bytes; preserves
//   x0 / x6 / w7 / x8 across the call. Clobbers x1, w2, w3, w4,
//   w5, x9 (buffer-start scratch), w10 (nibble scratch). Stack
//   frame balanced: 32 bytes pushed in prologue, popped in
//   epilogue. Caller MUST ensure sp is valid before `bl emit_hex32`.
//
// Nibble scratch is w10 (not w7) — _entry reserves w7 for the
// observed-magic value across the VIRTIO probe / fail path, and
// keeping the scratch out of w7 makes that invariant stable
// against future reorders. w10 is caller-saved per AAPCS64 and
// not live across this function's boundary.
emit_hex32:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    // Stack scratch: sp+16..sp+24.
    add     x1, sp, #16
    mov     x9, x1                   // keep buffer start
    mov     w2, #8
.Lhex_digit:
    lsr     w10, w5, #28              // top nibble
    and     w10, w10, #0xF
    cmp     w10, #10
    b.lo    .Lhex_num
    add     w10, w10, #('A' - '0' - 10)
.Lhex_num:
    add     w10, w10, #'0'
    strb    w10, [x1], #1
    lsl     w5, w5, #4
    subs    w2, w2, #1
    b.ne    .Lhex_digit

    mov     x1, x9                   // buffer start for emit_bytes
    mov     w2, #8
    bl      emit_bytes

    ldp     x29, x30, [sp], #32
    ret

.section .rodata, "a"
// Kept out of .text so a branch-coverage disassembler doesn't have to
// decode the string bytes as instructions. The stub's `adr` loads the
// address regardless of section; the linker resolves across sections.
layout_ok_msg:
    .ascii  "LAYOUT-OK\n"
layout_ok_msg_end:

ready_msg:
    .ascii  "READY\n"
ready_msg_end:

virtio_ok_msg:
    .ascii  "VIRTIO:OK\n"
virtio_ok_msg_end:

virtio_fail_msg:
    .ascii  "VIRTIO:FAIL magic="
virtio_fail_msg_end:

status_driver_msg:
    .ascii  "STATUS:DRIVER\n"
status_driver_msg_end:

status_fail_msg:
    .ascii  "STATUS:FAIL status="
status_fail_msg_end:

reset_fail_msg:
    .ascii  "RESET:FAIL status="
reset_fail_msg_end:

features_ok_msg:
    .ascii  "FEATURES:OK\n"
features_ok_msg_end:

features_no_v1_prefix_msg:
    .ascii  "FEATURES:FAIL no-v1 dev_lo="
features_no_v1_prefix_msg_end:

features_hi_sep_msg:
    .ascii  " dev_hi="
features_hi_sep_msg_end:

features_rejected_msg:
    .ascii  "FEATURES:FAIL rejected status="
features_rejected_msg_end:

queues_rx_prefix_msg:
    .ascii  "QUEUES:RX="
queues_rx_prefix_msg_end:
queues_tx_prefix_msg:
    .ascii  " TX="
queues_tx_prefix_msg_end:
queues_ready_msg:
    .ascii  "QUEUES:READY\n"
queues_ready_msg_end:
queues_fail_rx_msg:
    .ascii  "QUEUES:FAIL no-queue q=0 (max=0)"
queues_fail_rx_msg_end:
queues_fail_tx_msg:
    .ascii  "QUEUES:FAIL no-queue q=1 (max=0)"
queues_fail_tx_msg_end:
queues_ready_fail_msg:
    .ascii  "QUEUES:FAIL queue-not-ready"
queues_ready_fail_msg_end:

driver_ok_msg:
    .ascii  "DRIVER_OK\n"
driver_ok_msg_end:
driver_ok_fail_msg:
    .ascii  "DRIVER_OK:FAIL status="
driver_ok_fail_msg_end:

rx_populated_msg:
    .ascii  "RX:POPULATED\n"
rx_populated_msg_end:
newline_msg:
    .ascii  "\n"
newline_msg_end:

// tx_test_pkt's src MAC must stay in sync with the GUEST_MAC
// constants in arch/aarch64/l2/dispatcher.S (which the MAC
// filter compares incoming dst MACs against, and which the
// dispatcher writes as SHA into ARP reply payloads enqueued
// via the TX API). Each file asserts ETH-016 locally;
// cross-file consistency is human discipline.
.equ GUEST_MAC_BYTE_0,          0x02
.equ GUEST_MAC_BYTE_1,          0x00
.equ GUEST_MAC_BYTE_2,          0x00
.equ GUEST_MAC_BYTE_3,          0x00
.equ GUEST_MAC_BYTE_4,          0x00
.equ GUEST_MAC_BYTE_5,          0x01

// ETH-016 (IEEE 802.3 §4.1.2.1): outgoing source MAC MUST
// have the unicast bit clear. tx_test_pkt's src MAC is the
// only outgoing src this file places; the dispatcher's
// ARP reply builder writes its own (asserted there too).
.if GUEST_MAC_BYTE_0 & 1
.error "ETH-016: GUEST_MAC_BYTE_0 has multicast bit set"
.endif

// Canary Ethernet frame submitted on TX for VIO-T MVP. Layout:
//   12 B virtio_net_hdr (all zero — no GSO/CSUM features negotiated)
//   14 B Ethernet header (broadcast dst, local-admin src, EtherType
//        0x88B5 = IEEE 802.1 local experimental EthType #1)
//   46 B 0xAB canary payload (pads to the 60-byte Ethernet minimum)
// Total wire bytes seen by virtio: 72. Frame matches x86_64
// tx_test_pkt at arch/x86_64/.../boot.S:1178-1184.
tx_test_pkt:
    .byte   0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0          // virtio_net_hdr
    .byte   0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF          // dst MAC broadcast
    .byte   GUEST_MAC_BYTE_0, GUEST_MAC_BYTE_1, GUEST_MAC_BYTE_2, GUEST_MAC_BYTE_3, GUEST_MAC_BYTE_4, GUEST_MAC_BYTE_5  // src MAC = GUEST_MAC
    .byte   0x88, 0xB5                                  // EtherType 0x88B5
    .fill   46, 1, 0xAB                                 // 46-byte canary

.ifdef TXAPI_PREBAKE
// TX API end-to-end test pre-bake rodata. dst MAC pointer for
// l2_tx_enqueue's first arg; marker string for the emit before
// the alloc/enqueue calls so the test can verify pre-bake ran.
txapi_prebake_dst_mac:
    .byte   0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
txapi_prebake_msg:
    .ascii  "TXAPI:PREBAKE\n"
txapi_prebake_msg_end:
// Fake MAC for the 6.d arp_resolve cache-HIT test. Distinctive
// value to make a bad-copy bug visually obvious.
arp_resolve_test_fake_mac:
    .byte   0x02, 0xDE, 0xAD, 0xBE, 0xEF, 0x99
.endif

// _image_end is defined in the linker script (placed after both .text
// and .rodata) so the Image header's image_size covers the entire file.
