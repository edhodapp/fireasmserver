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
}

/* PVH protocol requires the entry point to fit in 32 bits.
   The note descriptor is a .long, which would silently
   truncate a >4GiB symbol value. Catch that at link time. */
ASSERT(_start < 0x100000000, "PVH entry point must fit in 32 bits")
