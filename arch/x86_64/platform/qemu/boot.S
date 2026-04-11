# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Ed Hodapp
#
# Minimal x86_64 QEMU guest stub for pipeline verification.
# Multiboot entry — QEMU boots this in 32-bit protected mode.
# Writes "READY\n" to serial port 0x3F8 (COM1) and halts.

.intel_syntax noprefix

.set MB_MAGIC, 0x1BADB002
.set MB_FLAGS, 0x0
.set MB_CHECKSUM, -(MB_MAGIC + MB_FLAGS)

.section .multiboot, "a"
.align 4
.long MB_MAGIC
.long MB_FLAGS
.long MB_CHECKSUM

.section .text
.code32
.globl _start

_start:
    # Serial port COM1 = 0x3F8
    mov dx, 0x3F8

    mov al, 'R'
    out dx, al

    mov al, 'E'
    out dx, al

    mov al, 'A'
    out dx, al

    mov al, 'D'
    out dx, al

    mov al, 'Y'
    out dx, al

    mov al, '\n'
    out dx, al

    cli
.loop:
    hlt
    jmp .loop
