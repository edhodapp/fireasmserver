// SPDX-License-Identifier: AGPL-3.0-or-later
//
// AArch64 bump allocator over the .memreq table (D059 +
// D060 step 3B). Calls into memlayout_run_bytecode (step 3A)
// to evaluate each record's size and alignment expressions,
// then forward-bumps non-stack lifetimes from heap_start /
// reverse-bumps stack lifetimes from ram_top. Differential-
// tested under user-mode QEMU against the Python reference
// at tooling/src/memlayout/reference.py — see
// tooling/tests/test_memlayout_alloc_diff.py.
//
// ABI (matches memlayout_call_t in bcvm_abi.h):
//   void memlayout_run_allocator(memlayout_call_t *call);
// On entry: x0 = pointer to memlayout_call_t.
// On return: call->rc_out =
//   0                            success
//   BCVM_ERR_*  (1..17)          inner-VM error from a size or
//                                align expression
//   MEMLAYOUT_ERR_OVERFLOW (100) bump-pointer crossing
//   MEMLAYOUT_ERR_HEAP_TOP (101) heap_start > ram_top at entry
//   MEMLAYOUT_ERR_BAD_LIFETIME (102) lifetime byte not 0..3
//
// On success, every record in [memreq_start, memreq_end) has
// its assigned_addr (offset 32) and assigned_size (offset 40)
// fields populated, and call->forward_end_out / call->
// reverse_end_out are written.
//
// Stack frame: 96 bytes. Holds saved x29/x30, callee-saves
// x19..x27, and an 80-byte scratch bcvm_call_t (the
// allocator reuses one such struct across every per-record
// VM invocation rather than building a fresh one on each
// pass — bytecode VM call overhead stays small).
//
// Register map within the body:
//   x19 — call struct pointer (the outer memlayout_call_t)
//   x20 — current record pointer
//   x21 — memreq_end (loop bound)
//   x22 — forward bump (live during pass 1)
//   x23 — reverse bump (live during pass 2)
//   x24 — bcvm_call_t scratch base on local stack
//
// Pass-2 forward-cross-check: for each stack region, after
// reverse-bumping, assert candidate >= forward_end. If not,
// MEMLAYOUT_ERR_OVERFLOW.

.include "bcvm_errors.inc"

// -- memlayout-side error codes --
.equ MEMLAYOUT_ERR_OVERFLOW,     100
.equ MEMLAYOUT_ERR_HEAP_TOP,     101
.equ MEMLAYOUT_ERR_BAD_LIFETIME, 102

// -- Field offsets in memlayout_call_t --
.equ ML_OFF_MEMREQ_START,    0
.equ ML_OFF_MEMREQ_END,      8
.equ ML_OFF_CPU_VALUES,      16
.equ ML_OFF_CPU_COUNT,       24
.equ ML_OFF_TUN_VALUES,      32
.equ ML_OFF_TUN_COUNT,       40
.equ ML_OFF_HEAP_START,      48
.equ ML_OFF_RAM_TOP,         56
.equ ML_OFF_RC_OUT,          64
.equ ML_OFF_FORWARD_OUT,     72
.equ ML_OFF_REVERSE_OUT,     80

// -- Field offsets in each .memreq record --
.equ MR_OFF_SIZE_BC,         4
.equ MR_OFF_ALIGN_BC,        20
.equ MR_OFF_LIFETIME,        30
.equ MR_OFF_ASSIGNED_ADDR,   32
.equ MR_OFF_ASSIGNED_SIZE,   40
.equ MR_RECORD_BYTES,        48
.equ MR_SIZE_BC_BYTES,       16
.equ MR_ALIGN_BC_BYTES,      8

// -- bcvm_call_t field offsets --
.equ BC_OFF_CODE,        0
.equ BC_OFF_CODE_LEN,    8
.equ BC_OFF_CPU_VALUES,  16
.equ BC_OFF_CPU_COUNT,   24
.equ BC_OFF_TUN_VALUES,  32
.equ BC_OFF_TUN_COUNT,   40
.equ BC_OFF_RC,          48
.equ BC_OFF_RESULT,      56
.equ BC_SIZE_BYTES,      64

// -- Lifetime values --
.equ LT_STEADY,    0
.equ LT_INIT_ONLY, 1
.equ LT_IMMUT,     2
.equ LT_STACK,     3

    .text
    .balign 4
    .globl  memlayout_run_allocator
    .type   memlayout_run_allocator, %function

memlayout_run_allocator:
    // Frame: 16 (x29/x30) + 64 (callee-saves x19..x26) + 64
    // (bcvm scratch struct) = 144. Round up to 16-aligned: OK
    // (144 is already aligned).
    stp     x29, x30, [sp, #-144]!
    stp     x19, x20, [sp, #16]
    stp     x21, x22, [sp, #32]
    stp     x23, x24, [sp, #48]
    stp     x25, x26, [sp, #64]
    mov     x29, sp

    // bcvm_call scratch lives at sp + 80, occupies 64 bytes.
    add     x24, sp, #80

    mov     x19, x0                     // outer call ptr

    // Heap > top → MEMLAYOUT_ERR_HEAP_TOP.
    ldr     x0, [x19, #ML_OFF_HEAP_START]
    ldr     x1, [x19, #ML_OFF_RAM_TOP]
    cmp     x0, x1
    b.hi    .Lalloc_heap_top

    mov     x22, x0                     // forward bump
    mov     x23, x1                     // reverse bump

    // Pre-fill the immutable fields of the bcvm_call scratch.
    ldr     x0, [x19, #ML_OFF_CPU_VALUES]
    str     x0, [x24, #BC_OFF_CPU_VALUES]
    ldr     x0, [x19, #ML_OFF_CPU_COUNT]
    str     x0, [x24, #BC_OFF_CPU_COUNT]
    ldr     x0, [x19, #ML_OFF_TUN_VALUES]
    str     x0, [x24, #BC_OFF_TUN_VALUES]
    ldr     x0, [x19, #ML_OFF_TUN_COUNT]
    str     x0, [x24, #BC_OFF_TUN_COUNT]

    // ------- Pass 1: forward, non-stack lifetimes ----------
    ldr     x20, [x19, #ML_OFF_MEMREQ_START]
    ldr     x21, [x19, #ML_OFF_MEMREQ_END]

.Lalloc_pass1_loop:
    cmp     x20, x21
    b.hs    .Lalloc_pass2_init

    ldrb    w0, [x20, #MR_OFF_LIFETIME]
    cmp     w0, #LT_STACK
    b.eq    .Lalloc_pass1_skip          // stacks done in pass 2
    cmp     w0, #LT_IMMUT
    b.hi    .Lalloc_bad_lifetime

    // Evaluate size_bc → x25.
    add     x0, x20, #MR_OFF_SIZE_BC
    mov     x1, #MR_SIZE_BC_BYTES
    bl      .Lalloc_eval_bc
    cbnz    w0, .Lalloc_inner_fail
    ldr     x25, [x24, #BC_OFF_RESULT]

    // Evaluate align_bc → x26.
    add     x0, x20, #MR_OFF_ALIGN_BC
    mov     x1, #MR_ALIGN_BC_BYTES
    bl      .Lalloc_eval_bc
    cbnz    w0, .Lalloc_inner_fail
    ldr     x26, [x24, #BC_OFF_RESULT]

    // align_up(forward, x26): require x26 power-of-two.
    cbz     x26, .Lalloc_overflow
    sub     x0, x26, #1
    and     x1, x26, x0
    cbnz    x1, .Lalloc_overflow        // non-pow2 align
    add     x22, x22, x0
    mvn     x1, x0
    and     x22, x22, x1                // forward = align_up

    // Bounds: forward + size <= ram_top (also implies no
    // u64 wrap because ram_top is in u64). Use addition with
    // carry detect.
    adds    x0, x22, x25
    b.cs    .Lalloc_overflow

    // Write assigned_addr, assigned_size.
    str     x22, [x20, #MR_OFF_ASSIGNED_ADDR]
    str     x25, [x20, #MR_OFF_ASSIGNED_SIZE]

    // Advance forward bump.
    mov     x22, x0

.Lalloc_pass1_skip:
    add     x20, x20, #MR_RECORD_BYTES
    b       .Lalloc_pass1_loop

.Lalloc_pass2_init:
    // ------- Pass 2: reverse, stack lifetimes only ----------
    ldr     x20, [x19, #ML_OFF_MEMREQ_START]

.Lalloc_pass2_loop:
    cmp     x20, x21
    b.hs    .Lalloc_done

    ldrb    w0, [x20, #MR_OFF_LIFETIME]
    cmp     w0, #LT_STACK
    b.ne    .Lalloc_pass2_skip

    add     x0, x20, #MR_OFF_SIZE_BC
    mov     x1, #MR_SIZE_BC_BYTES
    bl      .Lalloc_eval_bc
    cbnz    w0, .Lalloc_inner_fail
    ldr     x25, [x24, #BC_OFF_RESULT]

    add     x0, x20, #MR_OFF_ALIGN_BC
    mov     x1, #MR_ALIGN_BC_BYTES
    bl      .Lalloc_eval_bc
    cbnz    w0, .Lalloc_inner_fail
    ldr     x26, [x24, #BC_OFF_RESULT]

    // align must be positive power-of-two.
    cbz     x26, .Lalloc_overflow
    sub     x0, x26, #1
    and     x1, x26, x0
    cbnz    x1, .Lalloc_overflow

    // size > reverse → underflow.
    cmp     x25, x23
    b.hi    .Lalloc_overflow

    sub     x1, x23, x25                // candidate = rev - size
    mvn     x2, x0                      // ~(align - 1)
    and     x1, x1, x2                  // align_down

    // Cross-check vs forward bump.
    cmp     x1, x22
    b.lo    .Lalloc_overflow

    str     x1, [x20, #MR_OFF_ASSIGNED_ADDR]
    str     x25, [x20, #MR_OFF_ASSIGNED_SIZE]
    mov     x23, x1                     // reverse = new top

.Lalloc_pass2_skip:
    add     x20, x20, #MR_RECORD_BYTES
    b       .Lalloc_pass2_loop

.Lalloc_done:
    // Success.
    str     wzr, [x19, #ML_OFF_RC_OUT]
    str     x22, [x19, #ML_OFF_FORWARD_OUT]
    str     x23, [x19, #ML_OFF_REVERSE_OUT]
    b       .Lalloc_epilogue

// ---- Inner helper: invoke memlayout_run_bytecode against
//      the per-pass scratch struct.
//
// In:  x0 = code pointer (within the record)
//      x1 = code length (16 or 8)
// Out: w0 = bcvm_call.rc_out (0 on success)
// Clobbers x0-x18 (bytecode VM is a normal callee).
//
// Nested-call discipline: the inner `bl memlayout_run_bytecode`
// clobbers x30 with its own return address. We must save the
// outer caller's x30 on the stack before that inner bl and
// restore it before returning, otherwise the `ret` here uses
// the wrong return address — caught 2026-04-26 as an aarch64-
// only timeout under qemu-aarch64-static (the differential
// test for the "forward_crosses_reverse" case spins forever).
.Lalloc_eval_bc:
    stp     x29, x30, [sp, #-16]!
    str     x0, [x24, #BC_OFF_CODE]
    str     x1, [x24, #BC_OFF_CODE_LEN]
    mov     x0, x24
    bl      memlayout_run_bytecode
    ldr     w0, [x24, #BC_OFF_RC]
    ldp     x29, x30, [sp], #16
    ret

.Lalloc_inner_fail:
    // x24-relative rc was already loaded in w0 by the caller.
    str     w0, [x19, #ML_OFF_RC_OUT]
    str     xzr, [x19, #ML_OFF_FORWARD_OUT]
    str     xzr, [x19, #ML_OFF_REVERSE_OUT]
    b       .Lalloc_epilogue

.Lalloc_heap_top:
    mov     w0, #MEMLAYOUT_ERR_HEAP_TOP
    str     w0, [x19, #ML_OFF_RC_OUT]
    str     xzr, [x19, #ML_OFF_FORWARD_OUT]
    str     xzr, [x19, #ML_OFF_REVERSE_OUT]
    b       .Lalloc_epilogue

.Lalloc_overflow:
    mov     w0, #MEMLAYOUT_ERR_OVERFLOW
    str     w0, [x19, #ML_OFF_RC_OUT]
    str     xzr, [x19, #ML_OFF_FORWARD_OUT]
    str     xzr, [x19, #ML_OFF_REVERSE_OUT]
    b       .Lalloc_epilogue

.Lalloc_bad_lifetime:
    mov     w0, #MEMLAYOUT_ERR_BAD_LIFETIME
    str     w0, [x19, #ML_OFF_RC_OUT]
    str     xzr, [x19, #ML_OFF_FORWARD_OUT]
    str     xzr, [x19, #ML_OFF_REVERSE_OUT]
    b       .Lalloc_epilogue

.Lalloc_epilogue:
    ldp     x25, x26, [sp, #64]
    ldp     x23, x24, [sp, #48]
    ldp     x21, x22, [sp, #32]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #144
    ret
    .size   memlayout_run_allocator, . - memlayout_run_allocator

    .section .note.GNU-stack, "", %progbits
