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

    // SP at the bootstrap stack reserved by the linker script
    // (D059 step 4.1: __bootstrap_stack_top). 4 KiB scratch,
    // 16-byte aligned, anchored 64 KiB past the image. Required
    // before bl init_memory_layout — even though the current
    // stub doesn't push, the future allocator's diagnostic path
    // will, and the contract is pre-bootstrap-stack-safe, not
    // pre-any-stack-safe.
    //
    // CPU0-only by construction: the MPIDR park gate above filters
    // secondary cores to WFE before this SP setup. Multi-core wake
    // (post-step-5) gives each secondary its own .memreq-assigned
    // stack from the allocator's per-core table; the bootstrap
    // stack is never shared across cores.
    ldr     x5, =__bootstrap_stack_top
    mov     sp, x5

    // Run the init-time static memory allocator (D060 step 4.2).
    // Returns immediately today (.memreq is empty); wire-up
    // exists so the call site stops being a no-op the moment a
    // module declares a region (step 5). Mirrors the firecracker
    // platform.
    bl      init_memory_layout

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
    // Placeholder for secondary_entry (post-step-5). Today: every
    // secondary core lands here from the MPIDR park gate at _entry
    // and stays in WFE until power-off. Post-step-5: this becomes
    // the PSCI_CPU_ON target — wakes here, reads MPIDR, indexes
    // into the allocator's per-core stack-base table, sets SP to
    // its own dedicated stack, then enters the per-core actor loop.
    wfe
    b       .Ldone

.section .rodata, "a"
ready_msg:
    .ascii  "READY\n"
ready_msg_end:

// _image_end defined in the linker script, placed after .text and
// .rodata so image_size covers the whole file.
