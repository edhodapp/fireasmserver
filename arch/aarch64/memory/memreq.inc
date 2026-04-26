// SPDX-License-Identifier: AGPL-3.0-or-later
// AArch64 .memreq macro definition. Step-1 plumbing for D059 +
// D060: emits a 48-byte fixed-layout record into the dedicated
// `.memreq` ELF section. The record carries every input the
// init-time bump allocator (D060 phase 2) needs to compute and
// assign an absolute address for the region: identity, owner,
// lifetime, writability, and bytecode expressions for size and
// alignment.
//
// At this point in the retrofit the macro is defined but no
// caller invokes it; the section therefore links as zero-size.
// Modules start declaring regions in step 5; the bytecode
// interpreter that consumes the records lands in step 3.
//
// Record layout (matches the Python reference at
// tooling/src/memlayout/reference.py once that lands in step 2):
//
//   offset  size  field
//   0       4     name_hash    fnv1a of region name (author-supplied,
//                              audit-verified per D060)
//   4       16    size_bc      size-expression bytecode (END-terminated)
//   20      8     align_bc     alignment-expression bytecode
//   28      2     owner_id     0 = boot core, 1..N-1 = worker core,
//                              0xFFFF = shared-readonly
//   30      1     lifetime     0 = steady_state, 1 = init_only,
//                              2 = immutable_after_init, 3 = stack
//   31      1     writable     0 = read-only after init, 1 = read/write
//   32      8     assigned_addr   filled in by allocator at init
//   40      8     assigned_size   filled in by allocator at init
//
// Total: 48 bytes, 8-byte aligned.

.altmacro

.macro memreq region, name_hash, size_bc, align_bc, owner, lifetime, writable
    .pushsection .memreq, "a", %progbits
    .balign 8
__memreq_rec__\region:
    .long   \name_hash
    \size_bc
    .balign 4
    .org    __memreq_rec__\region + 20
    \align_bc
    .balign 1
    .org    __memreq_rec__\region + 28
    .short  \owner
    .byte   \lifetime
    .byte   \writable
__memreq_assigned__\region:
    .quad   0                        // assigned_addr (allocator-written)
    .quad   0                        // assigned_size (allocator-written)
    .org    __memreq_rec__\region + 48
    .popsection

    // Linker-visible pointer at the assigned_addr slot. Module code
    // does `ldr x0, =__memreq_addr__<region>` followed by `ldr x0,
    // [x0]` to load the runtime address.
    .global __memreq_addr__\region
    .set    __memreq_addr__\region, __memreq_assigned__\region
    .global __memreq_size__\region
    .set    __memreq_size__\region, __memreq_assigned__\region + 8
.endm
