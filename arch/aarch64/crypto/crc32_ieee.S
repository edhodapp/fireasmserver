// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// CRC-32 IEEE 802.3 (Ethernet FCS polynomial-correct form).
//
// Contract (AAPCS64):
//   x0 = const void *data
//   x1 = size_t      len
// Returns:
//   w0 = uint32_t    crc
//
// Parameters:
//   polynomial  = 0xEDB88320  (reflected form of 0x04C11DB7)
//   init        = 0xFFFFFFFF
//   final XOR   = 0xFFFFFFFF
//   zero-length = 0x00000000
//
// References:
//   IEEE 802.3-2018 §3.2.9 (FCS definition).
//   Arm ARM DDI 0487 J.a §C7.2.65-68 (crc32b/crc32h/crc32w/crc32x
//       natively implement the IEEE 802.3 polynomial in reflected form).
//
// Build-time feature assertion: the +crc (FEAT_CRC32) extension must
// be present. Enabled via .arch below; D034's AArch64 profiles
// (Cortex-A72 / Neoverse / Apple) all carry crc32 as mandatory.

    .arch   armv8-a+crc

    .text
    .globl  crc32_ieee_802_3
    .type   crc32_ieee_802_3, %function
    .balign 16

// ABI: leaf function, no callee-saved regs touched, no stack frame.
// Register usage:
//   x0  : data pointer (input, advanced)
//   x1  : remaining length (input, decremented)
//   w2  : running CRC (init 0xFFFFFFFF, finalized with ~ at exit)
//   x3  : scratch (loaded 8-byte chunk)
//   w3  : scratch (loaded 4/2/1-byte chunk)
crc32_ieee_802_3:
    mov     w2, #0xFFFFFFFF         // init state

    // 8-byte main loop: the widest update in one instruction.
    // The loop condition is "at least 8 bytes remaining," not
    // "exactly zero," so folding to `subs` doesn't eliminate the
    // subsequent `cmp`. Kept as plain `sub` + `cmp`.
    cmp     x1, #8
    b.lo    .Ltail_4
.Lloop_8:
    ldr     x3, [x0], #8
    crc32x  w2, w2, x3
    sub     x1, x1, #8
    cmp     x1, #8
    b.hs    .Lloop_8

    // Residue 0..7 bytes. Each of bits 2/1/0 of x1 is independent,
    // so we test them directly against the untouched x1 without
    // decrementing — the pointer does advance on each sub-load.
.Ltail_4:
    tbz     x1, #2, .Ltail_2
    ldr     w3, [x0], #4
    crc32w  w2, w2, w3

.Ltail_2:
    tbz     x1, #1, .Ltail_1
    ldrh    w3, [x0], #2
    crc32h  w2, w2, w3

.Ltail_1:
    tbz     x1, #0, .Ldone
    ldrb    w3, [x0], #1
    crc32b  w2, w2, w3

.Ldone:
    mvn     w0, w2                  // final XOR with 0xFFFFFFFF
    ret

    .size   crc32_ieee_802_3, . - crc32_ieee_802_3

// Non-executable stack marker (mirrors the x86_64 sibling).
    .section .note.GNU-stack, "", %progbits
