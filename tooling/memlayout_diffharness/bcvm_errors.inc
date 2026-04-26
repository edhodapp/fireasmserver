// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Assembler-side equivalents of enum bcvm_err in bcvm_abi.h.
// Two files because GAS and NASM both need .equ-style
// constants in their own syntax; keeping the C enum and the
// assembler symbols in sync is enforced by the differential
// test harness (mismatched values would surface as wrong rc
// codes from the asm side).
//
// This file is GAS-syntax (aarch64). x86_64 NASM uses
// bcvm_errors.nasm.inc.

.equ BCVM_ERR_OK,                 0
.equ BCVM_ERR_EMPTY_BYTECODE,     1
.equ BCVM_ERR_MISSING_END,        2
.equ BCVM_ERR_END_EMPTY_STACK,    3
.equ BCVM_ERR_END_STACK_MULTI,    4
.equ BCVM_ERR_UNKNOWN_OPCODE,     5
.equ BCVM_ERR_TRUNCATED_PAYLOAD,  6
.equ BCVM_ERR_STACK_OVERFLOW,     7
.equ BCVM_ERR_STACK_UNDERFLOW,    8
.equ BCVM_ERR_VALUE_OUT_OF_U64,   9
.equ BCVM_ERR_CPU_FIELD_OOR,     10
.equ BCVM_ERR_TUNING_FIELD_OOR,  11
.equ BCVM_ERR_DIV_LIT_ZERO,      12
.equ BCVM_ERR_ALIGN_ZERO,        13
.equ BCVM_ERR_ALIGN_NOT_POW2,    14
.equ BCVM_ERR_MUL_OVERFLOW,      15
.equ BCVM_ERR_ALIGN_UP_OVERFLOW, 16
.equ BCVM_ERR_THUNK_UNREGISTERED, 17
