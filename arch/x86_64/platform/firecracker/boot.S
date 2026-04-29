; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; x86_64 Firecracker guest — Shape-B boot path per D062 + D063.
;
; Boot sequence:
;   1. PVH _start lands in [bits 32]: validate start_info magic at
;      [ebx], establish ESP, emit READY, jmp mode_switch_to_long_mode.
;   2. mode_switch_to_long_mode (arch/x86_64/memory/mode_switch.S)
;      enters long mode with low 4 GiB identity-mapped at 2 MiB
;      granularity and tail-jumps to kernel_main_64.
;   3. kernel_main_64 [bits 64]: calls init_memory_layout (D060
;      step 4.2's wire-up, returns immediately while .memreq is
;      empty), then runs the full virtio-net init through TX:RECLAIMED.
;
; The marker chain — READY → VIRTIO:OK → STATUS:DRIVER → FEATURES:OK →
; QUEUES:RX/TX → QUEUES:READY → DRIVER_OK → RX:POPULATED → RX:FRAME →
; RX:RETURNED → TX:SUBMITTED → TX:RECLAIMED — is the L2-init handshake
; observed by the tracer-bullet harness in tooling/tracer_bullet/run_local.sh.
;
; Per Shape B (chosen 2026-04-29): the entire post-mode-switch path
; runs in long mode, replacing the prior single-mode [bits 32] design
; that worked in the absence of paging. D063 amended D062 with low-
; 4-GiB identity coverage so the virtio-MMIO at 0xC0001000 is
; reachable post-mode-switch.
;
; PVH boot entry state (Xen PVH spec):
;   CS = flat 32-bit code segment (loader-handed)
;   DS, ES, FS, GS, SS = flat 32-bit data segments
;   ESP undefined — we establish our own
;   EFLAGS.DF undefined — we cld it ourselves
;   EBX = 32-bit phys ptr to hvm_start_info struct (first u32 = magic)
;   IF = 0
;   Paging: off
;
; NASM per D048.

; ----- Constants -----

XEN_ELFNOTE_PHYS32_ENTRY equ 18
COM1_PORT                equ 0x3F8
COM1_LSR_OFFSET          equ 5            ; LSR port = COM1_PORT + 5 = 0x3FD
UART_LSR_THRE            equ 0x20         ; Transmit Holding Register Empty
UART_POLL_TIMEOUT        equ 1 << 20      ; ~1 ms at modern CPU speeds
VIRTIO_MMIO_BASE         equ 0xC0001000
VIRTIO_MAGIC             equ 0x74726976   ; "virt" little-endian

; PVH start_info magic (Xen "PVH boot protocol" — first u32 of the
; struct EBX points at on entry). Catches non-PVH entry early.
PVH_START_INFO_MAGIC     equ 0x336EC578   ; "xEn3" little-endian

; Virtio-MMIO register offsets (Virtio 1.2 §4.2.2, Table 4.1).
VIRTIO_MMIO_DEVICE_FEATURES     equ 0x010
VIRTIO_MMIO_DEVICE_FEATURES_SEL equ 0x014
VIRTIO_MMIO_DRIVER_FEATURES     equ 0x020
VIRTIO_MMIO_DRIVER_FEATURES_SEL equ 0x024
VIRTIO_MMIO_QUEUE_SEL           equ 0x030
VIRTIO_MMIO_QUEUE_NUM_MAX       equ 0x034
VIRTIO_MMIO_QUEUE_NUM           equ 0x038
VIRTIO_MMIO_QUEUE_READY         equ 0x044
VIRTIO_MMIO_QUEUE_NOTIFY        equ 0x050
VIRTIO_MMIO_STATUS              equ 0x070
VIRTIO_MMIO_QUEUE_DESC_LOW      equ 0x080
VIRTIO_MMIO_QUEUE_DESC_HIGH     equ 0x084
VIRTIO_MMIO_QUEUE_DRIVER_LOW    equ 0x090
VIRTIO_MMIO_QUEUE_DRIVER_HIGH   equ 0x094
VIRTIO_MMIO_QUEUE_DEVICE_LOW    equ 0x0A0
VIRTIO_MMIO_QUEUE_DEVICE_HIGH   equ 0x0A4

; Virtqueue layout — 12 KiB per queue (page-aligned slots for desc,
; avail, used). 4 MiB base, well above the loaded image at 1 MiB.
; Firecracker zero-initializes guest RAM, so all rings are idx=0,
; flags=0 at boot — exactly the "uninhabited queue" state the spec
; requires at QueueReady time.
VIRTQ_BASE          equ 0x400000
VIRTQ_STRIDE        equ 0x3000
VIRTQ_OFF_DESC      equ 0x0000
VIRTQ_OFF_AVAIL     equ 0x1000
VIRTQ_OFF_USED      equ 0x2000

VIRTQ_MAX_SIZE      equ 0x100    ; 256 descriptors max
RX_QUEUE_BASE       equ VIRTQ_BASE + 0 * VIRTQ_STRIDE
TX_QUEUE_BASE       equ VIRTQ_BASE + 1 * VIRTQ_STRIDE

VIRTQ_DESC_F_NEXT     equ 1
VIRTQ_DESC_F_WRITE    equ 2
VIRTQ_DESC_F_INDIRECT equ 4

; RX buffer pool. 256 buffers × 2 KiB each = 512 KiB total, above the
; virtqueue pages (which end at 0x406000). 2 KiB per buffer fits a
; 12 B virtio_net_hdr + max-size tagged Ethernet frame with slack.
RX_BUFFER_BASE      equ 0x500000
RX_BUFFER_SIZE      equ 0x800

; Device Status (Virtio 1.2 §2.1).
VIRTIO_STATUS_ACKNOWLEDGE equ 1
VIRTIO_STATUS_DRIVER      equ 2
VIRTIO_STATUS_DRIVER_OK   equ 4
VIRTIO_STATUS_FEATURES_OK equ 8
VIRTIO_STATUS_FAILED      equ 0x80

; Feature bit (Virtio 1.2 §6 — VIRTIO_F_VERSION_1 is bit 32 globally,
; bit 0 of the high page). MVP accepts VERSION_1 only.
VIRTIO_F_VERSION_1_HIBIT  equ 1

; ----- External symbols (resolved by linker) -----

extern mode_switch_to_long_mode
extern init_memory_layout

; ----- PVH ELF note -----

section .note.Xen note alloc align=4
    dd 4
    dd 4
    dd XEN_ELFNOTE_PHYS32_ENTRY
    db 'Xen', 0
    align 4
    dd _start

section .text

; ================================================================
; [bits 32] PVH prologue
; ================================================================
; Validates the loader-handed state, emits READY (observable even
; if the mode switch faults), and tail-jumps to the shared mode
; switch. emit_bytes_32 is the [bits 32] LSR-polled serial writer
; used pre-mode-switch.

[bits 32]

global _start
_start:
    ; Defensive: PVH spec leaves IF=0 and DF undefined. cli is
    ; insurance against a non-compliant loader; cld ensures rep
    ; movs / lodsb wouldn't be surprised by a bad direction (the
    ; current prologue doesn't use string ops, but mode_switch.S
    ; runs `rep stosd` at entry).
    cli

    ; ESP at 2 MiB — well above the loaded image and below the
    ; bootstrap stack mode_switch.S reloads RSP from after long
    ; mode entry. Required because PVH leaves ESP undefined and
    ; the call/ret pairs in emit_bytes_32 below need a valid stack.
    mov esp, 0x200000

    cld

    ; UART base for emit_bytes_32 / failure paths.
    mov dx, COM1_PORT

    ; Validate PVH start_info pointer. EBX = phys ptr to
    ; hvm_start_info per Xen PVH boot protocol; first u32 of the
    ; struct is magic = 0x336EC578 ("xEn3" little-endian). Any
    ; mismatch means we were entered by something other than a
    ; PVH-compliant loader and the rest of the boot contract is
    ; void. EBX == 0 is also a fail (some misconfigured firmware
    ; might leave it cleared).
    test ebx, ebx
    jz .pvh_fail
    cmp dword [ebx], PVH_START_INFO_MAGIC
    jne .pvh_fail

    ; Emit READY in [bits 32] before the mode switch. Two reasons:
    ;   1. emit_bytes uses `in al, dx` / `out dx, al` which require
    ;      a current stack frame for `call/ret`. The mode switch
    ;      doesn't preserve [bits 32] return addresses, so emitting
    ;      AFTER the switch from [bits 64] code requires a separate
    ;      [bits 64] writer (we have one — see emit_bytes below).
    ;   2. If the mode switch faults (triple-fault → silent reset),
    ;      READY having reached the harness lets a developer locate
    ;      the failure: "READY observed → fault is post-mode-switch"
    ;      vs "READY missing → fault is in the [bits 32] prologue
    ;      or mode-switch body itself."
    lea esi, [ready_msg]
    mov ecx, ready_len
    call emit_bytes_32

    ; Hand off to the shared mode-switch body. JMP not CALL — the
    ; mode switch invalidates this stack frame's [bits 32] return
    ; address. Control reaches kernel_main_64 in [bits 64], which
    ; does its own work and never returns here.
    jmp mode_switch_to_long_mode

.pvh_fail:
    ; PVH_START_INFO_MAGIC mismatch (or EBX = 0). Emit a marker so
    ; a developer running the cell manually can see the failure
    ; mode, then halt. We don't dump the observed magic in hex
    ; (would need a [bits 32] hex-emit helper not currently in
    ; this file); the static marker is sufficient for the rare
    ; "loader handed wrong magic" case.
    lea esi, [pvh_fail_msg]
    mov ecx, pvh_fail_len
    call emit_bytes_32
    cli
.pvh_halt:
    hlt
    jmp .pvh_halt

; emit_bytes_32 — [bits 32] LSR-polled serial writer.
; Used only by the PVH prologue above (READY emit + PVH:FAIL
; emit). Same contract as the [bits 64] emit_bytes used post-
; mode-switch. Mirroring rather than factoring because the bit-
; mode difference is what we're crossing — a shared include can't
; be assembled twice in different bit modes from a single source.
;
;   In:  ESI = source ptr, ECX = byte count, DX = COM1_PORT.
;   Out: CF = 0 on full success, CF = 1 on UART timeout.
;   Clobbers: eax, ecx, esi, edi, flags. Preserves: dx, ebx, ebp, esp.
emit_bytes_32:
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
; [bits 64] kernel main — full virtio-net init in long mode
; ================================================================
; Reached only via mode_switch_to_long_mode's tail jump. State on
; entry: long mode, paging on with low 4 GiB identity-mapped (D063),
; RSP = __bootstrap_stack_top (D060 step 4.1), null DS/ES/FS/GS,
; SS = DATA64_SEL.
;
; Calling/register conventions in this file:
;   r12 — VIRTIO_MMIO_BASE pinned for the entire kernel_main_64.
;         Never written by emit_bytes / emit_hex32; the [r12 + off]
;         addressing pattern works around the fact that
;         VIRTIO_MMIO_BASE = 0xC0001000 sign-extends *negative* as a
;         32-bit displacement and so cannot be used directly as
;         absolute disp32 in [bits 64]. RAM addresses (VIRTQ_BASE,
;         RX_BUFFER_BASE) are < 2 GiB and use direct disp32.
;   dx  — COM1_PORT pinned across emit_bytes calls.
;   eax/ebx/ecx/edx/esi/edi/ebp — same roles as the prior [bits 32]
;         code; long-mode auto-zero-extends 32-bit writes to the
;         full 64-bit register. Pointer ops use rax/rbx/rsi/rdi/rbp.

; Per-queue programming macro — [bits 64] form using r12 indirect.
; Same logic as the prior [bits 32] macro; only the addressing
; mode changed.
;
; Parameters:
;   %1 = queue index (0 = RX, 1 = TX)
;   %2 = descriptor table physical address (u32 immediate)
;   %3 = available ring physical address (u32 immediate)
;   %4 = used ring physical address (u32 immediate)
;   %5 = fail-path label (target on QueueReady read-back mismatch)
; Clobbers: eax, flags, MMIO at [r12 + ...], avail+used ring headers
;           at [%3] / [%4]. Preserves all GPRs except eax (and dx
;           explicitly).
%macro program_queue 5
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_SEL], %1
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_QUEUE_NUM_MAX]
    lfence
    cmp eax, VIRTQ_MAX_SIZE
    jbe %%size_ok
    mov eax, VIRTQ_MAX_SIZE
%%size_ok:
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NUM], eax
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DESC_LOW], %2
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DESC_HIGH], 0
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DRIVER_LOW], %3
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DRIVER_HIGH], 0
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DEVICE_LOW], %4
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DEVICE_HIGH], 0
    ; Avail/Used ring header zero (belt-and-suspenders; Firecracker
    ; zero-inits but spec doesn't mandate it).
    mov dword [%3], 0
    mov dword [%4], 0
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_READY], 1
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_QUEUE_READY]
    lfence
    cmp eax, 1
    jne %5
%endmacro

[bits 64]
global kernel_main_64
kernel_main_64:
    ; Step 4.2: D060 init-time static memory allocator. Returns
    ; immediately today (.memreq is empty); the wire-up exists so
    ; the call site stops being a no-op the moment a module
    ; declares a region (step 5).
    call init_memory_layout

    ; UART base for all emit_bytes / emit_hex32 calls. Set BEFORE
    ; the LAYOUT-OK emit below so the marker can land immediately
    ; on init_memory_layout's return.
    mov dx, COM1_PORT

    ; Emit "LAYOUT-OK\n" — D060 step 4.2 success marker. Reached
    ; only when init_memory_layout returned cleanly (it halts
    ; internally on overflow per its contract). Tracer-bullet
    ; harness can assert on this to know the allocator pass landed.
    lea rsi, [rel layout_ok_msg]
    mov ecx, layout_ok_len
    call emit_bytes

    ; Pin VIRTIO_MMIO_BASE in r12 for the rest of the function.
    ; The 32-bit-dest form `mov r12d, imm32` zero-extends to r12,
    ; which is what we want — `mov r12, imm32` would sign-extend
    ; (turning 0xC0001000 into 0xFFFFFFFFC0001000). The encoding
    ; saved here vs movabs r64,imm64 is 4 bytes, but correctness
    ; is the actual reason for the form.
    mov r12d, VIRTIO_MMIO_BASE

    ; --- Probe virtio-MMIO MagicValue register ---
    ; Virtio 1.2 §4.2.2: barrier before/after MMIO. x86 TSO:
    ; lfence around loads, mfence around stores.
    lfence
    mov eax, [r12]                  ; MagicValue
    lfence
    cmp eax, VIRTIO_MAGIC
    jne .virtio_fail

    lea rsi, [rel virtio_ok_msg]
    mov ecx, virtio_ok_len
    call emit_bytes

    ; --- Device-status init prefix (VIO-001, VIO-002, VIO-003) ---
    ; Virtio 1.2 §2.1.2 steps 1-3.

    ; VIO-001: reset device.
    mfence
    mov dword [r12 + VIRTIO_MMIO_STATUS], 0
    mfence

    ; §4.2.4 reset wait — bounded.
    mov ecx, 0x100000
.reset_wait:
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    test eax, eax
    jz  .reset_done
    dec ecx
    jnz .reset_wait
.reset_done:

    ; VIO-002: RMW ACKNOWLEDGE.
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_ACKNOWLEDGE
    mfence
    mov dword [r12 + VIRTIO_MMIO_STATUS], eax
    mfence

    ; VIO-003: RMW DRIVER.
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_DRIVER
    mfence
    mov dword [r12 + VIRTIO_MMIO_STATUS], eax
    mfence

    ; Cross-check: ACK | DRIVER, no other bits.
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    cmp eax, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER
    jne .status_fail

    lea rsi, [rel status_driver_msg]
    mov ecx, status_driver_len
    call emit_bytes

    ; --- Feature negotiation (VIO-004..006) ---

    ; VIO-004 part 1a: read low-page offered features (saved in ebx
    ; for diagnostic dump on no-v1 fail; preserved across emit_bytes).
    mfence
    mov dword [r12 + VIRTIO_MMIO_DEVICE_FEATURES_SEL], 0
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_DEVICE_FEATURES]
    lfence
    mov ebx, eax

    ; VIO-004 part 1b: check VERSION_1 in high page.
    mfence
    mov dword [r12 + VIRTIO_MMIO_DEVICE_FEATURES_SEL], 1
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_DEVICE_FEATURES]
    lfence
    test eax, VIRTIO_F_VERSION_1_HIBIT
    jz .features_fail_no_v1

    ; VIO-004 part 2: write driver features. Low page = 0; high = VERSION_1.
    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES_SEL], 0
    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES], 0
    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES_SEL], 1
    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES], VIRTIO_F_VERSION_1_HIBIT
    mfence

    ; VIO-005: RMW FEATURES_OK.
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_FEATURES_OK
    mfence
    mov dword [r12 + VIRTIO_MMIO_STATUS], eax
    mfence

    ; VIO-006: re-read Status. Must be ACK|DRIVER|FEATURES_OK.
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    cmp eax, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK
    jne .features_fail_rejected

    lea rsi, [rel features_ok_msg]
    mov ecx, features_ok_len
    call emit_bytes

    ; --- Virtqueue discovery (VIO-007 part 1) ---

    ; Read QueueNumMax for queue 0 (RX).
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_SEL], 0
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_QUEUE_NUM_MAX]
    lfence
    test eax, eax
    jz .queue_fail_rx
    mov ebx, eax                    ; save RX max across next probe

    ; Read QueueNumMax for queue 1 (TX).
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_SEL], 1
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_QUEUE_NUM_MAX]
    lfence
    test eax, eax
    jz .queue_fail_tx

    ; Emit: QUEUES:RX=XXXXXXXX TX=XXXXXXXX
    push rax                        ; preserve TX across emit_bytes
    lea rsi, [rel queues_rx_prefix_msg]
    mov ecx, queues_rx_prefix_len
    call emit_bytes
    mov eax, ebx                    ; RX max → hex
    call emit_hex32
    lea rsi, [rel queues_tx_sep_msg]
    mov ecx, queues_tx_sep_len
    call emit_bytes
    pop rax                         ; restore TX (low 32 bits)
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes

    ; --- VIO-007 Slice 2: initialize virtqueues ---
    program_queue 0, \
        RX_QUEUE_BASE + VIRTQ_OFF_DESC, \
        RX_QUEUE_BASE + VIRTQ_OFF_AVAIL, \
        RX_QUEUE_BASE + VIRTQ_OFF_USED, \
        .queue_ready_fail
    program_queue 1, \
        TX_QUEUE_BASE + VIRTQ_OFF_DESC, \
        TX_QUEUE_BASE + VIRTQ_OFF_AVAIL, \
        TX_QUEUE_BASE + VIRTQ_OFF_USED, \
        .queue_ready_fail

    lea rsi, [rel queues_ready_msg]
    mov ecx, queues_ready_len
    call emit_bytes

    ; --- VIO-008: set DRIVER_OK ---
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_DRIVER_OK
    mfence
    mov dword [r12 + VIRTIO_MMIO_STATUS], eax
    mfence

    ; Cross-check full expected state ACK|DRIVER|FEATURES_OK|DRIVER_OK = 0x0F.
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    cmp eax, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK | VIRTIO_STATUS_DRIVER_OK
    jne .driver_ok_fail

    lea rsi, [rel driver_ok_msg]
    mov ecx, driver_ok_len
    call emit_bytes

    ; --- VIO-R-002: pre-populate RX with 256 WRITE-flagged buffers ---
    ; rdi = loop counter (0..255), rax = scratch, rbx = desc-entry pointer.
    xor edi, edi
.rx_fill_loop:
    ; Descriptor table entry at desc_base + i*16.
    mov eax, edi
    shl eax, 4
    lea rbx, [rax + RX_QUEUE_BASE + VIRTQ_OFF_DESC]

    ; Buffer addr: RX_BUFFER_BASE + i*2048. Stays < 4 GiB so addr_hi=0.
    mov eax, edi
    shl eax, 11
    add eax, RX_BUFFER_BASE

    mov dword [rbx + 0],  eax
    mov dword [rbx + 4],  0
    mov dword [rbx + 8],  RX_BUFFER_SIZE
    mov word  [rbx + 12], VIRTQ_DESC_F_WRITE
    mov word  [rbx + 14], 0

    ; AvailRing.ring[i] = i. Ring slots start past the u16 flags + u16 idx
    ; header at offset +4. RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 4 = 0x401004,
    ; well within disp32 sign-extended range.
    mov [RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 4 + rdi*2], di

    inc edi
    cmp edi, VIRTQ_MAX_SIZE
    jl  .rx_fill_loop

    ; Drain descriptor/ring stores before idx bump.
    mfence

    ; Publish AvailRing.idx = 256.
    mov word [RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 2], VIRTQ_MAX_SIZE

    ; Drain idx bump before kick.
    mfence

    ; QueueNotify(0) — kick RX. Write-only register.
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 0
    mfence

    lea rsi, [rel rx_populated_msg]
    mov ecx, rx_populated_len
    call emit_bytes

    ; --- VIO-R-003/004: poll Used Ring for first RX completion ---
    mov ecx, 100000000
.rx_wait_loop:
    lfence
    mov ax, [RX_QUEUE_BASE + VIRTQ_OFF_USED + 2]
    lfence
    test ax, ax
    jnz .rx_received
    dec ecx
    jnz .rx_wait_loop

    lea rsi, [rel rx_timeout_msg]
    mov ecx, rx_timeout_len
    call emit_bytes
    jmp .halt

.rx_received:
    ; ring[0]: id (u32 @ +4), len (u32 @ +8). edi ← id, ebx ← len.
    lfence
    mov edi, [RX_QUEUE_BASE + VIRTQ_OFF_USED + 4]
    mov ebx, [RX_QUEUE_BASE + VIRTQ_OFF_USED + 8]
    lfence

    ; Bounds check id < VIRTQ_MAX_SIZE.
    cmp edi, VIRTQ_MAX_SIZE
    jae .rx_bad_id_fail

    ; Buffer addr: RX_BUFFER_BASE + id*2048.
    mov eax, edi
    shl eax, 11
    add eax, RX_BUFFER_BASE

    ; Read virtio_net_hdr.hdr_len (u16 @ +2) and num_buffers (u16 @ +10).
    ; eax holds the buffer's 32-bit physical address; long-mode auto-
    ; zero-extends to rax for the address calculation.
    lfence
    movzx esi, word [rax + 2]                        ; hdr_len
    movzx ebp, word [rax + 10]                       ; num_buffers
    lfence

    ; Validate num_buffers == 1 under MVP no-MRG_RXBUF policy.
    cmp ebp, 1
    jne .rx_num_bufs_fail

    ; Emit: RX:FRAME id=... hdr_len=... num_bufs=... used_len=...
    ; Push order: used_len, num_bufs, hdr_len, id (last → first pop = id).
    push rbx                                         ; used_len
    push rbp                                         ; num_buffers
    push rsi                                         ; hdr_len
    push rdi                                         ; id

    lea rsi, [rel rx_frame_prefix_msg]
    mov ecx, rx_frame_prefix_len
    call emit_bytes
    pop rax
    call emit_hex32

    lea rsi, [rel rx_frame_hdr_len_msg]
    mov ecx, rx_frame_hdr_len_len
    call emit_bytes
    pop rax
    call emit_hex32

    lea rsi, [rel rx_frame_num_bufs_msg]
    mov ecx, rx_frame_num_bufs_len
    call emit_bytes
    pop rax
    call emit_hex32

    lea rsi, [rel rx_frame_used_len_msg]
    mov ecx, rx_frame_used_len_len
    call emit_bytes
    pop rax
    call emit_hex32

    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes

    ; --- VIO-R-006/007: return consumed descriptor + notify ---
    ; rdi is clobbered by the emit_hex32 chain above; re-read id from
    ; ring[0].id. Safe in this one-shot MVP — the device's consumer
    ; index advances only when we advance our consumer-shadow, which
    ; we don't track here.
    lfence
    mov edi, [RX_QUEUE_BASE + VIRTQ_OFF_USED + 4]
    lfence

    ; Current AvailRing.idx (u16). The shared-ring re-read pattern
    ; works here because we're the sole writer (§2.7.13) and this
    ; one-shot MVP returns one descriptor only. A multi-frame loop
    ; MUST replace this with a driver-private shadow (task #17).
    movzx eax, word [RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 2]

    ; Slot index = idx mod 256 = idx & 0xFF.
    mov ebx, eax
    and ebx, VIRTQ_MAX_SIZE - 1

    ; ring[slot] = id.
    mov [RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 4 + rbx*2], di

    ; Barrier before idx bump.
    mfence

    ; idx += 1.
    inc eax
    mov [RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 2], ax

    ; Barrier before notify.
    mfence

    ; Kick RX (queue 0).
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 0
    mfence

    lea rsi, [rel rx_returned_msg]
    mov ecx, rx_returned_len
    call emit_bytes

    ; --- VIO-T-002..005: submit one canary Ethernet frame ---
    lea rbx, [TX_QUEUE_BASE + VIRTQ_OFF_DESC]
    ; addr_lo = tx_test_pkt absolute address. NASM in [bits 64]
    ; with `mov dword [...], imm32` writes a sign-extended-to-64
    ; immediate; tx_test_pkt's address is < 4 GiB so the 32-bit
    ; immediate is correct as a phys address. Stays valid only
    ; while paging identity-maps low 4 GiB (D063); a future non-
    ; identity layout would need a phys-frame translation here.
    mov dword [rbx + 0],  tx_test_pkt
    mov dword [rbx + 4],  0
    mov dword [rbx + 8],  tx_test_pkt_len
    mov word  [rbx + 12], 0
    mov word  [rbx + 14], 0

    ; AvailRing.ring[0] = 0 (head descriptor of single-desc TX).
    mov word [TX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 4], 0

    ; Barrier before idx bump.
    mfence

    ; Publish AvailRing.idx = 1.
    mov word [TX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 2], 1

    ; Barrier before notify.
    mfence

    ; Kick TX (queue 1).
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 1
    mfence

    lea rsi, [rel tx_submitted_msg]
    mov ecx, tx_submitted_len
    call emit_bytes

    ; --- VIO-T-006: poll TX Used Ring for completion ---
    mov ecx, 100000000
.tx_wait_loop:
    lfence
    mov ax, [TX_QUEUE_BASE + VIRTQ_OFF_USED + 2]
    lfence
    test ax, ax
    jnz .tx_reclaimed
    dec ecx
    jnz .tx_wait_loop

    lea rsi, [rel tx_timeout_msg]
    mov ecx, tx_timeout_len
    call emit_bytes
    jmp .halt

.tx_reclaimed:
    lfence
    mov edi, [TX_QUEUE_BASE + VIRTQ_OFF_USED + 4]    ; id
    mov ebx, [TX_QUEUE_BASE + VIRTQ_OFF_USED + 8]    ; len
    lfence

    ; Bounds-check id symmetrically with RX.
    cmp edi, VIRTQ_MAX_SIZE
    jae .tx_bad_id_fail

    ; Emit: TX:RECLAIMED id=XXXXXXXX used_len=XXXXXXXX
    push rbx                                         ; used_len
    push rdi                                         ; id

    lea rsi, [rel tx_reclaimed_prefix_msg]
    mov ecx, tx_reclaimed_prefix_len
    call emit_bytes
    pop rax
    call emit_hex32

    lea rsi, [rel tx_used_len_msg]
    mov ecx, tx_used_len_len
    call emit_bytes
    pop rax
    call emit_hex32

    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .halt

.driver_ok_fail:
    push rax
    lea rsi, [rel driver_ok_fail_msg]
    mov ecx, driver_ok_fail_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.rx_bad_id_fail:
    push rdi
    lea rsi, [rel rx_bad_id_msg]
    mov ecx, rx_bad_id_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.rx_num_bufs_fail:
    push rbp
    lea rsi, [rel rx_num_bufs_msg]
    mov ecx, rx_num_bufs_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.tx_bad_id_fail:
    push rdi
    lea rsi, [rel tx_bad_id_msg]
    mov ecx, tx_bad_id_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.queue_ready_fail:
    lea rsi, [rel queues_ready_fail_msg]
    mov ecx, queues_ready_fail_len
    call emit_bytes
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.queue_fail_rx:
    lea rsi, [rel queues_fail_rx_msg]
    mov ecx, queues_fail_rx_len
    call emit_bytes
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.queue_fail_tx:
    lea rsi, [rel queues_fail_tx_msg]
    mov ecx, queues_fail_tx_len
    call emit_bytes
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.features_fail_no_v1:
    push rax                        ; preserve dev_hi
    lea rsi, [rel features_no_v1_prefix_msg]
    mov ecx, features_no_v1_prefix_len
    call emit_bytes
    mov eax, ebx                    ; dev_lo → hex
    call emit_hex32
    lea rsi, [rel features_hi_sep_msg]
    mov ecx, features_hi_sep_len
    call emit_bytes
    pop rax                         ; restore dev_hi
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.features_fail_rejected:
    push rax
    lea rsi, [rel features_rejected_msg]
    mov ecx, features_rejected_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.status_fail:
    push rax
    lea rsi, [rel status_fail_msg]
    mov ecx, status_fail_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.virtio_fail:
    ; eax holds the bogus magic. Skip .set_failed because the
    ; device's register layout is unverified — a Status write would
    ; be blind.
    push rax
    lea rsi, [rel virtio_fail_msg]
    mov ecx, virtio_fail_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .halt

.set_failed:
    ; VIO-009: signal the device that the driver gave up.
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_FAILED
    mfence
    mov dword [r12 + VIRTIO_MMIO_STATUS], eax
    mfence
    ; fall through

.halt:
    cli
.halt_loop:
    hlt
    jmp .halt_loop


; ================================================================
; emit_bytes — [bits 64] LSR-polled serial writer
; ================================================================
; Same contract as the [bits 32] emit_bytes_32 above, just running
; in long mode. Used by all post-mode-switch serial output in
; kernel_main_64. `in/out` work identically in long mode (port I/O
; is unchanged across protected / long modes).
;
;   In:  RSI = source ptr, ECX = byte count, DX = COM1_PORT.
;   Out: CF = 0 on full success, CF = 1 on UART timeout.
;   Clobbers: eax, ecx, esi/rsi, edi, flags.
;   Preserves: dx, rbx, rbp, rsp, r12 (and all other callee-saved).

[bits 64]
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

; ================================================================
; emit_hex32 — [bits 64] hex-emit helper
; ================================================================
; Renders eax as 8 uppercase hex digits via emit_bytes. 8-byte
; stack scratch. Preserves dx (caller's COM1_PORT) and rbx (saved
; on entry); the caller's r12 is preserved because emit_bytes does.
;   In:  EAX = 32-bit value to render.
;   Clobbers: eax, ecx, esi/rsi, edi.
;   Preserves: dx, rbx, rbp, rsp, r12.

emit_hex32:
    push rbx
    sub rsp, 16                   ; 8 bytes scratch + 8 bytes stack alignment
    mov ebx, eax                  ; preserve source across rotate loop
    lea rsi, [rsp]
    mov ecx, 8
.digit:
    rol ebx, 4                    ; top nibble → low 4 bits
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
; Strings + TX canary packet
; ================================================================
; Same content as the prior [bits 32] data — only their addressing
; mode (RIP-relative via `lea rsi, [rel label]`) changed.

ready_msg:       db 'READY', 10
ready_len        equ $ - ready_msg

layout_ok_msg:   db 'LAYOUT-OK', 10
layout_ok_len    equ $ - layout_ok_msg

pvh_fail_msg:    db 'PVH:FAIL bad magic', 10
pvh_fail_len     equ $ - pvh_fail_msg

virtio_ok_msg:   db 'VIRTIO:OK', 10
virtio_ok_len    equ $ - virtio_ok_msg

virtio_fail_msg: db 'VIRTIO:FAIL magic='
virtio_fail_len  equ $ - virtio_fail_msg

status_driver_msg: db 'STATUS:DRIVER', 10
status_driver_len  equ $ - status_driver_msg

status_fail_msg:   db 'STATUS:FAIL status='
status_fail_len    equ $ - status_fail_msg

features_ok_msg:       db 'FEATURES:OK', 10
features_ok_len        equ $ - features_ok_msg

features_no_v1_prefix_msg: db 'FEATURES:FAIL no-v1 dev_lo='
features_no_v1_prefix_len  equ $ - features_no_v1_prefix_msg

features_hi_sep_msg:   db ' dev_hi='
features_hi_sep_len    equ $ - features_hi_sep_msg

features_rejected_msg: db 'FEATURES:FAIL rejected status='
features_rejected_len  equ $ - features_rejected_msg

queues_rx_prefix_msg:  db 'QUEUES:RX='
queues_rx_prefix_len   equ $ - queues_rx_prefix_msg

queues_tx_sep_msg:     db ' TX='
queues_tx_sep_len      equ $ - queues_tx_sep_msg

queues_fail_rx_msg:    db 'QUEUES:FAIL no-queue q=0 (max=0)'
queues_fail_rx_len     equ $ - queues_fail_rx_msg

queues_fail_tx_msg:    db 'QUEUES:FAIL no-queue q=1 (max=0)'
queues_fail_tx_len     equ $ - queues_fail_tx_msg

queues_ready_msg:      db 'QUEUES:READY', 10
queues_ready_len       equ $ - queues_ready_msg

queues_ready_fail_msg: db 'QUEUES:FAIL queue-not-ready'
queues_ready_fail_len  equ $ - queues_ready_fail_msg

driver_ok_msg:         db 'DRIVER_OK', 10
driver_ok_len          equ $ - driver_ok_msg

rx_populated_msg:      db 'RX:POPULATED', 10
rx_populated_len       equ $ - rx_populated_msg

rx_timeout_msg:        db 'RX:TIMEOUT', 10
rx_timeout_len         equ $ - rx_timeout_msg

rx_frame_prefix_msg:   db 'RX:FRAME id='
rx_frame_prefix_len    equ $ - rx_frame_prefix_msg
rx_frame_hdr_len_msg:  db ' hdr_len='
rx_frame_hdr_len_len   equ $ - rx_frame_hdr_len_msg
rx_frame_num_bufs_msg: db ' num_bufs='
rx_frame_num_bufs_len  equ $ - rx_frame_num_bufs_msg
rx_frame_used_len_msg: db ' used_len='
rx_frame_used_len_len  equ $ - rx_frame_used_len_msg

rx_bad_id_msg:         db 'RX:FAIL bad_id='
rx_bad_id_len          equ $ - rx_bad_id_msg
rx_num_bufs_msg:       db 'RX:FAIL num_bufs='
rx_num_bufs_len        equ $ - rx_num_bufs_msg

rx_returned_msg:       db 'RX:RETURNED', 10
rx_returned_len        equ $ - rx_returned_msg

tx_submitted_msg:      db 'TX:SUBMITTED', 10
tx_submitted_len       equ $ - tx_submitted_msg
tx_timeout_msg:        db 'TX:TIMEOUT', 10
tx_timeout_len         equ $ - tx_timeout_msg
tx_reclaimed_prefix_msg: db 'TX:RECLAIMED id='
tx_reclaimed_prefix_len  equ $ - tx_reclaimed_prefix_msg
tx_used_len_msg:       db ' used_len='
tx_used_len_len        equ $ - tx_used_len_msg
tx_bad_id_msg:         db 'TX:FAIL bad_id='
tx_bad_id_len          equ $ - tx_bad_id_msg

tx_test_pkt:
    times 12 db 0                                    ; virtio_net_hdr zero
    db 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF            ; dst MAC broadcast
    db 0x02, 0x00, 0x00, 0x00, 0x00, 0x01            ; src MAC local-admin
    db 0x88, 0xB5                                    ; EtherType 0x88B5
    times 46 db 0xAB                                 ; 46-byte canary payload
tx_test_pkt_len        equ $ - tx_test_pkt

driver_ok_fail_msg:    db 'DRIVER_OK:FAIL status='
driver_ok_fail_len     equ $ - driver_ok_fail_msg

newline:         db 10
