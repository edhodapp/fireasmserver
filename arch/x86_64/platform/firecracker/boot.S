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
; Full layout isn't needed yet — only the registers touched by the
; init prefix (VIO-001..003) and feature negotiation (VIO-004..006).
; Virtqueue registers (QueueSel, QueueDesc*, QueueDriver*, QueueDevice*,
; QueueReady, QueueNotify) land with VIO-007.
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

; Virtqueue memory region. 12 KiB per queue — page-aligned slots
; for descriptor table (4 KiB), available ring (4 KiB), used ring
; (4 KiB). Actual spec-required sizes at queue_size=256 are
; ~4 KiB / ~518 B / ~2054 B; page alignment costs ~5 KiB per
; queue against 128 MiB of guest RAM (negligible) and simplifies
; every address computation to a base + page-offset.
;
; Location: 4 MiB, well above the stack at 2 MiB and the .text
; segment (loaded at 1 MiB, runs out long before 2 MiB).
; Firecracker zero-initializes guest RAM, so ``idx = 0``,
; ``flags = 0`` across every ring at boot — which is exactly
; the "uninhabited queue" state the spec requires at QueueReady
; time.
VIRTQ_BASE          equ 0x400000
VIRTQ_STRIDE        equ 0x3000   ; 12 KiB per queue
VIRTQ_OFF_DESC      equ 0x0000
VIRTQ_OFF_AVAIL     equ 0x1000
VIRTQ_OFF_USED      equ 0x2000

; Largest queue size this layout supports: 256 descriptors at
; 16 B each = 4096 B exactly, filling the descriptor-table page.
; A device reporting ``QueueNumMax > 256`` is spec-allowed to
; offer that size; the driver clamps to the size it can back
; with allocated memory (Virtio 1.2 §4.1.4.3 permits any queue
; size that is a power of 2 ≤ QueueNumMax). Today Firecracker
; reports exactly 256; the clamp protects a future device or
; VMM change from silently overflowing into the available-ring
; page.
VIRTQ_MAX_SIZE      equ 0x100    ; 256 descriptors
RX_QUEUE_BASE       equ VIRTQ_BASE + 0 * VIRTQ_STRIDE
TX_QUEUE_BASE       equ VIRTQ_BASE + 1 * VIRTQ_STRIDE

; Virtqueue descriptor flag bits (Virtio 1.2 §2.7.5.1).
VIRTQ_DESC_F_NEXT     equ 1    ; buffer continues via next field
VIRTQ_DESC_F_WRITE    equ 2    ; device may write to this buffer (RX)
VIRTQ_DESC_F_INDIRECT equ 4    ; buffer contains a descriptor chain

; RX buffer pool. 256 buffers × 2 KiB each = 512 KiB total, living
; directly above the virtqueue pages (virtqueues end at
; VIRTQ_BASE + 2*VIRTQ_STRIDE = 0x406000). 2 KiB per buffer
; comfortably holds the 12-byte virtio_net_hdr + a max-size tagged
; Ethernet frame (1522 B incl. VLAN, FCS stripped by host) with
; slack. One buffer per descriptor — no descriptor chaining
; (VIRTIO_NET_F_MRG_RXBUF wasn't negotiated under the VIO-F-001
; MVP subset, so every RX completion fits in a single descriptor).
RX_BUFFER_BASE      equ 0x500000
RX_BUFFER_SIZE      equ 0x800    ; 2 KiB

; Device Status bit values (Virtio 1.2 §2.1, "Device Status Field").
; Bit-position numbering in the REQUIREMENTS.md VIO table is the
; bit index; the MMIO Status register is written with OR-ed values.
VIRTIO_STATUS_ACKNOWLEDGE equ 1    ; bit 0 — driver found the device
VIRTIO_STATUS_DRIVER      equ 2    ; bit 1 — driver knows how to use it
VIRTIO_STATUS_DRIVER_OK   equ 4    ; bit 2 — device is live (VIO-008)
VIRTIO_STATUS_FEATURES_OK equ 8    ; bit 3 — feature subset accepted
VIRTIO_STATUS_FAILED      equ 0x80 ; bit 7 — driver has given up (VIO-009)

; Feature bits (Virtio 1.2 §6 — transport + device-specific). Bits
; are 64-wide; MMIO exposes them via two 32-bit pages selected by
; DeviceFeaturesSel / DriverFeaturesSel (0=bits 0-31, 1=bits 32-63).
; VIRTIO_F_VERSION_1 is bit 32 globally → bit 0 of the high page.
; MVP policy: accept VERSION_1 only. MAC, STATUS, MQ, CSUM, GSO,
; EVENT_IDX stay off until the code paths that consume them land
; (see VIO-F-001..007 in docs/l2/REQUIREMENTS.md §4.2).
VIRTIO_F_VERSION_1_HIBIT  equ 1   ; bit 32 == 1 in the high page

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

; Per-queue programming macro used by VIO-007 Slice 2. Selects
; the queue, reads QueueNumMax and writes it back as QueueNum,
; programs physical addresses for descriptor / available / used
; rings, toggles QueueReady to 1, and read-backs to confirm it
; stuck. On read-back mismatch, jumps to ``.queue_ready_fail``
; (which halts through .set_failed per VIO-009).
;
; Barrier envelope: lfence around loads, mfence around stores —
; the pattern already set by VIO-001..006 up-file.
;
; Scope constraint: the ``jne .queue_ready_fail`` uses a local
; label that's resolved against the most-recent non-local label
; at the expansion site. Today both expansions and the
; ``.queue_ready_fail`` target are inside ``_start:``'s scope,
; so the label resolves. Future refactors that insert a new
; non-local label between the expansions and the target would
; fail at assembly time — loud, not silent, but worth noting.
;
; Parameters:
;   %1 = queue index (0 = RX, 1 = TX) — written to QueueSel
;   %2 = descriptor table physical address (u32)
;   %3 = available ring physical address (u32)
;   %4 = used ring physical address (u32)
; Clobbers: eax, flags. Preserves: everything else.
%macro program_queue 4
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_SEL], %1
    mfence
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_NUM_MAX]
    lfence
    ; Clamp to VIRTQ_MAX_SIZE — the allocated 4 KiB descriptor-
    ; table page holds at most 256 entries. A larger QueueNumMax
    ; would overflow into the available-ring page on the device
    ; side.
    cmp eax, VIRTQ_MAX_SIZE
    jbe %%size_ok
    mov eax, VIRTQ_MAX_SIZE
%%size_ok:
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_NUM], eax
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_DESC_LOW], %2
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_DESC_HIGH], 0
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_DRIVER_LOW], %3
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_DRIVER_HIGH], 0
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_DEVICE_LOW], %4
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_DEVICE_HIGH], 0
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_READY], 1
    mfence
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_READY]
    lfence
    cmp eax, 1
    jne .queue_ready_fail
%endmacro

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

    ; --- Feature negotiation (VIO-004, VIO-005, VIO-006) ---
    ; Virtio 1.2 §2.1.2 steps 4-6. MVP policy (documented with the
    ; VIRTIO_F_VERSION_1_HIBIT constant near the top): accept
    ; VERSION_1 only; everything else waits for its consuming code
    ; to land. Bits 0-31 are left unselected; bits 32-63 get exactly
    ; one bit — VERSION_1.
    ;
    ; Both feature pages are read. We accept nothing from the low
    ; page in MVP, but the value is saved in ebx (preserved across
    ; emit_bytes / emit_hex32) so failure diagnostics can show what
    ; the device actually offered. When MAC / STATUS / CTRL_VQ land
    ; in follow-up commits, the read stays; only the low-page
    ; driver-features write gains bits.

    ; VIO-004 part 1a: read low-page offered features, save for diag.
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DEVICE_FEATURES_SEL], 0
    mfence
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DEVICE_FEATURES]
    lfence
    mov ebx, eax

    ; VIO-004 part 1b: check VERSION_1 is offered (high page bit 0).
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DEVICE_FEATURES_SEL], 1
    mfence
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DEVICE_FEATURES]
    lfence
    test eax, VIRTIO_F_VERSION_1_HIBIT
    jz .features_fail_no_v1

    ; VIO-004 part 2: write driver features. Low page = 0 (we accept
    ; nothing bits 0-31), high page = VERSION_1 only.
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DRIVER_FEATURES_SEL], 0
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DRIVER_FEATURES], 0
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DRIVER_FEATURES_SEL], 1
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_DRIVER_FEATURES], VIRTIO_F_VERSION_1_HIBIT
    mfence

    ; VIO-005: set FEATURES_OK in status (RMW).
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_FEATURES_OK
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS], eax
    mfence

    ; VIO-006: re-read Status. If FEATURES_OK cleared, the device
    ; rejected our subset and we MUST abort init. Equality compare
    ; against the expected full status value (ACK|DRIVER|FEATURES_OK)
    ; also catches FAILED and any unexpected stray bits, consistent
    ; with the VIO-003 cross-check above.
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    cmp eax, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK
    jne .features_fail_rejected

    lea esi, [features_ok_msg]
    mov ecx, features_ok_len
    call emit_bytes

    ; --- Virtqueue discovery (VIO-007, part 1: QueueNumMax) ---
    ; Virtio 1.2 §4.2.2: QueueSel selects a queue; QueueNumMax
    ; returns the device's max supported queue size, or 0 if the
    ; queue index is unsupported. For virtio-net with no CTRL_VQ
    ; and no MQ (the only features MVP accepts):
    ;   queue 0 = RX (receiveq), per §5.1.2
    ;   queue 1 = TX (transmitq)
    ; Both must be supported and non-zero to proceed.
    ;
    ; This commit is discovery-only. Memory allocation, QueueNum
    ; write, QueueDesc/Driver/Device physical-address programming,
    ; and the QueueReady toggle land in the follow-up commit that
    ; completes VIO-007. The marker here advances as each stage
    ; does.

    ; Read QueueNumMax for queue 0 (RX).
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_SEL], 0
    mfence
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_NUM_MAX]
    lfence
    test eax, eax
    jz .queue_fail_rx
    mov ebx, eax                    ; save RX max across next probe

    ; Read QueueNumMax for queue 1 (TX).
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_SEL], 1
    mfence
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_NUM_MAX]
    lfence
    test eax, eax
    jz .queue_fail_tx

    ; Emit: QUEUES:RX=XXXXXXXX TX=XXXXXXXX
    push eax                        ; preserve TX across emit_bytes
    lea esi, [queues_rx_prefix_msg]
    mov ecx, queues_rx_prefix_len
    call emit_bytes
    mov eax, ebx                    ; RX max → hex
    call emit_hex32
    lea esi, [queues_tx_sep_msg]
    mov ecx, queues_tx_sep_len
    call emit_bytes
    pop eax                         ; restore TX
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes

    ; --- VIO-007 Slice 2: initialize virtqueues ---
    ; Program QueueNum, descriptor/avail/used physical addresses,
    ; and QueueReady for each queue. Per Virtio 1.2 §2.1.2 step 7
    ; + §4.2.2 register semantics. Memory bases are compile-time
    ; constants anchored at ``VIRTQ_BASE`` with page-aligned
    ; slots per queue (desc / avail / used). No descriptor
    ; population yet — that's L2 RX/TX plumbing in a later
    ; commit. Slice 2 proves the plumbing and flips VIO-007 to
    ; ``implemented``.
    program_queue 0, \
        RX_QUEUE_BASE + VIRTQ_OFF_DESC, \
        RX_QUEUE_BASE + VIRTQ_OFF_AVAIL, \
        RX_QUEUE_BASE + VIRTQ_OFF_USED
    program_queue 1, \
        TX_QUEUE_BASE + VIRTQ_OFF_DESC, \
        TX_QUEUE_BASE + VIRTQ_OFF_AVAIL, \
        TX_QUEUE_BASE + VIRTQ_OFF_USED

    lea esi, [queues_ready_msg]
    mov ecx, queues_ready_len
    call emit_bytes

    ; --- VIO-008: set DRIVER_OK (bit 2) in Status ---
    ; Virtio 1.2 §2.1.2 step 8 — final bit in the init sequence.
    ; After this write, the device is "live": the virtqueues
    ; programmed above become consumable, the device can start
    ; filling the RX used-ring from any buffers the driver later
    ; posts, and interrupts (if enabled) start firing. Slice 2
    ; didn't post any buffers yet, so the ring stays idle until
    ; L2 RX/TX plumbing lands in a later commit — the live
    ; transition here is still the right step to complete init
    ; cleanly.
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_DRIVER_OK
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS], eax
    mfence

    ; Read-back: full expected state is
    ;   ACKNOWLEDGE | DRIVER | FEATURES_OK | DRIVER_OK = 0x0F.
    ; Equality compare catches both DRIVER_OK-didn't-stick and any
    ; stray bit (including FAILED) that crept in during init,
    ; consistent with the VIO-003 / VIO-006 cross-checks above.
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    cmp eax, VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK | VIRTIO_STATUS_DRIVER_OK
    jne .driver_ok_fail

    lea esi, [driver_ok_msg]
    mov ecx, driver_ok_len
    call emit_bytes

    ; --- VIO-R-002: pre-populate RX queue with WRITE-flagged buffers ---
    ; Virtio 1.2 §5.1.6.3 — the driver populates the RX queue with
    ; descriptors marked WRITE before the device has anything to
    ; deposit. With DRIVER_OK already asserted above, the device is
    ; live and will begin using these descriptors as soon as it sees
    ; them on the Available Ring.
    ;
    ; All 256 RX descriptor slots get filled here. Each descriptor
    ; points at a distinct 2 KiB buffer in the pool at
    ; RX_BUFFER_BASE; each buffer fits the 12-byte virtio_net_hdr
    ; plus a max-size tagged Ethernet frame. No descriptor chaining
    ; (VIRTIO_NET_F_MRG_RXBUF not negotiated — see VIO-F-001 / the
    ; VIRTIO_F_VERSION_1-only MVP subset).
    ;
    ; Order enforced by §2.7.13 / §2.7.11:
    ;   1. Write descriptor table entries
    ;   2. Write Available Ring slots 0..N-1 with descriptor indices
    ;   3. mfence — descriptor/ring stores drain before idx bump
    ;   4. Publish AvailRing.idx = N
    ;   5. mfence — idx bump drains before QueueNotify write
    ;   6. QueueNotify = 0 (kick the RX queue)
    ; Register use in this block: edi = loop counter (0..255);
    ; eax = per-iteration scratch (desc offset, then buffer addr);
    ; ebx = current descriptor-entry pointer. All three are
    ; clobber-safe — the block ends at emit_bytes (which preserves
    ; ebx but clobbers eax/ecx/esi/edi), and no subsequent code
    ; reads any of them before they're re-initialized.
    xor edi, edi
.rx_fill_loop:
    ; Descriptor table entry at desc_base + i * 16.
    mov eax, edi
    shl eax, 4
    lea ebx, [RX_QUEUE_BASE + VIRTQ_OFF_DESC + eax]

    ; Buffer address: RX_BUFFER_BASE + i * 2 KiB. Low 32 bits go
    ; into addr_lo; addr_hi stays 0 (pool is well under 4 GiB).
    mov eax, edi
    shl eax, 11
    add eax, RX_BUFFER_BASE

    mov dword [ebx + 0],  eax                   ; addr low
    mov dword [ebx + 4],  0                     ; addr high
    mov dword [ebx + 8],  RX_BUFFER_SIZE        ; len
    mov word  [ebx + 12], VIRTQ_DESC_F_WRITE    ; flags (device may write)
    mov word  [ebx + 14], 0                     ; next (unused, no chain)

    ; Available Ring slot i holds descriptor index i. Ring slots
    ; start at AvailRing offset 4 (after the u16 flags + u16 idx
    ; header).
    mov [RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 4 + edi*2], di

    inc edi
    cmp edi, VIRTQ_MAX_SIZE
    jl  .rx_fill_loop

    ; Step 3: drain the descriptor/ring stores.
    mfence

    ; Step 4: publish idx = 256. Firecracker zero-inits guest RAM
    ; so idx was 0 before this write; after it, the device sees all
    ; 256 slots as available.
    mov word [RX_QUEUE_BASE + VIRTQ_OFF_AVAIL + 2], VIRTQ_MAX_SIZE

    ; Step 5: idx bump drains before the kick.
    mfence

    ; Step 6: QueueNotify(0) kicks the RX queue. The register is
    ; write-only per spec; no read-back check is possible. If the
    ; device doesn't see this write, RX stalls forever — but that
    ; would also manifest as zero completions when we start reading
    ; the Used Ring in a follow-up commit.
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_QUEUE_NOTIFY], 0
    mfence

    lea esi, [rx_populated_msg]
    mov ecx, rx_populated_len
    call emit_bytes
    jmp .halt

.driver_ok_fail:
    ; eax holds the observed status. Dump it for diagnostics
    ; and halt via the VIO-009 FAILED-bit path.
    push eax
    lea esi, [driver_ok_fail_msg]
    mov ecx, driver_ok_fail_len
    call emit_bytes
    pop eax
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.queue_ready_fail:
    ; QueueReady didn't read back as 1 after we wrote it — the
    ; device rejected the programming for some reason (bad
    ; address alignment, unsupported queue size, ...). Dump and
    ; halt through the VIO-009 FAILED path.
    lea esi, [queues_ready_fail_msg]
    mov ecx, queues_ready_fail_len
    call emit_bytes
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.queue_fail_rx:
    ; QueueSel=0 returned QueueNumMax=0 — device does not support
    ; the RX queue. Fatal: MVP needs both queues. Emit the marker
    ; and route through .set_failed (VIO-009).
    lea esi, [queues_fail_rx_msg]
    mov ecx, queues_fail_rx_len
    call emit_bytes
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.queue_fail_tx:
    ; QueueSel=1 returned QueueNumMax=0 — device does not support
    ; the TX queue. Fatal (same reason as .queue_fail_rx).
    lea esi, [queues_fail_tx_msg]
    mov ecx, queues_fail_tx_len
    call emit_bytes
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.features_fail_no_v1:
    ; Device did not offer VIRTIO_F_VERSION_1. ebx=dev_lo, eax=dev_hi.
    ; Emit: FEATURES:FAIL no-v1 dev_lo=XXXXXXXX dev_hi=XXXXXXXX
    push eax                        ; preserve dev_hi across emit_bytes
    lea esi, [features_no_v1_prefix_msg]
    mov ecx, features_no_v1_prefix_len
    call emit_bytes
    mov eax, ebx                    ; dev_lo → hex
    call emit_hex32
    lea esi, [features_hi_sep_msg]
    mov ecx, features_hi_sep_len
    call emit_bytes
    pop eax                         ; restore dev_hi
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.features_fail_rejected:
    ; Device cleared FEATURES_OK after we set it (or set an unexpected
    ; bit). eax holds the observed status; dump it.
    push eax
    lea esi, [features_rejected_msg]
    mov ecx, features_rejected_len
    call emit_bytes
    pop eax
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.status_fail:
    ; eax holds the observed status value. Emit the marker and the
    ; value so a debugger sees exactly what the device reported.
    push eax
    lea esi, [status_fail_msg]
    mov ecx, status_fail_len
    call emit_bytes
    pop eax
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .set_failed

.virtio_fail:
    ; eax holds the actual magic value. Print "VIRTIO:FAIL magic=" then
    ; the eight hex digits, then "\n". Saves a lot of time versus
    ; having to bisect from the VMM log that the address was wrong.
    ; Skips .set_failed — if magic didn't match, the device's register
    ; layout is unverified and a Status write to offset 0x70 would
    ; be blind.
    push eax                        ; preserve magic across emit_bytes
    lea esi, [virtio_fail_msg]
    mov ecx, virtio_fail_len
    call emit_bytes
    pop eax
    call emit_hex32
    lea esi, [newline]
    mov ecx, 1
    call emit_bytes
    jmp .halt

.set_failed:
    ; VIO-009: signal the device that the driver has given up. Any
    ; fatal init error downstream of the MagicValue probe routes
    ; here before halt. RMW preserves whatever Status bits are
    ; already set (ACK / DRIVER / FEATURES_OK in various combinations
    ; depending on which stage failed). Only entered after the probe
    ; matched — .virtio_fail skips this step for the reason noted
    ; above.
    lfence
    mov eax, [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS]
    lfence
    or  eax, VIRTIO_STATUS_FAILED
    mfence
    mov dword [VIRTIO_MMIO_BASE + VIRTIO_MMIO_STATUS], eax
    mfence
    ; fall through

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

driver_ok_fail_msg:    db 'DRIVER_OK:FAIL status='
driver_ok_fail_len     equ $ - driver_ok_fail_msg

newline:         db 10
