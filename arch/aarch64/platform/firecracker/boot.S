// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// Minimal aarch64 Firecracker guest stub for pipeline verification.
// Writes "READY\n" to the emulated 8250/16550 UART and WFE-halts.
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
//   SERIAL MMIO       = RTC + MMIO_LEN        = 0x4000_2000   ← this stub
//   (MMIO_LEN = 0x1000)
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

    // UART base in x0 = 0x4000_2000 (Firecracker aarch64 SERIAL_MEM_START).
    // Needs movz+movk — not encodable in a single 16-bit-with-shift op.
    movz    x0, #0x4000, lsl #16    // x0 = 0x40000000
    movk    x0, #0x2000              // x0 = 0x40002000

    // Message pointer in x1, remaining byte count in w2. Length is a
    // link-time symbol-difference constant — editing ready_msg keeps
    // things consistent automatically.
    adr     x1, ready_msg
    mov     w2, #(ready_msg_end - ready_msg)

.Lwrite:
    cbz     w2, .Lflush

    // Poll LSR (offset 0x05), bit 5 = THRE. Wait while THRE is CLEAR
    // (busy), proceed when THRE is SET (ready-to-send).
    // `yield` is a HINT — lets the hypervisor schedule away from this
    // vCPU during the spin-wait.
.Lwait:
    yield
    ldrb    w3, [x0, #0x05]
    tbz     w3, #5, .Lwait

    // Write one byte to THR (offset 0x00).
    ldrb    w4, [x1], #1
    strb    w4, [x0]
    sub     w2, w2, #1
    b       .Lwrite

    // Drain any outstanding UART writes before halting. Secondary CPUs
    // that branched directly to .Ldone skip this — they never wrote.
.Lflush:
    dsb     sy

.Ldone:
    // WFE forever — Firecracker sees halt via MMIO quiescence.
    wfe
    b       .Ldone

.section .rodata, "a"
// Kept out of .text so a branch-coverage disassembler doesn't have to
// decode the string bytes as instructions. The stub's `adr` loads the
// address regardless of section; the linker resolves across sections.
ready_msg:
    .ascii  "READY\n"
ready_msg_end:

.section .text, "ax"
_image_end:
