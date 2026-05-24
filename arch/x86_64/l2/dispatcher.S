; SPDX-License-Identifier: AGPL-3.0-or-later
;
; L2 dispatcher — x86_64 (FSA-1/2/3 + post-review SysV cleanup).
; Counterpart to arch/aarch64/l2/dispatcher.S.
;
; FSA framing (D012 / D043, Astier paper):
;   - The dispatcher is the engine. It picks events (today: a
;     hardcoded sequence of one RX consume + one TX submit) and
;     drives them to completion.
;   - Each event runs as a bounded transition with explicit
;     barriers around the device-shared rings.
;   - Buffer pools are sized statically (see pools.S) and never
;     allocated dynamically post-init.
;
; FSA-1 scope (extraction): single-shot wrapper around the prior
; inline RX/TX path.
; FSA-2 scope (multi-frame RX): bounded RX_FRAME_BUDGET=16 drain
; loop with batched AvailRing.idx publish + RX QueueNotify.
; FSA-3 scope (race fix): no x86 code change — TSO provides
; acquire-on-load naturally; see comment block lower.
;
; SysV AMD64 ABI compliance (Ed, 2026-05-21): we are bare metal,
; not running under a kernel that imposes SysV — but we *adopt*
; SysV strictly across the project. Rationale: we expect to call
; into SSE/AVX/AES-NI helpers (crypto/) from these paths in
; future work; SysV §3.2.2 mandates rsp ≡ 0 (mod 16) at the CALL
; instruction so a callee can use 16-aligned vector loads
; (movdqa, movapd, vmovapd) safely. Pre-FSA-3 code violated this
; in two ways: (a) the prologue's two pushes left rsp ≡ 8 (mod 16)
; at the function body, and (b) the emit-chain push/pop pattern
; alternated alignment. Both are corrected below.
;
; Acknowledged deviation (Gemini #3): emit_bytes and emit_hex32
; (defined in arch/x86_64/platform/firecracker/boot.S) use a
; custom register convention — rsi/ecx for emit_bytes (source
; ptr + length) and rax for emit_hex32 (value) — rather than
; SysV's rdi/rsi/rdx. The helpers are bare-metal-internal
; (never called from C), so the deviation is local to this
; build. Changing the convention would cascade into boot.S and
; every other caller of those helpers; tracked as a future
; standardization pass.
;
; Caller contract (from kernel_main_64 in boot.S):
;   - On entry: r12 = VIRTIO_MMIO base, r13 = N (queue size),
;     r14 = next_avail shadow, r15 = virtq base.
;   - rbx and rbp are SysV callee-saved and modified by this
;     function; the prologue/epilogue save+restore them.
;   - r12, r13, r15 are also callee-saved AND read-only inside
;     this function; preserved trivially.
;   - r14 is SysV callee-saved AND used inside this function as
;     a working register for the RX next_avail counter. The
;     prologue `push r14` / epilogue `pop r14` preserves the
;     caller's value as the SysV ABI requires.
;     FSA-4: the actual next_avail value persists across calls
;     via `l2_state` (a memreq region), not via r14 — the
;     dispatcher loads l2_state.rx_next_avail into r14 at entry
;     and writes the advanced value back at exit. r14's role
;     here is "scratch working register for the RX recycle loop,"
;     not "input/output parameter."
;   - On return: eax = 0 → caller jmps to .halt (clean halt
;                — both success and timeout paths land here;
;                pre-FSA-1 behavior, no return-code differentiation
;                between "consumed N frames" and "wait timed out");
;                eax = 1 → caller jmps to .set_failed (sets
;                VIRTIO_STATUS_FAILED then halts).

; --- Virtio constants needed by the dispatcher ---
;
; DUPLICATED from arch/x86_64/platform/firecracker/boot.S. Values
; are spec-defined (Virtio 1.2 §4.2 for MMIO regs, §2.7 for virtq
; layout); drift is bounded. Refactor into a shared include is a
; future cleanup (not FSA-1 scope).
VIRTIO_MMIO_QUEUE_NOTIFY    equ 0x050
VIRTQ_STRIDE                equ 0x3000
VIRTQ_OFF_DESC              equ 0x0000
VIRTQ_OFF_AVAIL             equ 0x1000
VIRTQ_OFF_USED              equ 0x2000
TX_QUEUE_OFF                equ 1 * VIRTQ_STRIDE
VIRTQ_MAX_SIZE              equ 0x100

; Poll budget for the RX/TX wait loops. Diverges from the aarch64
; dispatcher's POLL_BUDGET = 10000000 (1e7) by 10x — x86_64 has no
; `yield` in its busy-wait body so each iteration is cheaper, and
; the larger budget gives more wall-clock slack for the host VMM
; to schedule the device thread.
POLL_BUDGET                 equ 100000000

; tx_test_pkt layout = 12 (virtio_net_hdr) + 14 (Ethernet) + 46
; (canary payload) = 72 bytes. Keep in sync with the byte sequence
; in boot.S.
TX_TEST_PKT_LEN             equ 72

; FSA-2: per-dispatch RX frame budget. Caps how many RX completions
; the multi-frame consume loop drains in a single l2_dispatch call.
; Per D043 ("bounded transitions") the dispatcher must not
; monopolize the CPU on a long burst. Mirrors aarch64's
; RX_FRAME_BUDGET (16).
RX_FRAME_BUDGET             equ 16

; FSA-3: no x86 code changes were needed for the phantom-frame
; race fix that landed on aarch64 (LDARH for load-acquire on idx).
; x86 TSO (Intel SDM Vol. 3A §8.2.2) provides:
;   - Loads are not reordered with other loads.
;   - Stores are not reordered with other stores.
;   - Loads may be reordered with prior stores to a different
;     location (StoreLoad reordering is the ONLY allowed
;     reordering on x86 outside of NT stores / WC memory).
; So a consumer reading UsedRing.idx followed by ring[slot]
; followed by the buffer is naturally ordered. The producer
; (Firecracker virtio-net VMM thread, also x86) writes buffer
; then ring then idx; under TSO those stores cannot be reordered.
; Together: any observer that sees the new idx also sees the
; preceding ring + buffer writes. No memory-fence reasoning
; required for the consumer path.
;
; The lfence/mfence sprinkled through the existing path are
; therefore conservative and could be narrowed. They stay as a
; matter of defensive symmetry with aarch64 (where the barriers
; ARE load-bearing) and project policy: tolerate the wasted
; cycles to keep the cross-arch reading symmetric. A future
; cleanup could measure timeout-path CPU usage and decide to
; remove some lfences.

; Externally defined in arch/x86_64/platform/firecracker/boot.S.
extern emit_bytes
extern emit_hex32
extern newline
extern tx_test_pkt
extern __memreq_addr__rx_buffer

; FSA-4: persistent shadow storage (4 × u16, see regions.yaml l2_state).
extern __memreq_addr__l2_state

; FSA-4(A): writable TX buffer for ARP replies (see regions.yaml tx_arp_buf).
extern __memreq_addr__tx_arp_buf

; FSA-4: per-dispatch TX reclaim budget. Same role as
; RX_FRAME_BUDGET for the multi-frame TX reclaim loop — caps the
; number of TX completions drained per dispatcher invocation so
; the bounded-transition invariant holds on a heavy TX-completion
; backlog. Mirrors aarch64.
TX_RECLAIM_BUDGET           equ 16

; FSA-4: l2_state field offsets (mirror regions.yaml doc).
L2_STATE_RX_NEXT_AVAIL      equ 0
L2_STATE_RX_USED_SHADOW     equ 2
L2_STATE_TX_NEXT_AVAIL      equ 4
L2_STATE_TX_USED_SHADOW     equ 6

; GUEST_MAC — the locally-administered unicast address this
; guest answers to. Must stay in sync with the same constants in
; arch/x86_64/platform/firecracker/boot.S (used for the
; tx_test_pkt canary's src MAC). Cross-file consistency is human
; discipline; each file asserts its own ETH-016 invariant.
GUEST_MAC_BYTE_0  equ 0x02
GUEST_MAC_BYTE_1  equ 0x00
GUEST_MAC_BYTE_2  equ 0x00
GUEST_MAC_BYTE_3  equ 0x00
GUEST_MAC_BYTE_4  equ 0x00
GUEST_MAC_BYTE_5  equ 0x01

; ETH-016 (IEEE 802.3 §4.1.2.1): outgoing source MAC MUST have
; the unicast bit clear (bit 0 of byte 0 = 0). This dispatcher
; uses GUEST_MAC as the source MAC for every outgoing frame it
; builds (ARP replies via arp_reply_template — and, when the
; TX API lands per docs/l2/TX_API.md, every queued L3 TX too).
; The build-time check catches a malformed-MAC edit before it
; ships.
%if GUEST_MAC_BYTE_0 & 1
%error "ETH-016: GUEST_MAC_BYTE_0 has multicast bit set"
%endif

; Derived LE encodings for the MAC filter's halfword/word
; compares. NASM evaluates the bitwise OR over equ symbols
; fine; aarch64 GNU as can't (CLAUDE.md gotcha) — the aarch64
; dispatcher hardcodes the equivalent values with a sync
; comment.
GUEST_MAC_LE_LO32 equ (GUEST_MAC_BYTE_0 | (GUEST_MAC_BYTE_1 << 8) | (GUEST_MAC_BYTE_2 << 16) | (GUEST_MAC_BYTE_3 << 24))
GUEST_MAC_LE_HI16 equ (GUEST_MAC_BYTE_4 | (GUEST_MAC_BYTE_5 << 8))

global l2_dispatch

[bits 64]
section .text

l2_dispatch:
    ; SysV AMD64 prologue + FSA-4 reentrance scaffolding.
    ;
    ; Function entry: rsp ≡ 8 (mod 16) per the SysV calling
    ; convention. Callee-saved set used here:
    ;   - rbx, rbp: modified by the function body
    ;   - r14:      RX next_avail, loaded from l2_state below
    ;   - r12, r13, r15: read-only — no save needed
    ;
    ; Three 8-byte pushes (rbp, rbx, r14) + a 32-byte local frame
    ; bring rsp ≡ 0 (mod 16) and reserve six dword-sized slots
    ; (with padding for 8-byte alignment of each pair):
    ;   [rsp +  0..3] : per-dispatch frame counter (FSA-4; bounds
    ;                   the RX consume and TX reclaim loops)
    ;   [rsp +  4..7] : hdr_len save (FSA-4(A); preserved across
    ;                   the optional ARP:REQUEST marker emit since
    ;                   emit_bytes consumes esi via lodsb)
    ;   [rsp +  8..11]: TX next_avail stash (FSA-4; snapshot of
    ;                   advanced r9, kept across the TX emit chain
    ;                   for writeback at .l2_tx_writeback)
    ;   [rsp + 12..15]: id save (FSA-4(A); preserved across the
    ;                   optional ARP:REQUEST marker emit since
    ;                   emit_bytes clobbers edi as its LSR-poll
    ;                   counter = UART_POLL_TIMEOUT)
    ;   [rsp + 16..19]: arp_match flag (FSA-4(A); zeroed at entry,
    ;                   set in the ARP recognition path)
    ;   [rsp + 20..23]: sender_ip stash (FSA-4(A); ARP reply
    ;                   patches THA/TPA from here)
    ;   [rsp + 24..29]: sender_mac stash (6 bytes for ARP reply)
    ;   [rsp + 30..31]: padding for 16-alignment
    push rbp
    mov rbp, rsp
    push rbx
    push r14
    sub rsp, 32                                      ; FSA-4(A): grew from 16 → 32 for ARP stash

    ; --- FSA-4: load persistent shadows from l2_state ---
    ;
    ; l2_state holds four u16 counters (regions.yaml docs the
    ; layout): rx_next_avail @ +0, rx_used_shadow @ +2,
    ; tx_next_avail @ +4, tx_used_shadow @ +6. boot.S initialises
    ; rx_next_avail = VIRTQ_MAX_SIZE after RX:POPULATED; the other
    ; three are zero-init via the allocator pass.
    ;
    ; movzx loads zero-extended u16, so r14d/ebp range is [0,65535]
    ; on entry — matching Virtio u16 idx wrap semantics. The
    ; subsequent `cmp ax, bp` in the wait loops is naturally
    ; wrap-safe (16-bit subregister compare).
    mov rax, [rel __memreq_addr__l2_state]
    movzx r14d, word [rax + L2_STATE_RX_NEXT_AVAIL]
    movzx ebp,  word [rax + L2_STATE_RX_USED_SHADOW]
    mov dword [rsp + 0], 0                           ; per-dispatch RX frame counter
    mov dword [rsp + 8], 0                           ; tx_next_avail stash (TX phase will fill)
    mov dword [rsp + 16], 0                          ; FSA-4(A): arp_match flag

    ; --- VIO-R-003/004: poll RX Used Ring for first completion ---
    mov ecx, POLL_BUDGET
.l2_rx_wait_loop:
    ; Gemini (8): `pause` is the x86 architecturally-recommended
    ; spin-wait hint — improves power efficiency on SMT cores and
    ; reduces pipeline stalls on memory-order-violation snoops.
    ; The aarch64 equivalent is `yield`, used in the same role on
    ; the matching wait loop. lfence here is dropped: x86 TSO
    ; guarantees `mov ax, [...]` is not reordered with subsequent
    ; loads, so the load-load ordering the lfence provided is
    ; already free.
    pause
    mov ax, [r15 + VIRTQ_OFF_USED + 2]
    cmp ax, bp
    jne .l2_rx_received
    dec ecx
    jnz .l2_rx_wait_loop

    lea rsi, [rel rx_timeout_msg]
    mov ecx, rx_timeout_len
    call emit_bytes
    jmp .l2_ok

.l2_rx_received:
    ; FSA-2: multi-frame consume loop. The wait loop above
    ; established idx != shadow on entry, so the first iteration
    ; proceeds unconditionally. Subsequent iterations re-enter via
    ; .l2_rx_consume_check (post-consume budget + queue probe).
    ;
    ; INVARIANT (FSA-3): every entry into .l2_rx_consume_body —
    ; both the fall-through from .l2_rx_received AND the back-edge
    ; from .l2_rx_consume_check — is preceded by a load of
    ; UsedRing.idx with at least acquire-equivalent ordering. On
    ; x86_64 that is naturally provided by TSO (the `mov ax, [...]`
    ; in the wait loop / queue probe; the surrounding lfence is
    ; conservative). Subsequent reads of UsedRing.ring[slot],
    ; virtio_net_hdr, and the buffer payload rely on this ordering
    ; to see the producer's released state. If a future edit adds
    ; a third entry point into .l2_rx_consume_body, it MUST
    ; establish the same acquire on idx first.
    ;
    ; Per-frame work: read ring slot, bounds-check id, read
    ; virtio_net_hdr, validate num_buffers, emit RX:FRAME, then
    ; write the descriptor id into AvailRing.ring[next_avail & mask].
    ; The AvailRing.idx publish + RX QueueNotify are BATCHED to
    ; .l2_rx_drained — one mfence + one idx store + one notify
    ; covers all frames consumed this dispatch. Spec-compliant
    ; per Virtio 1.2 §2.7.13.3.
.l2_rx_consume_body:
    ; P2: slot-indexed read using H2 consumer-shadow.
    mov eax, ebp                                     ; eax = shadow
    lea ecx, [r13d - 1]                              ; ecx = mask = N - 1
    and eax, ecx                                     ; eax = slot
    lea r10, [r15 + rax*8 + VIRTQ_OFF_USED]          ; entry base
    lfence
    mov edi, [r10 + 4]                               ; ring[slot].id
    mov ebx, [r10 + 8]                               ; ring[slot].len
    lfence

    cmp edi, VIRTQ_MAX_SIZE
    jae .l2_rx_bad_id_fail

    ; --- ETH-003 / ETH-013: frame-size bounds (RX:DROP path) ---
    ;
    ; used_len (ebx) includes the 12-byte virtio_net_hdr, so the
    ; Ethernet wire range [60, 1518] becomes [72, 1530] in virtio
    ; terms. Runt and oversize frames are dropped at L2 per
    ; docs/l2/REQUIREMENTS.md — the dispatcher emits RX:DROP,
    ; recycles the descriptor (the device must get the buffer
    ; back regardless of whether we used it), and re-enters the
    ; consume loop. ARP recognition + RX:FRAME emit are skipped.
    cmp ebx, 72
    jb  .l2_rx_drop
    cmp ebx, 1530
    ja  .l2_rx_drop

    ; Buffer addr: load rx_buffer pool base, then add id*2048.
    ;
    ; memreq indirection (codegen convention): __memreq_addr__X
    ; is a NASM `equ` alias for the label __memreq_assigned__X,
    ; which marks an 8-byte storage slot holding the assigned
    ; runtime address. `mov rax, [rel __memreq_addr__rx_buffer]`
    ; reads from that slot to get the actual pool base. The
    ; bracketed deref is REQUIRED — without it we'd get the
    ; address of the slot, not the pool. See codegen at
    ; tooling/src/memreq_codegen/.
    mov rax, [rel __memreq_addr__rx_buffer]
    mov ecx, edi
    shl ecx, 11
    add rax, rcx

    ; --- ETH-006/008, MAC-001..005: MAC filter ---
    ;
    ; Frame's Ethernet destination at buffer + (12 bytes of
    ; virtio_net_hdr) + 0. Accept on:
    ;   - multicast / broadcast (bit 0 of byte 0 set) — every
    ;     real multicast group address (e.g. IPv6 ff02::/16
    ;     mapping to 33:33:xx:xx:xx:xx) and the all-ones
    ;     broadcast fit this branch. Multicast filtering at a
    ;     finer grain (IGMP-aware joined groups only) is out of
    ;     scope until the project actually uses any.
    ;   - unicast equal to GUEST_MAC (02:00:00:00:00:01).
    ;
    ; LE-loaded u32 of bytes 0-3: 02 00 00 00 → 0x00000002.
    ; Followed by LE-loaded u16 of bytes 4-5: 00 01 → 0x0100.
    ;
    ; Bit-0-of-byte-0 in wire order = bit 0 of the LE-loaded u32,
    ; so the multicast/broadcast check is a single `test r10d, 1`.
    ;
    ; Register: r10d. SysV caller-saved; verified dead at this
    ; point. Liveness trace (grep `r10` in this file to confirm):
    ;   - Last prior r10 read: the slot entry-base ldrs at the
    ;     top of .l2_rx_consume_body (`lea r10, [r15 + rax*8 +
    ;     VIRTQ_OFF_USED]` → `mov edi, [r10 + 4]; mov ebx,
    ;     [r10 + 8]`). r10 is untouched between that pair and
    ;     here.
    ;   - Next r10 access: the ARP stash below (`mov r10d, [rax
    ;     + 34]` and friends) — all WRITES, so observing our
    ;     value wouldn't be possible even if it were live.
    ;   - The recycle path at .l2_rx_drained re-derives r10
    ;     independently. The .l2_rx_drop / .l2_rx_drop_mac
    ;     paths don't read r10 at all.
    ; So clobbering r10d here is safe across every successor.
    ;
    ; (rdx is also safe to use now — emit_bytes self-loads
    ; COM1_PORT on entry as of 2026-05-22, so the prior project-
    ; wide "do not clobber dx across emit_bytes" rule no longer
    ; applies. r10d was chosen for this MAC filter mostly because
    ; it was free; either register would work today.)
    ;
    ; Reads from rax+N, never writes to rax — buffer base stays
    ; valid for the hdr_len / num_buffers loads below.
    mov r10d, [rax + 12]                             ; dst MAC bytes 0-3 (LE u32)
    test r10d, 1                                     ; multicast/broadcast?
    jnz .l2_mac_accept
    cmp r10d, GUEST_MAC_LE_LO32                      ; GUEST_MAC bytes 0-3 LE
    jne .l2_rx_drop_mac
    movzx r10d, word [rax + 16]                      ; dst MAC bytes 4-5
    cmp r10d, GUEST_MAC_LE_HI16                      ; GUEST_MAC bytes 4-5 LE
    jne .l2_rx_drop_mac
.l2_mac_accept:

    ; --- ETH-015: source MAC unicast-bit check ---
    ;
    ; The source MAC field identifies the originating station,
    ; which is by definition a unicast endpoint (IEEE 802.3-2018
    ; §4.1.2.1). Frames with bit 0 of byte 0 = 1 (multicast or
    ; broadcast source) are malformed and MUST be dropped per
    ; ETH-015. Defensive against a malicious or buggy peer that
    ; spoofs a multicast source to confuse downstream MAC
    ; learning or trigger reply storms — neither matters to
    ; this guest today, but rejecting the frame is cheap and
    ; correct.
    ;
    ; Source MAC lives at buffer + 18 (12 virtio_net_hdr + 6
    ; dst MAC). One byte-test is enough; we don't care about
    ; the rest of the source MAC for this gate.
    test byte [rax + 18], 1                          ; src MAC byte 0, bit 0
    jnz .l2_rx_drop_src

    ; --- ETH-018: PAUSE frame drop ---
    ;
    ; Per IEEE 802.3x-1997 §31B.1, receivers that do not
    ; implement link-level flow control MUST silently discard
    ; MAC Control frames. PAUSE is the canonical MAC Control
    ; frame: EtherType 0x8808 (= 0x0888 LE16) at buffer + 24.
    ; We don't inspect the opcode (PAUSE = 0x0001, MPCP = 0x0002,
    ; etc.) — any 0x8808 frame is discarded as a class.
    ;
    ; Acting on received PAUSE (throttling our TX) is the
    ; inverse direction and stays deferred per D045's "PAUSE
    ; rate response stays deferred" list. This gate just makes
    ; sure such frames don't surface as RX:FRAME and confuse
    ; whatever the dispatcher hands them to.
    cmp word [rax + 24], 0x0888                      ; EtherType (PAUSE = 0x8808 BE)
    je .l2_rx_drop_pause

    ; virtio_net_hdr.hdr_len (u16 @ +2) and num_buffers (u16 @ +10).
    lfence
    movzx esi, word [rax + 2]                        ; hdr_len
    movzx r11d, word [rax + 10]                      ; num_buffers
    lfence

    cmp r11d, 1
    jne .l2_rx_num_bufs_fail

    ; --- FSA-4(A): ARP request recognition ---
    ;
    ; Three short-circuit ne-checks on the inbound frame:
    ;   EtherType @ +24 = 0x0806 BE (= 0x0608 LE16)
    ;   ARP OPER  @ +32 = 0x0001 BE (= 0x0100 LE16) — request
    ;   TPA       @ +50 = 192.168.42.2 BE → 0x022AA8C0 LE32
    ;
    ; On match: stash SHA (6 B @ +34) + SPA (4 B @ +40), set
    ; arp_match flag, emit ARP:REQUEST marker. The post-RX-drain
    ; code reads the flag + stash to build the reply.
    ;
    ; Register-save discipline across the (potential) ARP:REQUEST
    ; emit. emit_bytes clobbers rsi (consumed via lodsb) AND rdi
    ; (used as the LSR-poll inner counter — `mov edi,
    ; UART_POLL_TIMEOUT` in boot.S, which is 0x00100000). Both
    ; values are needed AFTER the recognition block (rsi for the
    ; RX:FRAME hdr_len emit, rdi for the RX:FRAME id emit and the
    ; AvailRing recycle). Save unconditionally — one mov each is
    ; cheaper than gating on whether ARP matched.
    ;
    ; The rdi save was added 2026-05-22 in response to the
    ; clean-Claude pre-push review on the harness commit. The
    ; first L2 integration run noticed `id=00100000` in the
    ; iter-2 RX:FRAME marker — UART_POLL_TIMEOUT's residue from
    ; emit_bytes leaking through the push chain.
    mov [rsp + 4], esi                               ; save hdr_len
    mov [rsp + 12], edi                              ; save id

    cmp word [rax + 24], 0x0608                      ; EtherType (0x0806 BE)
    jne .l2_no_arp
    cmp word [rax + 32], 0x0100                      ; OPER (0x0001 BE)
    jne .l2_no_arp
    cmp dword [rax + 50], 0x022AA8C0                 ; TPA = 192.168.42.2
    jne .l2_no_arp

    ; ARP match. Stash sender MAC + IP.
    ;
    ; Historical context (kept because the bug shaped the
    ; project): pre-2026-05-22 this code used edx as scratch and
    ; corrupted COM1_PORT (which emit_bytes carried in dx across
    ; calls). Every subsequent emit_bytes wedged in its LSR-poll
    ; loop reading a bogus port, producing the 0xa8c5 bus_error
    ; spam visible in Firecracker logs. Diagnosed via the L2
    ; integration harness. The structural fix moved the dx pin
    ; INTO emit_bytes (see boot.S header) so dx is no longer
    ; load-bearing across calls.
    ;
    ; r10d is caller-saved per SysV and dead at this point (last
    ; used as the slot entry-base; no further read). Today rdx
    ; would also work; r10d is kept for diff-minimal continuity
    ; with the live code.
    ;
    ; Still-applicable register avoidance around the RX:FRAME
    ; emit chain below: rdi carries the descriptor id and r11
    ; carries num_buffers — both PUSHED for the emit chain
    ; before being clobbered. Don't pick those.
    mov r10d, [rax + 34]                             ; SHA bytes 0-3
    mov [rsp + 24], r10d
    movzx r10d, word [rax + 38]                      ; SHA bytes 4-5
    mov [rsp + 28], r10w
    mov r10d, [rax + 40]                             ; SPA
    mov [rsp + 20], r10d
    mov dword [rsp + 16], 1                          ; arp_match = 1

    lea rsi, [rel arp_request_msg]
    mov ecx, arp_request_len
    call emit_bytes
.l2_no_arp:
    mov esi, [rsp + 4]                               ; restore hdr_len
    mov edi, [rsp + 12]                              ; restore id

    ; --- Emit: RX:FRAME id=... hdr_len=... num_bufs=... used_len=... ---
    ;
    ; Strict SysV: push 4 args (32 bytes, preserves 16-alignment
    ; since rsp ≡ 0 (mod 16) entering this block) and read them
    ; via [rsp + offset] across the emit chain instead of popping
    ; one at a time. The pop-after-call pattern alternates rsp
    ; alignment (each 8-byte pop toggles bit 3); array access
    ; keeps every CALL at rsp ≡ 0 (mod 16). Stack slot map:
    ;     [rsp +  0] = id          (pushed last)
    ;     [rsp +  8] = hdr_len
    ;     [rsp + 16] = num_buffers
    ;     [rsp + 24] = used_len    (pushed first)
    push rbx                                         ; used_len
    push r11                                         ; num_buffers
    push rsi                                         ; hdr_len
    push rdi                                         ; id

    lea rsi, [rel rx_frame_prefix_msg]
    mov ecx, rx_frame_prefix_len
    call emit_bytes
    mov rax, [rsp + 0]                               ; id
    call emit_hex32

    lea rsi, [rel rx_frame_hdr_len_msg]
    mov ecx, rx_frame_hdr_len_len
    call emit_bytes
    mov rax, [rsp + 8]                               ; hdr_len
    call emit_hex32

    lea rsi, [rel rx_frame_num_bufs_msg]
    mov ecx, rx_frame_num_bufs_len
    call emit_bytes
    mov rax, [rsp + 16]                              ; num_buffers
    call emit_hex32

    lea rsi, [rel rx_frame_used_len_msg]
    mov ecx, rx_frame_used_len_len
    call emit_bytes
    mov rax, [rsp + 24]                              ; used_len
    call emit_hex32

    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes

    add rsp, 32                                      ; release the 4 saved args

    ; --- VIO-R-006: per-frame AvailRing.ring[slot] = id ---
    ; The emit chain clobbered rdi/rcx/rax (SysV caller-saved);
    ; re-derive id from the current shadow's slot. FSA-1's
    ; predecessor inline code hardcoded ring[0].id here, which
    ; was correct for the single-shot case (shadow always 0)
    ; but would break under multi-frame iteration.
    mov eax, ebp                                     ; eax = shadow
    lea ecx, [r13d - 1]                              ; ecx = mask
    and eax, ecx                                     ; eax = slot
    lea r10, [r15 + rax*8 + VIRTQ_OFF_USED]          ; entry base
    lfence
    mov edi, [r10 + 4]                               ; re-read ring[slot].id
    lfence

    mov eax, r14d                                    ; eax = next_avail
    lea ecx, [r13d - 1]                              ; ecx = mask
    and eax, ecx                                     ; eax = next_avail mod N
    mov [r15 + VIRTQ_OFF_AVAIL + 4 + rax*2], di      ; ring[avail_slot] = id

    ; Advance both shadows + the per-dispatch counter after the
    ; recycle write. ebp tracks the consumer shadow (persistent,
    ; loaded from l2_state); the dword at [rsp + 0] tracks how
    ; many frames THIS dispatch has consumed (bounded by
    ; RX_FRAME_BUDGET).
    inc r14d
    inc ebp
    inc dword [rsp + 0]

.l2_rx_consume_check:
    ; Budget check first — FSA-4 separated counter from shadow.
    ; Pre-FSA-4 this read ebp directly; under reentrance ebp can
    ; start non-zero from persistent state, so the budget needs
    ; its own dispatch-local counter.
    cmp dword [rsp + 0], RX_FRAME_BUDGET
    jae .l2_rx_drained

    ; Queue probe — is there another completion already available?
    ; No busy-wait here; this is a non-blocking drain. No pause
    ; (we're not spinning) and no lfence (x86 TSO orders the load
    ; against subsequent loads naturally). `cmp ax, bp` is a 16-bit
    ; subregister compare that wraps correctly with the u16 idx.
    mov ax, [r15 + VIRTQ_OFF_USED + 2]
    cmp ax, bp
    jne .l2_rx_consume_body
    ; else: idx == shadow → queue drained, fall through

.l2_rx_drained:
    ; --- VIO-R-007: batched AvailRing.idx publish + RX kick ---
    ; Two mfences (was three): first orders per-slot ring writes
    ; against idx publish, second orders idx against notify.
    mfence
    mov [r15 + VIRTQ_OFF_AVAIL + 2], r14w             ; AvailRing.idx = next_avail
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 0     ; kick RX

    ; Emit RX:RETURNED once per dispatch (after all frames consumed).
    lea rsi, [rel rx_returned_msg]
    mov ecx, rx_returned_len
    call emit_bytes

    ; --- FSA-4: write back RX shadows to l2_state ---
    ;
    ; mov word truncates the 32-bit r14d/ebp to u16 on store, so
    ; even when the in-register values have overflowed 16 bits
    ; mid-dispatch (e.g., r14d = 256+N after N frames consumed
    ; this call), the persisted u16 wraps modulo 65536 correctly.
    mov rax, [rel __memreq_addr__l2_state]
    mov word [rax + L2_STATE_RX_NEXT_AVAIL],  r14w
    mov word [rax + L2_STATE_RX_USED_SHADOW], bp

    ; --- VIO-T-002..005: submit one canary Ethernet frame ---
    ;
    ; FSA-4: tx_next_avail loaded from l2_state. desc_idx still
    ; fixed at 0 because TX_BUF_COUNT = 1; FSA-5 will introduce
    ; a free-list when TX gains multiple buffer slots.
    movzx r9d, word [rax + L2_STATE_TX_NEXT_AVAIL]
    xor r10d, r10d                                   ; desc_idx (FSA-5: from free-list)

    ; --- FSA-4(A): build ARP reply if matched, else use canary ---
    ;
    ; If an ARP request matched during RX consume (arp_match @
    ; [rsp + 16]), build a reply frame in tx_arp_buf by copying
    ; the rodata arp_reply_template and patching dst MAC + THA +
    ; TPA from the stashed sender info. r8 = chosen TX buffer addr
    ; (tx_arp_buf when arp_match, tx_test_pkt when not).
    cmp dword [rsp + 16], 0
    je .l2_use_canary

    ; Copy the 72-byte template into tx_arp_buf via 4× 16-byte
    ; movdqu pairs of GPR loads. Actually use 64-bit movs for
    ; simplicity (no SSE register needed here): 9× mov rcx + mov
    ; [r8 + N], rcx covers 72 bytes.
    mov r8, [rel __memreq_addr__tx_arp_buf]
    lea rdi, [rel arp_reply_template]
    mov rcx, [rdi + 0]
    mov [r8 + 0], rcx
    mov rcx, [rdi + 8]
    mov [r8 + 8], rcx
    mov rcx, [rdi + 16]
    mov [r8 + 16], rcx
    mov rcx, [rdi + 24]
    mov [r8 + 24], rcx
    mov rcx, [rdi + 32]
    mov [r8 + 32], rcx
    mov rcx, [rdi + 40]
    mov [r8 + 40], rcx
    mov rcx, [rdi + 48]
    mov [r8 + 48], rcx
    mov rcx, [rdi + 56]
    mov [r8 + 56], rcx
    mov rcx, [rdi + 64]
    mov [r8 + 64], rcx

    ; Patch dst MAC @ +12 from sender_mac stash (6 bytes).
    mov ecx, [rsp + 24]                              ; SHA bytes 0-3
    mov [r8 + 12], ecx
    movzx ecx, word [rsp + 28]                       ; SHA bytes 4-5
    mov [r8 + 16], cx

    ; Patch THA @ +44 from sender_mac stash.
    mov ecx, [rsp + 24]
    mov [r8 + 44], ecx
    movzx ecx, word [rsp + 28]
    mov [r8 + 48], cx

    ; Patch TPA @ +50 from sender_ip stash.
    mov ecx, [rsp + 20]
    mov [r8 + 50], ecx

    lea rsi, [rel arp_reply_msg]
    mov ecx, arp_reply_len
    call emit_bytes
    ; emit_bytes clobbers rax/rcx/rsi/rdi; r8 (caller-saved) is
    ; not touched by the helper's implementation but the contract
    ; doesn't promise it. Re-derive tx_arp_buf for the desc write.
    mov r8, [rel __memreq_addr__tx_arp_buf]
    jmp .l2_have_tx_addr

.l2_use_canary:
    lea r8, [rel tx_test_pkt]

.l2_have_tx_addr:
    ; rbx = desc[desc_idx] base. Each desc is 16 bytes.
    lea rbx, [r15 + TX_QUEUE_OFF + VIRTQ_OFF_DESC]
    shl r10, 4                                       ; r10 *= 16
    add rbx, r10                                     ; rbx = desc[desc_idx]
    shr r10, 4                                       ; restore r10 = desc_idx for ring write
    ; Write desc.addr (u64) — r8 < 4 GiB so the upper 32 bits of
    ; the 64-bit phys address are zero. Splitting into two 32-bit
    ; stores both because TX_TEST_PKT_LEN @ +8 wants a 32-bit
    ; store and because the upper half is unconditionally zero.
    mov [rbx + 0],  r8
    mov dword [rbx + 8],  TX_TEST_PKT_LEN
    mov word  [rbx + 12], 0
    mov word  [rbx + 14], 0

    ; avail_slot = tx_next_avail mod N. ring[avail_slot] = desc_idx.
    mov eax, r9d
    lea ecx, [r13d - 1]                              ; mask = N - 1
    and eax, ecx                                     ; eax = avail_slot
    mov [r15 + TX_QUEUE_OFF + VIRTQ_OFF_AVAIL + 4 + rax*2], r10w

    mfence

    ; tx_next_avail++; publish AvailRing.idx = tx_next_avail.
    ; Stash the advanced value in the local frame slot for
    ; writeback at .l2_tx_writeback below — r9 is SysV caller-
    ; saved and emit_bytes can clobber it across the TX:SUBMITTED
    ; / TX:RECLAIMED emit chains.
    inc r9d
    mov [rsp + 8], r9d
    mov [r15 + TX_QUEUE_OFF + VIRTQ_OFF_AVAIL + 2], r9w

    mfence

    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 1    ; kick TX

    lea rsi, [rel tx_submitted_msg]
    mov ecx, tx_submitted_len
    call emit_bytes

    ; --- VIO-T-006: poll TX Used Ring + multi-frame reclaim ---
    ;
    ; FSA-4: tx_used_shadow now loaded from l2_state (was reset
    ; to 0 each call pre-FSA-4). The wait loop polls for the
    ; FIRST completion past the persistent shadow; once seen,
    ; .l2_tx_reclaim_body drains additional completions (analogous
    ; to the RX multi-frame consume loop) up to TX_RECLAIM_BUDGET.
    mov rax, [rel __memreq_addr__l2_state]
    movzx ebp, word [rax + L2_STATE_TX_USED_SHADOW]
    mov dword [rsp + 0], 0                           ; reinit per-dispatch counter for TX
    mov ecx, POLL_BUDGET
.l2_tx_wait_loop:
    pause                                            ; Gemini (8): see RX wait loop
    mov ax, [r15 + TX_QUEUE_OFF + VIRTQ_OFF_USED + 2]
    cmp ax, bp
    jne .l2_tx_reclaim_body
    dec ecx
    jnz .l2_tx_wait_loop

    lea rsi, [rel tx_timeout_msg]
    mov ecx, tx_timeout_len
    call emit_bytes
    jmp .l2_tx_writeback                             ; writeback then clean halt

.l2_tx_reclaim_body:
    ; FSA-4: multi-frame TX reclaim. The wait loop above
    ; established idx != shadow on entry; this body drains
    ; additional completions up to TX_RECLAIM_BUDGET (analogous
    ; to .l2_rx_consume_body for the RX side). x86 TSO provides
    ; acquire-on-load for the slot reads.
    ;
    ; Note vs .l2_rx_consume_body: that block has `lfence` pairs
    ; around its slot reads (a defensive residual from FSA-1
    ; before the TSO analysis at the top of this file matured);
    ; the new TX reclaim block omits them because they are
    ; redundant under TSO. Either remove the RX lfences in a
    ; future cleanup pass, or leave both styles as they are —
    ; documented here so a maintainer reading the two side by
    ; side doesn't have to wonder which is correct.
    mov eax, ebp                                     ; eax = shadow
    lea ecx, [r13d - 1]                              ; ecx = mask = N - 1
    and eax, ecx                                     ; eax = slot
    lea r10, [r15 + rax*8 + TX_QUEUE_OFF + VIRTQ_OFF_USED]
    mov edi, [r10 + 4]                               ; ring[slot].id
    mov ebx, [r10 + 8]                               ; ring[slot].len

    cmp edi, VIRTQ_MAX_SIZE
    jae .l2_tx_bad_id_fail

    ; --- Emit: TX:RECLAIMED id=XXXXXXXX used_len=XXXXXXXX ---
    ; Strict SysV: 2 pushes = 16 bytes, preserves 16-alignment.
    ;     [rsp + 0] = id        (pushed last)
    ;     [rsp + 8] = used_len  (pushed first)
    push rbx                                         ; used_len
    push rdi                                         ; id

    lea rsi, [rel tx_reclaimed_prefix_msg]
    mov ecx, tx_reclaimed_prefix_len
    call emit_bytes
    mov rax, [rsp + 0]                               ; id
    call emit_hex32

    lea rsi, [rel tx_used_len_msg]
    mov ecx, tx_used_len_len
    call emit_bytes
    mov rax, [rsp + 8]                               ; used_len
    call emit_hex32

    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes

    add rsp, 16

    ; Advance shadow + per-dispatch counter.
    inc ebp                                          ; tx_used_shadow++
    inc dword [rsp + 0]                              ; per-dispatch counter++

.l2_tx_reclaim_check:
    ; Budget check first.
    cmp dword [rsp + 0], TX_RECLAIM_BUDGET
    jae .l2_tx_writeback

    ; Queue probe — non-blocking; drain anything else already there.
    mov ax, [r15 + TX_QUEUE_OFF + VIRTQ_OFF_USED + 2]
    cmp ax, bp
    jne .l2_tx_reclaim_body
    ; else: queue drained, fall through

.l2_tx_writeback:
    ; --- FSA-4: write back TX shadows to l2_state ---
    ; tx_next_avail comes from the stash slot at [rsp + 8] because
    ; the value in r9 may have been clobbered by emit chains since
    ; the submit. tx_used_shadow rides in rbp through the TX phase.
    mov rax, [rel __memreq_addr__l2_state]
    mov ecx, [rsp + 8]
    mov word [rax + L2_STATE_TX_NEXT_AVAIL],  cx
    mov word [rax + L2_STATE_TX_USED_SHADOW], bp
    jmp .l2_ok

.l2_rx_drop:
    ; --- ETH-003 / ETH-013: drop a malformed-size RX frame ---
    ;
    ; Branched here from .l2_rx_consume_body when used_len (ebx)
    ; is outside the [72, 1530] virtio range. ebx is SysV
    ; callee-saved and survives the emit chain; edi (id) is NOT
    ; preserved by emit_hex32 — stash it through the local frame
    ; slot at [rsp + 12] (the ARP recognition's id-save slot —
    ; the two code paths are mutually exclusive within an
    ; iteration, so the reuse is safe).
    mov [rsp + 12], edi                              ; save id

    lea rsi, [rel rx_drop_msg]
    mov ecx, rx_drop_len
    call emit_bytes
    mov eax, ebx                                     ; used_len
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes

    mov edi, [rsp + 12]                              ; restore id
    jmp .l2_rx_drop_recycle

.l2_rx_drop_mac:
    ; --- ETH-006/008, MAC-001..005: drop a wrong-MAC RX frame ---
    ;
    ; Branched here from the MAC filter when the dst MAC is unicast
    ; and not GUEST_MAC. used_len (ebx) and frame size are valid;
    ; we just don't process frames that aren't for us. id (edi)
    ; needs the same stash discipline as the size-bounds path
    ; because emit_bytes clobbers edi via its LSR-poll counter.
    mov [rsp + 12], edi                              ; save id

    lea rsi, [rel rx_drop_mac_msg]
    mov ecx, rx_drop_mac_len
    call emit_bytes

    mov edi, [rsp + 12]                              ; restore id
    jmp .l2_rx_drop_recycle

.l2_rx_drop_src:
    ; --- ETH-015: drop a malformed-source-MAC RX frame ---
    ;
    ; Branched here from the source-MAC check when the source MAC's
    ; byte 0 bit 0 is set (multicast / broadcast source — invalid
    ; per IEEE 802.3 §4.1.2.1). Frame is structurally well-formed
    ; through the dst MAC filter; only the src field is wrong.
    mov [rsp + 12], edi                              ; save id

    lea rsi, [rel rx_drop_src_msg]
    mov ecx, rx_drop_src_len
    call emit_bytes

    mov edi, [rsp + 12]                              ; restore id
    jmp .l2_rx_drop_recycle

.l2_rx_drop_pause:
    ; --- ETH-018: drop a PAUSE / MAC Control RX frame ---
    ;
    ; Branched here when EtherType = 0x8808 (MAC Control). The
    ; frame passed every other gate (size, dst MAC, src MAC) —
    ; we just don't act on flow-control frames at all, per
    ; D045's deferred-feature list and ETH-018's "silently
    ; discard" mandate. Counter (rx_pause_dropped) deferred
    ; until stats infrastructure exists; the serial marker is
    ; the testable artifact today.
    mov [rsp + 12], edi                              ; save id

    lea rsi, [rel rx_drop_pause_msg]
    mov ecx, rx_drop_pause_len
    call emit_bytes

    mov edi, [rsp + 12]                              ; restore id
    ; fall through to recycle

.l2_rx_drop_recycle:
    ; Shared recycle code reached by both .l2_rx_drop (size bounds)
    ; and .l2_rx_drop_mac (MAC filter). Write id to AvailRing.ring
    ; [next_avail & mask] so the device gets the buffer back, then
    ; advance the shadows + per-dispatch counter and re-enter the
    ; consume loop.
    ;
    ; The store uses `di` (low 16 of restored edi); descriptor ids
    ; are u16 in the AvailRing per Virtio 1.2 §2.7.13. id was
    ; bounds-checked < VIRTQ_MAX_SIZE (256) above, so the upper 16
    ; bits are 0 anyway — the narrow loses no information.
    mov eax, r14d
    lea ecx, [r13d - 1]
    and eax, ecx
    mov [r15 + VIRTQ_OFF_AVAIL + 4 + rax*2], di

    inc r14d
    inc ebp
    inc dword [rsp + 0]

    jmp .l2_rx_consume_check

.l2_rx_bad_id_fail:
    ; FSA-4 writeback intentionally skipped on validation-fail
    ; paths: l2_dispatch returns eax = 1 → caller sets
    ; VIRTIO_STATUS_FAILED and halts. No subsequent dispatch
    ; will consume the persisted shadows, so leaving l2_state
    ; at its on-entry values is correct. Same applies to
    ; .l2_rx_num_bufs_fail and .l2_tx_bad_id_fail below.
    ;
    ; Strict SysV: 1-arg fail path. A single push would leave
    ; rsp ≡ 8 (mod 16) for the subsequent CALL — instead, reserve
    ; a 16-byte aligned scratch frame and stash the saved value
    ; via mov [rsp + N], reg.
    sub rsp, 16
    mov [rsp + 0], rdi                               ; save id
    lea rsi, [rel rx_bad_id_msg]
    mov ecx, rx_bad_id_len
    call emit_bytes
    mov rax, [rsp + 0]
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    add rsp, 16
    jmp .l2_fail

.l2_rx_num_bufs_fail:
    sub rsp, 16
    mov [rsp + 0], r11                               ; save num_buffers
    lea rsi, [rel rx_num_bufs_msg]
    mov ecx, rx_num_bufs_len
    call emit_bytes
    mov rax, [rsp + 0]
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    add rsp, 16
    jmp .l2_fail

.l2_tx_bad_id_fail:
    sub rsp, 16
    mov [rsp + 0], rdi                               ; save id
    lea rsi, [rel tx_bad_id_msg]
    mov ecx, tx_bad_id_len
    call emit_bytes
    mov rax, [rsp + 0]
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    add rsp, 16
    jmp .l2_fail

.l2_ok:
    xor eax, eax
    jmp .l2_epilogue

.l2_fail:
    mov eax, 1
    ; fall through

.l2_epilogue:
    add rsp, 32                                      ; release local frame (FSA-4 + FSA-4(A))
    pop r14                                          ; restore caller's r14 (SysV)
    pop rbx
    pop rbp
    ret

; --- Strings used by l2_dispatch ---
section .rodata
rx_timeout_msg:          db 'RX:TIMEOUT', 10
rx_timeout_len           equ $ - rx_timeout_msg

rx_frame_prefix_msg:     db 'RX:FRAME id='
rx_frame_prefix_len      equ $ - rx_frame_prefix_msg
rx_frame_hdr_len_msg:    db ' hdr_len='
rx_frame_hdr_len_len     equ $ - rx_frame_hdr_len_msg
rx_frame_num_bufs_msg:   db ' num_bufs='
rx_frame_num_bufs_len    equ $ - rx_frame_num_bufs_msg
rx_frame_used_len_msg:   db ' used_len='
rx_frame_used_len_len    equ $ - rx_frame_used_len_msg

rx_bad_id_msg:           db 'RX:FAIL bad_id='
rx_bad_id_len            equ $ - rx_bad_id_msg
rx_num_bufs_msg:         db 'RX:FAIL num_bufs='
rx_num_bufs_len          equ $ - rx_num_bufs_msg

rx_drop_msg:             db 'RX:DROP used_len='
rx_drop_len              equ $ - rx_drop_msg
rx_drop_mac_msg:         db 'RX:DROP mac', 10
rx_drop_mac_len          equ $ - rx_drop_mac_msg
rx_drop_src_msg:         db 'RX:DROP src', 10
rx_drop_src_len          equ $ - rx_drop_src_msg
rx_drop_pause_msg:       db 'RX:DROP pause', 10
rx_drop_pause_len        equ $ - rx_drop_pause_msg

rx_returned_msg:         db 'RX:RETURNED', 10
rx_returned_len          equ $ - rx_returned_msg

tx_submitted_msg:        db 'TX:SUBMITTED', 10
tx_submitted_len         equ $ - tx_submitted_msg
tx_timeout_msg:          db 'TX:TIMEOUT', 10
tx_timeout_len           equ $ - tx_timeout_msg
tx_reclaimed_prefix_msg: db 'TX:RECLAIMED id='
tx_reclaimed_prefix_len  equ $ - tx_reclaimed_prefix_msg
tx_used_len_msg:         db ' used_len='
tx_used_len_len          equ $ - tx_used_len_msg
tx_bad_id_msg:           db 'TX:FAIL bad_id='
tx_bad_id_len            equ $ - tx_bad_id_msg

; FSA-4(A) ARP responder markers.
arp_request_msg:         db 'ARP:REQUEST', 10
arp_request_len          equ $ - arp_request_msg
arp_reply_msg:           db 'ARP:REPLY', 10
arp_reply_len            equ $ - arp_reply_msg

; FSA-4(A) ARP reply template — 72 bytes, copied verbatim into
; tx_arp_buf then patched with dst MAC / THA / TPA from the
; stashed request info. Pre-baked fields: src MAC (GUEST_MAC),
; EtherType (ARP), HTYPE / PTYPE / HLEN / PLEN, OPER (reply),
; SHA (GUEST_MAC), SPA (GUEST_IP = 192.168.42.2).
;
; GUEST_MAC = 02:00:00:00:00:01 matches the tx_test_pkt canary's
; src MAC for ARP-table consistency.
align 16
arp_reply_template:
    ; virtio_net_hdr (12 zero bytes)
    times 12 db 0
    ; Ethernet header
    times 6  db 0                                    ; dst MAC (patched)
    db GUEST_MAC_BYTE_0, GUEST_MAC_BYTE_1, GUEST_MAC_BYTE_2, \
       GUEST_MAC_BYTE_3, GUEST_MAC_BYTE_4, GUEST_MAC_BYTE_5  ; src MAC = GUEST_MAC
    db 0x08, 0x06                                    ; EtherType = ARP (BE)
    ; ARP payload (28 bytes)
    db 0x00, 0x01                                    ; HTYPE = Ethernet (BE)
    db 0x08, 0x00                                    ; PTYPE = IPv4 (BE)
    db 0x06                                          ; HLEN
    db 0x04                                          ; PLEN
    db 0x00, 0x02                                    ; OPER = reply (BE)
    db GUEST_MAC_BYTE_0, GUEST_MAC_BYTE_1, GUEST_MAC_BYTE_2, \
       GUEST_MAC_BYTE_3, GUEST_MAC_BYTE_4, GUEST_MAC_BYTE_5  ; SHA = GUEST_MAC
    db 0xC0, 0xA8, 0x2A, 0x02                        ; SPA = GUEST_IP
    times 6  db 0                                    ; THA (patched)
    times 4  db 0                                    ; TPA (patched)
    ; Ethernet padding to 60-byte minimum frame.
    times 18 db 0
arp_reply_template_end:

; Mark the stack as non-executable. Without this section the linker
; assumes RWX stack and warns; the other NASM-emitted units in this
; build (arch/x86_64/memory/*.S) do the same.
section .note.GNU-stack noalloc noexec nowrite progbits
