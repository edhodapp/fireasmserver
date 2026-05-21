; SPDX-License-Identifier: AGPL-3.0-or-later
;
; L2 dispatcher — FSA-1 skeleton (single-shot wrapper around the
; existing RX/TX path). x86_64 counterpart to
; arch/aarch64/l2/dispatcher.S.
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
; FSA-1 scope (intentionally narrow):
;   - Same external behavior as the prior inline code. One RX
;     completion consumed, one TX canary submitted + reclaimed,
;     same marker emissions.
;   - The dispatcher is still single-iteration. Multi-frame loop
;     semantics land with FSA-2 (task #16).
;
; Caller contract (from kernel_main_64 in boot.S):
;   - On entry: r12 = VIRTIO_MMIO base, r13 = N (queue size),
;     r14 = next_avail shadow, r15 = virtq base.
;   - rbx and rbp are SysV callee-saved and modified by this
;     function; the prologue/epilogue save+restore them.
;   - r12, r13, r15 are also callee-saved AND read-only inside
;     this function; preserved trivially.
;   - r14 is callee-saved per ABI but MODIFIED here — the RX-
;     consume path bumps next_avail by 1 (`inc r14d`). The caller
;     (kernel_main_64) does not read r14 post-call on either
;     return arm: success → `jmp .halt`, failure → `jmp .set_failed`
;     which only references r12. If a future caller cares about
;     r14 surviving the call, either save/restore r14 here or
;     surface the advanced value as a return.
;   - On return: eax = 0 → caller jmps to .halt (clean halt);
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
; to schedule the device thread. (aarch64 budget was tightened by
; task #15 once `yield` was identified as load-bearing for
; producer-consumer timing under KVM.)
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

; FSA-3: no x86 code changes needed for the phantom-frame race
; fix that landed on aarch64 (LDARH for load-acquire on idx).
; x86 TSO (Total Store Order) provides load-acquire semantics on
; every ordinary load — loads are never reordered with other
; loads, and loads are never reordered with prior stores on the
; same CPU. So the consumer reading UsedRing.idx followed by
; ring[slot] followed by the buffer is naturally ordered.
;
; The producer (Firecracker virtio-net VMM thread on x86_64) also
; benefits from TSO — stores are never reordered with other
; stores. A write to UsedRing.idx is guaranteed to be observed
; after all prior buffer + ring writes by any single observer.
;
; The lfence/mfence sprinkled through the existing path are
; therefore conservative and could be reduced (see Intel SDM
; Vol. 3A §8.2.2 — "Loads are not reordered with other loads.").
; They stay for now as defensive symmetry with aarch64, where the
; barriers ARE load-bearing. A future cleanup could narrow them.
;
; The race only manifested on aarch64 per the FSA-3 task notes
; (~15-25% on Pi 5; never observed on x86_64).

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
    ; SysV AMD64 prologue. rbx and rbp are callee-saved and the
    ; dispatcher modifies both. r12-r15 are also callee-saved but
    ; the dispatcher *preserves* their pre-existing values (they
    ; were set up by kernel_main_64 and must survive the call) —
    ; no save needed since the body never overwrites them.
    push rbp
    mov rbp, rsp
    push rbx

    ; --- VIO-R-003/004: poll RX Used Ring for first completion ---
    ;
    ; H2: maintain a consumer-shadow in rbp (RX used_shadow).
    xor ebp, ebp
    mov ecx, POLL_BUDGET
.l2_rx_wait_loop:
    lfence
    mov ax, [r15 + VIRTQ_OFF_USED + 2]
    lfence
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
    ; Per-frame work: read ring slot, bounds-check id, read
    ; virtio_net_hdr, validate num_buffers, emit RX:FRAME, then
    ; write the descriptor id into AvailRing.ring[next_avail & mask].
    ; The AvailRing.idx publish + RX QueueNotify are BATCHED to
    ; .l2_rx_drained — one mfence + one idx store + one notify
    ; covers all frames consumed this dispatch. Spec-compliant
    ; per Virtio 1.2 §2.7.13.3.
    ;
    ; Frame budget: rbp doubles as the consumer shadow and the
    ; per-dispatch frame counter (initialized to 0 at .l2_rx_wait_loop
    ; setup). FSA-3+ will separate them when shadow becomes
    ; persistent across dispatcher calls.
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

    ; Buffer addr: __memreq_addr__rx_buffer + id*2048.
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

    ; Emit: RX:FRAME id=... hdr_len=... num_bufs=... used_len=...
    ; Push order: used_len, num_bufs, hdr_len, id (last → first pop = id).
    push rbx                                         ; used_len
    push r11                                         ; num_buffers
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
    ; No busy-wait here; this is a non-blocking drain.
    lfence
    mov ax, [r15 + VIRTQ_OFF_USED + 2]
    lfence
    cmp ax, bp
    jne .l2_rx_consume_body
    ; else: idx == shadow → queue drained, fall through

.l2_rx_drained:
    ; --- VIO-R-007: batched AvailRing.idx publish + RX kick ---
    mfence
    mov [r15 + VIRTQ_OFF_AVAIL + 2], r14w             ; AvailRing.idx = next_avail
    mfence
    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 0     ; kick RX
    mfence

    ; Emit RX:RETURNED once per dispatch (after all frames consumed).
    lea rsi, [rel rx_returned_msg]
    mov ecx, rx_returned_len
    call emit_bytes

    ; --- VIO-T-002..005: submit one canary Ethernet frame ---
    lea rbx, [r15 + TX_QUEUE_OFF + VIRTQ_OFF_DESC]
    ; addr is 32-bit immediate; tx_test_pkt is < 4 GiB so the
    ; sign-extended-to-64 immediate is correct as a phys address.
    ; Valid while paging identity-maps low 4 GiB (D063).
    mov dword [rbx + 0],  tx_test_pkt
    mov dword [rbx + 4],  0
    mov dword [rbx + 8],  TX_TEST_PKT_LEN
    mov word  [rbx + 12], 0
    mov word  [rbx + 14], 0

    mov word [r15 + TX_QUEUE_OFF + VIRTQ_OFF_AVAIL + 4], 0   ; ring[0] = 0

    mfence

    mov word [r15 + TX_QUEUE_OFF + VIRTQ_OFF_AVAIL + 2], 1   ; idx = 1

    mfence

    mov dword [r12 + VIRTIO_MMIO_QUEUE_NOTIFY], 1            ; kick TX
    mfence

    lea rsi, [rel tx_submitted_msg]
    mov ecx, tx_submitted_len
    call emit_bytes

    ; --- VIO-T-006: poll TX Used Ring for completion ---
    ; H2: TX consumer-shadow restart (independent of RX queue).
    xor ebp, ebp
    mov ecx, POLL_BUDGET
.l2_tx_wait_loop:
    lfence
    mov ax, [r15 + TX_QUEUE_OFF + VIRTQ_OFF_USED + 2]
    lfence
    cmp ax, bp
    jne .l2_tx_reclaimed
    dec ecx
    jnz .l2_tx_wait_loop

    lea rsi, [rel tx_timeout_msg]
    mov ecx, tx_timeout_len
    call emit_bytes
    jmp .l2_ok

.l2_tx_reclaimed:
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
    jmp .l2_ok

.l2_rx_bad_id_fail:
    push rdi
    lea rsi, [rel rx_bad_id_msg]
    mov ecx, rx_bad_id_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .l2_fail

.l2_rx_num_bufs_fail:
    push r11
    lea rsi, [rel rx_num_bufs_msg]
    mov ecx, rx_num_bufs_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .l2_fail

.l2_tx_bad_id_fail:
    push rdi
    lea rsi, [rel tx_bad_id_msg]
    mov ecx, tx_bad_id_len
    call emit_bytes
    pop rax
    call emit_hex32
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes
    jmp .l2_fail

.l2_ok:
    xor eax, eax
    jmp .l2_epilogue

.l2_fail:
    mov eax, 1
    ; fall through

.l2_epilogue:
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
