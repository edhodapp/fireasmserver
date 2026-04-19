# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Ed Hodapp
#
# x86_64 Firecracker guest — first step of the virtio-net L2 driver:
# the MMIO probe. PVH boot entry lands here in 32-bit protected mode
# with paging off and interrupts off (Xen PVH spec). A tiny stack is
# available but small; we use one level of call/ret only.
#
# Sequence:
#   1. Emit "READY\n" on COM1 (preserves the existing tracer-bullet
#      marker so downstream harnesses keep passing).
#   2. Read the 32-bit virtio-MMIO MagicValue register at the first
#      virtio device slot. Per Firecracker v1.15.1 x86_64 layout:
#        MMIO32_MEM_START  = FIRST_ADDR_PAST_32BITS - 1 GiB = 0xC0000000
#        MEM_32BIT_DEVICES = MMIO32_MEM_START + MMIO_LEN    = 0xC0001000
#      (source: Firecracker src/vmm/src/arch/x86_64/layout.rs)
#      The register value MUST be 0x74726976 ("virt" little-endian)
#      per Virtio 1.2 §4.2.2 — this is VIO-Q-001/§2.7 territory and
#      the first observable cross-check with the device.
#   3. Emit "VIRTIO:OK\n" on match, "VIRTIO:FAIL\n" on mismatch.
#   4. Halt.
#
# Assumes the Firecracker JSON config includes a network-interface
# entry (host_dev_name=tap0). If not, the MMIO read at 0xC0001000
# would produce a "MissingAddressRange" bus error and Firecracker
# would kill the guest — readable in its log, caught by the timeout.

.intel_syntax noprefix

.set XEN_ELFNOTE_PHYS32_ENTRY, 18
.set COM1_PORT,                 0x3F8
.set VIRTIO_MMIO_BASE,          0xC0001000
.set VIRTIO_MAGIC,              0x74726976   # "virt" little-endian

# PVH ELF Note. namesz=4 covers "Xen\0"; descsz=4 is the entry
# address that follows. The @note section type is required so ld
# emits the section as SHT_NOTE rather than SHT_PROGBITS.
.section .note.Xen, "a", @note
.align 4
.long 4
.long 4
.long XEN_ELFNOTE_PHYS32_ENTRY
.asciz "Xen"
# Name field must be padded to 4-byte alignment before the descriptor.
# "Xen\0" happens to be 4 bytes so this .balign is a no-op today, but
# if namesz ever grows (unlikely) the alignment guarantee stays.
.balign 4
.long _start

.section .text
.code32
.globl _start

_start:
    # PVH spec leaves ESP undefined on entry. Point it at 2 MiB, well
    # above .text (ends < 0x100100) and inside the 128 MiB guest RAM,
    # so our call/ret pairs have somewhere valid to push return
    # addresses. Without this, the first CALL pushes to [0xfffffffc]
    # (ESP=0 wraps), which is MMIO space past guest RAM — RET then
    # reads garbage from there and jumps somewhere random.
    mov esp, 0x200000
    mov dx, OFFSET COM1_PORT

    # --- Emit "READY\n" ---
    # OFFSET is mandatory here. Under GAS .intel_syntax noprefix, a bare
    # .equ symbol as a MOV source defaults to a memory reference, not an
    # immediate — `mov ecx, ready_len` becomes `mov ecx, [6]` (load from
    # address 6), not `mov ecx, 6`. OFFSET forces the immediate form.
    # LEA above is fine without OFFSET because LEA's semantics compute
    # the operand's effective address regardless of the memory framing.
    lea esi, ready_msg
    mov ecx, OFFSET ready_len
    call emit_bytes

    # --- Probe virtio-MMIO MagicValue register ---
    # MagicValue is a 32-bit MMIO read at VIRTIO_MMIO_BASE + 0x0.
    # In 32-bit protected mode with paging off, we can dereference
    # the physical address directly.
    mov eax, [VIRTIO_MMIO_BASE]
    cmp eax, VIRTIO_MAGIC
    jne .virtio_fail

    # Match — announce and halt.
    lea esi, virtio_ok_msg
    mov ecx, OFFSET virtio_ok_len
    call emit_bytes
    jmp .halt

.virtio_fail:
    lea esi, virtio_fail_msg
    mov ecx, OFFSET virtio_fail_len
    call emit_bytes

.halt:
    cli
.halt_loop:
    hlt
    jmp .halt_loop

# --- Helper: write <ecx> bytes from <esi> to port in dx ---
# Caller preserves dx (already set). One-byte-at-a-time via the
# legacy serial port; the COM1 path is only used for diagnostic
# markers, not for product data, so tight-loop throughput is fine.
emit_bytes:
    test ecx, ecx
    jz 9f
1:
    mov al, [esi]
    out dx, al
    inc esi
    dec ecx
    jnz 1b
9:
    ret

# Diagnostic strings (in .text — small, and we don't need a separate
# .rodata section for 30 bytes of static data).
ready_msg:
    .ascii "READY\n"
.equ ready_len, . - ready_msg

virtio_ok_msg:
    .ascii "VIRTIO:OK\n"
.equ virtio_ok_len, . - virtio_ok_msg

virtio_fail_msg:
    .ascii "VIRTIO:FAIL\n"
.equ virtio_fail_len, . - virtio_fail_msg
