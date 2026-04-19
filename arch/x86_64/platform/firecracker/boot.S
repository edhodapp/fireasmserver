; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; x86_64 Firecracker guest — first step of the virtio-net L2 driver:
; the MMIO probe. PVH boot entry lands here in 32-bit protected mode
; with paging off and interrupts off (Xen PVH spec). A tiny stack is
; available but small; we use one level of call/ret only.
;
; Sequence:
;   1. Emit "READY\n" on COM1 (preserves the existing tracer-bullet
;      marker so downstream harnesses keep passing).
;   2. Read the 32-bit virtio-MMIO MagicValue register at the first
;      virtio device slot. Per Firecracker v1.15.1 x86_64 layout:
;        MMIO32_MEM_START  = FIRST_ADDR_PAST_32BITS - 1 GiB = 0xC0000000
;        MEM_32BIT_DEVICES = MMIO32_MEM_START + MMIO_LEN    = 0xC0001000
;      (source: Firecracker src/vmm/src/arch/x86_64/layout.rs)
;      The register value MUST be 0x74726976 ("virt" little-endian)
;      per Virtio 1.2 §4.2.2 — this is VIO-Q-001/§2.7 territory and
;      the first observable cross-check with the device.
;   3. Emit "VIRTIO:OK\n" on match, "VIRTIO:FAIL\n" on mismatch.
;   4. Halt.
;
; Assumes the Firecracker JSON config includes a network-interface
; entry (host_dev_name=tap0). If not, the MMIO read at 0xC0001000
; would produce a "MissingAddressRange" bus error and Firecracker
; would kill the guest — readable in its log, caught by the timeout.
;
; NASM per D048. A bare symbol as a MOV source is always immediate
; here; a bracketed symbol is always a memory reference. No OFFSET
; keyword — NASM is not MASM and does not emulate it.

XEN_ELFNOTE_PHYS32_ENTRY equ 18
COM1_PORT                equ 0x3F8
VIRTIO_MMIO_BASE         equ 0xC0001000
VIRTIO_MAGIC             equ 0x74726976   ; "virt" little-endian

; PVH ELF note. namesz=4 covers "Xen\0"; descsz=4 is the entry
; address that follows. NASM's `note` section-type directive emits
; SHT_NOTE directly, no ld name-heuristics needed.
section .note.Xen note alloc align=4
    dd 4
    dd 4
    dd XEN_ELFNOTE_PHYS32_ENTRY
    db 'Xen', 0
    align 4
    dd _start

section .text
[bits 32]

global _start
_start:
    ; PVH spec leaves ESP undefined on entry. Point it at 2 MiB, well
    ; above .text (ends < 0x100100) and inside the 128 MiB guest RAM,
    ; so our call/ret pairs have somewhere valid to push return
    ; addresses. Without this, the first CALL pushes to [0xfffffffc]
    ; (ESP=0 wraps), which is MMIO space past guest RAM — RET then
    ; reads garbage from there and jumps somewhere random.
    mov esp, 0x200000
    mov dx, COM1_PORT

    ; --- Emit "READY\n" ---
    lea esi, [ready_msg]
    mov ecx, ready_len
    call emit_bytes

    ; --- Probe virtio-MMIO MagicValue register ---
    ; MagicValue is a 32-bit MMIO read at VIRTIO_MMIO_BASE + 0x0.
    ; In 32-bit protected mode with paging off, we can dereference
    ; the physical address directly.
    mov eax, [VIRTIO_MMIO_BASE]
    cmp eax, VIRTIO_MAGIC
    jne .virtio_fail

    ; Match — announce and halt.
    lea esi, [virtio_ok_msg]
    mov ecx, virtio_ok_len
    call emit_bytes
    jmp .halt

.virtio_fail:
    lea esi, [virtio_fail_msg]
    mov ecx, virtio_fail_len
    call emit_bytes

.halt:
    cli
.halt_loop:
    hlt
    jmp .halt_loop

; --- Helper: write <ecx> bytes from <esi> to port in dx ---
; Caller preserves dx (already set). One-byte-at-a-time via the
; legacy serial port; the COM1 path is only used for diagnostic
; markers, not for product data, so tight-loop throughput is fine.
emit_bytes:
    test ecx, ecx
    jz .done
.next:
    mov al, [esi]
    out dx, al
    inc esi
    dec ecx
    jnz .next
.done:
    ret

; Diagnostic strings (in .text — small, and we don't need a separate
; .rodata section for 30 bytes of static data).
ready_msg:       db 'READY', 10
ready_len        equ $ - ready_msg

virtio_ok_msg:   db 'VIRTIO:OK', 10
virtio_ok_len    equ $ - virtio_ok_msg

virtio_fail_msg: db 'VIRTIO:FAIL', 10
virtio_fail_len  equ $ - virtio_fail_msg
