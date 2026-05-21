// SPDX-License-Identifier: AGPL-3.0-or-later
//
// L2 buffer pool sizing — static, per D043 (FSA runtime: static
// pools, cooperative dispatch, bounded transitions).
//
// FSA-1 scope: declare the size/count constants symbolically.
// Storage itself stays in the existing .memreq region for RX
// (allocator-placed; address resolved via __memreq_addr__rx_buffer)
// and as the rodata canary frame for TX (tx_test_pkt in boot.S).
//
// FSA-2+ may introduce a dedicated TX buffer pool and explicit
// free-list metadata. The size constants here become the
// authoritative source for both arches when that lands.
//
// Values mirror arch/aarch64/platform/firecracker/regions.yaml
// (rx_buffer.size = 524288 = 256 * 2048). Keep in sync if the
// region grows.

.equ RX_BUF_COUNT,      256
.equ RX_BUF_SIZE,       2048
.equ TX_BUF_COUNT,      1
.equ TX_BUF_SIZE,       72
