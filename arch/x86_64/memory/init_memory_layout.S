; SPDX-License-Identifier: AGPL-3.0-or-later
;
; Step-1 stub for the init-time memory allocator (D059 + D060).
; The real allocator lands in step 3 alongside the bytecode VM.
; This stub exists so the function symbol, the per-arch entry
; point, and the build wiring are all in place before any caller
; or any record exists.
;
; Contract (final form, partially honored today):
;   void init_memory_layout(void)
;   - Pre-stack safe (uses no memory stack; runs before RSP is
;     installed on the real boot stack).
;   - Walks the .memreq record table at [__memreq_start, __memreq_end),
;     evaluates each record's size and alignment bytecode, and
;     assigns an absolute address into the record's assigned_addr /
;     assigned_size fields.
;   - Forward-bumps non-stack regions starting at __heap_start;
;     reverse-bumps stack regions from __ram_top.
;   - Fires the init_complete fence on success; halts with a
;     LAYOUT-OVERFLOW marker on bump-pointer crossing.
;
; Today the stub only returns. The .memreq section is empty
; (no module declares regions yet), so even when the real
; allocator lands in step 3, this code path is a no-op until
; step 5 starts migrating regions.
;
; Bits-mode shim: x86_64 firecracker boots via PVH (64-bit ELF
; wrapping a 32-bit entry stub); x86_64 qemu boots via Multiboot1
; (32-bit ELF). The function compiles into either via the per-
; platform NASM `-f elf{32,64}` flag selected by the arch
; Makefile. The body uses only `ret`, which assembles identically
; under both bits modes.

%ifidn __OUTPUT_FORMAT__, elf64
[bits 64]
%else
[bits 32]
%endif

section .text

global init_memory_layout
init_memory_layout:
    ret

; PT_NOTE for non-executable stack — match boot.asm's convention.
section .note.GNU-stack noalloc noexec nowrite progbits
