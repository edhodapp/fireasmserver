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
; avail, used). The base address is allocator-placed via the `virtq`
; region in regions.yaml (D066 step 5c, hot-tier); r15 is pinned to
; assigned_addr at kernel_main_64 entry by the codegen-emitted
; memreq_pin_hot.inc. Firecracker zero-initializes guest RAM, so all
; rings are idx=0 / flags=0 at boot — the "uninhabited queue" state
; the spec requires at QueueReady time.
;
; Queue addressing under the migration:
;   RX queue base = r15 + RX_QUEUE_OFF (= r15)
;   TX queue base = r15 + TX_QUEUE_OFF (= r15 + VIRTQ_STRIDE)
; Within each queue: + VIRTQ_OFF_{DESC,AVAIL,USED}.
VIRTQ_STRIDE        equ 0x3000
VIRTQ_OFF_DESC      equ 0x0000
VIRTQ_OFF_AVAIL     equ 0x1000
VIRTQ_OFF_USED      equ 0x2000

VIRTQ_MAX_SIZE      equ 0x100    ; 256 descriptors max
RX_QUEUE_OFF        equ 0 * VIRTQ_STRIDE
TX_QUEUE_OFF        equ 1 * VIRTQ_STRIDE

VIRTQ_DESC_F_NEXT     equ 1
VIRTQ_DESC_F_WRITE    equ 2
VIRTQ_DESC_F_INDIRECT equ 4

; RX buffer pool — 256 × 2 KiB = 512 KiB total. Allocator-placed
; via the `rx_buffer` region in regions.yaml (D066 step 5b);
; consumers load `__memreq_addr__rx_buffer` for the base address.
; The per-buffer size stays a `.equ` constant because it's a
; protocol-level slot width (12 B virtio_net_hdr + max Ethernet
; frame with slack), not a region.
RX_BUFFER_SIZE      equ 0x800

; Device Status (Virtio 1.2 §2.1).
VIRTIO_STATUS_ACKNOWLEDGE equ 1
VIRTIO_STATUS_DRIVER      equ 2
VIRTIO_STATUS_DRIVER_OK   equ 4
VIRTIO_STATUS_FEATURES_OK equ 8
VIRTIO_STATUS_FAILED      equ 0x80

; Feature bit (Virtio 1.2 §6 — VIRTIO_F_VERSION_1 is bit 32 globally,
; bit 0 of the high page).
VIRTIO_F_VERSION_1_HIBIT  equ 1

; ================================================================
; Virtio feature negotiation policy
; ================================================================
; The driver negotiates a SUBSET of the features the device
; offers. Three classes per Virtio 1.2 §2.2:
;
;   REQUIRED — must be offered by the device AND set in our
;              driver_features. If the device doesn't offer one
;              of these, we fail fast at .features_fail_no_v1.
;
;   ACCEPTED — set in driver_features iff offered by the device.
;              An offered ACCEPTED bit becomes a driver-side
;              capability; an unoffered one is silently absent.
;
;   REJECTED — implicit: any bit not in REQUIRED or ACCEPTED.
;              These bits stay zero in driver_features regardless
;              of whether the device offered them.
;
; Negotiation formula:
;     driver_features = (offered & ACCEPTED) | REQUIRED
;
; Today's policy (minimum viable for an actual L2-only guest):
;
;   REQUIRED_LO = 0 — no net-specific feature in the low page is
;     architecturally load-bearing today.
;   REQUIRED_HI = VIRTIO_F_VERSION_1_HIBIT — the entire MMIO 1.2
;     protocol assumes modern mode. Legacy mode would need a
;     completely different driver shape.
;
;   ACCEPTED_LO = 0 — every §5.1.3 bit is rejected because we
;     have no infrastructure for the feature it advertises:
;       F_CSUM / F_GUEST_CSUM (0,1):    no checksum offload yet
;       F_CTRL_GUEST_OFFLOADS (2):      no control vq
;       F_MTU (3):                      MTU is implicit
;       F_MAC (5):                      we hardcode GUEST_MAC
;       F_GSO / GUEST_TSO* / UFO (6-10):need RX reassembly
;       F_HOST_TSO* / HOST_UFO (11-14): no segmented TX
;       F_MRG_RXBUF (15):               we assume num_buffers=1
;       F_STATUS (16):                  link state not consumed yet
;       F_CTRL_VQ + variants (17-23):   no control vq
;       F_MQ (22):                      single queue pair
;       F_CTRL_MAC_ADDR (23):           MAC is hardcoded
;   ACCEPTED_HI = VIRTIO_F_VERSION_1_HIBIT — these high-page
;     extensions are explicitly REJECTED today:
;       VIRTIO_F_ACCESS_PLATFORM (33):  no IOMMU
;       VIRTIO_F_RING_PACKED (34):      we implement split rings
;       VIRTIO_F_IN_ORDER (35):         no in-order optimization
;       VIRTIO_F_ORDER_PLATFORM (36):   TSO + LDARH cover ordering
;       VIRTIO_F_SR_IOV (37):           no PCI / SR-IOV
;       VIRTIO_F_NOTIFICATION_DATA (38):not consumed
;
; Future expansion: adding a feature to ACCEPTED requires adding
; the consuming code that USES that feature. Don't expand the
; mask speculatively — every accepted bit is a wire-protocol
; contract we have to honor.
VIRTIO_REQUIRED_FEATURES_LO   equ 0
VIRTIO_REQUIRED_FEATURES_HI   equ VIRTIO_F_VERSION_1_HIBIT
VIRTIO_ACCEPTED_FEATURES_LO   equ 0
VIRTIO_ACCEPTED_FEATURES_HI   equ VIRTIO_F_VERSION_1_HIBIT

; ----- External symbols (resolved by linker) -----

extern mode_switch_to_long_mode
extern init_memory_layout
extern l2_dispatch          ; arch/x86_64/l2/dispatcher.S (FSA-1)
extern l2_tx_pending_init   ; arch/x86_64/l2/tx_api.S — Vyukov ring seq[] pre-init
extern arp_cache_tick       ; arch/x86_64/l2/arp_cache.S — D068 item 6.e timer ticks
extern arp_send_request     ; arch/x86_64/l2/arp_cache.S — 6.f gratuitous ARP at boot
%ifdef TXAPI_PREBAKE
; arch/x86_64/l2/tx_api.S — producer-side functions for the TX
; API end-to-end test pre-bake. Build-flag-gated so production
; builds (TXAPI_PREBAKE undefined) don't pull these symbols.
extern l2_tx_buffer_pool_alloc
extern l2_tx_buffer_addr
extern l2_tx_enqueue
; arch/x86_64/l2/arp_cache.S — outbound ARP initiator
; (D068 phase 6.c) + arp_resolve API (6.d) +
; arp_cache_insert (6.a). All gated on TXAPI_PREBAKE so the
; production build doesn't auto-call them until 6.f wires
; gratuitous ARP intentionally.
extern arp_send_request
extern arp_resolve
extern arp_cache_insert
L2_TX_PAYLOAD_OFFSET equ 26
%endif

; ----- PVH ELF note -----

section .note.Xen note alloc align=4
    dd 4
    dd 4
    dd XEN_ELFNOTE_PHYS32_ENTRY
    db 'Xen', 0
    align 4
    dd _start

section .text

; D066 step 5a — `.memreq` records emitted by `memreq-codegen`
; from arch/x86_64/platform/firecracker/regions.yaml. The
; included file switches to `[section .memreq]`, emits records,
; and ends with `__SECT__` so this `.text` is restored before
; any code below.
%include "memreq_records.inc"

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
;   eax/ebx/ecx/edx/esi/edi/ebp — same roles as the prior [bits 32]
;         code; long-mode auto-zero-extends 32-bit writes to the
;         full 64-bit register. Pointer ops use rax/rbx/rsi/rdi/rbp.
;         No register is reserved across emit_bytes / emit_hex32
;         beyond SysV's standard callee-saved set — those helpers
;         self-load COM1_PORT into dx on entry so callers can use
;         rdx freely. (Pre-2026-05-22 dx was pinned to COM1_PORT
;         across every call; that constraint produced two real
;         bugs and is now lifted at the source.)

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
; program_queue: configure one virtqueue via the virtio-MMIO
; control window. r15 (D066 step 5c hot-tier pin) holds VIRTQ_BASE.
; r12 holds VIRTIO_MMIO_BASE per kernel_main_64's manual pin.
;
;   %1 = queue index (0 for RX, 1 for TX) — passed to QUEUE_SEL.
;   %2 = queue offset within the virtq region (RX_QUEUE_OFF /
;        TX_QUEUE_OFF) — added to r15 to find this queue's bytes.
;   %3 = .fail_label to branch to if QUEUE_READY readback != 1.
;
; Truncating r15 + offset + per-ring offset to 32 bits via
; `lea eax, [r15 + ...]` is correct: the allocator places virtq
; under low 4 GiB (D063 identity-map coverage), so the high 32 bits
; of every address here are zero. QUEUE_*_HIGH gets a literal 0.
%macro program_queue 3
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
    lea eax, [r15 + %2 + VIRTQ_OFF_DESC]
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DESC_LOW], eax
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DESC_HIGH], 0
    mfence
    lea eax, [r15 + %2 + VIRTQ_OFF_AVAIL]
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DRIVER_LOW], eax
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DRIVER_HIGH], 0
    mfence
    lea eax, [r15 + %2 + VIRTQ_OFF_USED]
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DEVICE_LOW], eax
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_DEVICE_HIGH], 0
    ; Avail/Used ring header zero (belt-and-suspenders; Firecracker
    ; zero-inits but spec doesn't mandate it).
    mov dword [r15 + %2 + VIRTQ_OFF_AVAIL], 0
    mov dword [r15 + %2 + VIRTQ_OFF_USED], 0
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_READY], 1
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_QUEUE_READY]
    lfence
    cmp eax, 1
    jne %3
%endmacro

[bits 64]
global kernel_main_64
kernel_main_64:
    ; D060 step 4.2 + D066 step 5a/b/c/d: init-time static memory
    ; allocator. .memreq carries four records (smoke_test,
    ; rx_buffer, virtq, kstack_boot); the allocator iterates them
    ; and writes assigned_addr/assigned_size in place.
    call init_memory_layout

    ; Hot-tier register pins emitted by `memreq-codegen` from the
    ; `tier: hot` regions in regions.yaml (D066 Q-B). Loads
    ; r15 = __memreq_assigned__virtq (VIRTQ_BASE) so all subsequent
    ; RX/TX queue accesses use r15-relative addressing.
%include "memreq_pin_hot.inc"

    ; D066 step 5d — switch SP from the linker-reserved bootstrap
    ; stack to the allocator-placed kernel stack. The bootstrap
    ; stack was only needed for init_memory_layout itself
    ; (REQ MR-003); kstack_boot is lifetime=stack so the allocator
    ; reverse-bumped it down from __ram_top per AL-002. x86_64
    ; grows down — SP = assigned_addr + assigned_size.
    ;
    ; Safe to switch here: init_memory_layout's return already
    ; consumed its bootstrap-stack frame, and we've not yet made
    ; any post-allocator `call` that would expect a return address
    ; on the bootstrap stack (the memreq_pin_hot.inc above is
    ; pure register loads).
    mov rax, [rel __memreq_assigned__kstack_boot]
    add rax, [rel __memreq_size__kstack_boot]
    mov rsp, rax

    ; (No dx pin needed — emit_bytes / emit_hex32 load COM1_PORT
    ; internally on each call. Pre-2026-05-22 this slot held a
    ; `mov dx, COM1_PORT` to pin the UART base across the whole
    ; function; the pin was removed when the COM1_PORT load was
    ; moved into emit_bytes itself, freeing rdx as a normal
    ; caller-saved scratch register.)

    ; Emit "LAYOUT-OK\n" — D060 step 4.2 success marker. Reached
    ; only when init_memory_layout returned cleanly (it halts
    ; internally on overflow per its contract). Tracer-bullet
    ; harness can assert on this to know the allocator pass landed.
    lea rsi, [rel layout_ok_msg]
    mov ecx, layout_ok_len
    call emit_bytes

    ; --- TX API: one-time Vyukov ring pre-init ---
    ;
    ; The l2_tx_pending region holds the producer-facing send queue
    ; (docs/l2/TX_API.md). The Vyukov MPSC discipline requires
    ; seq[i] = i at start; the allocator's zero-init leaves all
    ; seq fields at 0, which is correct only for slot 0. Walk the
    ; ring once here to fix the other slots. After this point any
    ; caller (test scaffolding today, L3 later) can call
    ; l2_tx_enqueue safely.
    call l2_tx_pending_init

    ; Pin VIRTIO_MMIO_BASE in r12 for the rest of the function.
    ; The 32-bit-dest form `mov r12d, imm32` zero-extends to r12,
    ; which is what we want — `mov r12, imm32` would sign-extend
    ; (turning 0xC0001000 into 0xFFFFFFFFC0001000). The encoding
    ; saved here vs movabs r64,imm64 is 4 bytes, but correctness
    ; is the actual reason for the form.
    mov r12d, VIRTIO_MMIO_BASE

    ; --- Probe virtio-MMIO MagicValue register ---
    ; ============================================================
    ; P4 (H5 part 2) — x86_64 init-path barrier policy.
    ;
    ; The init / probe / negotiation / queue-setup / notify
    ; sequence uses mfence around stores and lfence around loads
    ; per the Virtio 1.2 §4.2.2 envelope. Under x86 TSO most of
    ; these are conservative — stores from a single core are
    ; observed in program order by other observers without an
    ; explicit fence — but the strongest envelope is the least-
    ; surprising choice for one-shot init hand-off sequences.
    ;
    ; Unlike aarch64 there's no narrower mfence variant on x86
    ; (lfence/mfence/sfence are domain-flat). The notify path
    ; (cacheable virtqueue store → mfence → MMIO notify write) is
    ; the only place mfence is genuinely load-bearing; the rest
    ; could be sfence or removed under careful analysis. Same
    ; trade-off as aarch64 — not worth the churn for a one-shot
    ; init that already runs in a few microseconds.
    ; ============================================================

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

    ; §4.2.4 reset wait — bounded. H4: on timeout, emit an
    ; explicit RESET:FAIL diagnostic rather than falling through
    ; and conflating with the STATUS:FAIL path. eax holds the last
    ; observed non-zero status value at timeout exit.
    mov ecx, 0x100000
.reset_wait:
    lfence
    mov eax, [r12 + VIRTIO_MMIO_STATUS]
    lfence
    test eax, eax
    jz  .reset_done
    dec ecx
    jnz .reset_wait
    jmp .reset_fail
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
    ;
    ; Policy-driven per VIRTIO_REQUIRED_FEATURES_* /
    ; VIRTIO_ACCEPTED_FEATURES_* defined near the top of this
    ; file. The formula:
    ;     driver_features = (offered & ACCEPTED) | REQUIRED
    ; expresses both halves at once — required bits are pinned
    ; on, accepted bits pass through iff the device offered
    ; them, rejected bits stay off no matter what.

    ; VIO-004 part 1a: read low-page offered features (saved in
    ; ebx for the AND with ACCEPTED_LO below AND for the
    ; diagnostic dump on no-required fail).
    mfence
    mov dword [r12 + VIRTIO_MMIO_DEVICE_FEATURES_SEL], 0
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_DEVICE_FEATURES]
    lfence
    mov ebx, eax

    ; VIO-004 part 1b: read high-page offered features.
    mfence
    mov dword [r12 + VIRTIO_MMIO_DEVICE_FEATURES_SEL], 1
    mfence
    lfence
    mov eax, [r12 + VIRTIO_MMIO_DEVICE_FEATURES]
    lfence

    ; Verify every REQUIRED_HI bit is offered by the device.
    ; The `and` masks off non-required bits, then the equality
    ; compare confirms every required bit survived. With
    ; REQUIRED_HI == VIRTIO_F_VERSION_1_HIBIT today this is
    ; equivalent to the prior `test eax, VIRTIO_F_VERSION_1_HIBIT
    ; / jz` form; it generalises when REQUIRED gains bits.
    ; (Low-page required check is omitted today because
    ; REQUIRED_LO = 0 — every bit is satisfied vacuously. Add
    ; the symmetric ebx-based check when REQUIRED_LO is non-
    ; zero.)
    mov ecx, eax
    and ecx, VIRTIO_REQUIRED_FEATURES_HI
    cmp ecx, VIRTIO_REQUIRED_FEATURES_HI
    jne .features_fail_no_v1

    ; VIO-004 part 2: compute and write driver features.
    ;     driver = (offered & ACCEPTED) | REQUIRED
    ; ebx still holds offered_lo; eax holds offered_hi.
    and ebx, VIRTIO_ACCEPTED_FEATURES_LO
    or  ebx, VIRTIO_REQUIRED_FEATURES_LO
    and eax, VIRTIO_ACCEPTED_FEATURES_HI
    or  eax, VIRTIO_REQUIRED_FEATURES_HI

    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES_SEL], 0
    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES], ebx
    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES_SEL], 1
    mfence
    mov dword [r12 + VIRTIO_MMIO_DRIVER_FEATURES], eax
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
    mov ebx, eax                    ; save RX max (raw) for diag emit

    ; Pin clamped RX queue size in r13 for the populate loop and for
    ; the AvailRing.idx publish. The program_queue macro clamps the
    ; QueueNum the device sees to MIN(QueueNumMax, VIRTQ_MAX_SIZE);
    ; the driver-side population MUST match or it would write past
    ; the avail-ring end. Without this, a device reporting
    ; QueueNumMax < 256 would corrupt memory at populate. Firecracker
    ; reports 256 today (seen in the trace as QUEUES:RX=00000100), so
    ; the clamp is a no-op in production — the protection is for any
    ; future VMM with smaller queues.
    cmp eax, VIRTQ_MAX_SIZE
    jbe .Lrx_max_clamped
    mov eax, VIRTQ_MAX_SIZE
.Lrx_max_clamped:
    mov r13d, eax                   ; r13 = clamped RX queue size = N

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
    ; r15 (memreq virtq pin) holds VIRTQ_BASE; the macro computes
    ; per-queue desc/avail/used addresses as r15-relative offsets.
    program_queue 0, RX_QUEUE_OFF, .queue_ready_fail
    program_queue 1, TX_QUEUE_OFF, .queue_ready_fail

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
    lea rbx, [rax + r15 + VIRTQ_OFF_DESC]

    ; Buffer addr: __memreq_addr__rx_buffer (cold-tier lookup) + i*2048.
    ; One memory load per loop iteration; acceptable because RX
    ; populate runs once at boot. addr_hi=0 because the allocator
    ; places under low 4 GiB (current identity-map coverage per D063).
    mov rax, [rel __memreq_addr__rx_buffer]
    mov ecx, edi
    shl ecx, 11
    add rax, rcx

    mov dword [rbx + 0],  eax
    mov dword [rbx + 4],  0
    mov dword [rbx + 8],  RX_BUFFER_SIZE
    mov word  [rbx + 12], VIRTQ_DESC_F_WRITE
    mov word  [rbx + 14], 0

    ; AvailRing.ring[i] = i. Ring slots start past the u16 flags + u16 idx
    ; header at offset +4. [r15 + VIRTQ_OFF_AVAIL + 4 + rdi*2] is a
    ; SIB-form addressing the i-th ring slot; the VIRTQ_OFF_AVAIL + 4
    ; constant fits in disp32 and r15 holds VIRTQ_BASE (allocator-
    ; placed under low 4 GiB per D063).
    mov [r15 + VIRTQ_OFF_AVAIL + 4 + rdi*2], di

    inc edi
    cmp edi, r13d
    jl  .rx_fill_loop

    ; Drain descriptor/ring stores before idx bump.
    mfence

    ; Publish AvailRing.idx = N (clamped queue size). Today N = 256
    ; (Firecracker), so the wire form is identical to the prior
    ; hardcoded VIRTQ_MAX_SIZE. The r13 plumbing protects against a
    ; future VMM with a smaller power-of-2 queue size.
    mov word [r15 + VIRTQ_OFF_AVAIL + 2], r13w

    ; Pin driver-private next-avail shadow in r14. After RX populate
    ; idx == N, so next_avail = N. The shadow replaces the prior
    ; pattern of re-reading AvailRing.idx from shared memory in the
    ; return path — the field is driver-owned (Virtio §2.7.13), but
    ; trusting device-readable shared memory for our own state is
    ; the kind of pattern that turns into a security incident the
    ; first time a malicious or buggy VMM tampers with it.
    mov r14d, r13d

    ; Drain idx bump before kick.
    mfence

    ; QueueNotify(0) — kick RX. Write-only register.
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 0
    mfence

    lea rsi, [rel rx_populated_msg]
    mov ecx, rx_populated_len
    call emit_bytes

    ; --- FSA-4: initialise l2_state.rx_next_avail ---
    ;
    ; Match the AvailRing.idx just published, which used r13w
    ; (the runtime-clamped queue size, MIN(QueueNumMax, VIRTQ_MAX_SIZE)).
    ; Hardcoding VIRTQ_MAX_SIZE here would diverge if a future
    ; VMM ever reports QueueNumMax < VIRTQ_MAX_SIZE — clean-Claude
    ; flagged this in the pre-push review.
    ;
    ; The other three shadows (rx_used_shadow, tx_next_avail,
    ; tx_used_shadow) are not initialised here: they read as zero
    ; on first call because Firecracker pre-zeros guest RAM on VM
    ; creation. (The allocator pass writes assigned_addr /
    ; assigned_size into each memreq record but does NOT zero the
    ; backing storage.) Same precondition the existing virtqueue
    ; uninhabited-at-boot reasoning relies on.
    ;
    ; Layout in arch/x86_64/.../regions.yaml's l2_state entry.
    mov rax, [rel __memreq_addr__l2_state]
    mov word [rax + 0], r13w                          ; rx_next_avail = N (clamped)
    mfence

    ; --- D068 item 6.f: gratuitous ARP at boot ---
    ;
    ; Announce our MAC binding for GUEST_IP (192.168.42.2
    ; wire LE = 0x012AA8C0 — wait, GUEST_IP wire bytes are
    ; c0 a8 2a 02 → LE u32 = 0x022AA8C0). arp_send_request
    ; uses SPA = GUEST_IP in the request body, so TPA =
    ; GUEST_IP yields SPA == TPA per RFC 5227.
    mov edi, 0x022AA8C0
    call arp_send_request

    ; --- FSA-1: hand off to L2 dispatcher ---
    ;
    ; Everything from this point through RX consume, TX submit, and
    ; TX reclaim lives in arch/x86_64/l2/dispatcher.S behind the
    ; single SysV entry point `l2_dispatch`. The function returns
    ;   eax = 0  → clean halt (success path or completion timeout)
    ;   eax = 1  → set VIRTIO_STATUS_FAILED then halt (validation fail)
    ;
    ; Entry contract: r12 = VIRTIO_MMIO base, r13 = N, r14 =
    ; next_avail, r15 = virtq base. SysV callee-saved set (rbx,
    ; rbp, r12-r15) preserved by l2_dispatch's prologue/epilogue.
    ; FSA-4(A): loop l2_dispatch so reentrance from FSA-4(B) is
    ; actually exercised. 100 iterations gives the host enough
    ; time to send an ARP request (or other inbound traffic) and
    ; observe the reply land via tap0. rbx holds the counter
    ; across `call l2_dispatch` (SysV callee-saved; dispatcher
    ; saves/restores rbx in its prologue/epilogue).
%ifdef TXAPI_PREBAKE
    ; --- TX API end-to-end test pre-bake (phase b.4) ---
    ;
    ; Build-flag-gated: -DTXAPI_PREBAKE=1 turns this on. OFF for
    ; production builds (Pi deploys, normal CD). ON for the
    ; tooling/tests/integration/test_l2_tx_api.py test which
    ; sniffs tap0 for the resulting wire frame.
    lea rsi, [rel txapi_prebake_msg]
    mov ecx, txapi_prebake_len
    call emit_bytes
    ;
    ; Allocates one pool buffer, writes the ASCII payload
    ; "TXAPI-TEST" at +PAYLOAD_OFFSET, enqueues one request
    ; targeting broadcast MAC with the Local Experimental
    ; ethertype 0x88B5. The dispatcher's TX phase drains the
    ; queue on the first iteration of .dispatch_loop and submits
    ; the wire frame to the host's virtio-net device.
    ;
    ; Caller-pin discipline: r12 (VIRTIO_MMIO_BASE), r13 (N),
    ; r15 (virtq base) are not touched by any call in this block
    ; per the tx_api.S clobber lists (none use r12-r15). r14 is
    ; freshly loaded by the dispatcher's prologue from l2_state
    ; on the first .dispatch_loop iteration, so its pre-call
    ; contents don't matter here.
    call l2_tx_buffer_pool_alloc                ; eax = pool_idx
    push rax                                    ; save idx across the addr call
    mov edi, eax
    call l2_tx_buffer_addr                      ; rax = pool buffer base
    add rax, L2_TX_PAYLOAD_OFFSET               ; rax = producer payload slot
    ; ASCII bytes "TXAPI-TEST" = 0x54 58 41 50 49 2D 54 45 53 54.
    ; As LE 64-bit at +0: bytes 0..7 = T X A P I - T E
    ;   = 0x4554_2D49_5041_5854
    mov rcx, 0x45542D4950415854
    mov [rax + 0], rcx
    mov word [rax + 8], 0x5453                  ; bytes 8-9 = 'S' 'T' (LE = 0x5453)
    pop rax                                     ; rax = pool_idx
    lea rdi, [rel txapi_prebake_dst_mac]
    ; ethertype wire bytes 0x88 0xB5 — stored LE in slot so the
    ; consumer's strh copies them verbatim to wire bytes 24,25.
    mov esi, 0xB588
    mov edx, eax                                ; pool_idx
    mov ecx, 10                                 ; payload_len
    call l2_tx_enqueue
    ; eax = 0 (OK) or 1 (FULL). The first enqueue cannot fail —
    ; pool just inited and ring is empty. We do not assert; a
    ; FULL return would result in no frame being sent and the
    ; pytest assertion would catch it as a missing capture.

    ; --- Phase 6.c: also exercise the ARP initiator. ---
    ; Send a broadcast ARP request for HOST_DEFAULT_IP
    ; (192.168.42.1, wire bytes c0 a8 2a 01 → LE u32 = 0x012AA8C0).
    ; The dispatcher's TX queue-drain consumer (b.3) submits
    ; this on the same or next iteration as the TXAPI test
    ; frame; tooling/tests/integration/test_arp_initiator.py
    ; sniffs tap0 for it.
    mov edi, 0x012AA8C0
    call arp_send_request

    ; --- Phase 6.d: exercise arp_resolve (cache + initiator). ---
    ; Use a DIFFERENT target IP (192.168.42.99 → wire LE
    ; 0x632AA8C0) so the resolve traffic doesn't collide with
    ; the 6.c HOST_IP request just enqueued. Three steps:
    ;   1. resolve(IP) on empty cache → MISS path: insert
    ;      INCOMPLETE, send request, return PENDING.
    ;   2. arp_cache_insert(IP, fake_mac, REACHABLE) directly,
    ;      bypassing the wire (the host wouldn't reply to a
    ;      synthetic test IP anyway).
    ;   3. resolve(IP) again → cache HIT REACHABLE → OK.
    ; The test asserts ARP:RESOLVE_PENDING then ARP:RESOLVE_OK
    ; in that order.
    sub rsp, 16                                      ; scratch for resolve out-buf
    mov edi, 0x632AA8C0
    mov rsi, rsp
    call arp_resolve                                 ; expect PENDING
    mov edi, 0x632AA8C0
    lea rsi, [rel arp_resolve_test_fake_mac]
    mov edx, 2                                       ; ARP_STATE_REACHABLE
    call arp_cache_insert
    mov edi, 0x632AA8C0
    mov rsi, rsp
    call arp_resolve                                 ; expect OK
    add rsp, 16

    ; --- Phase 6.e.2: INCOMPLETE entry that nobody replies to. ---
    ; 192.168.42.100 (wire LE 0x642AA8C0) isn't in tap0's host
    ; kernel routing → Linux won't reply. The arp_resolve
    ; below inserts an INCOMPLETE entry and sends one request;
    ; tick fires after ARP_AGING_CYCLES and transitions to
    ; FAILED. Test asserts ARP:STATE_CHANGE ip=642AA8C0
    ; old=00000001 new=00000005.
    sub rsp, 16                                      ; scratch for resolve out-buf
    mov edi, 0x642AA8C0
    mov rsi, rsp
    call arp_resolve                                 ; expect PENDING (cache miss)
    add rsp, 16
%endif
    ; Dispatch loop iteration cap. Was 100 (Pi flake era). Bumped
    ; to 200 to give the 6.e state machine's per-entry transition
    ; chains (REACHABLE → STALE → PROBE → 3 retries → FAILED is
    ; up to 6 actions × ~4 iters/action = ~24 iters per entry,
    ; and tests now pre-bake two entries that drive concurrent
    ; chains) enough runway to complete observably.
    mov ebx, 500
.dispatch_loop:
    call l2_dispatch
    test eax, eax
    jnz .set_failed
    ; D068 item 6.e: drive ARP cache timer-based transitions
    ; (REACHABLE → STALE; further transitions in follow-on
    ; commits). One tick per dispatcher iteration. Tick uses
    ; only caller-saved regs + its own stack frame; r12/r13/r15
    ; pinned state survives automatically (callee-saved).
    call arp_cache_tick
    dec ebx
    jnz .dispatch_loop
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

.reset_fail:
    ; H4: bounded reset wait timed out. eax holds the last observed
    ; non-zero status value. Distinct from .status_fail (which fires
    ; on a post-ACK / FEATURES_OK / DRIVER_OK status mismatch) so
    ; the diagnostic disambiguates "device never reset" from
    ; "device reset but the next stage's status assertion failed".
    push rax
    lea rsi, [rel reset_fail_msg]
    mov ecx, reset_fail_len
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
;   In:  RSI = source ptr, ECX = byte count.
;   Out: CF = 0 on full success, CF = 1 on UART timeout.
;   Clobbers: eax, ecx, edx (loaded with COM1_PORT internally),
;             esi/rsi, edi, flags.
;   Preserves: rbx, rbp, rsp, r12 (and all other callee-saved).
;
; History (2026-05-22): emit_bytes used to REQUIRE dx = COM1_PORT
; on entry AND preserved dx across the call. That contract turned
; rdx into a project-wide "do not clobber across emit_bytes"
; register and produced two real bugs (FSA-4(A) ARP-recognition
; clobber → bus_error spam; near-miss in MAC filter's register
; choice). The pin is now load-internal: emit_bytes derives
; COM1_PORT from the assemble-time constant on every entry,
; so callers (a) don't need to set dx and (b) can use rdx as a
; normal caller-saved scratch register across the call. The
; cycle cost is a single `mov dx, imm16` per emit_bytes
; invocation — UART throughput is irrelevant compared to the
; safety win.

; Cross-file symbols consumed by arch/x86_64/l2/dispatcher.S:
;   emit_bytes, emit_hex32 — UART write helpers
;   newline                — single-byte 0x0A string
;   tx_test_pkt            — canary TX frame (72 bytes)
global emit_bytes
global emit_hex32
global newline
global tx_test_pkt

[bits 64]
emit_bytes:
    test ecx, ecx
    jz .done
    ; Load COM1_PORT internally so the caller doesn't have to pin
    ; it in dx across calls. The .next loop's add/sub COM1_LSR_OFFSET
    ; dance keeps dx synchronised with COM1_PORT between iterations,
    ; so this is a one-shot load per call regardless of byte count.
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

; ================================================================
; emit_hex32 — [bits 64] hex-emit helper
; ================================================================
; Renders eax as 8 uppercase hex digits via emit_bytes. 8-byte
; stack scratch. Preserves rbx (saved on entry); the caller's r12
; is preserved because emit_bytes does.
;
;   In:  EAX = 32-bit value to render.
;   Clobbers: eax, ecx, edx (inherited from emit_bytes), esi/rsi, edi.
;   Preserves: rbx, rbp, rsp, r12.

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

reset_fail_msg:    db 'RESET:FAIL status='
reset_fail_len     equ $ - reset_fail_msg

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


; tx_test_pkt's src MAC must stay in sync with the GUEST_MAC
; constants in arch/x86_64/l2/dispatcher.S (which the MAC
; filter compares incoming dst MACs against, and which the
; dispatcher writes as SHA into ARP reply payloads enqueued
; via the TX API). Each file asserts ETH-016 locally;
; cross-file consistency is human discipline.
GUEST_MAC_BYTE_0  equ 0x02
GUEST_MAC_BYTE_1  equ 0x00
GUEST_MAC_BYTE_2  equ 0x00
GUEST_MAC_BYTE_3  equ 0x00
GUEST_MAC_BYTE_4  equ 0x00
GUEST_MAC_BYTE_5  equ 0x01

; ETH-016 (IEEE 802.3 §4.1.2.1): outgoing source MAC MUST
; have the unicast bit clear. tx_test_pkt's src MAC is the
; only outgoing src this file places — the dispatcher's
; ARP reply builder writes its own (asserted there too).
; Build-time check covers it before ship.
%if GUEST_MAC_BYTE_0 & 1
%error "ETH-016: GUEST_MAC_BYTE_0 has multicast bit set"
%endif

tx_test_pkt:
    times 12 db 0                                    ; virtio_net_hdr zero
    db 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF            ; dst MAC broadcast
    db GUEST_MAC_BYTE_0, GUEST_MAC_BYTE_1, GUEST_MAC_BYTE_2, \
       GUEST_MAC_BYTE_3, GUEST_MAC_BYTE_4, GUEST_MAC_BYTE_5  ; src MAC = GUEST_MAC
    db 0x88, 0xB5                                    ; EtherType 0x88B5
    times 46 db 0xAB                                 ; 46-byte canary payload

%ifdef TXAPI_PREBAKE
; Broadcast dst MAC for the TX API end-to-end test pre-bake.
; Distinct label so the rodata pointer is stable for the
; `lea rdi, [rel ...]` load.
txapi_prebake_dst_mac:
    db 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
txapi_prebake_msg:     db 'TXAPI:PREBAKE', 10
txapi_prebake_len      equ $ - txapi_prebake_msg
; Fake MAC for the 6.d arp_resolve cache-HIT test. Caller
; passes this as the mac_ptr arg to arp_cache_insert; the
; subsequent resolve copies it into the output buffer.
; Distinctive value to make a bad-copy bug visually obvious.
arp_resolve_test_fake_mac:
    db 0x02, 0xDE, 0xAD, 0xBE, 0xEF, 0x99
%endif

driver_ok_fail_msg:    db 'DRIVER_OK:FAIL status='
driver_ok_fail_len     equ $ - driver_ok_fail_msg

newline:         db 10

section .note.GNU-stack noalloc noexec nowrite progbits
