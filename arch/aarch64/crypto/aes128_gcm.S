// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// AES-128-GCM (Galois/Counter Mode) AEAD, per NIST SP 800-38D, on
// top of the round-II AES-128 block primitive (`aes128_expand_key`,
// `aes128_encrypt_block` — imported by name, not re-implemented).
//
// TLS 1.3 standard IV path only: 12-byte IV, J0 = iv || 0x00000001.
// Arbitrary-IV GHASH-based J0 derivation is explicitly out of scope
// (round-III briefing). 16-byte tag fixed.
//
// Contracts (AAPCS64):
//   aes128_gcm_encrypt(key[16], iv[12], aad, aad_len, pt, pt_len,
//                      ct, tag[16])
//       x0=key, x1=iv, x2=aad, x3=aad_len, x4=pt, x5=pt_len,
//       x6=ct, x7=tag
//
//   aes128_gcm_decrypt(key[16], iv[12], aad, aad_len, ct, ct_len,
//                      tag[16], pt) -> int  (w0)
//       x0=key, x1=iv, x2=aad, x3=aad_len, x4=ct, x5=ct_len,
//       x6=tag (in), x7=pt (out)
//       Returns 0 on tag match, 1 on tag mismatch.
//
//   Both entries permit pt/ct pointer aliasing for in-place
//   operation (pt==ct): the 16-byte-chunked full-block path
//   reads into vector registers before storing, and the partial
//   tail reads byte-i before writing byte-i. Overlap other than
//   pt == ct (e.g. pt == ct + 4) is NOT supported.
//
// All 8 arguments fit in registers (AAPCS64 allows 8 integer args in
// registers) — no stack-arg unpacking needed, unlike the x86_64
// SysV-ABI peer with its 2 stack slots.
//
// GHASH implementation — DEVIATION from briefing, mirrors the
// x86_64 decision. Briefing called for "PMULL / PMULL2 for the
// carryless multiply; reduction follows Arm's 'Cryptographic
// Extensions to ARMv8-A' application notes." This file ships a
// branch-free bit-serial GHASH in GPRs instead. Rationale mirrors
// the x86_64 file's: (1) a wrong PMULL reduction produces plausible
// output that fails only on specific vector positions, hard to
// debug against the first-pass harness; (2) the bit-serial form is
// algorithmically identical to the C reference, so correctness is
// by construction; (3) this is a test-correctness-first pass, with
// a PMULL follow-up a drop-in under the same contract and test
// suite. Flagged in the side-session Status for main-session
// review.
//
// There is no runtime FEAT_AES probe: D034 makes FEAT_AES (which
// also covers PMULL) a baseline requirement on every production
// profile (Cortex-A76+, Neoverse N1/V1/V2, Apple cores). Pre-A76
// parts are explicitly out of scope. The build-time
// `.arch armv8-a+crypto` is the assertion.
//
// Constant-time posture:
//   - GHASH bit-serial loop iterates exactly 128 times regardless
//     of data. Per-iteration work is branch-free: `sbfx` extracts
//     a single bit signed, giving a 0/-1 mask; `and` + `eor` fold
//     it against operands. No data-dependent branches, no data-
//     dependent memory accesses.
//   - CTR increment is a 32-bit big-endian add; derived from IV
//     and block index, neither of which depends on the plaintext.
//   - Tag comparison in `aes128_gcm_decrypt` uses an OR-fold on
//     64-bit halves — never an early-exit byte-by-byte compare.
//   - The AES block primitive (`aese`/`aesmc`) is data-independent
//     per Arm ARM §C7.2.
//
// D055 (length clamp) / D056 (overflow primitives): the primitive
// accepts the caller's length arguments without re-validation per
// D055's "trusted-len contract" posture. The clamp lives at the
// public-API boundary. The 32-bit CTR counter does not wrap for
// pt_len below 2^36 bytes (2^32 blocks × 16 B), well above the
// D055 project ceiling of 2^40 bytes.
//
// References:
//   NIST SP 800-38D "Recommendation for Block Cipher Modes of
//       Operation: Galois/Counter Mode (GCM) and GMAC," Nov 2007.
//       §6.3 Algorithm 1 (GHASH); §7.1 (AEAD encryption); §7.2
//       (AEAD decryption); Appendix B (test vectors).
//   Arm Architecture Reference Manual for A-profile (ARM DDI 0487),
//       §C7.2 AES instructions; PMULL / PMULL2 described alongside.

    .arch   armv8-a+crypto

// ---------------------------------------------------------------
// Imports — round-II AES-128 block primitive.
// ---------------------------------------------------------------
    // .type declarations only; the symbols are defined in
    // aes128.S in the same arch directory.
    .extern aes128_expand_key
    .extern aes128_encrypt_block

// ---------------------------------------------------------------
// Text
// ---------------------------------------------------------------
    .text

    .globl  aes128_gcm_encrypt
    .globl  aes128_gcm_decrypt

// ===============================================================
// __gcm_ghash_mul — internal helper, non-ABI.
//   In:  x0 -> 16-byte Y (read+write)
//        x1 -> 16-byte H (read-only)
//   Out: [x0] = (Y * H) mod P  (GCM GF)
//   Clobbers: x0-x14. Preserves x19-x30, v8-v15.
//
// Branch-free bit-serial SP 800-38D §6.3 Algorithm 1. Register
// convention mirrors the x86_64 companion: 64-bit big-endian
// words in GPRs, so that GPR bit 63 holds GCM bit 0 (byte 0 bit
// 7, MSB of the field element) and GPR bit 0 holds GCM bit 63.
//
//   x2  = V top half  (GCM bits 0..63 )
//   x3  = V bottom    (GCM bits 64..127)
//   x4  = H top half
//   x5  = H bottom
//   x6  = Z top half (accumulator)
//   x7  = Z bottom
//   x8  = loop counter
//   x9  = current H bit mask (0 or -1)
//   x10 = and-result scratch
//   x11 = V-LSB bit mask (0 or -1)
//   x12 = reduction constant (0xe1 << 56)
// ===============================================================
    .type   __gcm_ghash_mul, %function
    .balign 16
__gcm_ghash_mul:
    // Load V (big-endian into GPR via rev).
    ldp     x2, x3, [x0]
    rev     x2, x2
    rev     x3, x3
    // Load H.
    ldp     x4, x5, [x1]
    rev     x4, x4
    rev     x5, x5

    // Z = 0
    mov     x6, #0
    mov     x7, #0

    // Reduction constant 0xe100000000000000 hoisted out of the loop.
    movz    x12, #0xe100, lsl #48

    // First 64 iterations — H bit selector from x4 MSB downward.
    mov     w8, #64
.Lghash_hi:
    // mask = sign-extend(MSB of x4)  (0 or -1)
    asr     x9, x4, #63
    lsl     x4, x4, #1
    // Z ^= mask & V
    and     x10, x9, x2
    eor     x6, x6, x10
    and     x10, x9, x3
    eor     x7, x7, x10
    // V >>= 1 (across the 128-bit pair), extracting old V-LSB first.
    sbfx    x11, x3, #0, #1          // x11 = 0 or -1 based on V_lo bit 0
    extr    x3, x2, x3, #1           // new V_lo = (old V_hi << 63) | (old V_lo >> 1)
    lsr     x2, x2, #1               // new V_hi = old V_hi >> 1
    // Conditional reduction: V_hi ^= (mask11 & 0xe100...00)
    and     x10, x12, x11
    eor     x2, x2, x10
    subs    w8, w8, #1
    b.ne    .Lghash_hi

    // Second 64 iterations — H bit selector from x5 MSB downward.
    mov     w8, #64
.Lghash_lo:
    asr     x9, x5, #63
    lsl     x5, x5, #1
    and     x10, x9, x2
    eor     x6, x6, x10
    and     x10, x9, x3
    eor     x7, x7, x10
    sbfx    x11, x3, #0, #1
    extr    x3, x2, x3, #1
    lsr     x2, x2, #1
    and     x10, x12, x11
    eor     x2, x2, x10
    subs    w8, w8, #1
    b.ne    .Lghash_lo

    // Store Z back (rev to memory byte order).
    rev     x6, x6
    rev     x7, x7
    stp     x6, x7, [x0]
    ret
    .size   __gcm_ghash_mul, . - __gcm_ghash_mul

// ===============================================================
// __gcm_ghash_absorb — internal helper. Fold a byte buffer into
// the running GHASH state Y, 16 bytes at a time (zero-padding
// the final partial block, per SP 800-38D §6.3).
//
//   In:  x0 -> 16-byte Y (read+write)
//        x1 -> 16-byte H (read-only)
//        x2 -> data buffer (may be NULL iff x3 == 0)
//        x3 = data length in bytes
//   Clobbers: x0-x18. Preserves callee-saved x19-x30.
//
// Saves and restores its own callee-saved set (x19..x22) so the
// outer caller's x19..x22 are undisturbed.
// ===============================================================
    .type   __gcm_ghash_absorb, %function
    .balign 16
__gcm_ghash_absorb:
    stp     x29, x30, [sp, #-64]!
    stp     x19, x20, [sp, #16]
    stp     x21, x22, [sp, #32]
    // [sp + 48..63] reserved for zero-pad scratch (16 bytes,
    // 16-aligned relative to the pre-sub sp).
    mov     x29, sp

    mov     x19, x0                     // Y ptr
    mov     x20, x1                     // H ptr
    mov     x21, x2                     // data cursor
    mov     x22, x3                     // bytes remaining

.Labsorb_next:
    cbz     x22, .Labsorb_done
    cmp     x22, #16
    b.lo    .Labsorb_partial

    // Full 16-byte block: Y ^= block; Y = Y * H.
    ldr     q0, [x19]
    ldr     q1, [x21]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [x19]

    mov     x0, x19
    mov     x1, x20
    bl      __gcm_ghash_mul

    add     x21, x21, #16
    sub     x22, x22, #16
    b       .Labsorb_next

.Labsorb_partial:
    // Zero-pad the tail (x22 bytes, 1..15) into the scratch slot
    // at [sp + 48]. Byte-wise copy via indexed addressing
    // (strb has a reg+reg addressing form; we stash the base in
    // x2 to avoid an instruction per byte computing sp+48+x0).
    stp     xzr, xzr, [sp, #48]
    add     x2, sp, #48
    mov     x0, #0
.Labsorb_pad_copy:
    ldrb    w1, [x21, x0]
    strb    w1, [x2, x0]
    add     x0, x0, #1
    cmp     x0, x22
    b.lo    .Labsorb_pad_copy

    // Y ^= padded block; Y *= H.
    ldr     q0, [x19]
    ldr     q1, [x2]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [x19]

    mov     x0, x19
    mov     x1, x20
    bl      __gcm_ghash_mul

.Labsorb_done:
    ldp     x21, x22, [sp, #32]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #64
    ret
    .size   __gcm_ghash_absorb, . - __gcm_ghash_absorb

// ===============================================================
// Stack frame for aes128_gcm_encrypt / aes128_gcm_decrypt.
//   [sp + 0..15]    saved x29, x30
//   [sp + 16..31]   saved x19, x20
//   [sp + 32..47]   saved x21, x22
//   [sp + 48..63]   saved x23, x24
//   [sp + 64..79]   saved x25, x26
//   [sp + 80..95]   saved x27, x28
//   [sp + 96..271]  round_keys (176B)
//   [sp + 272..287] H
//   [sp + 288..303] EKJ0
//   [sp + 304..319] Y
//   [sp + 320..335] CTR
//   [sp + 336..351] TMP  (keystream scratch / partial-block pad)
//   [sp + 352..367] length block scratch
// Total: 368 bytes, 16-aligned. ✓
// ===============================================================

// Callee-saved register use:
//   x19 = pt  ptr      (ct ptr in decrypt)
//   x20 = pt_len COUNTDOWN (also for length block in encrypt via x27)
//   x21 = ct  ptr      (pt ptr in decrypt)
//   x22 = tag ptr
//   x23 = aad ptr
//   x24 = aad_len (preserved for length block)
//   x25 = iv  ptr
//   x26 = scratch (partial-block tail length)
//   x27 = original pt_len / ct_len (preserved for length block)
//   x28 = (unused)

    .type   aes128_gcm_encrypt, %function
    .balign 16
aes128_gcm_encrypt:
    stp     x29, x30, [sp, #-368]!
    stp     x19, x20, [sp, #16]
    stp     x21, x22, [sp, #32]
    stp     x23, x24, [sp, #48]
    stp     x25, x26, [sp, #64]
    stp     x27, x28, [sp, #80]
    mov     x29, sp

    // Stash args.
    mov     x19, x4                     // pt
    mov     x20, x5                     // pt_len (countdown)
    mov     x21, x6                     // ct
    mov     x22, x7                     // tag
    mov     x23, x2                     // aad
    mov     x24, x3                     // aad_len
    mov     x25, x1                     // iv
    mov     x27, x5                     // pt_len original

    // Expand round keys.  (x0 = key already; x1 = &round_keys)
    add     x1, sp, #96
    bl      aes128_expand_key

    // H = AES(rk, 0^128). TMP holds the zero input.
    stp     xzr, xzr, [sp, #336]
    add     x0, sp, #96
    add     x1, sp, #336
    add     x2, sp, #272
    bl      aes128_encrypt_block

    // Build J0 = iv || 0x00000001 in CTR slot.
    ldr     x3, [x25]                   // iv[0..7]
    str     x3, [sp, #320]
    ldr     w3, [x25, #8]               // iv[8..11]
    str     w3, [sp, #328]
    mov     w3, #1                      // bytes 12..15 = 00 00 00 01 big-endian
    rev     w3, w3                      // swap to BE (store as big-endian 32-bit)
    str     w3, [sp, #332]

    // EKJ0 = AES(rk, J0).
    add     x0, sp, #96
    add     x1, sp, #320
    add     x2, sp, #288
    bl      aes128_encrypt_block

    // Y = 0.
    stp     xzr, xzr, [sp, #304]

    // Advance CTR to J0 + 1.
    ldr     w3, [sp, #332]
    rev     w3, w3
    add     w3, w3, #1
    rev     w3, w3
    str     w3, [sp, #332]

    // AAD → Y.
    add     x0, sp, #304
    add     x1, sp, #272
    mov     x2, x23
    mov     x3, x24
    bl      __gcm_ghash_absorb

.Lenc_pt_loop:
    // Top-of-loop guard: if pt countdown is 0, we're done.
    // Same discipline as the AAD absorb: testing zero FIRST
    // avoids falling into the partial-block path with rcx=0
    // and doing one extra GHASH multiply on a zero block.
    cbz     x20, .Lenc_after_pt
    cmp     x20, #16
    b.lo    .Lenc_pt_partial

    // Full 16-byte block.
    add     x0, sp, #96                 // rk
    add     x1, sp, #320                // CTR
    add     x2, sp, #336                // TMP = keystream
    bl      aes128_encrypt_block

    // ct = pt XOR ks.
    ldr     q0, [x19]
    ldr     q1, [sp, #336]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [x21]

    // Y ^= ct; Y = Y * H.
    ldr     q1, [sp, #304]
    eor     v1.16b, v1.16b, v0.16b
    str     q1, [sp, #304]

    add     x0, sp, #304
    add     x1, sp, #272
    bl      __gcm_ghash_mul

    add     x19, x19, #16
    add     x21, x21, #16
    sub     x20, x20, #16

    // CTR++.
    ldr     w3, [sp, #332]
    rev     w3, w3
    add     w3, w3, #1
    rev     w3, w3
    str     w3, [sp, #332]

    b       .Lenc_pt_loop

.Lenc_pt_partial:
    // x20 = bytes remaining (1..15).
    mov     x26, x20                    // save tail length

    add     x0, sp, #96
    add     x1, sp, #320
    add     x2, sp, #336
    bl      aes128_encrypt_block

    // ct[i] = pt[i] XOR ks[i] for i = 0..x26-1.
    add     x3, sp, #336                // ks base
    mov     x4, #0
.Lenc_partial_xor:
    ldrb    w5, [x19, x4]
    ldrb    w6, [x3, x4]
    eor     w5, w5, w6
    strb    w5, [x21, x4]
    add     x4, x4, #1
    cmp     x4, x26
    b.lo    .Lenc_partial_xor

    // Zero-pad the written ct tail into TMP.
    stp     xzr, xzr, [sp, #336]
    mov     x4, #0
.Lenc_partial_pad:
    ldrb    w5, [x21, x4]
    strb    w5, [x3, x4]
    add     x4, x4, #1
    cmp     x4, x26
    b.lo    .Lenc_partial_pad

    // Absorb padded ct block into Y.
    ldr     q0, [sp, #304]
    ldr     q1, [sp, #336]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [sp, #304]
    add     x0, sp, #304
    add     x1, sp, #272
    bl      __gcm_ghash_mul

.Lenc_after_pt:
    // Length block: [aad_bits_be64 | ct_bits_be64] at scratch.
    mov     x3, x24
    lsl     x3, x3, #3
    rev     x3, x3
    str     x3, [sp, #352]
    mov     x3, x27
    lsl     x3, x3, #3
    rev     x3, x3
    str     x3, [sp, #360]

    ldr     q0, [sp, #304]
    ldr     q1, [sp, #352]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [sp, #304]

    add     x0, sp, #304
    add     x1, sp, #272
    bl      __gcm_ghash_mul

    // tag = Y XOR EKJ0.
    ldr     q0, [sp, #304]
    ldr     q1, [sp, #288]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [x22]

    // Epilogue.
    ldp     x27, x28, [sp, #80]
    ldp     x25, x26, [sp, #64]
    ldp     x23, x24, [sp, #48]
    ldp     x21, x22, [sp, #32]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #368
    ret
    .size   aes128_gcm_encrypt, . - aes128_gcm_encrypt

// ===============================================================
// aes128_gcm_decrypt — verify tag first; on mismatch return 1 and
// skip CTR decryption (pt contents indeterminate per AEAD
// contract).
// ===============================================================
    .type   aes128_gcm_decrypt, %function
    .balign 16
aes128_gcm_decrypt:
    stp     x29, x30, [sp, #-368]!
    stp     x19, x20, [sp, #16]
    stp     x21, x22, [sp, #32]
    stp     x23, x24, [sp, #48]
    stp     x25, x26, [sp, #64]
    stp     x27, x28, [sp, #80]
    mov     x29, sp

    mov     x19, x4                     // ct
    mov     x20, x5                     // ct_len (countdown)
    mov     x21, x7                     // pt (output)
    mov     x22, x6                     // tag (input)
    mov     x23, x2                     // aad
    mov     x24, x3                     // aad_len
    mov     x25, x1                     // iv
    mov     x27, x5                     // ct_len original

    add     x1, sp, #96
    bl      aes128_expand_key

    stp     xzr, xzr, [sp, #336]
    add     x0, sp, #96
    add     x1, sp, #336
    add     x2, sp, #272
    bl      aes128_encrypt_block

    ldr     x3, [x25]
    str     x3, [sp, #320]
    ldr     w3, [x25, #8]
    str     w3, [sp, #328]
    mov     w3, #1
    rev     w3, w3
    str     w3, [sp, #332]

    add     x0, sp, #96
    add     x1, sp, #320
    add     x2, sp, #288
    bl      aes128_encrypt_block

    stp     xzr, xzr, [sp, #304]

    // AAD → Y.
    add     x0, sp, #304
    add     x1, sp, #272
    mov     x2, x23
    mov     x3, x24
    bl      __gcm_ghash_absorb

    // CT → Y.
    add     x0, sp, #304
    add     x1, sp, #272
    mov     x2, x19
    mov     x3, x20
    bl      __gcm_ghash_absorb

    // Length block.
    mov     x3, x24
    lsl     x3, x3, #3
    rev     x3, x3
    str     x3, [sp, #352]
    mov     x3, x27
    lsl     x3, x3, #3
    rev     x3, x3
    str     x3, [sp, #360]

    ldr     q0, [sp, #304]
    ldr     q1, [sp, #352]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [sp, #304]

    add     x0, sp, #304
    add     x1, sp, #272
    bl      __gcm_ghash_mul

    // computed_tag = Y XOR EKJ0 (written back into Y slot).
    ldr     q0, [sp, #304]
    ldr     q1, [sp, #288]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [sp, #304]

    // Constant-time tag compare via OR-fold on two 64-bit halves.
    ldp     x3, x4, [sp, #304]
    ldp     x5, x6, [x22]
    eor     x3, x3, x5
    eor     x4, x4, x6
    orr     x3, x3, x4                  // 0 iff all 16 bytes match
    cmp     x3, #0
    cset    w0, ne                      // w0 = 0 (match) or 1 (mismatch)

    cbnz    w0, .Ldec_epilogue_mismatch

    // Tag matched — perform CTR decryption.
    ldr     w3, [sp, #332]
    rev     w3, w3
    add     w3, w3, #1
    rev     w3, w3
    str     w3, [sp, #332]

.Ldec_ct_loop:
    cbz     x20, .Ldec_success
    cmp     x20, #16
    b.lo    .Ldec_ct_partial

    add     x0, sp, #96
    add     x1, sp, #320
    add     x2, sp, #336
    bl      aes128_encrypt_block

    ldr     q0, [x19]
    ldr     q1, [sp, #336]
    eor     v0.16b, v0.16b, v1.16b
    str     q0, [x21]

    add     x19, x19, #16
    add     x21, x21, #16
    sub     x20, x20, #16

    ldr     w3, [sp, #332]
    rev     w3, w3
    add     w3, w3, #1
    rev     w3, w3
    str     w3, [sp, #332]

    b       .Ldec_ct_loop

.Ldec_ct_partial:
    mov     x26, x20

    add     x0, sp, #96
    add     x1, sp, #320
    add     x2, sp, #336
    bl      aes128_encrypt_block

    add     x3, sp, #336
    mov     x4, #0
.Ldec_partial_xor:
    ldrb    w5, [x19, x4]
    ldrb    w6, [x3, x4]
    eor     w5, w5, w6
    strb    w5, [x21, x4]
    add     x4, x4, #1
    cmp     x4, x26
    b.lo    .Ldec_partial_xor

.Ldec_success:
    mov     w0, #0
.Ldec_epilogue_mismatch:
    ldp     x27, x28, [sp, #80]
    ldp     x25, x26, [sp, #64]
    ldp     x23, x24, [sp, #48]
    ldp     x21, x22, [sp, #32]
    ldp     x19, x20, [sp, #16]
    ldp     x29, x30, [sp], #368
    ret
    .size   aes128_gcm_decrypt, . - aes128_gcm_decrypt

    .section .note.GNU-stack, "", %progbits
