// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// ARP cache (aarch64) — D068 working order item 6, phase 6.a.
// Mirror of arch/x86_64/l2/arp_cache.S; same layout, same
// three-pass insert (existing IP → FREE → LRU eviction).
//
// AAPCS64 calling convention:
//
//   arp_cache_lookup(w0 = ip_be)
//     → x0 = pointer to the cache ENTRY (32 bytes; IP at +0,
//       MAC at +4, state at +10, ...) on hit, 0 on miss.
//
//   arp_cache_insert(w0 = ip_be, x1 = mac_ptr, w2 = state)
//     → nothing.
//
// Uses x9-x14 as scratch (caller-saved); preserves x19-x28.

.equ ARP_CACHE_ENTRIES,         16
.equ ARP_CACHE_ENTRY_BYTES,     32
.equ ARP_CACHE_HEADER_BYTES,    64

.equ ARP_CACHE_HDR_GEN,         0

.equ ARP_ENTRY_IP,              0
.equ ARP_ENTRY_MAC,             4
.equ ARP_ENTRY_STATE,           10
.equ ARP_ENTRY_RETRY,           11
.equ ARP_ENTRY_LRU_GEN,         12

.equ ARP_STATE_FREE,            0
.equ ARP_STATE_INCOMPLETE,      1
.equ ARP_STATE_REACHABLE,       2
// 3=STALE, 4=PROBE, 5=FAILED — phase 6.e wires the transitions

.equ ARP_STATE_STALE,           3
.equ ARP_STATE_PROBE,           4
.equ ARP_STATE_FAILED,          5

.equ ARP_MAX_RETRIES,           3

.equ ARP_RESOLVE_OK,            0
.equ ARP_RESOLVE_PENDING,       1
.equ ARP_RESOLVE_FAILED,        2

.equ ARP_ENTRY_LAST_EVENT_CYCLES, 16    // u64
// ARP_ENTRY_NEXT_ACTION_CYCLES, 24 — phase 6.e follow-on

// Aging threshold: ~100 ms @ Pi 5's 54 MHz CNTVCT_EL0
// (5.4M cycles). Tuned for test-observability + low spurious
// triggers in production. CNTFRQ-based calibration is a
// future refinement.
.equ ARP_AGING_CYCLES,          5400000

// TX API + emit symbols for arp_send_request below.
.equ L2_TX_POOL_IDX_FULL,       0xFFFFFFFF
.equ L2_TX_PAYLOAD_OFFSET,      26
.equ GUEST_MAC_LE_LO32,         0x00000002
.equ GUEST_MAC_LE_HI16,         0x00000100

.text
.global arp_cache_lookup
.global arp_cache_insert
.global arp_send_request
.global arp_resolve
.global arp_cache_tick

// ================================================================
// arp_cache_lookup
// ================================================================
arp_cache_lookup:
    adrp    x9, __memreq_addr__arp_cache
    add     x9, x9, :lo12:__memreq_addr__arp_cache
    ldr     x9, [x9]                                  // x9 = cache base
    add     x10, x9, #ARP_CACHE_HEADER_BYTES          // x10 = entries[0]
    mov     w11, #0                                   // w11 = scan index
.Llookup_scan:
    ldr     w12, [x10, #ARP_ENTRY_IP]
    cmp     w12, w0
    b.eq    .Llookup_candidate
.Llookup_next:
    add     x10, x10, #ARP_CACHE_ENTRY_BYTES
    add     w11, w11, #1
    cmp     w11, #ARP_CACHE_ENTRIES
    b.lo    .Llookup_scan
    mov     x0, #0                                    // miss
    ret
.Llookup_candidate:
    ldrb    w12, [x10, #ARP_ENTRY_STATE]
    cbz     w12, .Llookup_next                        // FREE slot — ip=0 spurious match
    // Hit. Bump generation counter + stamp this entry's LRU.
    ldr     w12, [x9, #ARP_CACHE_HDR_GEN]
    add     w12, w12, #1
    str     w12, [x9, #ARP_CACHE_HDR_GEN]
    str     w12, [x10, #ARP_ENTRY_LRU_GEN]
    mov     x0, x10                                   // return entry pointer
    ret

// ================================================================
// arp_cache_insert
// ================================================================
arp_cache_insert:
    adrp    x9, __memreq_addr__arp_cache
    add     x9, x9, :lo12:__memreq_addr__arp_cache
    ldr     x9, [x9]                                  // x9 = cache base
    add     x10, x9, #ARP_CACHE_HEADER_BYTES          // x10 = entries[0]

    // --- Pass 1: existing IP match ---
    mov     x13, x10                                  // x13 = walker = entries[0]
    mov     w11, #0
.Lins_pass1:
    ldr     w12, [x13, #ARP_ENTRY_IP]
    cmp     w12, w0
    b.eq    .Lins_write                               // x13 = matching slot
    add     x13, x13, #ARP_CACHE_ENTRY_BYTES
    add     w11, w11, #1
    cmp     w11, #ARP_CACHE_ENTRIES
    b.lo    .Lins_pass1

    // --- Pass 2: any FREE slot ---
    mov     x13, x10
    mov     w11, #0
.Lins_pass2:
    ldrb    w12, [x13, #ARP_ENTRY_STATE]
    cbz     w12, .Lins_write                          // FREE → take it
    add     x13, x13, #ARP_CACHE_ENTRY_BYTES
    add     w11, w11, #1
    cmp     w11, #ARP_CACHE_ENTRIES
    b.lo    .Lins_pass2

    // --- Pass 3: LRU eviction ---
    mov     x13, x10                                  // best candidate = entries[0]
    ldr     w14, [x10, #ARP_ENTRY_LRU_GEN]            // best gen
    add     x15, x10, #ARP_CACHE_ENTRY_BYTES          // walker = entries[1]
    mov     w11, #1
.Lins_pass3:
    ldr     w12, [x15, #ARP_ENTRY_LRU_GEN]
    cmp     w12, w14
    b.hs    .Lins_pass3_next                          // not older
    mov     x13, x15
    mov     w14, w12
.Lins_pass3_next:
    add     x15, x15, #ARP_CACHE_ENTRY_BYTES
    add     w11, w11, #1
    cmp     w11, #ARP_CACHE_ENTRIES
    b.lo    .Lins_pass3

.Lins_write:
    // x13 = chosen slot.
    str     w0, [x13, #ARP_ENTRY_IP]
    // MAC: 4 bytes + 2 bytes from [x1].
    ldr     w12, [x1]
    str     w12, [x13, #ARP_ENTRY_MAC]
    ldrh    w12, [x1, #4]
    strh    w12, [x13, #(ARP_ENTRY_MAC + 4)]
    strb    w2, [x13, #ARP_ENTRY_STATE]
    strb    wzr, [x13, #ARP_ENTRY_RETRY]
    // Bump generation counter + stamp LRU.
    ldr     w12, [x9, #ARP_CACHE_HDR_GEN]
    add     w12, w12, #1
    str     w12, [x9, #ARP_CACHE_HDR_GEN]
    str     w12, [x13, #ARP_ENTRY_LRU_GEN]
    // Stamp last_event_time (phase 6.e). CNTVCT_EL0 gives a
    // 64-bit cycle count; aging is measured as `now -
    // last_event` per arp_cache_tick.
    mrs     x12, cntvct_el0
    str     x12, [x13, #ARP_ENTRY_LAST_EVENT_CYCLES]
    ret

// ================================================================
// arp_send_request — send a broadcast ARP request
// ================================================================
//   In:  w0 = target_ip_be
//   Out: w0 = 0 on OK, 1 on pool/queue FULL
//   AAPCS64; uses x19 callee-saved internally to stash
//   target_ip across the TX API call chain.
arp_send_request:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    stp     x19, x20, [sp, #16]                 // x19 = target_ip; x20 = pool_idx
    mov     w19, w0

    bl      l2_tx_buffer_pool_alloc             // w0 = pool_idx
    cmn     w0, #1                              // == 0xFFFFFFFF?
    b.eq    .Larp_send_full
    mov     w20, w0

    bl      l2_tx_buffer_addr                   // x0 = pool buffer base
    add     x9, x0, #L2_TX_PAYLOAD_OFFSET       // x9 = payload start

    mov     w10, #0x0100
    strh    w10, [x9, #0]                       // HTYPE 0x0001 BE
    mov     w10, #0x0008
    strh    w10, [x9, #2]                       // PTYPE 0x0800 BE
    mov     w10, #6
    strb    w10, [x9, #4]                       // HLEN
    mov     w10, #4
    strb    w10, [x9, #5]                       // PLEN
    mov     w10, #0x0100
    strh    w10, [x9, #6]                       // OPER 0x0001 BE (request)
    mov     w10, #GUEST_MAC_LE_LO32
    str     w10, [x9, #8]                       // SHA bytes 0-3
    mov     w10, #GUEST_MAC_LE_HI16
    strh    w10, [x9, #12]                      // SHA bytes 4-5
    ldr     w10, =0x022AA8C0                    // GUEST_IP wire LE
    str     w10, [x9, #14]                      // SPA
    str     wzr, [x9, #18]                      // THA bytes 0-3 = 0
    strh    wzr, [x9, #22]                      // THA bytes 4-5 = 0
    str     w19, [x9, #24]                      // TPA = target_ip

    adr     x0, arp_broadcast_mac               // dst MAC
    mov     w1, #0x0608                         // etype 0x0806 BE
    mov     w2, w20                             // pool_idx
    mov     w3, #28                             // payload_len
    bl      l2_tx_enqueue
    cbnz    w0, .Larp_send_full

    adr     x1, arp_request_sent_msg
    mov     w2, #(arp_request_sent_msg_end - arp_request_sent_msg)
    bl      emit_bytes
    mov     w5, w19
    bl      emit_hex32
    adr     x1, arp_send_newline
    mov     w2, #1
    bl      emit_bytes

    mov     w0, #0
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

.Larp_send_full:
    adr     x1, arp_request_full_msg
    mov     w2, #(arp_request_full_msg_end - arp_request_full_msg)
    bl      emit_bytes
    mov     w0, #1
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

.section .rodata
arp_broadcast_mac:
    .byte   0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
arp_request_sent_msg:
    .ascii  "ARP:REQUEST_SENT ip="
arp_request_sent_msg_end:
arp_request_full_msg:
    .ascii  "ARP:REQUEST_FULL\n"
arp_request_full_msg_end:
arp_send_newline:
    .byte   10

// ================================================================
// arp_resolve — L3-callable IP → MAC resolution
// ================================================================
//   In:  w0 = ip_be, x1 = out_mac_ptr (caller's 6-byte buffer)
//   Out: w0 = status: 0 OK, 1 PENDING, 2 FAILED
//   Uses x19 + x20 callee-saved internally.
//
// See the x86_64 mirror for the semantic contract.
arp_resolve:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    stp     x19, x20, [sp, #16]                 // x19 = ip; x20 = out_mac_ptr
    mov     w19, w0
    mov     x20, x1

    bl      arp_cache_lookup                    // x0 = entry ptr or 0
    cbz     x0, .Lresolve_miss

    // Cache hit — branch on state.
    ldrb    w9, [x0, #ARP_ENTRY_STATE]
    cmp     w9, #ARP_STATE_REACHABLE
    b.ne    .Lresolve_pending_hit

    // REACHABLE: copy MAC bytes from entry into caller's buffer.
    ldr     w9, [x0, #ARP_ENTRY_MAC]
    str     w9, [x20]
    ldrh    w9, [x0, #(ARP_ENTRY_MAC + 4)]
    strh    w9, [x20, #4]

    adr     x1, arp_resolve_ok_msg
    mov     w2, #(arp_resolve_ok_msg_end - arp_resolve_ok_msg)
    bl      emit_bytes
    mov     w5, w19
    bl      emit_hex32
    adr     x1, arp_send_newline
    mov     w2, #1
    bl      emit_bytes
    mov     w0, #ARP_RESOLVE_OK
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

.Lresolve_pending_hit:
    adr     x1, arp_resolve_pending_msg
    mov     w2, #(arp_resolve_pending_msg_end - arp_resolve_pending_msg)
    bl      emit_bytes
    mov     w5, w19
    bl      emit_hex32
    adr     x1, arp_send_newline
    mov     w2, #1
    bl      emit_bytes
    mov     w0, #ARP_RESOLVE_PENDING
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

.Lresolve_miss:
    // Insert INCOMPLETE placeholder so subsequent lookups (before
    // the reply lands) don't re-send a request.
    mov     w0, w19
    adr     x1, arp_zero_mac
    mov     w2, #ARP_STATE_INCOMPLETE
    bl      arp_cache_insert

    mov     w0, w19
    bl      arp_send_request
    cbnz    w0, .Lresolve_send_failed

    adr     x1, arp_resolve_pending_msg
    mov     w2, #(arp_resolve_pending_msg_end - arp_resolve_pending_msg)
    bl      emit_bytes
    mov     w5, w19
    bl      emit_hex32
    adr     x1, arp_send_newline
    mov     w2, #1
    bl      emit_bytes
    mov     w0, #ARP_RESOLVE_PENDING
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

.Lresolve_send_failed:
    adr     x1, arp_resolve_failed_msg
    mov     w2, #(arp_resolve_failed_msg_end - arp_resolve_failed_msg)
    bl      emit_bytes
    mov     w5, w19
    bl      emit_hex32
    adr     x1, arp_send_newline
    mov     w2, #1
    bl      emit_bytes
    mov     w0, #ARP_RESOLVE_FAILED
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #32
    ret

.section .rodata
arp_zero_mac:
    .byte   0x00, 0x00, 0x00, 0x00, 0x00, 0x00
arp_resolve_ok_msg:
    .ascii  "ARP:RESOLVE_OK ip="
arp_resolve_ok_msg_end:
arp_resolve_pending_msg:
    .ascii  "ARP:RESOLVE_PENDING ip="
arp_resolve_pending_msg_end:
arp_resolve_failed_msg:
    .ascii  "ARP:RESOLVE_FAILED ip="
arp_resolve_failed_msg_end:

// ================================================================
// arp_cache_tick — drive timer-based state transitions
// ================================================================
// Caller: dispatcher loop, once per iteration. Walks all 16
// entries; for each non-FREE entry, compares (now -
// last_event_time) against ARP_AGING_CYCLES and applies the
// appropriate transition.
//
// Today: only REACHABLE → STALE is implemented. Follow-on
// commits add INCOMPLETE retry, PROBE, FAILED transitions.
//
// Emits ARP:STATE_CHANGE ip=XXXXXXXX old=N new=N on each
// transition. Uses x19/x20/x21 callee-saved internally.
arp_cache_tick:
    stp     x29, x30, [sp, #-64]!
    mov     x29, sp
    stp     x19, x20, [sp, #16]
    stp     x21, x22, [sp, #32]
    str     x23, [sp, #48]

    mrs     x19, cntvct_el0                  // x19 = now
    adrp    x20, __memreq_addr__arp_cache
    add     x20, x20, :lo12:__memreq_addr__arp_cache
    ldr     x20, [x20]                       // x20 = cache base
    add     x21, x20, #ARP_CACHE_HEADER_BYTES // x21 = walker (entries[0])
    mov     w9, #0                           // w9 = slot index
.Ltick_scan:
    ldrb    w10, [x21, #ARP_ENTRY_STATE]
    cmp     w10, #ARP_STATE_REACHABLE
    b.eq    .Ltick_reachable
    cmp     w10, #ARP_STATE_INCOMPLETE
    b.eq    .Ltick_incomplete
    b       .Ltick_next

.Ltick_reachable:
    ldr     x11, [x21, #ARP_ENTRY_LAST_EVENT_CYCLES]
    sub     x11, x19, x11
    // ARP_AGING_CYCLES = 5_400_000 = 0x0052_4980. movz+movk
    // to avoid the literal-pool alignment hazard.
    movz    w12, #0x4980
    movk    w12, #0x0052, lsl #16
    cmp     x11, x12
    b.lo    .Ltick_next
    mov     w10, #ARP_STATE_STALE
    strb    w10, [x21, #ARP_ENTRY_STATE]
    str     x19, [x21, #ARP_ENTRY_LAST_EVENT_CYCLES]
    mov     w22, #ARP_STATE_REACHABLE       // old state for marker
    mov     w23, #ARP_STATE_STALE           // new state for marker
    b       .Ltick_emit_state_change

.Ltick_incomplete:
    // INCOMPLETE entry: 6.e.3 retry behaviour. After
    // AGING_CYCLES elapse, retry up to ARP_MAX_RETRIES times
    // before transitioning to FAILED.
    ldr     x11, [x21, #ARP_ENTRY_LAST_EVENT_CYCLES]
    sub     x11, x19, x11
    movz    w12, #0x4980
    movk    w12, #0x0052, lsl #16
    cmp     x11, x12
    b.lo    .Ltick_next
    ldrb    w10, [x21, #ARP_ENTRY_RETRY]
    cmp     w10, #ARP_MAX_RETRIES
    b.hs    .Ltick_incomplete_fail
    // Retry: bump retry_count, restart age clock, re-send.
    add     w10, w10, #1
    strb    w10, [x21, #ARP_ENTRY_RETRY]
    str     x19, [x21, #ARP_ENTRY_LAST_EVENT_CYCLES]
    ldr     w0, [x21]                       // ip arg for arp_send_request
    bl      arp_send_request
    b       .Ltick_next
.Ltick_incomplete_fail:
    mov     w10, #ARP_STATE_FAILED
    strb    w10, [x21, #ARP_ENTRY_STATE]
    str     x19, [x21, #ARP_ENTRY_LAST_EVENT_CYCLES]
    mov     w22, #ARP_STATE_INCOMPLETE
    mov     w23, #ARP_STATE_FAILED
    // fall through

.Ltick_emit_state_change:
    // w22 = old state, w23 = new state. x21 + x20 + x19
    // are callee-saved across emit calls.
    adr     x1, arp_state_change_prefix_msg
    mov     w2, #(arp_state_change_prefix_end - arp_state_change_prefix_msg)
    bl      emit_bytes
    ldr     w5, [x21]
    bl      emit_hex32
    adr     x1, arp_state_change_old_msg
    mov     w2, #(arp_state_change_old_end - arp_state_change_old_msg)
    bl      emit_bytes
    mov     w5, w22
    bl      emit_hex32
    adr     x1, arp_state_change_new_msg
    mov     w2, #(arp_state_change_new_end - arp_state_change_new_msg)
    bl      emit_bytes
    mov     w5, w23
    bl      emit_hex32
    adr     x1, arp_send_newline
    mov     w2, #1
    bl      emit_bytes
.Ltick_next:
    add     x21, x21, #ARP_CACHE_ENTRY_BYTES
    add     w9, w9, #1
    cmp     w9, #ARP_CACHE_ENTRIES
    b.lo    .Ltick_scan

    ldr     x23, [sp, #48]
    ldp     x21, x22, [sp, #32]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #64
    ret

.section .rodata
arp_state_change_prefix_msg:
    .ascii  "ARP:STATE_CHANGE ip="
arp_state_change_prefix_end:
arp_state_change_old_msg:
    .ascii  " old="
arp_state_change_old_end:
arp_state_change_new_msg:
    .ascii  " new="
arp_state_change_new_end:
