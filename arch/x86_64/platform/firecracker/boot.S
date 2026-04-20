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
;      gate, see `emit_bytes`). LSR polling has a ~1M-iteration timeout
;      so a wedged UART emulator doesn't hang the guest.
;   3. Probe the virtio-MMIO MagicValue register at the first virtio
;      device slot with the memory barriers Virtio 1.2 §4.2.2 mandates.
;      Per Firecracker v1.15.1 x86_64 layout:
;        MMIO32_MEM_START  = FIRST_ADDR_PAST_32BITS - 1 GiB = 0xC0000000
;        MEM_32BIT_DEVICES = MMIO32_MEM_START + MMIO_LEN    = 0xC0001000
;      (source: Firecracker src/vmm/src/arch/x86_64/layout.rs)
;      The register value MUST be 0x74726976 ("virt" little-endian)
;      per Virtio 1.2 §4.2.2. This is VIO-Q-001/§2.7 territory and the
;      first observable cross-check with the device.
;   4. Emit "VIRTIO:OK\n" on match, or "VIRTIO:FAIL magic=XXXXXXXX\n"
;      on mismatch — printing the actual magic is the first thing a
;      debugger reaches for, so it goes in now.
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
UART_POLL_TIMEOUT        equ 1 << 20      ; ~1 ms at modern CPU speeds
VIRTIO_MMIO_BASE         equ 0xC0001000
VIRTIO_MAGIC             equ 0x74726976   ; "virt" little-endian

; Virtio-MMIO register offsets (Virtio 1.2 §4.2.2, Table 4.1).
; Full layout isn't needed yet — only the Status register for the
; VIO-001..003 init prefix. More offsets land with feature
; negotiation (VIO-004..006) and virtqueue setup (VIO-007).
VIRTIO_MMIO_STATUS       equ 0x070

; Device Status bit values (Virtio 1.2 §2.1, "Device Status Field").
; Bit-position numbering in the REQUIREMENTS.md VIO table is the
; bit index; the MMIO Status register is written with OR-ed values.
VIRTIO_STATUS_ACKNOWLEDGE equ 1   ; bit 0 — driver found the device
VIRTIO_STATUS_DRIVER      equ 2   ; bit 1 — driver knows how to use it

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
    ; above .text (ends < 0x100200) and inside the 128 MiB guest RAM,
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
    ; and after each MMIO register access." On x86 TSO `lfence` is
    ; sufficient around a pure load — stores need `mfence`, loads need
    ; `lfence`. We only read here, so `lfence` satisfies the spec
    ; without the store-ordering cost `mfence` carries. When ring-ops
    ; (descriptor writes) land, the barrier choice becomes `mfence`
    ; at each store+load boundary; handle case-by-case.
    lfence
    mov eax, [VIRTIO_MMIO_BASE]
    lfence
    cmp eax, VIRTIO_MAGIC
    jne .virtio_fail

    ; Match — announce.
    lea esi, [virtio_ok_msg]
    mov ecx, virtio_ok_len
    call emit_bytes

    ; --- Device-status init prefix (VIO-001, VIO-002, VIO-003) ---
    ; Virtio 1.2 §2.1.2 steps 1-3: the driver (a) resets the device by
    ; writing 0 to Status, (b) sets ACKNOWLEDGE, (c) sets DRIVER.
    ; Later steps (features, FEATURES_OK, virtqueue init, DRIVER_OK)
    ; live in follow-up commits — see docs/l2/REQUIREMENTS.md §4.1
    ; VIO-004..009.
    ;
    ; §2.1 "Device Status only supports setting bits, not clearing
    ; them; clearing can be done only by resetting the device" — so
    ; the ACK/DRIVER steps are read-modify-write with OR, not blind
    ; stores that would trash other (spurious) bits.
    ;
    ; §4.2.2.2 mandates a memory barrier before and after each MMIO
    ; register access. The existing MagicValue probe above (lines
    ; 93-102) documents the x86-TSO barrier choice: lfence for
    ; loads, mfence for stores. This block follows the same pattern.
    ; The mfence between a register-only `or` and the subsequent
    ; store is redundant on x86 TSO — the preceding lfence already
    ; serializes the prior load — but keeping it matches the literal
    ; "before AND after each MMIO access" spec phrasing and the
    ; MagicValue probe's style. Cost is zero on this init-only path.

    ; VIO-001: reset device (write 0 to Status).
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS], 0
    mfence

    ; VIO-002: read-modify-write ACKNOWLEDGE.
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_ACKNOWLEDGE
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS], eax
    mfence

    ; VIO-003: read-modify-write DRIVER.
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_DRIVER
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS], eax
    mfence

    ; Confirm both bits stuck and nothing else crept in. Direct
    ; equality compare (not a masked one) catches FAILED (§2.1.2
    ; lists it as an error signal the driver must honor) and any
    ; other bit the device set unexpectedly mid-RMW.
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    cmp eax, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER
    jne .status_fail

    lea esi, [status_driver_msg]
    mov ecx, status_driver_len
    call emit_bytes
    jmp .halt

.status_fail:
    ; eax holds the masked status value (ACK|DRIVER bits only). Emit
    ; the marker and the observed value so a debugger sees exactly
    ; what the device reported.
    push eax
    lea esi, [status_fail_msg]
    mov ecx, status_fail_len
    call emit_bytes
    pop eax
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .halt

.virtio_fail:
    ; eax holds the actual magic value. Print "VIRTIO:FAIL magic=" then
    ; the eight hex digits, then "\n". Saves a lot of time versus
    ; having to bisect from the VMM log that the address was wrong.
    push eax                        ; preserve magic across emit_bytes
    lea esi, [virtio_fail_msg]
    mov ecx, virtio_fail_len
    call emit_bytes
    pop eax
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes

.halt:
    cli
.halt_loop:
    hlt
    jmp .halt_loop

; --- emit_bytes: write <ecx> bytes from <esi> to COM1 in <dx> ---
; Polls the 16550 Line Status Register for THRE (Transmit Holding
; Register Empty, bit 5) before each write, with a ~1M-iteration
; timeout so a wedged UART doesn't hang the guest. On timeout,
; abandons the remaining stream. `dx` holds COM1 THR on entry and
; is restored to COM1 THR before return so subsequent callers can
; reuse it.
;
; Return signalling via the carry flag:
;   CF=0 on full success (all <ecx> bytes written, or ecx==0).
;   CF=1 on timeout (UART never asserted THRE within the budget).
; Callers can `jc .error_path` immediately after the ret to branch
; on UART failure. Current callers don't consume the signal (a
; wedged UART during boot is unrecoverable anyway and we halt), but
; the signal is cheap to emit and future callers — a full L2 RX
; path that recovers from transient UART issues — can consume it
; without a helper rewrite.
;
; Clobbers: eax, ecx, esi, edi, flags. Preserves: dx, ebx, ebp, esp.
emit_bytes:
    test ecx, ecx
    jz .done
.next:
    add dx, COM1_LSR_OFFSET   ; dx = LSR port
    mov edi, UART_POLL_TIMEOUT
.wait_thre:
    in  al, dx
    test al, UART_LSR_THRE
    jnz .have_thre
    dec edi
    jnz .wait_thre
    ; Timeout. Restore dx, signal failure via CF=1, return.
    sub dx, COM1_LSR_OFFSET
    stc
    ret
.have_thre:
    sub dx, COM1_LSR_OFFSET   ; dx = THR port (== COM1_PORT)
    mov al, [esi]
    out dx, al
    inc esi
    dec ecx
    jnz .next
.done:
    clc
    ret

; --- emit_hex32: write eax as 8 uppercase hex digits to COM1 in dx ---
; Uses an 8-byte stack scratch buffer so the emit_bytes LSR-polling
; helper can do the actual serialization without duplication.
; Preserves dx (caller's COM1_PORT) and ebx (which we use internally
; and save on entry).
; Clobbers: eax, ecx, esi, edi. Preserves: dx, ebx.
emit_hex32:
    push ebx
    sub esp, 8                    ; 8-byte scratch buffer
    mov ebx, eax                  ; keep source across the rotate loop
    lea esi, [esp]
    mov ecx, 8
.digit:
    rol ebx, 4                    ; top nibble → low 4 bits of ebx
    mov eax, ebx
    and eax, 0x0F
    cmp al, 10
    jb .num
    add al, 'A' - '0' - 10        ; 'A'..'F'
.num:
    add al, '0'                   ; '0'..'9' (or adjusted above for hex)
    mov [esi], al
    inc esi
    dec ecx
    jnz .digit
    lea esi, [esp]
    mov ecx, 8
    call emit_bytes
    add esp, 8
    pop ebx
    ret

ready_msg:       db 'READY', 10
ready_len        equ $ - ready_msg

virtio_ok_msg:   db 'VIRTIO:OK', 10
virtio_ok_len    equ $ - virtio_ok_msg

virtio_fail_msg: db 'VIRTIO:FAIL magic='
virtio_fail_len  equ $ - virtio_fail_msg

status_driver_msg: db 'STATUS:DRIVER', 10
status_driver_len  equ $ - status_driver_msg

status_fail_msg:   db 'STATUS:FAIL status='
status_fail_len    equ $ - status_fail_msg

newline:         db 10
