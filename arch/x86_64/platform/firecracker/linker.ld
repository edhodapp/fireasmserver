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
     * Bootstrap stack for init_memory_layout() (D059 step 4.1).
     * 4 KiB scratch for one allocator pass; 64 KiB slack past the
     * image. Replaced by a .memreq lifetime=stack declaration in
     * step 5.
     *
     * No RAM-base term in the formula: this script sets
     * `. = 0x100000` at the top, so VMA equals runtime PA on
     * x86_64. `_image_end` is already a runtime address; adding a
     * RAM-base offset would land the stack past mapped guest RAM.
     * The contrasting convention is aarch64, where `. = 0x0` makes
     * symbols file offsets and the script must add RAM_BASE +
     * text_offset explicitly — see the aarch64 linker scripts for
     * that branch of the asymmetry.
     */
    __bootstrap_stack_size = 0x1000;
    __bootstrap_stack_top  = ALIGN(_image_end + 0x10000, 16);

    /*
     * Layout contract symbols. Firecracker's x86_64 default guest
     * RAM goes up to ~0xc0000000 (3 GiB) before the MMIO gap;
     * conservative bound for the bump allocator's reverse-from-
     * top stack placement is 0x40000000 (1 GiB) — well within
     * Firecracker's default --vcpu-count and --mem-size envelope
     * and free of the MMIO regions Firecracker maps above 3 GiB.
     */
    __heap_start = ALIGN(__bootstrap_stack_top, 0x1000);
    __ram_top    = 0x40000000;
}

/* PVH protocol requires the entry point to fit in 32 bits.
   The note descriptor is a .long, which would silently
   truncate a >4GiB symbol value. Catch that at link time. */
ASSERT(_start < 0x100000000, "PVH entry point must fit in 32 bits")
