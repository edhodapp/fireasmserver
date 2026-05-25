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
//     → x0 = pointer to 6-byte MAC on hit, 0 on miss.
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

.text
.global arp_cache_lookup
.global arp_cache_insert

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
    add     x0, x10, #ARP_ENTRY_MAC
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
    ret
