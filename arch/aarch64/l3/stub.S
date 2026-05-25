// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// L3 RX dispatch stub (aarch64) — D068 working order item 5.
// Mirror of arch/x86_64/l3/stub.S.
//
// Calling convention (AAPCS64):
//
//   l3_rx_dispatch(x0 = frame_addr, w1 = wire_len)
//
// See the x86_64 stub for the contract details. Production
// L3 replaces this file's body when it lands.

.text
.global l3_rx_dispatch

// emit_bytes / emit_hex32 are in platform/firecracker/boot.S
// and self-load UART_BASE per task #44.

l3_rx_dispatch:
    stp     x29, x30, [sp, #-32]!
    mov     x29, sp
    // Save args. x0 (frame_addr) is unused by the stub but
    // future L3 will want it; w1 (wire_len) is reloaded for
    // the emit_hex32 readback below.
    stp     x0, x1, [sp, #16]

    adr     x1, l3_rx_msg
    mov     w2, #(l3_rx_msg_end - l3_rx_msg)
    bl      emit_bytes
    ldr     w5, [sp, #24]                    // wire_len from saved w1
    bl      emit_hex32
    adr     x1, l3_newline
    mov     w2, #1
    bl      emit_bytes

    ldp     x0, x1, [sp, #16]                // restore args (caller-saved discipline)
    ldp     x29, x30, [sp], #32
    ret

.section .rodata
l3_rx_msg:
    .ascii  "L3:RX_FRAME len="
l3_rx_msg_end:
l3_newline:
    .byte   10
