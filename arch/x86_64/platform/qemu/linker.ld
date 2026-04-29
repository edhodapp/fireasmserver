/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Linker script for multiboot QEMU guest. */

ENTRY(_start)

SECTIONS
{
    . = 0x100000;

    .multiboot : { *(.multiboot) }
    .text : { *(.text) }

    /*
     * Init-time static memory layout (D059 + D060) — step-1 plumbing.
     * Empty today; bracket symbols resolve so the allocator iterates
     * a zero-length range cleanly. See firecracker linker.ld for
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
     * image. No RAM-base term — this script sets `. = 0x100000`,
     * so VMA equals runtime PA on x86_64. See the firecracker
     * linker.ld for the cross-arch asymmetry note.
     */
    __bootstrap_stack_size = 0x1000;
    __bootstrap_stack_top  = ALIGN(_image_end + 0x10000, 16);

    /*
     * Stage-1 boot identity-map page tables (D063, supersedes
     * D062's coverage clause: low 4 GiB instead of low 1 GiB).
     * 4 KiB-aligned, immediately past the bootstrap stack:
     *   __boot_pml4 —  4 KiB. 1 entry used.
     *   __boot_pdpt —  4 KiB. 4 entries used (one per GiB).
     *   __boot_pd   — 16 KiB. 4 contiguous PD pages, 2048 entries.
     * boot.S zeroes and populates them before the 32→64 mode
     * switch. Reservation only: symbol assignments don't advance
     * the location counter; __heap_start jumps past the 24 KiB
     * window so the bump allocator never lands here. See the
     * firecracker linker.ld for the full annotation.
     *
     * Note for QEMU specifically: default guest RAM is 128 MiB,
     * but the stage-1 identity map still covers low 4 GiB
     * uniformly per D063. The mappings beyond 128 MiB point at
     * physical addresses with no RAM behind them; merely
     * mapping is harmless — only an *access* to those addresses
     * would fault, and stage-1 boot.S only touches the loaded
     * image and the bootstrap stack (well within 128 MiB).
     */
    __boot_pml4 = ALIGN(__bootstrap_stack_top, 0x1000);
    __boot_pdpt = __boot_pml4 + 0x1000;
    __boot_pd   = __boot_pdpt + 0x1000;

    /* Layout contract symbols. QEMU -machine pc default RAM is
     * 128 MiB; conservative bound for stack reverse-bump is the
     * 128 MiB ceiling (0x8000000). */
    __heap_start = ALIGN(__boot_pd + 0x4000, 0x1000);
    __ram_top    = 0x8000000;
}

/* D062/D063 stage-1 boot page tables must be 4 KiB aligned (CR3
   hardware requirement) and must fit within guest RAM. See
   firecracker/linker.ld for the full annotation. The 0x4000 term
   reflects D063's PD growth from 4 KiB to 16 KiB. */
ASSERT((__boot_pml4 & 0xFFF) == 0, "boot PML4 must be 4 KiB aligned")
ASSERT(__boot_pd + 0x4000 <= __ram_top, "boot page tables overflow guest RAM")
