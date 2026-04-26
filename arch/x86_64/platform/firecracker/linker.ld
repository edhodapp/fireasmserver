/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Linker script for PVH-booted Firecracker guest. */

ENTRY(_start)

PHDRS
{
    text PT_LOAD FLAGS(5);  /* R+X */
    note PT_NOTE FLAGS(4);  /* R   */
}

SECTIONS
{
    . = 0x100000;

    .note.Xen : { *(.note.Xen) } :text :note

    .text : { *(.text) } :text

    /*
     * Init-time static memory layout (D059 + D060) — step-1 plumbing.
     * The .memreq input section collects 48-byte declaration records
     * emitted by arch/x86_64/memory/memreq.inc. Empty today (no
     * module declares regions until step 5). Bracket symbols
     * resolve so the allocator (step 3) iterates a zero-length
     * range cleanly without special-casing.
     */
    . = ALIGN(8);
    .memreq : {
        __memreq_start = .;
        KEEP(*(.memreq))
        __memreq_end = .;
    } :text

    _image_end = .;

    /*
     * Layout contract symbols. Firecracker's x86_64 default guest
     * RAM goes up to ~0xc0000000 (3 GiB) before the MMIO gap;
     * conservative bound for the bump allocator's reverse-from-
     * top stack placement is 0x40000000 (1 GiB) — well within
     * Firecracker's default --vcpu-count and --mem-size envelope
     * and free of the MMIO regions Firecracker maps above 3 GiB.
     */
    . = ALIGN(0x1000);
    __heap_start = .;
    __ram_top    = 0x40000000;
}

/* PVH protocol requires the entry point to fit in 32 bits.
   The note descriptor is a .long, which would silently
   truncate a >4GiB symbol value. Catch that at link time. */
ASSERT(_start < 0x100000000, "PVH entry point must fit in 32 bits")
