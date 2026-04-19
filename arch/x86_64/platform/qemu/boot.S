; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; x86_64 QEMU guest — initial boot stub for Multiboot1.
; First iteration of the real driver path, not a throwaway. Writes
; "READY\n" on COM1 via LSR-polled UART writes and halts.
;
; Multiboot1 entry state (per spec):
;   CS: flat 32-bit code segment, base 0, limit 0xFFFFFFFF
;   DS, ES, FS, GS, SS: flat 32-bit data segment, same base/limit
;   ESP: undefined — we must establish our own
;   EFLAGS.DF: undefined — we clear it ourselves
;
; NASM per D048.

COM1_PORT         equ 0x3F8
COM1_LSR_OFFSET   equ 5            ; LSR port = COM1_PORT + 5 = 0x3FD
UART_LSR_THRE     equ 0x20         ; Transmit Holding Register Empty
UART_POLL_TIMEOUT equ 1 << 20      ; ~1 ms at modern CPU speeds

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
    mov esp, 0x200000   ; establish a stack (Multiboot1 leaves ESP undef)
    cld
    mov dx, COM1_PORT

    lea esi, [ready_msg]
    mov ecx, ready_len
    call emit_bytes

    cli
.halt:
    hlt
    jmp .halt

; --- emit_bytes: write <ecx> bytes from <esi> to COM1 in <dx> ---
; Polls 16550 LSR for THRE (bit 5) before each write. Same contract
; as the firecracker stub's emit_bytes. Mirrored here rather than
; factored to a shared include: at 20 instructions it's cheaper to
; duplicate than to add build-time include plumbing for two divergent
; ELF classes (ELF32 here, ELF64 on firecracker).
;
; Return signalling via the carry flag:
;   CF=0 on full success.
;   CF=1 on timeout (UART never asserted THRE within the budget).
;
; Clobbers: eax, ecx, esi, edi, flags. Preserves: dx, ebx, ebp, esp.
emit_bytes:
    test ecx, ecx
    jz .done
.next:
    add dx, COM1_LSR_OFFSET
    mov edi, UART_POLL_TIMEOUT
.wait_thre:
    in  al, dx
    test al, UART_LSR_THRE
    jnz .have_thre
    dec edi
    jnz .wait_thre
    ; Timeout: UART wedged. Restore dx, signal failure via CF=1,
    ; return so the guest halts cleanly instead of hanging.
    sub dx, COM1_LSR_OFFSET
    stc
    ret
.have_thre:
    sub dx, COM1_LSR_OFFSET
    mov al, [esi]
    out dx, al
    inc esi
    dec ecx
    jnz .next
.done:
    clc
    ret

ready_msg: db 'READY', 10
ready_len  equ $ - ready_msg
