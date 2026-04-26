// SPDX-License-Identifier: AGPL-3.0-or-later
//
// AArch64 bytecode VM for D060 layer-3 size/align expressions.
// Differential-tested under user-mode QEMU against the Python
// reference at tooling/src/memlayout/. Both must agree on every
// (bytecode, cpu_values, tuning_values) input — see
// tooling/tests/test_memlayout_diff_aarch64.py.
//
// ABI (matches tooling/memlayout_diffharness/bcvm_abi.h):
//   void memlayout_run_bytecode(bcvm_call_t *call);
// On entry: x0 = pointer to bcvm_call_t.
// On return: call->rc_out = 0 (success) or BCVM_ERR_* (fail).
//            call->result_out = stack-top on success.
//
// Register map within the body:
//   x19 — call struct pointer (saved at entry, restored at exit)
//   x20 — code base
//   x21 — code_end (= code + code_len)
//   x22 — cpu_values base
//   x23 — cpu_count
//   x24 — tun_values base
//   x25 — tun_count
//   x10..x13 — 4-deep value stack (x10 = top)
//   w14 — stack depth (0..4)
//   x15 — instruction pointer (offset into code)
//   x0..x9 — scratch
//
// Push/pop are GAS macros so each invocation expands inline.
// The VM keeps no memory stack of its own; the function-level
// stp at entry is for the AAPCS-required save of x29/x30 +
// callee-saved x19..x25, which the boot.S step-4 caller will
// supply via __bootstrap_stack_top.
//
// Opcodes (must match memlayout.types.Opcode):
//   0x00 END
//   0x01 LIT       +4 bytes payload (u32 LE)
//   0x02 TUNING    +1 byte payload (field id)
//   0x03 CPU       +1 byte payload (field id)
//   0x04 MUL
//   0x05 DIV_LIT   +1 byte payload (u8 divisor)
//   0x06 ALIGN_UP
//   0x07 CALL_THUNK +4 bytes payload — asm side returns
//                   BCVM_ERR_THUNK_UNREGISTERED unconditionally
//                   (thunks are out of scope for step 3A).

.include "bcvm_errors.inc"

// ---- Macros: push value (in src reg), pop value (into dst reg)
//
// Push expands to: if depth == 4, jump to overflow; else
// shift x10..x13 down (x13←x12, x12←x11, x11←x10), then
// x10 = src, depth += 1.
//
// Pop expands to: if depth == 0, jump to underflow; else
// dst = x10, shift x10..x13 up (x10←x11, x11←x12, x12←x13,
// x13←0), depth -= 1.
//
// The shifts make every opcode's stack manipulation a
// fixed-cost sequence of mov instructions; modern AArch64
// OoO engines absorb them. Avoiding the shift would cost a
// per-opcode dispatch on depth (4-way jump table) which
// trades the shift for a branch — net wash.

.macro bcvm_push src
    cmp     w14, #4
    b.eq    .Lbcvm_stack_overflow
    mov     x13, x12
    mov     x12, x11
    mov     x11, x10
    mov     x10, \src
    add     w14, w14, #1
.endm

.macro bcvm_pop dst
    cbz     w14, .Lbcvm_stack_underflow
    mov     \dst, x10
    mov     x10, x11
    mov     x11, x12
    mov     x12, x13
    mov     x13, xzr
    sub     w14, w14, #1
.endm

// ---- Opcode constants

.equ OP_END,        0x00
.equ OP_LIT,        0x01
.equ OP_TUNING,     0x02
.equ OP_CPU,        0x03
.equ OP_MUL,        0x04
.equ OP_DIV_LIT,    0x05
.equ OP_ALIGN_UP,   0x06
.equ OP_CALL_THUNK, 0x07

// ---- Entry point

    .text
    .balign 4
    .globl  memlayout_run_bytecode
    .type   memlayout_run_bytecode, %function

memlayout_run_bytecode:
    stp     x29, x30, [sp, #-80]!
    stp     x19, x20, [sp, #16]
    stp     x21, x22, [sp, #32]
    stp     x23, x24, [sp, #48]
    str     x25,      [sp, #64]
    mov     x29, sp

    mov     x19, x0                     // call struct ptr

    ldr     x20, [x19, #0]              // code base
    ldr     x21, [x19, #8]              // code_len
    ldr     x22, [x19, #16]             // cpu_values
    ldr     x23, [x19, #24]             // cpu_count
    ldr     x24, [x19, #32]             // tun_values
    ldr     x25, [x19, #40]             // tun_count

    // Empty bytecode: code is null OR code_len is zero.
    cbz     x20, .Lbcvm_empty
    cbz     x21, .Lbcvm_empty

    add     x21, x21, x20               // x21 = code_end

    // Init stack and ip.
    mov     x10, xzr
    mov     x11, xzr
    mov     x12, xzr
    mov     x13, xzr
    mov     w14, wzr
    mov     x15, xzr

.Lbcvm_loop:
    add     x0, x20, x15                // current PC
    cmp     x0, x21
    b.hs    .Lbcvm_missing_end

    ldrb    w0, [x20, x15]
    add     x15, x15, #1

    cmp     w0, #OP_END
    b.eq    .Lbcvm_op_end
    cmp     w0, #OP_LIT
    b.eq    .Lbcvm_op_lit
    cmp     w0, #OP_TUNING
    b.eq    .Lbcvm_op_tuning
    cmp     w0, #OP_CPU
    b.eq    .Lbcvm_op_cpu
    cmp     w0, #OP_MUL
    b.eq    .Lbcvm_op_mul
    cmp     w0, #OP_DIV_LIT
    b.eq    .Lbcvm_op_div_lit
    cmp     w0, #OP_ALIGN_UP
    b.eq    .Lbcvm_op_align_up
    cmp     w0, #OP_CALL_THUNK
    b.eq    .Lbcvm_op_call_thunk
    mov     w0, #BCVM_ERR_UNKNOWN_OPCODE
    b       .Lbcvm_fail

// ---- LIT <u32 LE> ------------------------------------------------
.Lbcvm_op_lit:
    add     x0, x20, x15
    add     x0, x0, #4
    cmp     x0, x21
    b.hi    .Lbcvm_truncated
    ldrb    w0, [x20, x15]
    add     x15, x15, #1
    ldrb    w1, [x20, x15]
    add     x15, x15, #1
    orr     w0, w0, w1, lsl #8
    ldrb    w1, [x20, x15]
    add     x15, x15, #1
    orr     w0, w0, w1, lsl #16
    ldrb    w1, [x20, x15]
    add     x15, x15, #1
    orr     w0, w0, w1, lsl #24
    mov     x1, x0                      // zero-extend to x1
    bcvm_push x1
    b       .Lbcvm_loop

// ---- TUNING <u8> -------------------------------------------------
.Lbcvm_op_tuning:
    add     x0, x20, x15
    cmp     x0, x21
    b.hs    .Lbcvm_truncated
    ldrb    w0, [x20, x15]
    add     x15, x15, #1
    cmp     x0, x25
    b.hs    .Lbcvm_tuning_oor
    ldr     x1, [x24, x0, lsl #3]
    bcvm_push x1
    b       .Lbcvm_loop

// ---- CPU <u8> ----------------------------------------------------
.Lbcvm_op_cpu:
    add     x0, x20, x15
    cmp     x0, x21
    b.hs    .Lbcvm_truncated
    ldrb    w0, [x20, x15]
    add     x15, x15, #1
    cmp     x0, x23
    b.hs    .Lbcvm_cpu_oor
    ldr     x1, [x22, x0, lsl #3]
    bcvm_push x1
    b       .Lbcvm_loop

// ---- MUL ---------------------------------------------------------
.Lbcvm_op_mul:
    bcvm_pop x1                         // b
    bcvm_pop x2                         // a
    mul     x3, x2, x1                  // low 64
    umulh   x4, x2, x1                  // high 64; nonzero =
                                        // overflow per D056.
    cbnz    x4, .Lbcvm_mul_overflow
    bcvm_push x3
    b       .Lbcvm_loop

// ---- DIV_LIT <u8> ------------------------------------------------
.Lbcvm_op_div_lit:
    add     x0, x20, x15
    cmp     x0, x21
    b.hs    .Lbcvm_truncated
    ldrb    w0, [x20, x15]
    add     x15, x15, #1
    cbz     w0, .Lbcvm_div_zero
    bcvm_pop x1
    udiv    x1, x1, x0
    bcvm_push x1
    b       .Lbcvm_loop

// ---- ALIGN_UP ----------------------------------------------------
.Lbcvm_op_align_up:
    bcvm_pop x1                         // align
    bcvm_pop x2                         // value
    cbz     x1, .Lbcvm_align_zero
    sub     x3, x1, #1
    and     x3, x1, x3
    cbnz    x3, .Lbcvm_align_not_pow2
    sub     x3, x1, #1
    adds    x3, x2, x3                  // value + (align - 1)
    b.cs    .Lbcvm_align_up_overflow    // CS = unsigned carry
    sub     x4, x1, #1
    bic     x3, x3, x4                  // ~(align - 1) mask
    bcvm_push x3
    b       .Lbcvm_loop

// ---- CALL_THUNK <u32> --------------------------------------------
.Lbcvm_op_call_thunk:
    add     x0, x20, x15
    add     x0, x0, #4
    cmp     x0, x21
    b.hi    .Lbcvm_truncated
    mov     w0, #BCVM_ERR_THUNK_UNREGISTERED
    b       .Lbcvm_fail

// ---- END ---------------------------------------------------------
.Lbcvm_op_end:
    cbz     w14, .Lbcvm_end_empty
    cmp     w14, #1
    b.ne    .Lbcvm_end_multi
    str     wzr, [x19, #48]             // rc_out = 0
    str     x10, [x19, #56]             // result_out = top
    b       .Lbcvm_done

// ---- Failure tail. Each error site loads the code into w0
//      then falls into .Lbcvm_fail.
.Lbcvm_empty:
    mov     w0, #BCVM_ERR_EMPTY_BYTECODE
    b       .Lbcvm_fail
.Lbcvm_missing_end:
    mov     w0, #BCVM_ERR_MISSING_END
    b       .Lbcvm_fail
.Lbcvm_end_empty:
    mov     w0, #BCVM_ERR_END_EMPTY_STACK
    b       .Lbcvm_fail
.Lbcvm_end_multi:
    mov     w0, #BCVM_ERR_END_STACK_MULTI
    b       .Lbcvm_fail
.Lbcvm_truncated:
    mov     w0, #BCVM_ERR_TRUNCATED_PAYLOAD
    b       .Lbcvm_fail
.Lbcvm_stack_overflow:
    mov     w0, #BCVM_ERR_STACK_OVERFLOW
    b       .Lbcvm_fail
.Lbcvm_stack_underflow:
    mov     w0, #BCVM_ERR_STACK_UNDERFLOW
    b       .Lbcvm_fail
.Lbcvm_cpu_oor:
    mov     w0, #BCVM_ERR_CPU_FIELD_OOR
    b       .Lbcvm_fail
.Lbcvm_tuning_oor:
    mov     w0, #BCVM_ERR_TUNING_FIELD_OOR
    b       .Lbcvm_fail
.Lbcvm_div_zero:
    mov     w0, #BCVM_ERR_DIV_LIT_ZERO
    b       .Lbcvm_fail
.Lbcvm_align_zero:
    mov     w0, #BCVM_ERR_ALIGN_ZERO
    b       .Lbcvm_fail
.Lbcvm_align_not_pow2:
    mov     w0, #BCVM_ERR_ALIGN_NOT_POW2
    b       .Lbcvm_fail
.Lbcvm_mul_overflow:
    mov     w0, #BCVM_ERR_MUL_OVERFLOW
    b       .Lbcvm_fail
.Lbcvm_align_up_overflow:
    mov     w0, #BCVM_ERR_ALIGN_UP_OVERFLOW
    b       .Lbcvm_fail

.Lbcvm_fail:
    str     w0, [x19, #48]
    str     xzr, [x19, #56]
.Lbcvm_done:
    ldr     x25,      [sp, #64]
    ldp     x23, x24, [sp, #48]
    ldp     x21, x22, [sp, #32]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #80
    ret
    .size   memlayout_run_bytecode, . - memlayout_run_bytecode

    .section .note.GNU-stack, "", %progbits
