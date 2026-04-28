/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Linker script for multiboot QEMU guest. */

ENTRY(_start)

SECTIONS
{
    . = 0x100000;

    .multiboot : { *(.multiboot) }
    .text : { *(.text) }

    /*
     * Init-time static memory layout (D059 + D060) — step-1 plumbing.
     * Empty today; bracket symbols resolve so the allocator iterates
     * a zero-length range cleanly. See firecracker linker.ld for
     * the full annotation.
     */
    . = ALIGN(8);
    .memreq : {
        __memreq_start = .;
        KEEP(*(.memreq))
        __memreq_end = .;
    }

    _image_end = .;

    /*
     * Bootstrap stack for init_memory_layout() (D059 step 4.1).
     * 4 KiB scratch for one allocator pass; 64 KiB slack past the
     * image. No RAM-base term — this script sets `. = 0x100000`,
     * so VMA equals runtime PA on x86_64. See the firecracker
     * linker.ld for the cross-arch asymmetry note.
     */
    __bootstrap_stack_size = 0x1000;
    __bootstrap_stack_top  = ALIGN(_image_end + 0x10000, 16);

    /* Layout contract symbols. QEMU -machine pc default RAM is
     * 128 MiB; conservative bound for stack reverse-bump is the
     * 128 MiB ceiling (0x8000000). */
    __heap_start = ALIGN(__bootstrap_stack_top, 0x1000);
    __ram_top    = 0x8000000;
}
