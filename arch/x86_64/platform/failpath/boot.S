; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; x86_64 fail-path test guest — synthetic-stimulus stub.
;
; Boots as a PVH guest like the production firecracker target,
; runs the same allocator, links the same dispatcher.o — but
; SKIPS real virtio init. Instead, after init_memory_layout
; returns and the kstack switch lands, the stub pre-populates
; the RX virtqueue with a malformed used-ring entry (id out
; of range), calls l2_dispatch, and asserts via marker that
; the dispatcher's RX bad_id fail path fired.
;
; Why a stub guest at all: Firecracker's virtio-net is spec-
; compliant. It can't deliver an out-of-range descriptor id
; or a num_buffers > 1 header to the driver. The dispatcher's
; defensive validation checks for these conditions are
; therefore unreachable under any normal load. The stub
; reaches them by writing the malformed contents into
; virtqueue memory directly, bypassing the device. The
; dispatcher is the production code, unchanged.
;
; This is "Option A" from the 2026-05-24 fail-path test-
; coverage discussion (the alternative was a Python
; vhost-user-net backend running under QEMU, which would have
; meant migrating the entire L2 harness off Firecracker).
;
; Marker contract: this stub emits "FAILPATH:READY" after
; init, the dispatcher emits its expected RX:FAIL bad_id=…
; marker, the stub catches l2_dispatch's nonzero return value
; and emits "FAILPATH:DONE rc=<hex>" before halting. The
; integration test asserts on all three.
;
; The PVH header, [bits 32] prologue, mode-switch invocation,
; emit_bytes / emit_hex32 helpers, and tx_test_pkt rodata are
; duplicated from arch/x86_64/platform/firecracker/boot.S
; rather than refactored into a shared file. Duplication is
; the diff-minimal choice for a test stub: a refactor would
; touch production code AND add a shared include file just
; to save ~80 lines in a test-only build. If the stub grows
; into a longer-lived testing framework (more scenarios,
; aarch64 mirror, etc.) the refactor becomes worth doing —
; track at that point.
;
; Currently supported scenarios (selected at assemble time via
; -DFAILPATH_SCENARIO_<NAME>=1; one and only one must be set):
;   BAD_ID — RX used-ring entry has id >= VIRTQ_MAX_SIZE.
;            Expected marker: "RX:FAIL bad_id=00000100".

; --- Sanity: exactly one scenario must be defined ---
%ifndef FAILPATH_SCENARIO_BAD_ID
%error "FAILPATH_SCENARIO_BAD_ID is the only scenario currently implemented; assemble with -DFAILPATH_SCENARIO_BAD_ID=1"
%endif

; ----- Constants (duplicated from production firecracker boot.S) -----

XEN_ELFNOTE_PHYS32_ENTRY equ 18
COM1_PORT                equ 0x3F8
COM1_LSR_OFFSET          equ 5
UART_LSR_THRE            equ 0x20
UART_POLL_TIMEOUT        equ 1 << 20

PVH_START_INFO_MAGIC     equ 0x336EC578

VIRTQ_STRIDE             equ 0x3000
VIRTQ_OFF_DESC           equ 0x0000
VIRTQ_OFF_AVAIL          equ 0x1000
VIRTQ_OFF_USED           equ 0x2000
VIRTQ_MAX_SIZE           equ 0x100

; The malformed value the scenario injects (id = 0x100 == 256,
; one past the valid range). Chosen as an even round number that
; reads cleanly in the marker hex.
SCENARIO_BAD_ID_VALUE    equ 0x100

extern mode_switch_to_long_mode
extern init_memory_layout
extern l2_dispatch

; ----- PVH ELF note -----

section .note.Xen note alloc align=4
    dd 4
    dd 4
    dd XEN_ELFNOTE_PHYS32_ENTRY
    db 'Xen', 0
    align 4
    dd _start

section .text

%include "memreq_records.inc"

; ================================================================
; [bits 32] PVH prologue
; ================================================================
[bits 32]

global _start
_start:
    cli
    mov esp, 0x200000
    cld

    test ebx, ebx
    jz .pvh_fail
    cmp dword [ebx], PVH_START_INFO_MAGIC
    jne .pvh_fail

    lea esi, [failpath_ready_msg]
    mov ecx, failpath_ready_len
    call emit_bytes_32

    jmp mode_switch_to_long_mode

.pvh_fail:
    lea esi, [pvh_fail_msg]
    mov ecx, pvh_fail_len
    call emit_bytes_32
    cli
.pvh_halt:
    hlt
    jmp .pvh_halt

emit_bytes_32:
    test ecx, ecx
    jz .done
    mov dx, COM1_PORT
.next:
    add dx, COM1_LSR_OFFSET
    mov edi, UART_POLL_TIMEOUT
.wait_thre:
    in  al, dx
    test al, UART_LSR_THRE
    jnz .have_thre
    dec edi
    jnz .wait_thre
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

; ================================================================
; [bits 64] failpath kernel
; ================================================================
[bits 64]
global kernel_main_64
kernel_main_64:
    call init_memory_layout

%include "memreq_pin_hot.inc"

    mov rax, [rel __memreq_assigned__kstack_boot]
    add rax, [rel __memreq_size__kstack_boot]
    mov rsp, rax

    lea rsi, [rel failpath_boot_msg]
    mov ecx, failpath_boot_len
    call emit_bytes

    ; --- Scenario-specific virtqueue setup ---
    ;
    ; r15 holds VIRTQ_BASE (the RX queue base) per the
    ; memreq_pin_hot.inc include above.
%ifdef FAILPATH_SCENARIO_BAD_ID
    ; Bypass real virtio init entirely. The dispatcher's RX wait
    ; loop reads UsedRing.idx and compares against
    ; l2_state.rx_used_shadow. l2_state was zero-initialised by
    ; the allocator. Set idx = 1 → wait loop terminates → consume
    ; body reads ring[0].id, sees out-of-range, jumps to
    ; .l2_rx_bad_id_fail.
    ;
    ; UsedRing layout (Virtio 1.2 §2.7.8):
    ;   offset 0: flags (u16)         — leave at 0
    ;   offset 2: idx (u16)           — set to 1
    ;   offset 4: ring[].id (u32)     — set to SCENARIO_BAD_ID_VALUE
    ;   offset 8: ring[].len (u32)    — set to 100 (arbitrary,
    ;                                    never read on the fail path)
    mov word  [r15 + VIRTQ_OFF_USED + 2], 1
    mov dword [r15 + VIRTQ_OFF_USED + 4], SCENARIO_BAD_ID_VALUE
    mov dword [r15 + VIRTQ_OFF_USED + 8], 100
%endif

    mfence

    ; --- Call the production dispatcher ---
    ;
    ; Dispatcher contract requires r12 = VIRTIO_MMIO_BASE and r13
    ; = queue size N. We don't have a real virtio device — set r12
    ; to a safe address (it's only written when the dispatcher
    ; tries to QueueNotify, which on the bad_id fail path we don't
    ; reach) and r13 to VIRTQ_MAX_SIZE so the mask = N - 1 = 0xFF
    ; computation works correctly for the ring slot index.
    ;
    ; r14 is initialised to l2_state.rx_next_avail in the
    ; dispatcher's prologue from the l2_state region (zero-init by
    ; allocator). r15 already holds VIRTQ_BASE.
    mov r12d, 0xC0001000                ; VIRTIO_MMIO_BASE — unused on fail path
    mov r13d, VIRTQ_MAX_SIZE            ; N (256)

    call l2_dispatch

    ; --- Emit return code and halt ---
    ;
    ; Successful fail-path test: eax = 1 (dispatcher returned
    ; "fail" after emitting RX:FAIL marker). Unexpected success
    ; (eax = 0) would mean the bad_id check didn't fire, which
    ; is the regression we'd want to catch in the harness.
    push rax
    lea rsi, [rel failpath_done_msg]
    mov ecx, failpath_done_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes

.halt:
    cli
    hlt
    jmp .halt

; ================================================================
; emit_bytes — [bits 64] LSR-polled serial writer
; ================================================================
; Duplicated from production firecracker boot.S (see header
; rationale at the top of this file).
;
;   In:  RSI = source ptr, ECX = byte count.
;   Out: CF = 0 on full success, CF = 1 on UART timeout.
;   Clobbers: eax, ecx, edx, esi/rsi, edi, flags.
global emit_bytes
emit_bytes:
    test ecx, ecx
    jz .done
    mov dx, COM1_PORT
.next:
    add dx, COM1_LSR_OFFSET
    mov edi, UART_POLL_TIMEOUT
.wait_thre:
    in  al, dx
    test al, UART_LSR_THRE
    jnz .have_thre
    dec edi
    jnz .wait_thre
    sub dx, COM1_LSR_OFFSET
    stc
    ret
.have_thre:
    sub dx, COM1_LSR_OFFSET
    mov al, [rsi]
    out dx, al
    inc rsi
    dec ecx
    jnz .next
.done:
    clc
    ret

; emit_hex32 — duplicated from production firecracker boot.S.
;
;   In:  EAX = 32-bit value to render.
;   Clobbers: eax, ecx, edx, esi/rsi, edi.
;   Preserves: rbx, rbp, rsp, r12.
global emit_hex32
emit_hex32:
    push rbx
    sub rsp, 16
    mov ebx, eax
    lea rsi, [rsp]
    mov ecx, 8
.digit:
    rol ebx, 4
    mov eax, ebx
    and eax, 0x0F
    cmp al, 10
    jb .num
    add al, 'A' - '0' - 10
.num:
    add al, '0'
    mov [rsi], al
    inc rsi
    dec ecx
    jnz .digit
    lea rsi, [rsp]
    mov ecx, 8
    call emit_bytes
    add rsp, 16
    pop rbx
    ret

; ================================================================
; Strings + tx_test_pkt
; ================================================================
section .rodata

failpath_ready_msg: db 'READY', 10            ; matches production's READY marker so tracer-bullet boot smoke logic still works if anyone runs this stub through it
failpath_ready_len  equ $ - failpath_ready_msg

pvh_fail_msg:       db 'PVH:FAIL bad magic', 10
pvh_fail_len        equ $ - pvh_fail_msg

failpath_boot_msg:  db 'FAILPATH:BOOT', 10
failpath_boot_len   equ $ - failpath_boot_msg

failpath_done_msg:  db 'FAILPATH:DONE rc='
failpath_done_len   equ $ - failpath_done_msg

global newline
newline:            db 10

; tx_test_pkt — referenced by dispatcher.S as the canary TX
; frame address when no ARP reply is pending. On the bad_id
; fail path the dispatcher returns BEFORE reaching the TX
; submit phase, so this is never actually loaded. Provided
; to satisfy the dispatcher's `extern tx_test_pkt` import.
global tx_test_pkt
tx_test_pkt:
    times 72 db 0

section .note.GNU-stack noalloc noexec nowrite progbits
