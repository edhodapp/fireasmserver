# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Ed Hodapp
#
# Minimal x86_64 Firecracker guest stub for serial diagnostics.
# PVH boot entry — Firecracker's loader drops control here in
# 32-bit protected mode with paging off and interrupts off.
# Writes "READY\n" to serial port 0x3F8 (COM1) and halts.

.intel_syntax noprefix

.set XEN_ELFNOTE_PHYS32_ENTRY, 18

# PVH ELF Note. The loader scans PT_NOTE segments for type 18
# to discover the 32-bit physical entry point. namesz=4 covers
# "Xen\0"; descsz=4 is the entry address that follows. The
# @note section type is required so ld emits the section as
# SHT_NOTE rather than SHT_PROGBITS -- modern binutils infers
# this from the section name, but @note removes the dependency.
.section .note.Xen, "a", @note
.align 4
.long 4
.long 4
.long XEN_ELFNOTE_PHYS32_ENTRY
.asciz "Xen"
.long _start

.section .text
.code32
.globl _start

_start:
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
