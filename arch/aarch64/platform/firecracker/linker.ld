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

    /*
     * _image_end is defined here, *after* both .text and .rodata output
     * sections, so the Linux arm64 Image header's image_size field
     * (computed as `_image_end - _start` in boot.S) covers the full
     * image the loader needs to place in memory. Previously this symbol
     * lived in the assembly and got pulled into .text output, causing
     * image_size to exclude .rodata (e.g., the READY string) — Firecracker
     * loads the whole file regardless, but a stricter arm64 boot loader
     * would truncate per the spec.
     */
    _image_end = .;

    /DISCARD/ : {
        *(.comment)
        *(.note*)
        *(.ARM.attributes)
    }
}
