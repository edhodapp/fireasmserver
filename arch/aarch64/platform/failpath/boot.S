// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// aarch64 fail-path test guest — synthetic-stimulus stub
// (cross-arch mirror of arch/x86_64/platform/failpath/boot.S).
//
// Boots as a Firecracker arm64 guest like the production
// firecracker target, runs the same allocator, links the
// production dispatcher.o unchanged — but SKIPS real virtio
// init. Pre-populates virtqueue memory (and the RX buffer at
// id=0 for NUM_BUFS / TX_BAD_ID) with malformed contents,
// then calls l2_dispatch and asserts via marker that the
// dispatcher's defensive validation fired.
//
// Scenarios (mirror x86_64):
//   BAD_ID    — RX UsedRing.ring[0].id = 0x100, fail marker
//               "RX:FAIL bad_id=00000100".
//   NUM_BUFS  — RX buffer's virtio_net_hdr.num_buffers = 2
//               (valid dst/src/EtherType so the dispatcher
//                reaches the num_bufs check), fail marker
//               "RX:FAIL num_bufs=00000002".
//   TX_BAD_ID — valid RX → RX:FRAME → TX:SUBMITTED, then TX
//               UsedRing.ring[0].id = 0x100 trips
//               "TX:FAIL bad_id=00000100".
//
// Same duplication trade-off as x86_64: the Linux Image
// header, MPIDR park gate, init_memory_layout call, emit
// helpers, and tx_test_pkt rodata are duplicated from
// arch/aarch64/platform/firecracker/boot.S rather than
// refactored into a shared file. Diff-minimal for a test
// stub; refactor if the stub grows.

// --- Sanity: exactly one scenario must be defined ---
//
// GNU as doesn't support arbitrary preprocessor arithmetic
// (CLAUDE.md gotcha), but for this counter we can chain
// `.ifdef` blocks that each redefine a single .equ value to
// `.equ + 1`. Equivalent shape to the NASM stub's %assign
// chain.
.equ FAILPATH_SCENARIO_COUNT, 0
.ifdef FAILPATH_SCENARIO_BAD_ID
    .equ FAILPATH_SCENARIO_COUNT, (FAILPATH_SCENARIO_COUNT + 1)
.endif
.ifdef FAILPATH_SCENARIO_NUM_BUFS
    .equ FAILPATH_SCENARIO_COUNT, (FAILPATH_SCENARIO_COUNT + 1)
.endif
.ifdef FAILPATH_SCENARIO_TX_BAD_ID
    .equ FAILPATH_SCENARIO_COUNT, (FAILPATH_SCENARIO_COUNT + 1)
.endif
.if FAILPATH_SCENARIO_COUNT != 1
    .error "Exactly one of FAILPATH_SCENARIO_{BAD_ID,NUM_BUFS,TX_BAD_ID} must be defined via --defsym"
.endif

// ----- Constants (duplicated from production firecracker boot.S) -----

.equ UART_BASE,         0x40002000
.equ VIRTIO_MMIO_BASE,  0x40003000

.equ VIRTQ_STRIDE,      0x3000
.equ VIRTQ_OFF_DESC,    0x0000
.equ VIRTQ_OFF_AVAIL,   0x1000
.equ VIRTQ_OFF_USED,    0x2000
.equ TX_QUEUE_OFF,      1 * VIRTQ_STRIDE
.equ VIRTQ_MAX_SIZE,    0x100

// Malformed values the scenarios inject.
.equ SCENARIO_BAD_ID_VALUE,   0x100
.equ SCENARIO_NUM_BUFS_VALUE, 2

// GUEST_MAC bytes — duplicated from arch/aarch64/l2/dispatcher.S
// (FP_ prefix to keep the failpath stub's locals distinct from
// any global GUEST_MAC_BYTE_N the dispatcher may export).
.equ FP_GUEST_MAC_BYTE_0, 0x02
.equ FP_GUEST_MAC_BYTE_1, 0x00
.equ FP_GUEST_MAC_BYTE_2, 0x00
.equ FP_GUEST_MAC_BYTE_3, 0x00
.equ FP_GUEST_MAC_BYTE_4, 0x00
.equ FP_GUEST_MAC_BYTE_5, 0x01

.section .text, "ax"

// D066 step 5 — `.memreq` records emitted by `memreq-codegen` from
// arch/aarch64/platform/failpath/regions.yaml (a copy of
// firecracker's regions.yaml). The included file pushes `.memreq`
// section, emits records, and pops back to the previously-active
// section so this `.text` is restored.
.include "memreq_records.inc"

.globl _start

_start:
    // ----- Linux arm64 Image header (64 bytes) -----
    b       _entry
    .long   0
    .quad   0x80000              // text_offset
    .quad   _image_end - _start  // image_size
    .quad   0                    // flags
    .quad   0, 0, 0              // reserved
    .long   0x644D5241           // magic "ARM\x64"
    .long   0                    // reserved

_entry:
    // MPIDR park: only the primary core (Aff0..Aff3 == 0) runs
    // the failpath stub; secondaries WFE.
    mrs     x5, mpidr_el1
    ldr     x6, =0xFF00FFFFFF
    and     x5, x5, x6
    cbnz    x5, .Lhalt

    // Bootstrap stack for init_memory_layout — same shape as
    // production.
    ldr     x5, =__bootstrap_stack_top
    mov     sp, x5

    // UART base in x0, preserved across emit_bytes / emit_hex32
    // calls AND across init_memory_layout. Set before any emit
    // so the FAILPATH:READY (and the dispatcher's later
    // markers) reach the harness.
    movz    x0, #0x4000, lsl #16
    movk    x0, #0x2000              // x0 = UART_BASE

    adr     x1, failpath_ready_msg
    mov     w2, #(failpath_ready_msg_end - failpath_ready_msg)
    bl      emit_bytes

    bl      init_memory_layout

    // Hot-tier register pins from memreq codegen — x19 = VIRTQ_BASE.
    .include "memreq_pin_hot.inc"

    // Switch SP from the bootstrap stack to the allocator-placed
    // kernel stack (same as production firecracker boot).
    adrp    x5, __memreq_addr__kstack_boot
    add     x5, x5, :lo12:__memreq_addr__kstack_boot
    ldr     x6, [x5]
    adrp    x5, __memreq_size__kstack_boot
    add     x5, x5, :lo12:__memreq_size__kstack_boot
    ldr     x7, [x5]
    add     x6, x6, x7
    mov     sp, x6

    adr     x1, failpath_boot_msg
    mov     w2, #(failpath_boot_msg_end - failpath_boot_msg)
    bl      emit_bytes

    // --- Scenario-specific virtqueue / buffer pre-population ---
    //
    // x19 holds VIRTQ_BASE (RX queue base) per memreq_pin_hot.inc.

.ifdef FAILPATH_SCENARIO_BAD_ID
    // RX UsedRing.idx = 1, ring[0].id = SCENARIO_BAD_ID_VALUE.
    // l2_state shadows are zero-init; wait loop sees idx=1 vs
    // shadow=0 → enters consume body → id check fires.
    //
    // Virtio used-ring layout:
    //   +0  u16 flags  (leave 0)
    //   +2  u16 idx    (set 1)
    //   +4  u32 ring[0].id  (set SCENARIO_BAD_ID_VALUE)
    //   +8  u32 ring[0].len (set 100 — arbitrary, unread on fail)
    mov     w9, #1
    add     x10, x19, #VIRTQ_OFF_USED
    strh    w9, [x10, #2]
    ldr     w9, =SCENARIO_BAD_ID_VALUE
    str     w9, [x10, #4]
    mov     w9, #100
    str     w9, [x10, #8]
.endif

.ifdef FAILPATH_SCENARIO_NUM_BUFS
    // RX UsedRing.idx = 1, ring[0] = {id=0, len=100}. Then
    // populate rx_buffer[0] with virtio_net_hdr where
    // num_buffers (offset 10) = 2, plus a valid Ethernet header
    // so the frame passes dst MAC + src MAC + PAUSE gates.
    mov     w9, #1
    add     x10, x19, #VIRTQ_OFF_USED
    strh    w9, [x10, #2]
    str     wzr, [x10, #4]           // id = 0
    mov     w9, #100
    str     w9, [x10, #8]            // len = 100

    // Get rx_buffer base.
    adrp    x10, __memreq_addr__rx_buffer
    add     x10, x10, :lo12:__memreq_addr__rx_buffer
    ldr     x10, [x10]               // x10 = rx_buffer base

    // Zero first 32 bytes for cleanliness.
    stp     xzr, xzr, [x10, #0]
    stp     xzr, xzr, [x10, #16]

    // virtio_net_hdr.num_buffers at offset 10.
    mov     w9, #SCENARIO_NUM_BUFS_VALUE
    strh    w9, [x10, #10]

    // Ethernet dst MAC = GUEST_MAC (passes MAC filter).
    mov     w9, #FP_GUEST_MAC_BYTE_0
    strb    w9, [x10, #12]
    mov     w9, #FP_GUEST_MAC_BYTE_1
    strb    w9, [x10, #13]
    mov     w9, #FP_GUEST_MAC_BYTE_2
    strb    w9, [x10, #14]
    mov     w9, #FP_GUEST_MAC_BYTE_3
    strb    w9, [x10, #15]
    mov     w9, #FP_GUEST_MAC_BYTE_4
    strb    w9, [x10, #16]
    mov     w9, #FP_GUEST_MAC_BYTE_5
    strb    w9, [x10, #17]

    // Ethernet src MAC = 02:00:00:00:00:42 (unicast — passes
    // ETH-015's bit-0 check).
    mov     w9, #0x02
    strb    w9, [x10, #18]
    strb    wzr, [x10, #19]
    strb    wzr, [x10, #20]
    strb    wzr, [x10, #21]
    strb    wzr, [x10, #22]
    mov     w9, #0x42
    strb    w9, [x10, #23]

    // EtherType 0x88B5 BE (local-experimental — non-PAUSE).
    mov     w9, #0x88
    strb    w9, [x10, #24]
    mov     w9, #0xB5
    strb    w9, [x10, #25]
.endif

.ifdef FAILPATH_SCENARIO_TX_BAD_ID
    // Valid RX (num_buffers = 1, valid dst/src/EtherType) so
    // the dispatcher completes RX → TX phase. TX UsedRing
    // pre-populated with bad id so TX wait loop terminates
    // immediately and reclaim body reads the bad id.

    mov     w9, #1
    add     x10, x19, #VIRTQ_OFF_USED
    strh    w9, [x10, #2]
    str     wzr, [x10, #4]           // id = 0
    mov     w9, #100
    str     w9, [x10, #8]            // len = 100

    adrp    x10, __memreq_addr__rx_buffer
    add     x10, x10, :lo12:__memreq_addr__rx_buffer
    ldr     x10, [x10]

    stp     xzr, xzr, [x10, #0]
    stp     xzr, xzr, [x10, #16]

    mov     w9, #1
    strh    w9, [x10, #10]           // num_buffers = 1 ✓

    mov     w9, #FP_GUEST_MAC_BYTE_0
    strb    w9, [x10, #12]
    mov     w9, #FP_GUEST_MAC_BYTE_1
    strb    w9, [x10, #13]
    mov     w9, #FP_GUEST_MAC_BYTE_2
    strb    w9, [x10, #14]
    mov     w9, #FP_GUEST_MAC_BYTE_3
    strb    w9, [x10, #15]
    mov     w9, #FP_GUEST_MAC_BYTE_4
    strb    w9, [x10, #16]
    mov     w9, #FP_GUEST_MAC_BYTE_5
    strb    w9, [x10, #17]

    mov     w9, #0x02
    strb    w9, [x10, #18]
    strb    wzr, [x10, #19]
    strb    wzr, [x10, #20]
    strb    wzr, [x10, #21]
    strb    wzr, [x10, #22]
    mov     w9, #0x42
    strb    w9, [x10, #23]

    mov     w9, #0x88
    strb    w9, [x10, #24]
    mov     w9, #0xB5
    strb    w9, [x10, #25]

    // TX UsedRing.idx = 1, ring[0].id = bad value.
    mov     w9, #1
    add     x10, x19, #(TX_QUEUE_OFF + VIRTQ_OFF_USED)
    strh    w9, [x10, #2]
    ldr     w9, =SCENARIO_BAD_ID_VALUE
    str     w9, [x10, #4]
    mov     w9, #72
    str     w9, [x10, #8]
.endif

    dsb     sy

    // --- Call the production dispatcher ---
    //
    // Dispatcher contract (per arch/aarch64/l2/dispatcher.S):
    //   x0  = UART base (already set above)
    //   x6  = VIRTIO_MMIO base
    //   x19 = RX virtq base (already pinned via memreq)
    // All other registers free; dispatcher saves the AAPCS64
    // callee-saved set in its prologue.
    movz    x6, #0x4000, lsl #16
    movk    x6, #0x3000              // x6 = VIRTIO_MMIO_BASE

    bl      l2_dispatch

    // --- Emit return code and halt ---
    //
    // l2_dispatch returns 1 on the fail paths via `mov w0, #1;
    // b .Lepilogue`. That `mov w0` clobbers x0 (the dispatcher's
    // upper 32 bits get zeroed by the `mov w0`, so x0 = 1 now,
    // not UART_BASE). Our emit_bytes uses x0 as the UART base
    // address — so we MUST re-establish it before the next
    // emit. x86_64 doesn't have this gotcha because its
    // emit_bytes self-loads COM1_PORT internally per task #26;
    // aarch64's emit_bytes still trusts a caller-supplied
    // pointer (refactoring it to self-load UART_BASE is a
    // separate cleanup).
    mov     w20, w0
    movz    x0, #0x4000, lsl #16
    movk    x0, #0x2000              // x0 = UART_BASE
    adr     x1, failpath_done_msg
    mov     w2, #(failpath_done_msg_end - failpath_done_msg)
    bl      emit_bytes
    mov     w5, w20
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #(newline_msg_end - newline_msg)
    bl      emit_bytes

.Lhalt:
    wfe
    b       .Lhalt

// ================================================================
// Helpers — UART writers (duplicated from production firecracker
// boot.S; see this file's header for the duplication rationale)
// ================================================================

.global emit_bytes
.global emit_hex32
.global newline_msg
.global tx_test_pkt

emit_bytes:
    cbz     w2, .Lfp_emit_done
.Lfp_emit_loop:
    yield
    ldrb    w3, [x0, #0x05]
    dsb     nsh
    tbz     w3, #5, .Lfp_emit_loop
    ldrb    w4, [x1], #1
    strb    w4, [x0]
    dsb     nsh
    subs    w2, w2, #1
    b.ne    .Lfp_emit_loop
.Lfp_emit_done:
    ret

emit_hex32:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    add     x1, sp, #16
    mov     x9, x1
    mov     w2, #8
.Lfp_hex_digit:
    lsr     w10, w5, #28
    and     w10, w10, #0xF
    cmp     w10, #10
    b.lo    .Lfp_hex_num
    add     w10, w10, #('A' - '0' - 10)
.Lfp_hex_num:
    add     w10, w10, #'0'
    strb    w10, [x1], #1
    lsl     w5, w5, #4
    subs    w2, w2, #1
    b.ne    .Lfp_hex_digit
    mov     x1, x9
    mov     w2, #8
    bl      emit_bytes
    ldp     x29, x30, [sp], #32
    ret

// ----- Strings + tx_test_pkt -----

.section .rodata, "a"

failpath_ready_msg:
    .ascii  "READY\n"
failpath_ready_msg_end:

failpath_boot_msg:
    .ascii  "FAILPATH:BOOT\n"
failpath_boot_msg_end:

failpath_done_msg:
    .ascii  "FAILPATH:DONE rc="
failpath_done_msg_end:

newline_msg:
    .byte   10
newline_msg_end:

// tx_test_pkt — referenced by dispatcher.S as the canary TX
// frame address when no ARP reply is pending. On BAD_ID +
// NUM_BUFS scenarios the dispatcher returns BEFORE reaching
// the TX submit phase, so this is never loaded; on TX_BAD_ID
// the dispatcher reads desc.addr from here for the TX submit
// (the actual contents don't matter — we don't have a real
// virtio device that would interpret them). Provided to
// satisfy the dispatcher's `extern tx_test_pkt`.
.global tx_test_pkt
tx_test_pkt:
    .fill   72, 1, 0
