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

    /* Layout contract symbols. QEMU -machine pc default RAM is
     * 128 MiB; conservative bound for stack reverse-bump is the
     * 128 MiB ceiling (0x8000000). */
    . = ALIGN(0x1000);
    __heap_start = .;
    __ram_top    = 0x8000000;
}
