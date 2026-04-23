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
// data-dependent indexed loads anywhere: the key expansion uses
// `aese` itself (with a zero round key) as its SubBytes primitive,
// trading the earlier ldrb-into-rodata-S-box pattern for the same
// hardware instruction the message path uses. See the comment at
// `.Lkexp_loop` for the cache-timing fix history.
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
//   x3   Rcon base
//   x4   round counter 0..10 (pre-incremented: 1..10 in body)
//   w5-w8 running words w[4(i-1)..4i-1] for current round
//   w9   temp: RotWord(w[4i-1])
//   w10  accumulator for SubWord(temp)
//   w11  byte-extract scratch (ubfx destination / umov destination)
//   w13  Rcon byte for this iteration
//   x14  store-address scratch
//   v0   SubWord scratch vector (bytes placed at positions 0/4/8/12)
//   v1   zero round key for the aese-as-SubBytes trick (loop-invariant)
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

    adrp    x3, aes128_rcon
    add     x3, x3, :lo12:aes128_rcon

    // Zero round key for the aese-as-SubBytes trick, hoisted
    // out of the loop (loop-invariant).
    movi    v1.16b, #0

    mov     x4, xzr
.Lkexp_loop:
    ldrb    w13, [x3, x4]
    add     x4, x4, #1

    // RotWord
    ror     w9, w8, #8

    // Constant-time SubWord via `aese` with zero round key.
    //
    // Cache-timing hardening (2026-04-23 main-session fix of the
    // MEDIUM flagged in both clean-Claude and Gemini reviews of
    // the round-II AES-128 dispatch): the earlier draft indexed
    // a 256-byte rodata S-box with key-derived bytes. A
    // co-resident attacker observing cacheline-granularity
    // access patterns (FLUSH+RELOAD / PRIME+PROBE) could recover
    // key-derived byte distributions at each expand_key call.
    //
    // Trick — place the 4 bytes at state positions 0, 4, 8, 12
    // (row 0, columns 0..3). `aese v0, v1` computes
    // `ShiftRows(SubBytes(v0 XOR v1))`; with v1 = 0 it reduces
    // to `ShiftRows(SubBytes(v0))`. ShiftRows leaves row 0
    // unchanged, so after `aese` those four positions hold
    // SubBytes of the originals. No data-dependent memory
    // access; timing is instruction-fixed.
    //
    // Pattern matches OpenSSL / BoringSSL aarch64 AES key
    // expansion. v1 is a loop-invariant zero set above.
    movi    v0.16b, #0
    mov     v0.b[0],  w9
    ubfx    w11, w9, #8, #8
    mov     v0.b[4],  w11
    ubfx    w11, w9, #16, #8
    mov     v0.b[8],  w11
    ubfx    w11, w9, #24, #8
    mov     v0.b[12], w11

    aese    v0.16b, v1.16b

    // Pack SubWord = SB(B0) | SB(B1)<<8 | SB(B2)<<16 | SB(B3)<<24.
    umov    w10, v0.b[0]
    umov    w11, v0.b[4]
    orr     w10, w10, w11, lsl #8
    umov    w11, v0.b[8]
    orr     w10, w10, w11, lsl #16
    umov    w11, v0.b[12]
    orr     w10, w10, w11, lsl #24

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
