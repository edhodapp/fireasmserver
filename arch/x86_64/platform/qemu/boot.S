; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; x86_64 QEMU guest — Multiboot1 boot stub.
;
; Per D062 + D063: validates Multiboot1 entry state in [bits 32],
; emits READY on COM1 via LSR-polled UART writes, then jumps to the
; shared mode_switch_to_long_mode body. Control never returns from
; the mode switch; it lands in long mode at kernel_main_64, which
; calls init_memory_layout (D060 step 4.2's wire-up) and halts.
;
; Multiboot1 entry state (per spec):
;   EAX: 0x2BADB002 (boot-loader magic — handed to the kernel)
;   EBX: 32-bit physical pointer to the multiboot info struct
;   CS:  flat 32-bit code segment, base 0, limit 0xFFFFFFFF
;   DS, ES, FS, GS, SS: flat 32-bit data segment, same base/limit
;   ESP: undefined — we establish our own
;   EFLAGS.DF: undefined — we clear it ourselves
;   Paging: off
;
; NASM per D048.

COM1_PORT         equ 0x3F8
COM1_LSR_OFFSET   equ 5            ; LSR port = COM1_PORT + 5 = 0x3FD
UART_LSR_THRE     equ 0x20         ; Transmit Holding Register Empty
UART_POLL_TIMEOUT equ 1 << 20      ; ~1 ms at modern CPU speeds

MB_MAGIC          equ 0x1BADB002   ; header magic (kernel image side)
MB_FLAGS          equ 0x0
MB_CHECKSUM       equ -(MB_MAGIC + MB_FLAGS)
MB_BOOTLOADER_EAX equ 0x2BADB002   ; loader-handed magic at _start

extern mode_switch_to_long_mode

section .multiboot alloc align=4
    dd MB_MAGIC
    dd MB_FLAGS
    dd MB_CHECKSUM

section .text
[bits 32]

global _start
_start:
    ; Defensive: Multiboot1 spec §3.2 says interrupts are disabled
    ; at handoff, but a non-compliant chainloader could violate
    ; that. cli costs one byte and removes the entire class of
    ; "interrupt fires before any state is set up" failure modes.
    cli

    ; Establish ESP. Multiboot1 leaves it undefined; the
    ; mode_switch_to_long_mode body uses cli/cld defensively
    ; but still reads/writes to whatever stack the caller provides
    ; (e.g., for any future debug emit before page-table writes).
    ; 2 MiB is well above the loaded image (.text < 0x101000) and
    ; below the bootstrap stack reservation that mode_switch
    ; itself reloads RSP from after entering long mode.
    mov esp, 0x200000
    cld

    ; Validate Multiboot1 magic. EAX must be 0x2BADB002 per spec
    ; §3.2; anything else means we were entered by something other
    ; than a Multiboot1 loader and the rest of the boot contract is
    ; void. Emit MULTIBOOT:FAIL and halt rather than blunder into
    ; the mode switch with unknown state. We don't dump the
    ; observed EAX (would need a hex-emit helper not currently in
    ; this file); the static marker is sufficient for the rare
    ; "loader handed wrong magic" case.
    mov dx, COM1_PORT
    cmp eax, MB_BOOTLOADER_EAX
    jne .multiboot_fail

    ; EBX should hold the multiboot info struct pointer; we don't
    ; consume it at stage 1 (no command-line / module parsing yet),
    ; but a future task that does will pick it up before the mode
    ; switch destroys the [bits 32] register state. Today: ignored.

    ; Emit READY — observable by the tracer-bullet harness as the
    ; "boot reached the kernel image" marker. Lives in [bits 32]
    ; because emit_bytes uses 32-bit addressing and INs/OUTs that
    ; would need full retooling under [bits 64]; LAYOUT-OK (task #7)
    ; will emit from a separate [bits 64] writer post-mode-switch.
    lea esi, [ready_msg]
    mov ecx, ready_len
    call emit_bytes

    ; Hand off to the shared mode-switch body. JMP not CALL: the
    ; mode switch invalidates the 32-bit return address, so we
    ; never return to this stack frame. Control reaches
    ; kernel_main_64 in [bits 64], which calls init_memory_layout
    ; and halts.
    jmp mode_switch_to_long_mode

.multiboot_fail:
    ; Bad magic. Emit a diagnostic line so a developer running
    ; the cell manually can see what went wrong, then cli/hlt.
    lea esi, [multiboot_fail_msg]
    mov ecx, multiboot_fail_len
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

multiboot_fail_msg: db 'MULTIBOOT:FAIL bad magic', 10
multiboot_fail_len  equ $ - multiboot_fail_msg
