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
;   - r14 is SysV callee-saved AND modified inside this function
;     (the RX-consume path advances next_avail by 0..RX_FRAME_BUDGET).
;     Post-Gemini-review (HIGH finding): the prologue now
;     `push r14` and the epilogue `pop r14`, so caller's r14 value
;     is preserved as the SysV ABI requires. The dispatcher uses
;     r14 internally as a working register, advances it through
;     the consume loop, and restores the caller's value before
;     return. The internal advance is therefore LOST — if a
;     future reentrant caller needs the new next_avail to persist
;     across invocations, the value must move to persistent
;     storage (memreq region) or be returned via a new contract.
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

global l2_dispatch

[bits 64]
section .text

l2_dispatch:
    ; SysV AMD64 prologue.
    ;
    ; Function entry: rsp ≡ 8 (mod 16) per the SysV calling
    ; convention — the caller's CALL pushed an 8-byte return
    ; address onto a 16-aligned stack. Callee-saved set used here:
    ;   - rbx, rbp: modified by the function body
    ;   - r14:      modified by the RX-recycle loop (next_avail++)
    ;   - r12, r13, r15: read-only — no save needed
    ;
    ; Three 8-byte pushes (rbp, rbx, r14) bring rsp ≡ 0 (mod 16),
    ; which is what SysV §3.2.2 requires at every CALL. No extra
    ; padding needed — saving r14 here doubles as the alignment
    ; fix the pre-Gemini code used `sub rsp, 8` for.
    push rbp
    mov rbp, rsp
    push rbx
    push r14

    ; --- VIO-R-003/004: poll RX Used Ring for first completion ---
    ;
    ; H2: maintain a consumer-shadow in rbp (RX used_shadow). rbp
    ; doubles as the per-dispatch RX frame counter for the
    ; RX_FRAME_BUDGET bound; the two roles coincide because the
    ; dispatcher resets rbp to 0 at entry. FSA-2 amplifies the
    ; existing hazard from FSA-1 (more stale reads on a second
    ; invocation than before), but currently the dispatcher is
    ; called exactly once from kernel_main_64 so the issue is
    ; latent. FSA-3+ will separate shadow from counter when
    ; shadow becomes persistent across dispatcher calls.
    xor ebp, ebp
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

    ; virtio_net_hdr.hdr_len (u16 @ +2) and num_buffers (u16 @ +10).
    lfence
    movzx esi, word [rax + 2]                        ; hdr_len
    movzx r11d, word [rax + 10]                      ; num_buffers
    lfence

    cmp r11d, 1
    jne .l2_rx_num_bufs_fail

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

    ; Advance both shadows after the recycle write.
    inc r14d
    inc ebp

.l2_rx_consume_check:
    ; Budget check first — bounded-transition invariant.
    cmp ebp, RX_FRAME_BUDGET
    jae .l2_rx_drained

    ; Queue probe — is there another completion already available?
    ; No busy-wait here; this is a non-blocking drain. No pause
    ; (we're not spinning) and no lfence (x86 TSO orders the load
    ; against subsequent loads naturally).
    mov ax, [r15 + VIRTQ_OFF_USED + 2]
    cmp ax, bp
    jne .l2_rx_consume_body
    ; else: idx == shadow → queue drained, fall through

.l2_rx_drained:
    ; --- VIO-R-007: batched AvailRing.idx publish + RX kick ---
    ; Two mfences (was three): first orders per-slot ring writes
    ; against idx publish, second orders idx against notify.
    ; Gemini (9): the third (post-notify) mfence was unnecessary.
    mfence
    mov [r15 + VIRTQ_OFF_AVAIL + 2], r14w             ; AvailRing.idx = next_avail
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 0     ; kick RX

    ; Emit RX:RETURNED once per dispatch (after all frames consumed).
    lea rsi, [rel rx_returned_msg]
    mov ecx, rx_returned_len
    call emit_bytes

    ; --- VIO-T-002..005: submit one canary Ethernet frame ---
    ;
    ; Post-Gemini-review (#2): the prior code hardcoded
    ; `ring[0] = 0` and `idx = 1`, baking in the single-shot
    ; assumption. Now parameterized by two shadow counters so the
    ; shape is reentrant-ready (persistent storage of the shadows
    ; is FSA-4 work):
    ;
    ;   r9  = tx_next_avail (producer counter for TX AvailRing).
    ;         Today: initialised to 0 here. FSA-4: load from a
    ;         memreq tx_state region so the value persists across
    ;         dispatcher invocations.
    ;   r10 = desc_idx (which TX descriptor slot to write).
    ;         Today: fixed at 0 because TX_BUF_COUNT = 1.
    ;         FSA-4: from a free-list when TX gains slots.
    ;
    ; For r9 = 0, r10 = 0 the byte effect is identical to the
    ; prior hardcoded form: desc[0] = canary, ring[0] = 0,
    ; AvailRing.idx = 1. Mirrors the aarch64 dispatcher's
    ; w15/w16 shadow pair.
    xor r9d, r9d                                     ; tx_next_avail (FSA-4: load from persistent)
    xor r10d, r10d                                   ; desc_idx (FSA-4: from free-list)

    ; rbx = desc[desc_idx] base. Each desc is 16 bytes.
    lea rbx, [r15 + TX_QUEUE_OFF + VIRTQ_OFF_DESC]
    shl r10, 4                                       ; r10 *= 16
    add rbx, r10                                     ; rbx = desc[desc_idx]
    shr r10, 4                                       ; restore r10 = desc_idx for ring write
    ; addr is 32-bit immediate; tx_test_pkt is < 4 GiB so the
    ; sign-extended-to-64 immediate is correct as a phys address.
    ; Valid while paging identity-maps low 4 GiB (D063).
    mov dword [rbx + 0],  tx_test_pkt
    mov dword [rbx + 4],  0
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
    inc r9d
    mov [r15 + TX_QUEUE_OFF + VIRTQ_OFF_AVAIL + 2], r9w

    mfence

    ; Gemini (9): drop the trailing mfence after the notify store
    ; — no subsequent code depends on the notify having
    ; completed, and the upcoming .l2_tx_wait_loop has its own
    ; ordering.
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 1    ; kick TX

    lea rsi, [rel tx_submitted_msg]
    mov ecx, tx_submitted_len
    call emit_bytes

    ; --- VIO-T-006: poll TX Used Ring for completion ---
    ; H2: TX consumer-shadow restart (independent of RX queue).
    xor ebp, ebp
    mov ecx, POLL_BUDGET
.l2_tx_wait_loop:
    pause                                            ; Gemini (8): see RX wait loop
    mov ax, [r15 + TX_QUEUE_OFF + VIRTQ_OFF_USED + 2]
    cmp ax, bp
    jne .l2_tx_reclaimed
    dec ecx
    jnz .l2_tx_wait_loop

    lea rsi, [rel tx_timeout_msg]
    mov ecx, tx_timeout_len
    call emit_bytes
    jmp .l2_ok

.l2_tx_reclaimed:
    ; INVARIANT (FSA-3): the .l2_tx_wait_loop above established
    ; acquire-equivalent ordering on TX UsedRing.idx (via TSO);
    ; subsequent ring-slot reads inherit the ordering.
    mov eax, ebp                                     ; eax = shadow
    lea ecx, [r13d - 1]                              ; ecx = mask = N - 1
    and eax, ecx                                     ; eax = slot
    lea r10, [r15 + rax*8 + TX_QUEUE_OFF + VIRTQ_OFF_USED]
    lfence
    mov edi, [r10 + 4]                               ; ring[slot].id
    mov ebx, [r10 + 8]                               ; ring[slot].len
    lfence

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
    jmp .l2_ok

.l2_rx_bad_id_fail:
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

; Mark the stack as non-executable. Without this section the linker
; assumes RWX stack and warns; the other NASM-emitted units in this
; build (arch/x86_64/memory/*.S) do the same.
section .note.GNU-stack noalloc noexec nowrite progbits
