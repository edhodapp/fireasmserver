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
; Supported scenarios (selected at assemble time via
; -DFAILPATH_SCENARIO_<NAME>=1; exactly one must be set):
;   BAD_ID    — RX used-ring entry has id >= VIRTQ_MAX_SIZE.
;               Expected marker: "RX:FAIL bad_id=00000100".
;   NUM_BUFS  — RX frame's virtio_net_hdr.num_buffers != 1.
;               Expected marker: "RX:FAIL num_bufs=00000002".
;   HDR_FLAGS — RX frame's virtio_net_hdr.flags or .gso_type
;               non-zero (defense-in-depth check, added
;               2026-05-24).
;               Expected marker: "RX:FAIL hdr_flags=00000001".
;   TX_BAD_ID — valid RX completes, then TX UsedRing entry
;               has id >= VIRTQ_MAX_SIZE.
;               Expected marker: "TX:FAIL bad_id=00000100"
;               (after the dispatcher's RX:FRAME + TX:SUBMITTED
;               markers for the valid RX side).

; --- Sanity: exactly one scenario must be defined ---
; NASM's %assign supports incrementing on a self-reference; the
; %ifdef chain below bumps the counter once per defined scenario.
%assign FAILPATH_SCENARIO_COUNT 0
%ifdef FAILPATH_SCENARIO_BAD_ID
%assign FAILPATH_SCENARIO_COUNT FAILPATH_SCENARIO_COUNT + 1
%endif
%ifdef FAILPATH_SCENARIO_NUM_BUFS
%assign FAILPATH_SCENARIO_COUNT FAILPATH_SCENARIO_COUNT + 1
%endif
%ifdef FAILPATH_SCENARIO_HDR_FLAGS
%assign FAILPATH_SCENARIO_COUNT FAILPATH_SCENARIO_COUNT + 1
%endif
%ifdef FAILPATH_SCENARIO_TX_BAD_ID
%assign FAILPATH_SCENARIO_COUNT FAILPATH_SCENARIO_COUNT + 1
%endif
%if FAILPATH_SCENARIO_COUNT != 1
%error "Exactly one of FAILPATH_SCENARIO_{BAD_ID,NUM_BUFS,HDR_FLAGS,TX_BAD_ID} must be defined via -D"
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
TX_QUEUE_OFF             equ 1 * VIRTQ_STRIDE

; The malformed value the BAD_ID + TX_BAD_ID scenarios inject
; (id = 0x100 == 256, one past the valid range). Even round
; number that reads cleanly in the marker hex.
SCENARIO_BAD_ID_VALUE    equ 0x100

; The malformed value NUM_BUFS injects into virtio_net_hdr at
; buffer offset 10. Anything != 1 fails the dispatcher's check.
SCENARIO_NUM_BUFS_VALUE  equ 2

; GUEST_MAC bytes — duplicated from arch/x86_64/l2/dispatcher.S.
; Used by NUM_BUFS and TX_BAD_ID scenarios to construct a buffer
; whose dst MAC passes the MAC filter so the dispatcher reaches
; the per-frame header read (num_bufs) or proceeds to the TX
; phase. Stays in sync with dispatcher.S by code review — this
; is test-stub code and the values are stable (project ETH-016
; convention).
FP_GUEST_MAC_BYTE_0      equ 0x02
FP_GUEST_MAC_BYTE_1      equ 0x00
FP_GUEST_MAC_BYTE_2      equ 0x00
FP_GUEST_MAC_BYTE_3      equ 0x00
FP_GUEST_MAC_BYTE_4      equ 0x00
FP_GUEST_MAC_BYTE_5      equ 0x01

extern mode_switch_to_long_mode
extern init_memory_layout
extern l2_dispatch
; __memreq_addr__rx_buffer comes from memreq_records.inc (included
; below) — local to this object, not an extern.

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

%ifdef FAILPATH_SCENARIO_NUM_BUFS
    ; Same RX UsedRing setup as BAD_ID but with a VALID id, then
    ; populate the buffer at id=0 such that the dispatcher's
    ; num_buffers check fails.
    ;
    ; Order of gates the dispatcher applies before reaching the
    ; num_bufs check (see arch/x86_64/l2/dispatcher.S):
    ;   1. size bounds  (72 ≤ used_len ≤ 1530)         — len=100 ✓
    ;   2. dst MAC filter (= GUEST_MAC or multicast)   — dst=GUEST_MAC
    ;   3. src MAC unicast (byte 0 bit 0 = 0)          — src=02:..:42
    ;   4. PAUSE EtherType (≠ 0x8808)                  — type=0x88B5
    ;   5. num_buffers != 1 → .l2_rx_num_bufs_fail
    ;
    ; Buffer layout (offsets into rx_buffer[0]):
    ;   +0  : virtio_net_hdr (12 B zero except num_buffers at +10)
    ;   +12 : Ethernet header (dst MAC, src MAC, EtherType)
    mov word  [r15 + VIRTQ_OFF_USED + 2], 1
    mov dword [r15 + VIRTQ_OFF_USED + 4], 0            ; id = 0
    mov dword [r15 + VIRTQ_OFF_USED + 8], 100          ; len = 100

    mov rax, [rel __memreq_addr__rx_buffer]
    ; Zero clear the first 32 bytes of the buffer for cleanliness.
    mov qword [rax +  0], 0
    mov qword [rax +  8], 0
    mov qword [rax + 16], 0
    mov qword [rax + 24], 0
    ; virtio_net_hdr.num_buffers (u16 at offset 10) — the
    ; malformed value the dispatcher's check fires on.
    mov word  [rax + 10], SCENARIO_NUM_BUFS_VALUE
    ; Ethernet dst MAC = GUEST_MAC (passes the dispatcher's MAC
    ; filter via direct match).
    mov byte  [rax + 12], FP_GUEST_MAC_BYTE_0
    mov byte  [rax + 13], FP_GUEST_MAC_BYTE_1
    mov byte  [rax + 14], FP_GUEST_MAC_BYTE_2
    mov byte  [rax + 15], FP_GUEST_MAC_BYTE_3
    mov byte  [rax + 16], FP_GUEST_MAC_BYTE_4
    mov byte  [rax + 17], FP_GUEST_MAC_BYTE_5
    ; Ethernet src MAC = 02:00:00:00:00:42 (unicast — passes
    ; ETH-015's bit-0 check).
    mov dword [rax + 18], 0x00000002                   ; bytes 0-3 LE
    mov word  [rax + 22], 0x4200                       ; bytes 4-5 LE
    ; EtherType 0x88B5 BE (local-experimental — bypasses PAUSE
    ; and ARP gates, lands in the "generic frame" path that
    ; ultimately reaches the num_bufs check).
    mov byte  [rax + 24], 0x88
    mov byte  [rax + 25], 0xB5
%endif

%ifdef FAILPATH_SCENARIO_HDR_FLAGS
    ; Same RX UsedRing + Ethernet header setup as NUM_BUFS, but
    ; with num_buffers=1 (valid) and virtio_net_hdr.flags=1
    ; (= VIRTIO_NET_HDR_F_NEEDS_CSUM). With our negotiated
    ; feature set (no F_CSUM), the device should NEVER set
    ; this. Dispatcher's defense-in-depth check fires:
    ; "RX:FAIL hdr_flags=00000001".
    mov word  [r15 + VIRTQ_OFF_USED + 2], 1
    mov dword [r15 + VIRTQ_OFF_USED + 4], 0
    mov dword [r15 + VIRTQ_OFF_USED + 8], 100

    mov rax, [rel __memreq_addr__rx_buffer]
    mov qword [rax +  0], 0
    mov qword [rax +  8], 0
    mov qword [rax + 16], 0
    mov qword [rax + 24], 0
    ; virtio_net_hdr.flags (u8 @ +0) = 1; gso_type (u8 @ +1) = 0.
    ; Combined u16 read in the dispatcher = 0x0001.
    mov byte  [rax + 0], 1
    mov word  [rax + 10], 1                            ; num_buffers = 1
    mov byte  [rax + 12], FP_GUEST_MAC_BYTE_0
    mov byte  [rax + 13], FP_GUEST_MAC_BYTE_1
    mov byte  [rax + 14], FP_GUEST_MAC_BYTE_2
    mov byte  [rax + 15], FP_GUEST_MAC_BYTE_3
    mov byte  [rax + 16], FP_GUEST_MAC_BYTE_4
    mov byte  [rax + 17], FP_GUEST_MAC_BYTE_5
    mov dword [rax + 18], 0x00000002
    mov word  [rax + 22], 0x4200
    mov byte  [rax + 24], 0x88
    mov byte  [rax + 25], 0xB5
%endif

%ifdef FAILPATH_SCENARIO_TX_BAD_ID
    ; The dispatcher's TX phase only runs after a successful RX
    ; consume — RX:TIMEOUT skips TX entirely. So we need a valid
    ; RX setup (frame passes every gate, exits via the normal
    ; RX:FRAME path) PLUS a pre-populated TX UsedRing whose
    ; ring[0].id is out of range. Setup is the union of the
    ; valid-RX-frame from NUM_BUFS (with num_buffers=1 instead
    ; of 2) and a TX UsedRing matching BAD_ID's shape on the
    ; TX queue (at r15 + TX_QUEUE_OFF).

    ; Valid RX completion in slot 0.
    mov word  [r15 + VIRTQ_OFF_USED + 2], 1
    mov dword [r15 + VIRTQ_OFF_USED + 4], 0            ; id = 0
    mov dword [r15 + VIRTQ_OFF_USED + 8], 100          ; len = 100

    mov rax, [rel __memreq_addr__rx_buffer]
    mov qword [rax +  0], 0
    mov qword [rax +  8], 0
    mov qword [rax + 16], 0
    mov qword [rax + 24], 0
    mov word  [rax + 10], 1                            ; num_buffers = 1 ✓
    mov byte  [rax + 12], FP_GUEST_MAC_BYTE_0
    mov byte  [rax + 13], FP_GUEST_MAC_BYTE_1
    mov byte  [rax + 14], FP_GUEST_MAC_BYTE_2
    mov byte  [rax + 15], FP_GUEST_MAC_BYTE_3
    mov byte  [rax + 16], FP_GUEST_MAC_BYTE_4
    mov byte  [rax + 17], FP_GUEST_MAC_BYTE_5
    mov dword [rax + 18], 0x00000002
    mov word  [rax + 22], 0x4200
    mov byte  [rax + 24], 0x88
    mov byte  [rax + 25], 0xB5

    ; TX UsedRing pre-populated with a malformed id. The
    ; dispatcher's TX wait loop polls TX UsedRing.idx; once
    ; it sees idx != tx_used_shadow (= 0 from l2_state's zero
    ; init), it enters the reclaim body and reads ring[0].id.
    mov word  [r15 + TX_QUEUE_OFF + VIRTQ_OFF_USED + 2], 1
    mov dword [r15 + TX_QUEUE_OFF + VIRTQ_OFF_USED + 4], SCENARIO_BAD_ID_VALUE
    mov dword [r15 + TX_QUEUE_OFF + VIRTQ_OFF_USED + 8], 72
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
