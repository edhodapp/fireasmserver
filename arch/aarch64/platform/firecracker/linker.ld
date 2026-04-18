/* SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Linker script for the aarch64 Firecracker tracer-bullet stub.
 *
 * The resulting ELF is objcopy'd to raw binary (Linux arm64 Image format).
 * Firecracker reads the Image header's text_offset and loads at
 * RAM_BASE (0x80000000) + text_offset (0x80000). The stub uses only
 * PC-relative addressing (adr, b, tbnz) so the link-time VMA doesn't
 * matter — it runs wherever the loader places it.
 */

OUTPUT_ARCH(aarch64)
ENTRY(_start)

/*
 * This stub emits .text (header + code + string literals inside .text)
 * only. If a future .S adds .data or .bss sections, rework this script:
 * add them explicitly before _image_end below, and either move the
 * _image_end symbol here (so image_size in the Image header covers bss)
 * or zero bss in _start before use. Doing neither will silently produce
 * an Image whose header under-reports image_size and whose bss is not
 * cleared on entry.
 */

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

    /DISCARD/ : {
        *(.comment)
        *(.note*)
        *(.ARM.attributes)
    }
}
