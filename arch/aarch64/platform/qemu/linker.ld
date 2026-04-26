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

    /*
     * Init-time static memory layout (D059 + D060) — step-1 plumbing.
     * Empty today; bracket symbols resolve so the allocator iterates
     * a zero-length range cleanly. See the firecracker linker.ld for
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
     * Layout contract symbols. QEMU -M virt places the image at
     * RAM_BASE (0x40000000) + text_offset (0x80000); guest RAM is
     * configurable but defaults to 128 MiB, so the inclusive-
     * exclusive top is 0x48000000.
     */
    __heap_start = ALIGN(_image_end + (0x40000000 + 0x80000), 0x1000);
    __ram_top    = 0x48000000;

    /DISCARD/ : {
        *(.comment)
        *(.note*)
        *(.ARM.attributes)
    }
}
