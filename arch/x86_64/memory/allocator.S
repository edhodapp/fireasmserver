; SPDX-License-Identifier: AGPL-3.0-or-later
;
; x86_64 (NASM) bump allocator over the .memreq table (D059
; + D060 step 3B). Calls into memlayout_run_bytecode (step
; 3A) to evaluate each record's size and alignment
; expressions, then forward-bumps non-stack lifetimes from
; heap_start / reverse-bumps stack lifetimes from ram_top.
; Differential-tested under user-mode QEMU against the
; Python reference at tooling/src/memlayout/reference.py —
; see tooling/tests/test_memlayout_alloc_diff.py.
;
; ABI (matches memlayout_call_t in bcvm_abi.h):
;   void memlayout_run_allocator(memlayout_call_t *call);
; On entry: rdi = pointer to memlayout_call_t.
; On return: call->rc_out =
;   0                            success
;   BCVM_ERR_*  (1..17)          inner-VM error
;   MEMLAYOUT_ERR_OVERFLOW (100) bump-pointer crossing
;   MEMLAYOUT_ERR_HEAP_TOP (101) heap_start > ram_top at entry
;   MEMLAYOUT_ERR_BAD_LIFETIME (102) lifetime byte not 0..3
;
; Stack frame: 80 bytes for the bcvm_call_t scratch +
; saved callee-saves. Reused across every per-record VM
; invocation.
;
; Register map within the body:
;   rbx — outer call struct pointer
;   r12 — current record pointer
;   r13 — memreq_end (loop bound)
;   r14 — forward bump
;   r15 — reverse bump
;   rbp — bcvm_call_t scratch base on local stack

%include "bcvm_errors.nasm.inc"

; -- memlayout-side error codes --
%define MEMLAYOUT_ERR_OVERFLOW     100
%define MEMLAYOUT_ERR_HEAP_TOP     101
%define MEMLAYOUT_ERR_BAD_LIFETIME 102

; -- Field offsets in memlayout_call_t --
%define ML_OFF_MEMREQ_START    0
%define ML_OFF_MEMREQ_END      8
%define ML_OFF_CPU_VALUES      16
%define ML_OFF_CPU_COUNT       24
%define ML_OFF_TUN_VALUES      32
%define ML_OFF_TUN_COUNT       40
%define ML_OFF_HEAP_START      48
%define ML_OFF_RAM_TOP         56
%define ML_OFF_RC_OUT          64
%define ML_OFF_FORWARD_OUT     72
%define ML_OFF_REVERSE_OUT     80

; -- Field offsets in each .memreq record --
%define MR_OFF_SIZE_BC         4
%define MR_OFF_ALIGN_BC        20
%define MR_OFF_LIFETIME        30
%define MR_OFF_ASSIGNED_ADDR   32
%define MR_OFF_ASSIGNED_SIZE   40
%define MR_RECORD_BYTES        48
%define MR_SIZE_BC_BYTES       16
%define MR_ALIGN_BC_BYTES      8

; -- bcvm_call_t field offsets --
%define BC_OFF_CODE        0
%define BC_OFF_CODE_LEN    8
%define BC_OFF_CPU_VALUES  16
%define BC_OFF_CPU_COUNT   24
%define BC_OFF_TUN_VALUES  32
%define BC_OFF_TUN_COUNT   40
%define BC_OFF_RC          48
%define BC_OFF_RESULT      56
%define BC_SIZE_BYTES      64

; Allocator-local scratch slot inside the 72-byte stack frame:
; the first 64 bytes hold the bcvm_call struct (field offsets
; 0..63 above); the next 8 bytes hold a per-record size cache
; while the second eval (align_bc) runs. Named constant rather
; than a hardcoded 0x40 to avoid visual-confusion bugs with
; BC_OFF_TUN_COUNT (40 decimal vs 0x40 = 64 hex).
%define ALLOC_SCRATCH_SIZE_OFF 64

; -- Lifetime values --
%define LT_STEADY    0
%define LT_INIT_ONLY 1
%define LT_IMMUT     2
%define LT_STACK     3

[bits 64]

section .text

extern memlayout_run_bytecode

global memlayout_run_allocator
memlayout_run_allocator:
    push    rbx
    push    rbp
    push    r12
    push    r13
    push    r14
    push    r15
    ; Reserve 64 bytes of scratch for the bcvm_call struct
    ; (BC_SIZE_BYTES). Stack alignment: entry rsp%16==8;
    ; six pushes → 8+48=56 → rsp%16==8; sub 64 → rsp%16==8.
    ; SysV calls require rsp%16==0 at the call site, so add
    ; an extra 8 to align before bl memlayout_run_bytecode.
    sub     rsp, 72                     ; 64 (struct) + 8 (align)

    mov     rbx, rdi                    ; outer call ptr
    mov     rbp, rsp                    ; bcvm scratch base

    mov     rax, [rbx + ML_OFF_HEAP_START]
    mov     rcx, [rbx + ML_OFF_RAM_TOP]
    cmp     rax, rcx
    ja      .Lalloc_heap_top

    mov     r14, rax                    ; forward bump
    mov     r15, rcx                    ; reverse bump

    ; Pre-fill the immutable fields of the bcvm scratch.
    mov     rax, [rbx + ML_OFF_CPU_VALUES]
    mov     [rbp + BC_OFF_CPU_VALUES], rax
    mov     rax, [rbx + ML_OFF_CPU_COUNT]
    mov     [rbp + BC_OFF_CPU_COUNT], rax
    mov     rax, [rbx + ML_OFF_TUN_VALUES]
    mov     [rbp + BC_OFF_TUN_VALUES], rax
    mov     rax, [rbx + ML_OFF_TUN_COUNT]
    mov     [rbp + BC_OFF_TUN_COUNT], rax

    ; ------- Pass 1: forward, non-stack lifetimes --------
    mov     r12, [rbx + ML_OFF_MEMREQ_START]
    mov     r13, [rbx + ML_OFF_MEMREQ_END]

.Lalloc_pass1_loop:
    cmp     r12, r13
    jae     .Lalloc_pass2_init

    movzx   eax, byte [r12 + MR_OFF_LIFETIME]
    cmp     eax, LT_STACK
    je      .Lalloc_pass1_skip
    cmp     eax, LT_IMMUT
    ja      .Lalloc_bad_lifetime

    ; Eval size_bc → push assigned_size in [r12 + 40]; need
    ; both size and align values for the bump arithmetic.
    lea     rdi, [r12 + MR_OFF_SIZE_BC]
    mov     rsi, MR_SIZE_BC_BYTES
    call    .Lalloc_eval_bc
    test    eax, eax
    jnz     .Lalloc_inner_fail
    mov     rcx, [rbp + BC_OFF_RESULT]      ; size into rcx
    mov     [rbp + ALLOC_SCRATCH_SIZE_OFF], rcx                ; stash at sp + ALLOC_SCRATCH_SIZE_OFF

    lea     rdi, [r12 + MR_OFF_ALIGN_BC]
    mov     rsi, MR_ALIGN_BC_BYTES
    call    .Lalloc_eval_bc
    test    eax, eax
    jnz     .Lalloc_inner_fail
    mov     rdx, [rbp + BC_OFF_RESULT]      ; align into rdx

    ; align positive power-of-two?
    test    rdx, rdx
    jz      .Lalloc_overflow
    lea     rsi, [rdx - 1]
    test    rdx, rsi
    jnz     .Lalloc_overflow

    ; align_up forward bump. Detect carry on the
    ; align-mask add per D056 (Gemini MEDIUM 2026-04-26).
    add     r14, rsi
    jc      .Lalloc_overflow
    not     rsi
    and     r14, rsi

    ; Bounds: forward + size <= ram_top. Detect carry on
    ; the addition (u64 wrap) AND > ram_top (Gemini HIGH
    ; 2026-04-26 — pure forward overflow was undetected
    ; in the absence of stack regions).
    mov     rcx, [rbp + ALLOC_SCRATCH_SIZE_OFF]
    add     r14, rcx
    jc      .Lalloc_overflow                 ; u64 wrap
    cmp     r14, [rbx + ML_OFF_RAM_TOP]
    ja      .Lalloc_overflow                 ; > ram_top

    ; Write back. r14 now holds forward + size; the assigned
    ; addr is forward - size = r14 - rcx.
    mov     rax, r14
    sub     rax, rcx
    mov     [r12 + MR_OFF_ASSIGNED_ADDR], rax
    mov     [r12 + MR_OFF_ASSIGNED_SIZE], rcx

.Lalloc_pass1_skip:
    add     r12, MR_RECORD_BYTES
    jmp     .Lalloc_pass1_loop

.Lalloc_pass2_init:
    ; ------- Pass 2: reverse, stack lifetimes -------
    mov     r12, [rbx + ML_OFF_MEMREQ_START]

.Lalloc_pass2_loop:
    cmp     r12, r13
    jae     .Lalloc_done

    movzx   eax, byte [r12 + MR_OFF_LIFETIME]
    cmp     eax, LT_STACK
    jne     .Lalloc_pass2_skip

    lea     rdi, [r12 + MR_OFF_SIZE_BC]
    mov     rsi, MR_SIZE_BC_BYTES
    call    .Lalloc_eval_bc
    test    eax, eax
    jnz     .Lalloc_inner_fail
    mov     rcx, [rbp + BC_OFF_RESULT]
    mov     [rbp + ALLOC_SCRATCH_SIZE_OFF], rcx

    lea     rdi, [r12 + MR_OFF_ALIGN_BC]
    mov     rsi, MR_ALIGN_BC_BYTES
    call    .Lalloc_eval_bc
    test    eax, eax
    jnz     .Lalloc_inner_fail
    mov     rdx, [rbp + BC_OFF_RESULT]

    test    rdx, rdx
    jz      .Lalloc_overflow
    lea     rsi, [rdx - 1]
    test    rdx, rsi
    jnz     .Lalloc_overflow

    ; size > reverse → underflow.
    mov     rcx, [rbp + ALLOC_SCRATCH_SIZE_OFF]
    cmp     rcx, r15
    ja      .Lalloc_overflow

    ; candidate = reverse - size, then align_down.
    mov     rax, r15
    sub     rax, rcx
    not     rsi                              ; ~(align - 1)
    and     rax, rsi

    ; cross-check vs forward.
    cmp     rax, r14
    jb      .Lalloc_overflow

    mov     [r12 + MR_OFF_ASSIGNED_ADDR], rax
    mov     [r12 + MR_OFF_ASSIGNED_SIZE], rcx
    mov     r15, rax                         ; reverse = new top

.Lalloc_pass2_skip:
    add     r12, MR_RECORD_BYTES
    jmp     .Lalloc_pass2_loop

.Lalloc_done:
    mov     dword [rbx + ML_OFF_RC_OUT], 0
    mov     [rbx + ML_OFF_FORWARD_OUT], r14
    mov     [rbx + ML_OFF_REVERSE_OUT], r15
    jmp     .Lalloc_epilogue

; ---- Inner helper: invoke memlayout_run_bytecode with the
; scratch struct's code and code_len pre-set.
;
; In:  rdi = code pointer (within the record)
;      rsi = code length
; Out: eax = bcvm_call.rc_out (0 on success)
; Clobbers: rax, rcx, rdx, rsi, rdi, r8..r11 (per SysV).
;
; Stack alignment at the inner call: caller's frame already
; has the +8 padding. After we push for our call, total
; offset becomes 16-aligned automatically.
.Lalloc_eval_bc:
    mov     [rbp + BC_OFF_CODE], rdi
    mov     [rbp + BC_OFF_CODE_LEN], rsi
    mov     rdi, rbp
    call    memlayout_run_bytecode wrt ..plt
    mov     eax, dword [rbp + BC_OFF_RC]
    ret

.Lalloc_inner_fail:
    mov     dword [rbx + ML_OFF_RC_OUT], eax
    mov     qword [rbx + ML_OFF_FORWARD_OUT], 0
    mov     qword [rbx + ML_OFF_REVERSE_OUT], 0
    jmp     .Lalloc_epilogue

.Lalloc_heap_top:
    mov     dword [rbx + ML_OFF_RC_OUT], MEMLAYOUT_ERR_HEAP_TOP
    mov     qword [rbx + ML_OFF_FORWARD_OUT], 0
    mov     qword [rbx + ML_OFF_REVERSE_OUT], 0
    jmp     .Lalloc_epilogue

.Lalloc_overflow:
    mov     dword [rbx + ML_OFF_RC_OUT], MEMLAYOUT_ERR_OVERFLOW
    mov     qword [rbx + ML_OFF_FORWARD_OUT], 0
    mov     qword [rbx + ML_OFF_REVERSE_OUT], 0
    jmp     .Lalloc_epilogue

.Lalloc_bad_lifetime:
    mov     dword [rbx + ML_OFF_RC_OUT], MEMLAYOUT_ERR_BAD_LIFETIME
    mov     qword [rbx + ML_OFF_FORWARD_OUT], 0
    mov     qword [rbx + ML_OFF_REVERSE_OUT], 0
    jmp     .Lalloc_epilogue

.Lalloc_epilogue:
    add     rsp, 72
    pop     r15
    pop     r14
    pop     r13
    pop     r12
    pop     rbp
    pop     rbx
    ret

section .note.GNU-stack noalloc noexec nowrite progbits
