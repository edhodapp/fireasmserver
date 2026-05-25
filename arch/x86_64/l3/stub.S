; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; L3 RX dispatch stub (x86_64) — D068 working order item 5.
;
; This file defines the callable surface the L2 dispatcher
; uses to hand off each accepted, non-ARP RX frame. There is
; no real L3 in the build yet; this stub emits an
; L3:RX_FRAME marker (so the integration test can verify the
; hand-off fires) and returns. When real L3 lands it
; replaces this file's body.
;
; Calling convention (SysV AMD64):
;
;   l3_rx_dispatch(rdi = frame_addr, esi = wire_len)
;
;   frame_addr — pointer to the first byte of the Ethernet
;                header (dst MAC). The dispatcher passes
;                rx_buffer + 12 so the virtio_net_hdr is
;                already skipped.
;   wire_len   — bytes from start of dst MAC through end of
;                payload. Already validated by the dispatcher:
;                60 <= wire_len <= 1518, no FCS (virtio
;                strips it per §5.1.6.1).
;
; The stub does not retain the frame past return — caller is
; free to recycle the rx_buffer slot. Real L3 will either
; copy out what it needs synchronously or migrate to a
; queue-based hand-off (mirroring the TX API) when it gains
; multiple consumers.

extern emit_bytes
extern emit_hex32

global l3_rx_dispatch

[bits 64]
section .text

l3_rx_dispatch:
    ; Standard SysV prologue. push rbp aligns rsp to 16
    ; (entry: rsp ≡ 8); two further 8-byte pushes maintain
    ; alignment at each subsequent call site.
    push rbp
    mov rbp, rsp
    push rdi                                         ; preserve frame_addr (currently unused but
                                                     ; future L3 will want it)
    push rsi                                         ; preserve wire_len for the emit_hex32 readback

    lea rsi, [rel l3_rx_msg]
    mov ecx, l3_rx_msg_len
    call emit_bytes
    mov rax, [rsp + 0]                               ; rsi was pushed last → at [rsp]
    call emit_hex32                                  ; emit wire_len in hex
    lea rsi, [rel newline]
    mov ecx, 1
    call emit_bytes

    pop rsi
    pop rdi
    pop rbp
    ret

section .rodata
l3_rx_msg:               db 'L3:RX_FRAME len='
l3_rx_msg_len            equ $ - l3_rx_msg
newline:                 db 10

section .note.GNU-stack noalloc noexec nowrite progbits
