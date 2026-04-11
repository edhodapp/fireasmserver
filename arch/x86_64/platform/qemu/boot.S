# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Ed Hodapp
#
# Minimal x86_64 QEMU guest stub for pipeline verification.
# Multiboot entry — QEMU boots this in 32-bit protected mode.
# Writes "READY\n" to serial port 0x3F8 (COM1) and halts.

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
    # In protected mode, use outb via 'out' instruction

    mov $'R', %al
    mov $0x3F8, %dx
    out %al, %dx

    mov $'E', %al
    out %al, %dx

    mov $'A', %al
    out %al, %dx

    mov $'D', %al
    out %al, %dx

    mov $'Y', %al
    out %al, %dx

    mov $'\n', %al
    out %al, %dx

    cli
.loop:
    hlt
    jmp .loop
