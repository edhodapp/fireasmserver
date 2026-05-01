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
     * Stage-1 boot identity-map page tables (D063, supersedes
     * D062's coverage clause: low 4 GiB instead of low 1 GiB).
     * 4 KiB-aligned, immediately past the bootstrap stack:
     *   __boot_pml4 — 4 KiB.  1 entry used (PML4[0] → PDPT)
     *   __boot_pdpt — 4 KiB.  4 entries used (PDPT[0..3] →
     *                 PD[0..3], one PDPTE per GiB)
     *   __boot_pd   — 16 KiB. 4 contiguous PD pages, 2048 entries
     *                 total: identity-map low 4 GiB with 2 MiB
     *                 pages each carrying
     *                 (addr | PRESENT | WRITABLE | PAGE_SIZE)
     * boot.S zeroes all three reservations on entry and populates
     * them before the 32→64 mode switch. VMM-zeroed guest RAM
     * makes the zeroing belt-and-suspenders today, but the spec
     * doesn't mandate zero-init so we don't rely on it.
     *
     * Why low 4 GiB (D063): firecracker's VIRTIO_MMIO_BASE =
     * 0xC0001000 sits at 3 GiB + 4 KiB. D062's low-1-GiB
     * coverage would page-fault on first MMIO read after the
     * mode switch. Low 4 GiB also covers any future Firecracker
     * or QEMU MMIO movement within the 32-bit phys-addr window
     * with no per-platform map customization.
     *
     * Reservation only: symbol assignments don't advance the
     * location counter, parallel to __bootstrap_stack_top above.
     * The 24 KiB of page-table space is "implicitly reserved" by
     * advancing __heap_start past it — the bump allocator never
     * sees these addresses, so nothing else can land here.
     *
     * Stage-1 only — does not bind kernel virtual-memory policy
     * (per D062 + D063). The kernel may rebuild tables at any
     * virtual shape it chooses post-allocator.
     */
    __boot_pml4 = ALIGN(__bootstrap_stack_top, 0x1000);
    __boot_pdpt = __boot_pml4 + 0x1000;
    __boot_pd   = __boot_pdpt + 0x1000;

    /*
     * Advance . past the bootstrap-stack + page-table reservations
     * so a future input section can't accidentally land inside any
     * of them. GNU ld symbol assignments don't advance the location
     * counter on their own; without this, the reservations are
     * "implicit" and depend on no other section claiming the same
     * range. With this, the linker sees the reserved range as
     * occupied and refuses to place anything there.
     */
    . = __boot_pd + 0x4000;

    /*
     * Layout contract symbols. Firecracker's x86_64 default guest
     * RAM goes up to ~0xc0000000 (3 GiB) before the MMIO gap;
     * conservative bound for the bump allocator's reverse-from-
     * top stack placement is 0x40000000 (1 GiB) — well within
     * Firecracker's default --vcpu-count and --mem-size envelope
     * and free of the MMIO regions Firecracker maps above 3 GiB.
     */
    __heap_start = ALIGN(., 0x1000);
    __ram_top    = 0x40000000;
}

/* PVH protocol requires the entry point to fit in 32 bits.
   The note descriptor is a .long, which would silently
   truncate a >4GiB symbol value. Catch that at link time. */
ASSERT(_start < 0x100000000, "PVH entry point must fit in 32 bits")

/* D062/D063 stage-1 boot page tables must be 4 KiB aligned (CR3
   hardware requirement) and must fit within guest RAM. Aligned by
   construction (ALIGN(..., 0x1000) + 0x1000 increments), but a future
   change to the offsets could silently break alignment; CR3 with a
   misaligned PML4 base faults at the long-mode jump. Image growth
   could push the reservation past __ram_top with no diagnostic. Cheap
   link-time check for both. The 0x4000 term reflects D063's PD growth
   from 4 KiB to 16 KiB.

   The "contiguous 24 KiB" ASSERTs pin the layout that mode_switch.S's
   single rep-stosd zero-pass depends on (it zeroes 24 KiB starting
   at __boot_pml4 in one shot). A future layout change that gaps the
   regions or reorders them would silently leak non-zero memory into
   live page tables. */
ASSERT((__boot_pml4 & 0xFFF) == 0, "boot PML4 must be 4 KiB aligned")
ASSERT(__boot_pd + 0x4000 <= __ram_top, "boot page tables overflow guest RAM")
ASSERT(__boot_pdpt - __boot_pml4 == 0x1000, "boot PDPT must be contiguous with PML4")
ASSERT(__boot_pd - __boot_pdpt == 0x1000, "boot PD must be contiguous with PDPT")
ASSERT(__heap_start - __boot_pd >= 0x4000, "heap must clear the 16 KiB PD reservation")
ASSERT(__bootstrap_stack_top - __bootstrap_stack_size >= _image_end,
       "bootstrap stack body collides with the loaded image — _image_end grew past the slack budget")
ASSERT(__heap_start <= __ram_top,
       "heap_start past __ram_top — image too large or __ram_top set too low")
