/* SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Linker script for the aarch64 QEMU-virt tracer-bullet stub.
 *
 * Mirrors platform/firecracker/linker.ld. Output ELF is objcopy'd to
 * raw binary (Linux arm64 Image format). QEMU -kernel accepts this
 * format; -M virt places the image at RAM_BASE (0x40000000) +
 * text_offset (0x80000). Code uses only PC-relative addressing so
 * the link-time VMA is cosmetic.
 */

OUTPUT_ARCH(aarch64)
ENTRY(_start)

SECTIONS
{
    . = 0x0;

    .text : {
        KEEP(*(.text))
    }

    . = ALIGN(8);
    .rodata : {
        *(.rodata*)
    }

    _image_end = .;

    /DISCARD/ : {
        *(.comment)
        *(.note*)
        *(.ARM.attributes)
    }
}
