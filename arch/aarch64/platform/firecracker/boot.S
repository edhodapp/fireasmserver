// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// Minimal aarch64 Firecracker guest stub for pipeline verification.
// Writes "READY\n" to PL011 UART at 0x09000000, then WFE-halts.
//
// Image format: Linux arm64 Image (per the Firecracker aarch64 boot
// protocol — kernel file is a Linux Image header + code, loaded at
// RAM_BASE + text_offset).
//
// Firecracker aarch64 defaults:
//   RAM base  = 0x8000_0000
//   text_off  = 0x0008_0000
//   load addr = 0x8008_0000
//   PL011 UART MMIO = 0x0900_0000

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
    // secondary CPU (non-zero MPIDR_EL1.Aff0) at the WFE loop — the
    // primary does the UART work alone to avoid interleaved writes.
    // Firecracker assigns Aff0 = vCPU index, so Aff0=0 is the primary.
    mrs     x5, mpidr_el1
    and     x5, x5, #0xff
    cbnz    x5, .Ldone

    // PL011 UART base in x0.
    // movz #0x0900, lsl #16  == x0 = 0x0900_0000 (single instruction,
    // no literal pool — keeps the stub position-independent and small).
    movz    x0, #0x0900, lsl #16

    // Message pointer in x1, remaining byte count in w2. The length is
    // a link-time constant so editing ready_msg doesn't require keeping
    // a numeric count in sync.
    adr     x1, ready_msg
    mov     w2, #(ready_msg_end - ready_msg)

.Lwrite:
    cbz     w2, .Lflush

    // Poll FR (offset 0x18), bit 5 = TXFF. Wait until TX FIFO has room.
    // `yield` is a HINT on AArch64 — no semantic effect, just tells the
    // hypervisor this CPU is in a spin-wait so it can schedule elsewhere.
.Lwait:
    yield
    ldr     w3, [x0, #0x18]
    tbnz    w3, #5, .Lwait

    // Write one byte to DR (offset 0x00). strb matches the ldrb width —
    // upper bits of w4 are zero from the load, so a full-word str would
    // also work, but strb is idiomatic and safer if the load ever changes.
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

ready_msg:
    .ascii  "READY\n"
ready_msg_end:

_image_end:
