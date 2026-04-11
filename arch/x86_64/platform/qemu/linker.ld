/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Linker script for multiboot QEMU guest. */

ENTRY(_start)

SECTIONS
{
    . = 0x100000;

    .multiboot : { *(.multiboot) }
    .text : { *(.text) }
}
