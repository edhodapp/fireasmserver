; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; Minimal x86_64 QEMU guest stub for pipeline verification.
; Multiboot1 entry — QEMU boots this in 32-bit protected mode on
; -machine pc after SeaBIOS hands control over.
; Writes "READY\n" to serial port 0x3F8 (COM1) and halts.
;
; NASM per D048.

MB_MAGIC    equ 0x1BADB002
MB_FLAGS    equ 0x0
MB_CHECKSUM equ -(MB_MAGIC + MB_FLAGS)

section .multiboot alloc align=4
    dd MB_MAGIC
    dd MB_FLAGS
    dd MB_CHECKSUM

section .text
[bits 32]

global _start
_start:
    ; Serial port COM1 = 0x3F8
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

    mov al, 10       ; '\n'
    out dx, al

    cli
.loop:
    hlt
    jmp .loop
