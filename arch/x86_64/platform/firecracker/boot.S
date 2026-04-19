; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; x86_64 Firecracker guest — initial driver path for the L2
; virtio-net bring-up. This IS the real driver in its first
; iteration ("tracer bullet" in Hunt & Thomas's sense: the
; foundational end-to-end skeleton that real features slot into),
; not a throwaway diagnostic. The `READY` and `VIRTIO:OK` markers
; stay as the permanent L2-init handshake for the pre-push
; integration tests.
;
; PVH boot entry lands here in 32-bit protected mode with paging off
; and interrupts off (Xen PVH spec). We establish a stack at 2 MiB,
; clear the direction flag, then run through the init sequence.
;
; Sequence:
;   1. Establish ESP, clear DF.
;   2. Emit "READY\n" on COM1 with LSR-polled UART writes (16550 THRE
;      gate, see `emit_bytes`).
;   3. Probe the virtio-MMIO MagicValue register at the first virtio
;      device slot with the memory barriers Virtio 1.2 §4.2.2 mandates.
;      Per Firecracker v1.15.1 x86_64 layout:
;        MMIO32_MEM_START  = FIRST_ADDR_PAST_32BITS - 1 GiB = 0xC0000000
;        MEM_32BIT_DEVICES = MMIO32_MEM_START + MMIO_LEN    = 0xC0001000
;      (source: Firecracker src/vmm/src/arch/x86_64/layout.rs)
;      The register value MUST be 0x74726976 ("virt" little-endian)
;      per Virtio 1.2 §4.2.2. This is VIO-Q-001/§2.7 territory and the
;      first observable cross-check with the device.
;   4. Emit "VIRTIO:OK\n" on match, "VIRTIO:FAIL\n" on mismatch.
;   5. Halt.
;
; Assumes the Firecracker JSON config includes a network-interface
; entry (host_dev_name=tap0). If not, the MMIO read at 0xC0001000
; produces a "MissingAddressRange" bus error and Firecracker kills
; the guest — readable in its log, caught by the timeout.
;
; NASM per D048.

XEN_ELFNOTE_PHYS32_ENTRY equ 18
COM1_PORT                equ 0x3F8
COM1_LSR_OFFSET          equ 5            ; LSR port = COM1_PORT + 5 = 0x3FD
UART_LSR_THRE            equ 0x20         ; Transmit Holding Register Empty
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
    ; (ESP=0 wraps) — MMIO space past guest RAM — and RET then reads
    ; garbage back.
    mov esp, 0x200000

    ; Clear DF. The x86-64 ABI expects DF=0 on entry to any function,
    ; and future edits that add `rep movs` / `lodsb` must not be
    ; surprised by an unexpected direction.
    cld

    mov dx, COM1_PORT

    ; --- Emit "READY\n" ---
    lea esi, [ready_msg]
    mov ecx, ready_len
    call emit_bytes

    ; --- Probe virtio-MMIO MagicValue register ---
    ; Virtio 1.2 §4.2.2: "The driver MUST use a memory barrier before
    ; and after each MMIO register access." On x86 TSO `mfence` is
    ; largely redundant for cacheable memory, but the spec makes no
    ; exceptions for strongly-ordered host ISAs. Compliance first,
    ; performance tuning later once the full driver exists.
    mfence
    mov eax, [VIRTIO_MMIO_BASE]
    mfence
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

; --- emit_bytes: write <ecx> bytes from <esi> to COM1 in <dx> ---
; Polls the 16550 Line Status Register for THRE (Transmit Holding
; Register Empty, bit 5) before each write. Firecracker's
; vm_superio::Serial happens to accept writes without ever deasserting
; THRE, but any stricter UART emulator (bochs, some QEMU serial
; backends) or real hardware will drop bytes without this gate.
; `dx` holds COM1 THR on entry and is restored to COM1 THR before
; return so subsequent callers can reuse it.
emit_bytes:
    test ecx, ecx
    jz .done
.next:
    add dx, COM1_LSR_OFFSET   ; dx = LSR port
.wait_thre:
    in  al, dx
    test al, UART_LSR_THRE
    jz  .wait_thre
    sub dx, COM1_LSR_OFFSET   ; dx = THR port (== COM1_PORT)
    mov al, [esi]
    out dx, al
    inc esi
    dec ecx
    jnz .next
.done:
    ret

ready_msg:       db 'READY', 10
ready_len        equ $ - ready_msg

virtio_ok_msg:   db 'VIRTIO:OK', 10
virtio_ok_len    equ $ - virtio_ok_msg

virtio_fail_msg: db 'VIRTIO:FAIL', 10
virtio_fail_len  equ $ - virtio_fail_msg
