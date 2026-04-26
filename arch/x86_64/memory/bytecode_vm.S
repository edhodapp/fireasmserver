; SPDX-License-Identifier: AGPL-3.0-or-later
;
; x86_64 (NASM) bytecode VM for D060 layer-3 size/align
; expressions. Differential-tested under user-mode QEMU
; against the Python reference at tooling/src/memlayout/. Both
; must agree on every (bytecode, cpu_values, tuning_values)
; input — see tooling/tests/test_memlayout_diff_x86_64.py.
;
; ABI (matches tooling/memlayout_diffharness/bcvm_abi.h):
;   void memlayout_run_bytecode(bcvm_call_t *call);
; On entry: rdi = pointer to bcvm_call_t.
; On return: call->rc_out = 0 (success) or BCVM_ERR_* (fail).
;            call->result_out = stack-top on success.
;
; Register map within the body:
;   rbx — call struct pointer (callee-saved; saved at entry)
;   r12 — code base
;   r13 — code_end (= code + code_len)
;   r14 — cpu_values base
;   r15 — cpu_count
;   rbp — tun_values base (deliberately repurposed from frame
;                          ptr; we don't establish a frame)
;   r10 — tun_count
;   r11..r9 — value stack (r11 = top, r9 = bottom; only 4 slots)
;   ecx — stack depth (0..4)
;   r8  — instruction pointer (offset into code)
;   rax — scratch / return-register convention sink
;
; Push/pop are NASM macros so each invocation expands inline.
; The VM keeps no memory stack of its own at steady state;
; the function-level pushes at entry are the AAPCS-equivalent
; SysV ABI save of callee-saved registers, which the boot.S
; step-4 caller will supply via __bootstrap_stack_top.
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

; ---- 4-deep value stack via dedicated registers.
;
; Slots: r11 (top), rdx, rsi, r9 (bottom).
; Depth: ecx.
;
; Push expands to: if ecx == 4, jump to overflow; else
; r9 ← rsi, rsi ← rdx, rdx ← r11, r11 ← src; ecx += 1.
;
; Pop expands to: if ecx == 0, jump to underflow; else
; dst ← r11, r11 ← rdx, rdx ← rsi, rsi ← r9, r9 ← 0; ecx -= 1.

%macro bcvm_push 1
    cmp     ecx, 4
    je      .Lbcvm_stack_overflow
    mov     r9,  rsi
    mov     rsi, rdx
    mov     rdx, r11
    mov     r11, %1
    inc     ecx
%endmacro

%macro bcvm_pop 1
    test    ecx, ecx
    jz      .Lbcvm_stack_underflow
    mov     %1,  r11
    mov     r11, rdx
    mov     rdx, rsi
    mov     rsi, r9
    xor     r9d, r9d
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
    mov     r15, [rbx + 24]             ; cpu_count
    mov     rbp, [rbx + 32]             ; tun_values
    mov     r10, [rbx + 40]             ; tun_count

    ; Empty bytecode: code is null OR code_len is zero.
    test    r12, r12
    jz      .Lbcvm_empty
    test    r13, r13
    jz      .Lbcvm_empty

    add     r13, r12                    ; r13 = code_end

    ; Init stack and ip.
    xor     r11d, r11d
    xor     edx,  edx
    xor     esi,  esi
    xor     r9d,  r9d
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
    mov     eax, dword [r12 + r8]       ; LE u32 read
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
    cmp     rax, r10
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
    cmp     rax, r15
    jae     .Lbcvm_cpu_oor
    mov     rdi, [r14 + rax * 8]
    bcvm_push rdi
    jmp     .Lbcvm_loop

; ---- MUL ---------------------------------------------------------
; Pop b → rdi, pop a → rax, mul rdi (rdx:rax = a*b). Overflow
; if rdx != 0 (per D056). Push rax.
.Lbcvm_op_mul:
    bcvm_pop rdi                        ; b
    bcvm_pop rax                        ; a (must be in rax for mul)
    mul     rdi                         ; rdx:rax = rax * rdi
    test    rdx, rdx
    jnz     .Lbcvm_mul_overflow
    bcvm_push rax
    jmp     .Lbcvm_loop

; ---- DIV_LIT <u8> ------------------------------------------------
.Lbcvm_op_div_lit:
    lea     rax, [r12 + r8]
    cmp     rax, r13
    jae     .Lbcvm_truncated
    movzx   eax, byte [r12 + r8]
    inc     r8
    test    al, al
    jz      .Lbcvm_div_zero
    bcvm_pop rdi                        ; dividend
    mov     rsi, rax                    ; save divisor (rsi
                                        ; was top-1 before the
                                        ; pop, but the pop
                                        ; already shifted, so
                                        ; rsi is free to reuse
                                        ; here)
    mov     rax, rdi
    xor     edx, edx                    ; rdx:rax / rsi
    div     rsi
    bcvm_push rax
    jmp     .Lbcvm_loop

; ---- ALIGN_UP ----------------------------------------------------
.Lbcvm_op_align_up:
    bcvm_pop rdi                        ; align
    bcvm_pop rax                        ; value
    test    rdi, rdi
    jz      .Lbcvm_align_zero
    lea     r10, [rdi - 1]              ; align - 1 (TEMP; we
                                        ; reload r10/tun_count
                                        ; from the struct on
                                        ; tuning ops, but here
                                        ; we never touch
                                        ; tun_count again before
                                        ; loop iter ends)
    test    rdi, r10
    jnz     .Lbcvm_align_not_pow2
    add     rax, r10                    ; value + (align - 1)
    jc      .Lbcvm_align_up_overflow    ; CF on unsigned ovf
    not     r10                         ; ~(align - 1)
    and     rax, r10
    bcvm_push rax
    ; r10 was tun_count; restore from struct.
    mov     r10, [rbx + 40]
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

; ---- Failure tail ------------------------------------------------
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
