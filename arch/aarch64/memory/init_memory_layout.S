// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Step-1 stub for the init-time memory allocator (D059 + D060).
// The real allocator lands in step 3 alongside the bytecode VM.
// This stub exists so the function symbol, the per-arch entry
// point, and the build wiring are all in place before any caller
// or any record exists.
//
// Contract (final form, partially honored today):
//   void init_memory_layout(void)
//   - Pre-stack safe (uses no memory stack; runs before SP is
//     installed on the real boot stack).
//   - Walks the .memreq record table at [__memreq_start, __memreq_end),
//     evaluates each record's size and alignment bytecode, and
//     assigns an absolute address into the record's assigned_addr /
//     assigned_size fields.
//   - Forward-bumps non-stack regions starting at __heap_start;
//     reverse-bumps stack regions from __ram_top.
//   - Fires the init_complete fence on success; halts with a
//     LAYOUT-OVERFLOW marker on bump-pointer crossing.
//
// Today the stub only returns. The .memreq section is empty
// (no module declares regions yet), so even when the real
// allocator lands in step 3, this code path is a no-op until
// step 5 starts migrating regions.

    .text
    .globl  init_memory_layout
    .type   init_memory_layout, %function
    .balign 4
init_memory_layout:
    ret
    .size   init_memory_layout, . - init_memory_layout

    .section .note.GNU-stack, "", %progbits
