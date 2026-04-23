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
.equ VIRTIO_MMIO_BASE,  0x40003000
.equ VIRTIO_MAGIC_LO,   0x6976          // "vi" little-endian
.equ VIRTIO_MAGIC_HI,   0x7472          // "tr" little-endian

.section .text, "ax"
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
    // Mask covers Aff0 | Aff1 | Aff2 (24 bits). Firecracker v1.15.1
    // currently indexes vCPUs into Aff0 only, but Aff1/Aff2 are reserved
    // for cluster/socket hierarchy; a wider mask is cheap insurance
    // against a future hypervisor using them.
    mrs     x5, mpidr_el1
    and     x5, x5, #0xffffff
    cbnz    x5, .Ldone

    // UART base in x0, preserved across emit_bytes / emit_hex32 calls.
    movz    x0, #0x4000, lsl #16
    movk    x0, #0x2000              // x0 = UART_BASE

    // Emit "READY\n".
    adr     x1, ready_msg
    mov     w2, #(ready_msg_end - ready_msg)
    bl      emit_bytes

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
    // memory ops. Strict-ordered device memory (if we'd configured
    // MAIR for it) would make these DSBs redundant; under the
    // tracer-bullet setup we're in Normal memory mode, so be
    // explicit.
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
    b       .Lflush

.Lvirtio_fail:
    // Emit "VIRTIO:FAIL magic=XXXXXXXX\n".
    adr     x1, virtio_fail_msg
    mov     w2, #(virtio_fail_msg_end - virtio_fail_msg)
    bl      emit_bytes
    mov     w5, w7                   // observed magic into w5 for emit_hex32
    bl      emit_hex32
    adr     x1, newline_msg
    mov     w2, #1
    bl      emit_bytes

.Lflush:
    dsb     sy

.Ldone:
    // WFE forever — Firecracker sees halt via MMIO quiescence.
    wfe
    b       .Ldone


// ================================================================
// Helpers — UART writers
// ================================================================

// emit_bytes: write w2 bytes from [x1] via UART at x0.
//   In:      x0 = UART base (preserved)
//            x1 = source pointer (advances by w2)
//            w2 = byte count (decremented to 0)
//   Clobbers w3, w4, w2, x1.
//   Preserves x0, x5, x6, x7, x8, x30.
emit_bytes:
    cbz     w2, .Lemit_done
.Lemit_loop:
    yield
    ldrb    w3, [x0, #0x05]
    tbz     w3, #5, .Lemit_loop
    ldrb    w4, [x1], #1
    strb    w4, [x0]
    subs    w2, w2, #1
    b.ne    .Lemit_loop
.Lemit_done:
    ret

// emit_hex32: write w5 as 8 uppercase hex digits via UART at x0.
//   In:      x0 = UART base (preserved)
//            w5 = 32-bit value to render
//   Uses 8 bytes of stack scratch. Calls emit_bytes; preserves
//   x0 / x6 / w7 / x8 across the call. Clobbers w2, w3, w4, w5,
//   x1, w10 (nibble scratch), x9 (buffer-start scratch).
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
ready_msg:
    .ascii  "READY\n"
ready_msg_end:

virtio_ok_msg:
    .ascii  "VIRTIO:OK\n"
virtio_ok_msg_end:

virtio_fail_msg:
    .ascii  "VIRTIO:FAIL magic="
virtio_fail_msg_end:

newline_msg:
    .ascii  "\n"

// _image_end is defined in the linker script (placed after both .text
// and .rodata) so the Image header's image_size covers the entire file.
