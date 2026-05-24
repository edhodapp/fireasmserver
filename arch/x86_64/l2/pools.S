; SPDX-License-Identifier: AGPL-3.0-or-later
;
; L2 buffer pool sizing — static, per D043. x86_64 counterpart to
; arch/aarch64/l2/pools.S.
;
; Values mirror arch/x86_64/platform/firecracker/regions.yaml
; (rx_buffer.size = 524288 = 256 * 2048). Keep in sync if the
; region grows.

RX_BUF_COUNT    equ 256
RX_BUF_SIZE     equ 2048
TX_BUF_COUNT    equ 1
TX_BUF_SIZE     equ 72

section .note.GNU-stack noalloc noexec nowrite progbits
