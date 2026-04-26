; SPDX-License-Identifier: AGPL-3.0-or-later
;
; Assembler-side equivalents of enum bcvm_err in bcvm_abi.h.
; NASM-syntax companion to bcvm_errors.inc (which is GAS).

%define BCVM_ERR_OK                 0
%define BCVM_ERR_EMPTY_BYTECODE     1
%define BCVM_ERR_MISSING_END        2
%define BCVM_ERR_END_EMPTY_STACK    3
%define BCVM_ERR_END_STACK_MULTI    4
%define BCVM_ERR_UNKNOWN_OPCODE     5
%define BCVM_ERR_TRUNCATED_PAYLOAD  6
%define BCVM_ERR_STACK_OVERFLOW     7
%define BCVM_ERR_STACK_UNDERFLOW    8
%define BCVM_ERR_VALUE_OUT_OF_U64   9
%define BCVM_ERR_CPU_FIELD_OOR      10
%define BCVM_ERR_TUNING_FIELD_OOR   11
%define BCVM_ERR_DIV_LIT_ZERO       12
%define BCVM_ERR_ALIGN_ZERO         13
%define BCVM_ERR_ALIGN_NOT_POW2     14
%define BCVM_ERR_MUL_OVERFLOW       15
%define BCVM_ERR_ALIGN_UP_OVERFLOW  16
%define BCVM_ERR_THUNK_UNREGISTERED 17
