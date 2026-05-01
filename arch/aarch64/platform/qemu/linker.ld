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
     * Bootstrap stack for init_memory_layout() (D059 step 4.1).
     * 4 KiB scratch for one allocator pass; 64 KiB slack past the
     * image. The `(0x40000000 + 0x80000)` term is the link-time →
     * runtime conversion (this script uses `. = 0x0`); see the
     * firecracker linker.ld for the cross-arch asymmetry note.
     * ALIGN(..., 16) enforces AArch64 SP alignment.
     */
    __bootstrap_stack_size = 0x1000;
    __bootstrap_stack_top  = ALIGN(_image_end + (0x40000000 + 0x80000) + 0x10000, 16);

    /*
     * Advance . past the bootstrap-stack reservation so a future
     * input section can't accidentally land inside the stack body.
     * See the firecracker linker.ld for the full annotation.
     */
    . = __bootstrap_stack_top - (0x40000000 + 0x80000);

    /*
     * Layout contract symbols. QEMU -M virt places the image at
     * RAM_BASE (0x40000000) + text_offset (0x80000); guest RAM is
     * configurable but defaults to 128 MiB, so the inclusive-
     * exclusive top is 0x48000000.
     */
    __heap_start = ALIGN(__bootstrap_stack_top, 0x1000);
    __ram_top    = 0x48000000;

    /DISCARD/ : {
        *(.comment)
        *(.note*)
        *(.ARM.attributes)
    }
}

/* Invariant ASSERTs. See firecracker/linker.ld for the rationale. */
ASSERT(__heap_start <= __ram_top,
       "heap_start past __ram_top — image too large or __ram_top set too low")
ASSERT(__bootstrap_stack_top - __bootstrap_stack_size >= _image_end + (0x40000000 + 0x80000),
       "bootstrap stack body collides with the loaded image — _image_end grew past the slack budget")
