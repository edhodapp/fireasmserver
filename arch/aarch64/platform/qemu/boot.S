// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// Minimal aarch64 QEMU-virt guest stub for pipeline verification.
// Writes "READY\n" to PL011 UART at 0x09000000 and WFE-halts.
//
// Image format: Linux arm64 Image. `qemu-system-aarch64 -M virt
// -kernel <this>` loads at RAM_BASE (0x40000000) + text_offset
// (0x80000) = 0x40080000. Code is PC-relative so the absolute load
// address doesn't matter.
//
// Counterpart to platform/firecracker/boot.S — same header layout and
// control flow, different UART hardware: QEMU virt uses a real PL011
// at 0x09000000 (DR=0x00, FR=0x18, bit 5 = TXFF) whereas Firecracker
// emulates an 8250 via vm_superio::Serial at 0x40002000.

.section .text, "ax"
.globl _start

_start:
    // ----- Linux arm64 Image header (64 bytes) -----
    b       _entry
    // code1: the EFI-stub variant would put a second valid instruction
    // here; QEMU virt doesn't use that path, so 0 is fine.
    .long   0
    .quad   0x80000             // text_offset
    .quad   _image_end - _start // image_size
    .quad   0                   // flags
    .quad   0, 0, 0             // reserved
    .long   0x644D5241          // magic ("ARM\x64")
    .long   0

_entry:
    // Park secondary CPUs. Wider mask than strictly necessary (Aff0
    // alone suffices under QEMU virt today); matches the firecracker
    // stub for consistency.
    mrs     x5, mpidr_el1
    and     x5, x5, #0xffffff
    cbnz    x5, .Ldone

    // PL011 base in x0 (QEMU virt default).
    movz    x0, #0x0900, lsl #16

    adr     x1, ready_msg
    mov     w2, #(ready_msg_end - ready_msg)

.Lwrite:
    cbz     w2, .Lflush

    // PL011 FR register at offset 0x18, bit 5 = TXFF
    // (1 = TX FIFO full; wait while set).
.Lwait:
    yield
    ldr     w3, [x0, #0x18]
    tbnz    w3, #5, .Lwait

    // Write one byte to DR at offset 0x00. PL011 DR is 32-bit; the
    // low 8 bits are the transmit data. strb matches ldrb width.
    ldrb    w4, [x1], #1
    strb    w4, [x0]
    sub     w2, w2, #1
    b       .Lwrite

.Lflush:
    dsb     sy

.Ldone:
    wfe
    b       .Ldone

.section .rodata, "a"
ready_msg:
    .ascii  "READY\n"
ready_msg_end:

// _image_end defined in the linker script, placed after .text and
// .rodata so image_size covers the whole file.
