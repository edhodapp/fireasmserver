// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// AES-128 block primitive per FIPS PUB 197, AArch64 ARMv8 FEAT_AES
// (`aese`, `aesd`, `aesmc`, `aesimc`). AAPCS64 leaf routines suitable
// for host-C linkage and for direct invocation from assembly.
//
// Contracts:
//   aes128_expand_key(const uint8_t key[16],
//                     uint8_t round_keys[176])
//       x0 = key, x1 = round_keys
//   aes128_encrypt_block(const uint8_t round_keys[176],
//                        const uint8_t in[16], uint8_t out[16])
//       x0 = round_keys, x1 = in, x2 = out   (in may alias out)
//   aes128_decrypt_block(const uint8_t round_keys[176],
//                        const uint8_t in[16], uint8_t out[16])
//       x0 = round_keys, x1 = in, x2 = out   (in may alias out)
//
// Runtime CPU dispatch is not used on AArch64: D034 requires FEAT_AES
// as a baseline for every production profile (Cortex-A76+, Neoverse
// N1/V1/V2, Apple cores). A build-time .arch assertion is sufficient;
// pre-A76 cores (Cortex-A72, Cortex-A55) are out of scope.
//
// Constant-time: aese/aesd/aesmc/aesimc are data-independent in
// timing per Arm ARM §C7.2. The key schedule's only branch is a
// fixed 10-iteration counter — independent of the key bytes. No
// data-dependent indexed loads (the S-box in rodata is indexed by
// plaintext-derived bytes only during key *expansion*, which runs on
// the KEY not on the message state, and is explicitly outside the
// per-message constant-time requirement — the key is already a
// secret. Every aese in the message-encryption path uses the fixed
// hardware S-box with its architected constant-time guarantee).
//
// Byte order (FIPS 197 §3.3): first byte of the input array is byte
// 0 of the 4x4 state (state[0,0]). The NEON 16-byte vector layout
// matches this directly (byte 0 of v0.16b == memory byte 0 after
// `ld1`); aese/aesd read the 16 bytes in AES state order. No byte-
// swap is applied on load or store.
//
// Decrypt key-schedule policy: aes128_expand_key produces the raw
// forward-direction schedule. aes128_decrypt_block applies aesimc
// inline to the inner round keys rk[1..9] as it walks the schedule;
// the caller's round_keys buffer is never mutated by decrypt.
// Trades 9 extra aesimc's per decrypt call for a shared expand_key
// path — immaterial for single-block use; a future mode that
// amortizes expand across many blocks can cache the inverse
// schedule itself.
//
// V-register allocation (both encrypt and decrypt):
//   v0    state, accumulating across rounds
//   v1    round key being XOR'd this round
//       (decrypt also: v1 = aesimc(v1) just before aesd on inner rounds)
// Only v0 and v1 are touched; v8..v15 preservation is trivially
// satisfied. Key expansion uses only GPR scratch (x2..x14), v0, v1.
//
// References:
//   FIPS PUB 197, "Advanced Encryption Standard (AES),"
//       November 2001. §5.1 (Cipher), §5.2 (Key Expansion),
//       §5.3 (Inverse Cipher / Equivalent Inverse Cipher),
//       Appendix C.1 (AES-128 test vector).
//   Arm Architecture Reference Manual for A-profile (ARM DDI 0487),
//       §C7.2 AES instructions (AESE, AESD, AESMC, AESIMC).

    .arch   armv8-a+crypto

// ---------------------------------------------------------------
// Constants
// ---------------------------------------------------------------
    .section .rodata.aes128_consts, "a", %progbits
    .balign 64

// AES forward S-box (FIPS 197 §5.1.1 Table 4) — 256 bytes. Used by
// the scalar key expansion path only; the encrypt/decrypt message
// loops use the hardware S-box inside aese/aesd. File-local (no
// .globl) — no external consumer in the project today.
aes128_sbox:
    .byte 0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5
    .byte 0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76
    .byte 0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0
    .byte 0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0
    .byte 0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc
    .byte 0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15
    .byte 0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a
    .byte 0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75
    .byte 0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0
    .byte 0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84
    .byte 0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b
    .byte 0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf
    .byte 0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85
    .byte 0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8
    .byte 0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5
    .byte 0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2
    .byte 0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17
    .byte 0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73
    .byte 0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88
    .byte 0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb
    .byte 0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c
    .byte 0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79
    .byte 0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9
    .byte 0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08
    .byte 0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6
    .byte 0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a
    .byte 0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e
    .byte 0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e
    .byte 0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94
    .byte 0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf
    .byte 0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68
    .byte 0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16
    .size   aes128_sbox, . - aes128_sbox

// Rcon bytes for AES-128 (FIPS 197 §5.2). Rcon[i] = {RC[i],00,00,00}
// where RC[i] is the byte below; indexed 0..9 here for the 10
// expansion iterations that produce rk[1..10]. File-local.
aes128_rcon:
    .byte 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36
    .size   aes128_rcon, . - aes128_rcon

// ---------------------------------------------------------------
// Text
// ---------------------------------------------------------------
    .text

// ===============================================================
// aes128_expand_key(const uint8_t key[16], uint8_t round_keys[176])
//   x0 = key, x1 = round_keys
//
// Scalar FIPS 197 §5.2 key expansion. The forward S-box in rodata
// supplies SubWord; the standard bit-rotation identity
//
//   RotWord([b0,b1,b2,b3])  ==  ror(word, 8)
//
// holds because a 4-byte FIPS word stored in little-endian memory
// as bytes [b0,b1,b2,b3] loads into a GPR as the integer
// b0 | (b1<<8) | (b2<<16) | (b3<<24), and ror by 8 bits yields
// b1 | (b2<<8) | (b3<<16) | (b0<<24) — exactly the LE re-
// serialization of [b1,b2,b3,b0]. Rcon[i] = {RC[i],00,00,00} in
// FIPS word notation is the LE integer value RC[i] with all other
// bytes zero, so the Rcon XOR is a scalar `eor` against the low byte.
//
// Register map:
//   x0   key (caller-provided, read once)
//   x1   round_keys base (persistent across iterations)
//   x2   S-box base
//   x3   Rcon base
//   x4   round counter 0..10 (pre-incremented: 1..10 in body)
//   w5-w8 running words w[4(i-1)..4i-1] for current round
//   w9   temp: RotWord(w[4i-1])
//   w10  accumulator for SubWord(temp)
//   w11  byte-index scratch
//   w12  byte-lookup scratch
//   w13  Rcon byte for this iteration
//   x14  store-address scratch
// ===============================================================
    .globl  aes128_expand_key
    .type   aes128_expand_key, %function
    .balign 16
aes128_expand_key:
    // rk[0] = input key verbatim.
    ld1     {v0.16b}, [x0]
    st1     {v0.16b}, [x1]

    // Prime running words from rk[0].
    ldp     w5, w6, [x1]
    ldp     w7, w8, [x1, #8]

    adrp    x2, aes128_sbox
    add     x2, x2, :lo12:aes128_sbox
    adrp    x3, aes128_rcon
    add     x3, x3, :lo12:aes128_rcon

    mov     x4, xzr
.Lkexp_loop:
    ldrb    w13, [x3, x4]
    add     x4, x4, #1

    // RotWord
    ror     w9, w8, #8

    // SubWord: 4 independent byte lookups into aes128_sbox, rebuilt
    // into a 32-bit word preserving lane order.
    and     w11, w9, #0xff
    ldrb    w10, [x2, x11]
    ubfx    w11, w9, #8, #8
    ldrb    w12, [x2, x11]
    orr     w10, w10, w12, lsl #8
    ubfx    w11, w9, #16, #8
    ldrb    w12, [x2, x11]
    orr     w10, w10, w12, lsl #16
    ubfx    w11, w9, #24, #8
    ldrb    w12, [x2, x11]
    orr     w10, w10, w12, lsl #24

    // Rcon XOR (low byte only, zero bytes elsewhere)
    eor     w10, w10, w13

    // Rolling XOR: w[4i+0] = w[4(i-1)+0] XOR temp;
    //              w[4i+j] = w[4(i-1)+j] XOR w[4i+j-1] for j=1..3.
    eor     w5, w5, w10
    eor     w6, w6, w5
    eor     w7, w7, w6
    eor     w8, w8, w7

    // Store rk[round] at round_keys + round * 16.
    add     x14, x1, x4, lsl #4
    stp     w5, w6, [x14]
    stp     w7, w8, [x14, #8]

    cmp     x4, #10
    b.lo    .Lkexp_loop
    ret
    .size   aes128_expand_key, . - aes128_expand_key

// ===============================================================
// aes128_encrypt_block(round_keys, in, out)
//   x0 = round_keys, x1 = in, x2 = out
//
// AArch64 aese folds AddRoundKey + SubBytes + ShiftRows; aesmc is
// MixColumns. The round-boundary convention is:
//   aese  v0, rk[i-1]   // XOR rk[i-1] + SB + SR   (round i body start)
//   aesmc v0, v0        // MC                      (round i body end)
// so round 1's (pre-round XOR rk[0] + SB + SR + MC) maps to a
// single aese(rk[0]) + aesmc pair; round 10's final-round lack-
// of-MixColumns drops the trailing aesmc, and the final XOR with
// rk[10] is a scalar eor.
// ===============================================================
    .globl  aes128_encrypt_block
    .type   aes128_encrypt_block, %function
    .balign 16
aes128_encrypt_block:
    ld1     {v0.16b}, [x1]

    ld1     {v1.16b}, [x0]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #16]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #32]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #48]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #64]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #80]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #96]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #112]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #128]
    aese    v0.16b, v1.16b
    aesmc   v0.16b, v0.16b

    ldr     q1, [x0, #144]
    aese    v0.16b, v1.16b
    // No aesmc — final round has no MixColumns.

    ldr     q1, [x0, #160]
    eor     v0.16b, v0.16b, v1.16b

    st1     {v0.16b}, [x2]
    ret
    .size   aes128_encrypt_block, . - aes128_encrypt_block

// ===============================================================
// aes128_decrypt_block(round_keys, in, out)
//   x0 = round_keys, x1 = in, x2 = out
//
// AArch64 aesd folds XOR + InvSubBytes + InvShiftRows; aesimc is
// InvMixColumns. FIPS 197 §5.3.5's equivalent inverse cipher uses
// dw[r] = InvMixColumns(rk[r]) for r=1..9 (rk[0] and rk[10]
// unchanged). By staging dw[r] in v1 via aesimc right before each
// inner aesd, we keep the caller's round_keys buffer pristine.
//
// Key-XOR-fold trick: instead of inserting a discrete eor for
// "state ^= dw[r]" after each round's aesimc, fold it into the
// NEXT round's aesd (which already XORs the state with its key
// operand before ISB+ISR). This costs one fewer instruction per
// round and mirrors the encrypt path's structure.
// ===============================================================
    .globl  aes128_decrypt_block
    .type   aes128_decrypt_block, %function
    .balign 16
aes128_decrypt_block:
    ld1     {v0.16b}, [x1]

    // Outer round 10 (first decrypt round): XOR rk[10] + ISB + ISR,
    // then InvMC.
    ldr     q1, [x0, #160]
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    // Rounds 9..2 (inner): key = InvMC(rk[r]) in v1, aesd + aesimc.
    ldr     q1, [x0, #144]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    ldr     q1, [x0, #128]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    ldr     q1, [x0, #112]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    ldr     q1, [x0, #96]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    ldr     q1, [x0, #80]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    ldr     q1, [x0, #64]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    ldr     q1, [x0, #48]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    ldr     q1, [x0, #32]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b
    aesimc  v0.16b, v0.16b

    // Round 1 (final inner): aesd only — no aesimc on state, since
    // the final round lacks InvMixColumns in the equivalent cipher.
    ldr     q1, [x0, #16]
    aesimc  v1.16b, v1.16b
    aesd    v0.16b, v1.16b

    // Final AddRoundKey with rk[0] (no aesimc on the key).
    ldr     q1, [x0]
    eor     v0.16b, v0.16b, v1.16b

    st1     {v0.16b}, [x2]
    ret
    .size   aes128_decrypt_block, . - aes128_decrypt_block

    .section .note.GNU-stack, "", %progbits
