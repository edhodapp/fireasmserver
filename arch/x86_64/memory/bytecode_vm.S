; SPDX-License-Identifier: AGPL-3.0-or-later
;
; x86_64 (NASM) bytecode VM for D060 layer-3 size/align
; expressions. Differential-tested under user-mode QEMU
; against the Python reference at tooling/src/memlayout/. Both
; must agree on every (bytecode, cpu_values, tuning_values)
; input — see tooling/tests/test_memlayout_diff.py.
;
; ABI (matches tooling/memlayout_diffharness/bcvm_abi.h):
;   void memlayout_run_bytecode(bcvm_call_t *call);
; On entry: rdi = pointer to bcvm_call_t.
; On return: call->rc_out = 0 (success) or BCVM_ERR_* (fail).
;            call->result_out = stack-top on success.
;
; Register map within the body — chosen so that MUL's implicit
; rdx clobber and DIV_LIT's rsi/rdi temp use never touch the
; value stack:
;   Callee-saved (preserved across the call):
;     rbx — call struct pointer
;     rbp — tun_values base
;     r12 — code base
;     r13 — code_end (= code + code_len)
;     r14 — cpu_values base
;     r15 — value stack slot 3 (bottom)
;   Caller-saved:
;     r11 — value stack top (slot 0)
;     r10 — value stack slot 1
;     r9  — value stack slot 2
;     ecx — stack depth (0..4)
;     r8  — instruction pointer (offset into code)
;     rax, rdi, rsi, rdx — scratch (mul/div implicitly use
;                          rdx and rax; both are kept fully
;                          out of the stack).
;   Memory-access:
;     cpu_count via qword [rbx + 24]
;     tun_count via qword [rbx + 40]
;   (only consulted in CPU/TUNING opcodes; no caching needed.)
;
; This reorg fixes the HIGH bug from the first attempt: previous
; layout had rdx as stack slot 1 and rsi as stack slot 2; `mul`
; would clobber rdx mid-MUL, corrupting any value at depth≥2;
; `div`'s divisor temp aliased rsi the same way. Random Hypothesis
; bytecode rarely exercises the depth≥3-then-MUL shape, so the
; bug slipped past 100 random vectors per arch and 25 hand
; vectors. New layout keeps mul/div scratch in registers that
; are never simultaneously stack slots.
;
; Pre-stack-safe within the body — no push/pop instructions, no
; call/ret. The function-level pushes at entry follow the SysV
; ABI for callee-saved registers; the boot.S step-4 caller will
; supply __bootstrap_stack_top for that frame save.
;
; Opcodes (must match memlayout.types.Opcode):
;   0x00 END
;   0x01 LIT       +4 bytes payload (u32 LE)
;   0x02 TUNING    +1 byte payload (field id)
;   0x03 CPU       +1 byte payload (field id)
;   0x04 MUL
;   0x05 DIV_LIT   +1 byte payload (u8 divisor)
;   0x06 ALIGN_UP
;   0x07 CALL_THUNK +4 bytes payload — asm side returns
;                   BCVM_ERR_THUNK_UNREGISTERED unconditionally
;                   (thunks are out of scope for step 3A).

%include "bcvm_errors.nasm.inc"

; ---- 4-deep value stack: r11 (top), r10, r9, r15 (bottom).

%macro bcvm_push 1
    cmp     ecx, 4
    je      .Lbcvm_stack_overflow
    mov     r15, r9
    mov     r9,  r10
    mov     r10, r11
    mov     r11, %1
    inc     ecx
%endmacro

%macro bcvm_pop 1
    test    ecx, ecx
    jz      .Lbcvm_stack_underflow
    mov     %1,  r11
    mov     r11, r10
    mov     r10, r9
    mov     r9,  r15
    xor     r15d, r15d
    dec     ecx
%endmacro

; ---- Opcode constants

%define OP_END        0x00
%define OP_LIT        0x01
%define OP_TUNING     0x02
%define OP_CPU        0x03
%define OP_MUL        0x04
%define OP_DIV_LIT    0x05
%define OP_ALIGN_UP   0x06
%define OP_CALL_THUNK 0x07

; ---- Entry point

[bits 64]

section .text

global memlayout_run_bytecode
memlayout_run_bytecode:
    push    rbx
    push    rbp
    push    r12
    push    r13
    push    r14
    push    r15

    mov     rbx, rdi                    ; call struct ptr

    mov     r12, [rbx + 0]              ; code base
    mov     r13, [rbx + 8]              ; code_len
    mov     r14, [rbx + 16]             ; cpu_values
    mov     rbp, [rbx + 32]             ; tun_values

    ; Empty bytecode: code is null OR code_len is zero.
    test    r12, r12
    jz      .Lbcvm_empty
    test    r13, r13
    jz      .Lbcvm_empty

    add     r13, r12                    ; r13 = code_end

    ; Init stack and ip.
    xor     r11d, r11d
    xor     r10d, r10d
    xor     r9d,  r9d
    xor     r15d, r15d
    xor     ecx,  ecx
    xor     r8d,  r8d                   ; r8 = ip = 0

.Lbcvm_loop:
    ; Bounds: code + ip < code_end? else missing-END.
    lea     rax, [r12 + r8]
    cmp     rax, r13
    jae     .Lbcvm_missing_end

    movzx   eax, byte [r12 + r8]
    inc     r8

    cmp     eax, OP_END
    je      .Lbcvm_op_end
    cmp     eax, OP_LIT
    je      .Lbcvm_op_lit
    cmp     eax, OP_TUNING
    je      .Lbcvm_op_tuning
    cmp     eax, OP_CPU
    je      .Lbcvm_op_cpu
    cmp     eax, OP_MUL
    je      .Lbcvm_op_mul
    cmp     eax, OP_DIV_LIT
    je      .Lbcvm_op_div_lit
    cmp     eax, OP_ALIGN_UP
    je      .Lbcvm_op_align_up
    cmp     eax, OP_CALL_THUNK
    je      .Lbcvm_op_call_thunk
    mov     eax, BCVM_ERR_UNKNOWN_OPCODE
    jmp     .Lbcvm_fail

; ---- LIT <u32 LE> ------------------------------------------------
.Lbcvm_op_lit:
    lea     rax, [r12 + r8]
    add     rax, 4
    cmp     rax, r13
    ja      .Lbcvm_truncated
    mov     eax, dword [r12 + r8]       ; LE u32 read (single
                                        ; load — x86_64 supports
                                        ; unaligned 32-bit loads)
    add     r8, 4
    mov     rdi, rax                    ; zero-extend to 64
    bcvm_push rdi
    jmp     .Lbcvm_loop

; ---- TUNING <u8> -------------------------------------------------
.Lbcvm_op_tuning:
    lea     rax, [r12 + r8]
    cmp     rax, r13
    jae     .Lbcvm_truncated
    movzx   eax, byte [r12 + r8]
    inc     r8
    cmp     rax, [rbx + 40]             ; tun_count (memory)
    jae     .Lbcvm_tuning_oor
    mov     rdi, [rbp + rax * 8]
    bcvm_push rdi
    jmp     .Lbcvm_loop

; ---- CPU <u8> ----------------------------------------------------
.Lbcvm_op_cpu:
    lea     rax, [r12 + r8]
    cmp     rax, r13
    jae     .Lbcvm_truncated
    movzx   eax, byte [r12 + r8]
    inc     r8
    cmp     rax, [rbx + 24]             ; cpu_count (memory)
    jae     .Lbcvm_cpu_oor
    mov     rdi, [r14 + rax * 8]
    bcvm_push rdi
    jmp     .Lbcvm_loop

; ---- MUL ---------------------------------------------------------
; Pop b → rdi, pop a → rax. mul rdi: rdx:rax = a * b. Overflow
; iff rdx != 0 (per D056). Push rax. rdx is scratch, never a
; stack slot — clobber is harmless.
.Lbcvm_op_mul:
    bcvm_pop rdi                        ; b
    bcvm_pop rax                        ; a (must be in rax)
    mul     rdi                         ; rdx:rax = rax * rdi
    test    rdx, rdx
    jnz     .Lbcvm_mul_overflow
    bcvm_push rax
    jmp     .Lbcvm_loop

; ---- DIV_LIT <u8> ------------------------------------------------
; Read divisor (rax). Pop dividend → rdi. xchg rax, rdi puts
; dividend in rax + divisor in rdi. xor edx, edx zeroes the
; high half. div rdi: rax = quotient. rdx and rdi are scratch,
; never stack slots.
.Lbcvm_op_div_lit:
    lea     rax, [r12 + r8]
    cmp     rax, r13
    jae     .Lbcvm_truncated
    movzx   eax, byte [r12 + r8]
    inc     r8
    test    al, al
    jz      .Lbcvm_div_zero
    bcvm_pop rdi                        ; dividend
    xchg    rax, rdi                    ; rax=dividend, rdi=div
    xor     edx, edx
    div     rdi
    bcvm_push rax
    jmp     .Lbcvm_loop

; ---- ALIGN_UP ----------------------------------------------------
.Lbcvm_op_align_up:
    bcvm_pop rdi                        ; align
    bcvm_pop rax                        ; value
    test    rdi, rdi
    jz      .Lbcvm_align_zero
    lea     rsi, [rdi - 1]              ; mask = align - 1
    test    rdi, rsi
    jnz     .Lbcvm_align_not_pow2
    add     rax, rsi                    ; value + (align - 1)
    jc      .Lbcvm_align_up_overflow    ; D056 carry-detect
    not     rsi                         ; ~(align - 1)
    and     rax, rsi
    bcvm_push rax
    jmp     .Lbcvm_loop

; ---- CALL_THUNK <u32> --------------------------------------------
.Lbcvm_op_call_thunk:
    lea     rax, [r12 + r8]
    add     rax, 4
    cmp     rax, r13
    ja      .Lbcvm_truncated
    mov     eax, BCVM_ERR_THUNK_UNREGISTERED
    jmp     .Lbcvm_fail

; ---- END ---------------------------------------------------------
.Lbcvm_op_end:
    test    ecx, ecx
    jz      .Lbcvm_end_empty
    cmp     ecx, 1
    jne     .Lbcvm_end_multi
    mov     dword [rbx + 48], 0          ; rc_out = 0
    mov     [rbx + 56], r11              ; result_out = top
    jmp     .Lbcvm_done

; ---- Failure tail. Each error site loads the code into eax
;      then falls into .Lbcvm_fail.
.Lbcvm_empty:
    mov     eax, BCVM_ERR_EMPTY_BYTECODE
    jmp     .Lbcvm_fail
.Lbcvm_missing_end:
    mov     eax, BCVM_ERR_MISSING_END
    jmp     .Lbcvm_fail
.Lbcvm_end_empty:
    mov     eax, BCVM_ERR_END_EMPTY_STACK
    jmp     .Lbcvm_fail
.Lbcvm_end_multi:
    mov     eax, BCVM_ERR_END_STACK_MULTI
    jmp     .Lbcvm_fail
.Lbcvm_truncated:
    mov     eax, BCVM_ERR_TRUNCATED_PAYLOAD
    jmp     .Lbcvm_fail
.Lbcvm_stack_overflow:
    mov     eax, BCVM_ERR_STACK_OVERFLOW
    jmp     .Lbcvm_fail
.Lbcvm_stack_underflow:
    mov     eax, BCVM_ERR_STACK_UNDERFLOW
    jmp     .Lbcvm_fail
.Lbcvm_cpu_oor:
    mov     eax, BCVM_ERR_CPU_FIELD_OOR
    jmp     .Lbcvm_fail
.Lbcvm_tuning_oor:
    mov     eax, BCVM_ERR_TUNING_FIELD_OOR
    jmp     .Lbcvm_fail
.Lbcvm_div_zero:
    mov     eax, BCVM_ERR_DIV_LIT_ZERO
    jmp     .Lbcvm_fail
.Lbcvm_align_zero:
    mov     eax, BCVM_ERR_ALIGN_ZERO
    jmp     .Lbcvm_fail
.Lbcvm_align_not_pow2:
    mov     eax, BCVM_ERR_ALIGN_NOT_POW2
    jmp     .Lbcvm_fail
.Lbcvm_mul_overflow:
    mov     eax, BCVM_ERR_MUL_OVERFLOW
    jmp     .Lbcvm_fail
.Lbcvm_align_up_overflow:
    mov     eax, BCVM_ERR_ALIGN_UP_OVERFLOW
    jmp     .Lbcvm_fail

.Lbcvm_fail:
    mov     dword [rbx + 48], eax
    mov     qword [rbx + 56], 0
.Lbcvm_done:
    pop     r15
    pop     r14
    pop     r13
    pop     r12
    pop     rbp
    pop     rbx
    ret

section .note.GNU-stack noalloc noexec nowrite progbits
