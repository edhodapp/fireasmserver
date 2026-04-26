; SPDX-License-Identifier: AGPL-3.0-or-later
; x86_64 (NASM) .memreq macro definition. Step-1 plumbing for
; D059 + D060: emits a 48-byte fixed-layout record into the
; dedicated `.memreq` ELF section. The record carries every
; input the init-time bump allocator (D060 phase 2) needs to
; compute and assign an absolute address for the region:
; identity, owner, lifetime, writability, and bytecode
; expressions for size and alignment.
;
; At this point in the retrofit the macro is defined but no
; caller invokes it; the section therefore links as zero-size.
; Modules start declaring regions in step 5; the bytecode
; interpreter that consumes the records lands in step 3.
;
; Record layout (matches arch/aarch64/memory/memreq.inc and
; the Python reference at tooling/src/memlayout/reference.py
; once that lands in step 2):
;
;   offset  size  field
;   0       4     name_hash    fnv1a of region name (author-supplied,
;                              audit-verified per D060)
;   4       16    size_bc      size-expression bytecode (END-terminated)
;   20      8     align_bc     alignment-expression bytecode
;   28      2     owner_id     0 = boot core, 1..N-1 = worker core,
;                              0xFFFF = shared-readonly
;   30      1     lifetime     0 = steady_state, 1 = init_only,
;                              2 = immutable_after_init, 3 = stack
;   31      1     writable     0 = read-only after init, 1 = read/write
;   32      8     assigned_addr   filled in by allocator at init
;   40      8     assigned_size   filled in by allocator at init
;
; Total: 48 bytes, 8-byte aligned.

%ifndef FIREASM_MEMREQ_INC
%define FIREASM_MEMREQ_INC

%macro memreq 7
    ; args: %1=region, %2=name_hash,
    ;       %3=size_bytecode_label, %4=align_bytecode_label,
    ;       %5=owner, %6=lifetime, %7=writable
    ;
    ; size_bytecode_label and align_bytecode_label name labels
    ; whose .data block contains the END-terminated opcode stream.
    ; The macro copies up to 16 (size) / 8 (align) bytes from those
    ; labels into the record. (Real bytecode emission convention
    ; lands in step 3; for now the macro shape is fixed and the
    ; labels would just be empty placeholders if anyone called it.)

    [section .memreq progbits alloc noexec nowrite align=8]
    align 8
__memreq_rec__%1:
    dd      %2                     ; name_hash
    times 16 db 0                  ; size_bc placeholder (filled
                                   ; via incbin or a label-copy
                                   ; pattern when bytecode emission
                                   ; lands in step 3)
    times 8  db 0                  ; align_bc placeholder
    dw      %5                     ; owner_id
    db      %6                     ; lifetime
    db      %7                     ; writable
__memreq_assigned__%1:
    dq      0                      ; assigned_addr
    dq      0                      ; assigned_size
    __SECT__

    global  __memreq_addr__%1
    __memreq_addr__%1 equ __memreq_assigned__%1
    global  __memreq_size__%1
    __memreq_size__%1 equ __memreq_assigned__%1 + 8
%endmacro

%endif  ; FIREASM_MEMREQ_INC
