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
     * Init-time static memory layout (D059 + D060) — step-1 plumbing.
     * The .memreq input section collects 48-byte declaration records
     * emitted by arch/<isa>/memory/memreq.inc. Today no module
     * declares a region, so the section links as zero-size; the
     * __memreq_start / __memreq_end bracket symbols still resolve
     * correctly so the real allocator (lands in step 3) iterates
     * an empty range and returns immediately, without special-casing.
     */
    . = ALIGN(8);
    .memreq : {
        __memreq_start = .;
        KEEP(*(.memreq))
        __memreq_end = .;
    }

    /*
     * _image_end is defined here, *after* .text + .rodata + .memreq,
     * so the Linux arm64 Image header's image_size field (computed
     * as `_image_end - _start` in boot.S) covers the full image the
     * loader needs to place in memory. Previously this symbol lived
     * in the assembly and got pulled into .text output, causing
     * image_size to exclude .rodata (e.g., the READY string) —
     * Firecracker loads the whole file regardless, but a stricter
     * arm64 boot loader would truncate per the spec.
     */
    _image_end = .;

    /*
     * Layout contract symbols for the init-time bump allocator
     * (D059 + D060). __heap_start is the first 4-KiB-aligned byte
     * past the loaded image; __ram_top is the inclusive-exclusive
     * bound on guest physical RAM. Firecracker's aarch64 layout
     * places guest RAM at [0x80000000, 0x88000000) by default
     * (128 MiB). The allocator forward-bumps non-stack regions
     * from __heap_start and reverse-bumps stack regions from
     * __ram_top.
     */
    __heap_start = ALIGN(_image_end + (0x80000000 + 0x80000), 0x1000);
    __ram_top    = 0x88000000;

    /DISCARD/ : {
        *(.comment)
        *(.note*)
        *(.ARM.attributes)
    }
}
