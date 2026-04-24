/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (c) 2026 Ed Hodapp
 *
 * Host-side test driver for the per-arch AES-128-GCM assembly
 * routines. Mirrors the structure of crc32_test.c, sha256_test.c
 * and aes128_test.c: a portable-C reference implementation
 * validates every vector's expected output before any assembly is
 * exercised, then the asm entries are driven against both the
 * published vectors and a deterministic random sweep, and finally
 * the tag-comparison constant-time discipline is checked by
 * flipping one bit of the tag and asserting decrypt refuses.
 *
 * References:
 *   FIPS PUB 197, "Advanced Encryption Standard (AES),"
 *       Nov 2001. §5.1 Cipher, §5.2 KeyExpansion.
 *   NIST SP 800-38D, "Recommendation for Block Cipher Modes of
 *       Operation: Galois/Counter Mode (GCM) and GMAC," Nov 2007.
 *       §6 (specifications), §7 (algorithms for AEAD), Appendix B
 *       (AES-128 test cases).
 *   S. Gueron, "Intel Carry-Less Multiplication Instruction and
 *       its Usage for Computing the GCM Mode," Intel white paper.
 *
 * The reference implementation here is structurally distinct from
 * the assembly — scalar AES via the FIPS 197 S-box / MixColumns
 * path, and GHASH via a bit-serial GF(2^128) multiply (Algorithm
 * 1 of SP 800-38D §6.3). The asm uses hardware AES-NI / FEAT_AES
 * for encryption and hardware PCLMULQDQ / PMULL for GHASH. Two
 * structurally independent attempts at the same answer: if they
 * disagree on any byte, one of them is wrong, and the other tells
 * you which.
 *
 * No external OpenSSL / libcrypto / libsodium dependency.
 */
#include <stdalign.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "aes128_gcm_vectors.h"

/* ------------------------------------------------------------------
 * Assembly entry points.
 * ------------------------------------------------------------------ */
extern void aes128_gcm_encrypt(const uint8_t key[16],
                               const uint8_t iv[12],
                               const uint8_t *aad, size_t aad_len,
                               const uint8_t *pt,  size_t pt_len,
                               uint8_t *ct,
                               uint8_t tag[16]);

extern int  aes128_gcm_decrypt(const uint8_t key[16],
                               const uint8_t iv[12],
                               const uint8_t *aad, size_t aad_len,
                               const uint8_t *ct,  size_t ct_len,
                               const uint8_t tag[16],
                               uint8_t *pt);

/* Weak-linked on AArch64 (symbol not defined there — FEAT_AES +
 * PMULL are required by D034 as a baseline, so no probe is needed).
 * On x86_64 both AES-NI AND PCLMULQDQ are required per D057's
 * extended posture; the probe here is for PCLMULQDQ
 * (CPUID.(EAX=1):ECX[bit 1]). AES-NI is covered by the round-II
 * aes128_has_aesni; we include it so the skip path mirrors the
 * round-II driver's behavior. */
extern int aes128_gcm_has_pclmulqdq(void) __attribute__((weak));
extern int aes128_has_aesni(void)         __attribute__((weak));

/* ------------------------------------------------------------------
 * Reference AES-128 (encrypt + key expand only; GCM decrypt uses
 * the forward cipher through CTR mode, so no inverse path is
 * needed here).
 *
 * The tables below are copied into this TU deliberately rather
 * than shared via a common header: the per-primitive drivers are
 * self-contained, and the round-II aes128_test.c carries the same
 * data. The duplication is narrow, scoped, and keeps each driver
 * a standalone reproduction story.
 * ------------------------------------------------------------------ */
static const uint8_t REF_SBOX[256] = {
    0x63U, 0x7cU, 0x77U, 0x7bU, 0xf2U, 0x6bU, 0x6fU, 0xc5U,
    0x30U, 0x01U, 0x67U, 0x2bU, 0xfeU, 0xd7U, 0xabU, 0x76U,
    0xcaU, 0x82U, 0xc9U, 0x7dU, 0xfaU, 0x59U, 0x47U, 0xf0U,
    0xadU, 0xd4U, 0xa2U, 0xafU, 0x9cU, 0xa4U, 0x72U, 0xc0U,
    0xb7U, 0xfdU, 0x93U, 0x26U, 0x36U, 0x3fU, 0xf7U, 0xccU,
    0x34U, 0xa5U, 0xe5U, 0xf1U, 0x71U, 0xd8U, 0x31U, 0x15U,
    0x04U, 0xc7U, 0x23U, 0xc3U, 0x18U, 0x96U, 0x05U, 0x9aU,
    0x07U, 0x12U, 0x80U, 0xe2U, 0xebU, 0x27U, 0xb2U, 0x75U,
    0x09U, 0x83U, 0x2cU, 0x1aU, 0x1bU, 0x6eU, 0x5aU, 0xa0U,
    0x52U, 0x3bU, 0xd6U, 0xb3U, 0x29U, 0xe3U, 0x2fU, 0x84U,
    0x53U, 0xd1U, 0x00U, 0xedU, 0x20U, 0xfcU, 0xb1U, 0x5bU,
    0x6aU, 0xcbU, 0xbeU, 0x39U, 0x4aU, 0x4cU, 0x58U, 0xcfU,
    0xd0U, 0xefU, 0xaaU, 0xfbU, 0x43U, 0x4dU, 0x33U, 0x85U,
    0x45U, 0xf9U, 0x02U, 0x7fU, 0x50U, 0x3cU, 0x9fU, 0xa8U,
    0x51U, 0xa3U, 0x40U, 0x8fU, 0x92U, 0x9dU, 0x38U, 0xf5U,
    0xbcU, 0xb6U, 0xdaU, 0x21U, 0x10U, 0xffU, 0xf3U, 0xd2U,
    0xcdU, 0x0cU, 0x13U, 0xecU, 0x5fU, 0x97U, 0x44U, 0x17U,
    0xc4U, 0xa7U, 0x7eU, 0x3dU, 0x64U, 0x5dU, 0x19U, 0x73U,
    0x60U, 0x81U, 0x4fU, 0xdcU, 0x22U, 0x2aU, 0x90U, 0x88U,
    0x46U, 0xeeU, 0xb8U, 0x14U, 0xdeU, 0x5eU, 0x0bU, 0xdbU,
    0xe0U, 0x32U, 0x3aU, 0x0aU, 0x49U, 0x06U, 0x24U, 0x5cU,
    0xc2U, 0xd3U, 0xacU, 0x62U, 0x91U, 0x95U, 0xe4U, 0x79U,
    0xe7U, 0xc8U, 0x37U, 0x6dU, 0x8dU, 0xd5U, 0x4eU, 0xa9U,
    0x6cU, 0x56U, 0xf4U, 0xeaU, 0x65U, 0x7aU, 0xaeU, 0x08U,
    0xbaU, 0x78U, 0x25U, 0x2eU, 0x1cU, 0xa6U, 0xb4U, 0xc6U,
    0xe8U, 0xddU, 0x74U, 0x1fU, 0x4bU, 0xbdU, 0x8bU, 0x8aU,
    0x70U, 0x3eU, 0xb5U, 0x66U, 0x48U, 0x03U, 0xf6U, 0x0eU,
    0x61U, 0x35U, 0x57U, 0xb9U, 0x86U, 0xc1U, 0x1dU, 0x9eU,
    0xe1U, 0xf8U, 0x98U, 0x11U, 0x69U, 0xd9U, 0x8eU, 0x94U,
    0x9bU, 0x1eU, 0x87U, 0xe9U, 0xceU, 0x55U, 0x28U, 0xdfU,
    0x8cU, 0xa1U, 0x89U, 0x0dU, 0xbfU, 0xe6U, 0x42U, 0x68U,
    0x41U, 0x99U, 0x2dU, 0x0fU, 0xb0U, 0x54U, 0xbbU, 0x16U,
};

static const uint8_t REF_RCON[11] = {
    0x00U, 0x01U, 0x02U, 0x04U, 0x08U, 0x10U,
    0x20U, 0x40U, 0x80U, 0x1bU, 0x36U,
};

static uint8_t xtime_byte(uint8_t x) {
    uint8_t hi = (uint8_t)(x & 0x80U);
    uint8_t r  = (uint8_t)(x << 1);
    if (hi != 0U) {
        r = (uint8_t)(r ^ 0x1bU);
    }
    return r;
}

static void ref_sub_bytes(uint8_t s[16]) {
    for (size_t i = 0; i < 16U; ++i) {
        s[i] = REF_SBOX[s[i]];
    }
}

static void ref_shift_rows(uint8_t s[16]) {
    uint8_t t;
    t = s[1];  s[1]  = s[5];  s[5]  = s[9];  s[9]  = s[13]; s[13] = t;
    t = s[2];  s[2]  = s[10]; s[10] = t;
    t = s[6];  s[6]  = s[14]; s[14] = t;
    t = s[3];  s[3]  = s[15]; s[15] = s[11]; s[11] = s[7];  s[7]  = t;
}

static void ref_mix_columns(uint8_t s[16]) {
    for (size_t c = 0; c < 4U; ++c) {
        size_t o = c * 4U;
        uint8_t a = s[o];
        uint8_t b = s[o + 1U];
        uint8_t cc = s[o + 2U];
        uint8_t d = s[o + 3U];
        uint8_t t = (uint8_t)(a ^ b ^ cc ^ d);
        s[o]      = (uint8_t)(s[o]      ^ t ^ xtime_byte((uint8_t)(a ^ b)));
        s[o + 1U] = (uint8_t)(s[o + 1U] ^ t ^ xtime_byte((uint8_t)(b ^ cc)));
        s[o + 2U] = (uint8_t)(s[o + 2U] ^ t ^ xtime_byte((uint8_t)(cc ^ d)));
        s[o + 3U] = (uint8_t)(s[o + 3U] ^ t ^ xtime_byte((uint8_t)(d ^ a)));
    }
}

static void ref_add_round_key(uint8_t s[16], const uint8_t rk[16]) {
    for (size_t i = 0; i < 16U; ++i) {
        s[i] = (uint8_t)(s[i] ^ rk[i]);
    }
}

static void ref_aes_expand(const uint8_t key[16], uint8_t rk[176]) {
    memcpy(rk, key, 16U);
    uint8_t temp[4];
    for (size_t w = 4U; w < 44U; ++w) {
        temp[0] = rk[(w - 1U) * 4U + 0U];
        temp[1] = rk[(w - 1U) * 4U + 1U];
        temp[2] = rk[(w - 1U) * 4U + 2U];
        temp[3] = rk[(w - 1U) * 4U + 3U];
        if ((w % 4U) == 0U) {
            uint8_t b = temp[0];
            temp[0] = temp[1];
            temp[1] = temp[2];
            temp[2] = temp[3];
            temp[3] = b;
            for (size_t j = 0; j < 4U; ++j) {
                temp[j] = REF_SBOX[temp[j]];
            }
            temp[0] = (uint8_t)(temp[0] ^ REF_RCON[w / 4U]);
        }
        for (size_t j = 0; j < 4U; ++j) {
            rk[w * 4U + j] =
                (uint8_t)(rk[(w - 4U) * 4U + j] ^ temp[j]);
        }
    }
}

/* bugprone-easily-swappable-parameters: the three uint8_t* arguments
 * (round_keys, in, out) ARE swappable by the check's definition, but
 * this is the canonical AES signature (FIPS 197, OpenSSL AES_encrypt,
 * libsodium crypto_core_aes128) — reshaping for lint would diverge
 * from every AES reader's mental model. A mistaken argument swap
 * would be caught immediately by the reference-vs-vector and
 * reference-vs-asm checks downstream. */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static void ref_aes_encrypt(const uint8_t rk[176],
                            const uint8_t in[16],
                            uint8_t out[16]) {
    uint8_t state[16];
    memcpy(state, in, 16U);
    ref_add_round_key(state, rk);
    for (size_t r = 1U; r < 10U; ++r) {
        ref_sub_bytes(state);
        ref_shift_rows(state);
        ref_mix_columns(state);
        ref_add_round_key(state, &rk[r * 16U]);
    }
    ref_sub_bytes(state);
    ref_shift_rows(state);
    ref_add_round_key(state, &rk[160U]);
    memcpy(out, state, 16U);
}

/* ------------------------------------------------------------------
 * Reference GHASH (NIST SP 800-38D §6.3 Algorithm 1).
 *
 * Bit-serial GF(2^128) multiplication under the GCM convention:
 *   - The 128-bit field element's bit 0 is byte[0]'s MSB; bit 127
 *     is byte[15]'s LSB (SP 800-38D §6.3).
 *   - Reducing polynomial: x^128 + x^7 + x^2 + x + 1.
 *   - In byte-storage terms the constant R is {0xe1, 0, ..., 0}.
 *
 * "Right shift by 1" in the abstract field (bit i -> bit i+1) is:
 *   for j = 15 down to 1:
 *     V[j] = (V[j] >> 1) | ((V[j-1] & 1) << 7)
 *   V[0] >>= 1
 * with the LSB of V[15] being the bit that "falls off the end"
 * (i.e. was at field position 127, which is the position that needs
 * reducing when it becomes the new field-position-128 factor).
 *
 * Constant-time is NOT a concern for the reference — this is the
 * cross-check, not the production path. The asm uses hardware
 * PCLMULQDQ / PMULL with data-independent timing.
 * ------------------------------------------------------------------ */
static void ghash_mul(uint8_t z[16], const uint8_t h[16]) {
    uint8_t v[16];
    uint8_t r[16];
    memcpy(v, z, 16U);
    memset(r, 0, 16U);
    for (size_t bit = 0; bit < 128U; ++bit) {
        size_t  hbyte    = bit >> 3U;
        uint8_t hmask    = (uint8_t)(0x80U >> (bit & 7U));
        if ((h[hbyte] & hmask) != 0U) {
            for (size_t j = 0; j < 16U; ++j) {
                r[j] = (uint8_t)(r[j] ^ v[j]);
            }
        }
        uint8_t v_lsb = (uint8_t)(v[15] & 0x01U);
        for (size_t j = 15U; j > 0U; --j) {
            v[j] = (uint8_t)((v[j] >> 1) |
                             (uint8_t)((v[j - 1U] & 0x01U) << 7));
        }
        v[0] = (uint8_t)(v[0] >> 1);
        if (v_lsb != 0U) {
            v[0] = (uint8_t)(v[0] ^ 0xe1U);
        }
    }
    memcpy(z, r, 16U);
}

/* Fold a data buffer into the GHASH state. Absorbs full 16-byte
 * blocks; the final partial block (if any) is zero-padded to 16
 * bytes before folding, per SP 800-38D §6.3.
 *
 * bugprone-easily-swappable-parameters: h and data are both
 * const uint8_t pointers of different semantic roles (hash
 * subkey vs input buffer). The parameter order here matches the
 * literal shape of the SP 800-38D Algorithm 1 signature
 * (X, H, A); reshaping for lint would diverge from the spec
 * reader's mental model. Swap hazard is caught by the outer
 * validate_reference() pass against NIST Appendix B. */
static void ghash_update(uint8_t y[16],
                         // NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
                         const uint8_t h[16],
                         const uint8_t *data, size_t data_len) {
    size_t offset = 0;
    while (offset + 16U <= data_len) {
        for (size_t j = 0; j < 16U; ++j) {
            y[j] = (uint8_t)(y[j] ^ data[offset + j]);
        }
        ghash_mul(y, h);
        offset += 16U;
    }
    if (offset < data_len) {
        uint8_t block[16];
        size_t  rem = data_len - offset;
        memset(block, 0, 16U);
        memcpy(block, &data[offset], rem);
        for (size_t j = 0; j < 16U; ++j) {
            y[j] = (uint8_t)(y[j] ^ block[j]);
        }
        ghash_mul(y, h);
    }
}

/* Serialize a 64-bit unsigned into 8 big-endian bytes. */
static void be64_store(uint8_t out[8], uint64_t x) {
    out[0] = (uint8_t)((x >> 56) & 0xffU);
    out[1] = (uint8_t)((x >> 48) & 0xffU);
    out[2] = (uint8_t)((x >> 40) & 0xffU);
    out[3] = (uint8_t)((x >> 32) & 0xffU);
    out[4] = (uint8_t)((x >> 24) & 0xffU);
    out[5] = (uint8_t)((x >> 16) & 0xffU);
    out[6] = (uint8_t)((x >>  8) & 0xffU);
    out[7] = (uint8_t)( x        & 0xffU);
}

/* 32-bit big-endian increment of the low 4 bytes of a 16-byte counter
 * block, wrapping at 2^32 per SP 800-38D §7.1.
 *
 * cppcheck-suppress knownConditionTrueFalse — cppcheck's value
 * tracking misses that `(uint8_t)(0xFFU + 1U) == 0U` is reachable
 * when ctr[j] is 0xFF; the check is the carry detection for
 * multi-byte increment. Documented rather than restructured
 * because the current form matches the CTR carry idiom in every
 * reference GCM implementation. */
static void ctr_incr_be32(uint8_t ctr[16]) {
    for (size_t i = 0; i < 4U; ++i) {
        size_t  j = 15U - i;
        uint8_t v = (uint8_t)(ctr[j] + 1U);
        ctr[j] = v;
        // cppcheck-suppress knownConditionTrueFalse
        if (v != 0U) {
            return;
        }
    }
}

/* ------------------------------------------------------------------
 * Reference AES-128-GCM encrypt / decrypt.
 *
 * The reference computes the full GCM composition — AES, CTR, GHASH,
 * tag — in plain scalar code. Cross-checked against NIST SP 800-38D
 * Appendix B before the asm is ever called; cross-checks the asm
 * byte-for-byte on every vector and every sweep case.
 * ------------------------------------------------------------------ */
/* bugprone-easily-swappable-parameters: same rationale as
 * ref_gcm_encrypt — SP 800-38D-shaped signature (K, IV, A, C);
 * reshaping for lint would fight the spec's own parameter order
 * and the call-site expectation from every GCM API. Swap hazard
 * is caught by the NIST-vector pass in validate_reference(). */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static void ref_gcm_compute_tag(const uint8_t rk[176],
                                const uint8_t iv[12],
                                const uint8_t *aad, size_t aad_len,
                                const uint8_t *ct,  size_t ct_len,
                                uint8_t tag[16]) {
    uint8_t h[16];
    uint8_t zero[16];
    uint8_t j0[16];
    uint8_t ekj0[16];
    uint8_t y[16];
    uint8_t lenblock[16];

    memset(zero, 0, 16U);
    ref_aes_encrypt(rk, zero, h);

    memcpy(j0, iv, 12U);
    j0[12] = 0x00U;
    j0[13] = 0x00U;
    j0[14] = 0x00U;
    j0[15] = 0x01U;
    ref_aes_encrypt(rk, j0, ekj0);

    memset(y, 0, 16U);
    ghash_update(y, h, aad, aad_len);
    ghash_update(y, h, ct,  ct_len);

    be64_store(&lenblock[0], (uint64_t)aad_len * 8U);
    be64_store(&lenblock[8], (uint64_t)ct_len  * 8U);
    for (size_t j = 0; j < 16U; ++j) {
        y[j] = (uint8_t)(y[j] ^ lenblock[j]);
    }
    ghash_mul(y, h);

    for (size_t j = 0; j < 16U; ++j) {
        tag[j] = (uint8_t)(y[j] ^ ekj0[j]);
    }
}

/* bugprone-easily-swappable-parameters: AEAD contract carries
 * multiple same-typed pointer/length pairs by design; reshaping
 * would diverge from every GCM API on the planet. The vector and
 * sweep checks catch argument-swap mistakes end-to-end. */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static void ref_gcm_encrypt(const uint8_t key[16],
                            const uint8_t iv[12],
                            const uint8_t *aad, size_t aad_len,
                            const uint8_t *pt,  size_t pt_len,
                            uint8_t *ct,
                            uint8_t tag[16]) {
    uint8_t rk[176];
    uint8_t ctr[16];
    uint8_t ks[16];
    ref_aes_expand(key, rk);

    memcpy(ctr, iv, 12U);
    ctr[12] = 0x00U;
    ctr[13] = 0x00U;
    ctr[14] = 0x00U;
    ctr[15] = 0x02U;                      /* J0 + 1 for first keystream */

    for (size_t off = 0; off < pt_len; off += 16U) {
        ref_aes_encrypt(rk, ctr, ks);
        size_t take = (pt_len - off >= 16U) ? 16U : (pt_len - off);
        for (size_t j = 0; j < take; ++j) {
            ct[off + j] = (uint8_t)(pt[off + j] ^ ks[j]);
        }
        ctr_incr_be32(ctr);
    }

    ref_gcm_compute_tag(rk, iv, aad, aad_len, ct, pt_len, tag);
}

/* Returns 0 on tag match (pt written), non-zero on mismatch
 * (pt contents indeterminate per the AEAD contract). */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static int ref_gcm_decrypt(const uint8_t key[16],
                           const uint8_t iv[12],
                           const uint8_t *aad, size_t aad_len,
                           const uint8_t *ct,  size_t ct_len,
                           const uint8_t tag[16],
                           uint8_t *pt) {
    uint8_t rk[176];
    uint8_t expected[16];
    uint8_t ctr[16];
    uint8_t ks[16];
    ref_aes_expand(key, rk);

    ref_gcm_compute_tag(rk, iv, aad, aad_len, ct, ct_len, expected);

    uint8_t diff = 0;
    for (size_t j = 0; j < 16U; ++j) {
        diff = (uint8_t)(diff | (uint8_t)(expected[j] ^ tag[j]));
    }
    if (diff != 0U) {
        return 1;
    }

    memcpy(ctr, iv, 12U);
    ctr[12] = 0x00U;
    ctr[13] = 0x00U;
    ctr[14] = 0x00U;
    ctr[15] = 0x02U;
    for (size_t off = 0; off < ct_len; off += 16U) {
        ref_aes_encrypt(rk, ctr, ks);
        size_t take = (ct_len - off >= 16U) ? 16U : (ct_len - off);
        for (size_t j = 0; j < take; ++j) {
            pt[off + j] = (uint8_t)(ct[off + j] ^ ks[j]);
        }
        ctr_incr_be32(ctr);
    }
    return 0;
}

/* ------------------------------------------------------------------
 * Helpers.
 * ------------------------------------------------------------------ */
static int cmp_bytes(const uint8_t *a, const uint8_t *b, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        if (a[i] != b[i]) {
            return 0;
        }
    }
    return 1;
}

static void print_bytes(const char *label,
                        const uint8_t *b, size_t n) {
    printf("%s", label);
    for (size_t i = 0; i < n; ++i) {
        printf("%02x", b[i]);
    }
    puts("");
}

static int validate_reference(void) {
    for (size_t i = 0; i < AES128_GCM_N_VECTORS; ++i) {
        const struct aes128_gcm_vector *v = &AES128_GCM_VECTORS[i];
        uint8_t ct[64];
        uint8_t tag[16];
        uint8_t pt[64];
        int     rc;

        if (v->pt_len > sizeof ct) {
            printf("FATAL  vector %s pt_len=%zu exceeds scratch\n",
                   v->name, v->pt_len);
            return 0;
        }

        ref_gcm_encrypt(v->key, v->iv,
                        v->aad, v->aad_len,
                        v->pt, v->pt_len,
                        ct, tag);
        if (!cmp_bytes(tag, v->tag, 16U)) {
            printf("FATAL  reference tag mismatch on %s\n", v->name);
            print_bytes("  exp=", v->tag, 16U);
            print_bytes("  got=", tag,    16U);
            return 0;
        }
        if (v->pt_len > 0U && !cmp_bytes(ct, v->ct, v->pt_len)) {
            printf("FATAL  reference ct mismatch on %s\n", v->name);
            print_bytes("  exp=", v->ct, v->pt_len);
            print_bytes("  got=", ct,    v->pt_len);
            return 0;
        }

        rc = ref_gcm_decrypt(v->key, v->iv,
                             v->aad, v->aad_len,
                             v->ct, v->pt_len,
                             v->tag, pt);
        if (rc != 0) {
            printf("FATAL  reference decrypt rejected valid tag on %s\n",
                   v->name);
            return 0;
        }
        if (v->pt_len > 0U && !cmp_bytes(pt, v->pt, v->pt_len)) {
            printf("FATAL  reference decrypt pt mismatch on %s\n",
                   v->name);
            return 0;
        }
    }
    return 1;
}

static int check_named_vectors(void) {
    int failures = 0;
    for (size_t i = 0; i < AES128_GCM_N_VECTORS; ++i) {
        const struct aes128_gcm_vector *v = &AES128_GCM_VECTORS[i];
        uint8_t asm_ct[64];
        uint8_t asm_tag[16];
        uint8_t asm_pt[64];
        int     rc;

        aes128_gcm_encrypt(v->key, v->iv,
                           v->aad, v->aad_len,
                           v->pt, v->pt_len,
                           asm_ct, asm_tag);
        if (!cmp_bytes(asm_tag, v->tag, 16U)) {
            printf("FAIL  encrypt tag %s\n", v->name);
            print_bytes("  exp=", v->tag,  16U);
            print_bytes("  got=", asm_tag, 16U);
            ++failures;
        }
        if (v->pt_len > 0U && !cmp_bytes(asm_ct, v->ct, v->pt_len)) {
            printf("FAIL  encrypt ct %s\n", v->name);
            print_bytes("  exp=", v->ct,   v->pt_len);
            print_bytes("  got=", asm_ct,  v->pt_len);
            ++failures;
        }

        rc = aes128_gcm_decrypt(v->key, v->iv,
                                v->aad, v->aad_len,
                                v->ct, v->pt_len,
                                v->tag, asm_pt);
        if (rc != 0) {
            printf("FAIL  decrypt rejected valid tag %s rc=%d\n",
                   v->name, rc);
            ++failures;
        } else if (v->pt_len > 0U &&
                   !cmp_bytes(asm_pt, v->pt, v->pt_len)) {
            printf("FAIL  decrypt pt %s\n", v->name);
            print_bytes("  exp=", v->pt,  v->pt_len);
            print_bytes("  got=", asm_pt, v->pt_len);
            ++failures;
        }
    }
    return failures;
}

/* Deterministic pseudo-random sweep. Same LCG family as
 * sha256_test.c / aes128_test.c sweep; parameterised with a
 * per-case shift so 32 cases explore a reproducible chunk of
 * the input space without any reliance on rand()/stdlib seeds.
 *
 * Cases cover: zero-length AAD + zero-length PT (the degenerate
 * tag-only case), zero-length AAD + non-aligned PT, non-aligned
 * AAD + zero-length PT, and both non-zero at varying partial-
 * block offsets to exercise the zero-padding paths. */
struct sweep_case {
    const char *label;
    size_t      aad_len;
    size_t      pt_len;
};

static const struct sweep_case SWEEP_CASES[] = {
    {"zero",           0U,  0U},
    {"pt=1",           0U,  1U},
    {"pt=15",          0U, 15U},
    {"pt=16",          0U, 16U},
    {"pt=17",          0U, 17U},
    {"pt=31",          0U, 31U},
    {"pt=32",          0U, 32U},
    {"pt=33",          0U, 33U},
    {"aad=1",          1U,  0U},
    {"aad=15",        15U,  0U},
    {"aad=16",        16U,  0U},
    {"aad=17",        17U,  0U},
    {"aad=32",        32U,  0U},
    {"aad=1/pt=1",     1U,  1U},
    {"aad=15/pt=15",  15U, 15U},
    {"aad=16/pt=16",  16U, 16U},
    {"aad=17/pt=17",  17U, 17U},
    {"aad=31/pt=31",  31U, 31U},
    {"aad=32/pt=32",  32U, 32U},
    {"aad=33/pt=33",  33U, 33U},
    {"aad=3/pt=48",    3U, 48U},
    {"aad=7/pt=64",    7U, 64U},
    {"aad=20/pt=60",  20U, 60U},
    {"aad=48/pt=80",  48U, 80U},
    {"aad=64/pt=64",  64U, 64U},
    {"aad=65/pt=63",  65U, 63U},
    {"aad=96/pt=16",  96U, 16U},
    {"aad=96/pt=96",  96U, 96U},
    {"aad=127/pt=1", 127U,  1U},
    {"aad=1/pt=127",   1U,127U},
    {"aad=128/pt=0", 128U,  0U},
    {"aad=0/pt=128",   0U,128U},
};
#define SWEEP_N (sizeof SWEEP_CASES / sizeof SWEEP_CASES[0])

/* Deterministic fill: distinct mixer per buffer so key / iv / aad
 * / pt don't collide.
 *
 * bugprone-easily-swappable-parameters: n (size_t) and mix
 * (uint32_t) are implicitly convertible, but their semantic roles
 * (length vs PRNG seed) are unrelated. Swapping them would either
 * write a length-many bytes filled by a small-n seed (likely
 * truncating everything to near-zero) or nothing at all — an
 * obvious failure mode that would surface instantly in the first
 * sweep iteration. Accept the NOLINT here rather than force a
 * wrapper struct for a 3-line internal helper. */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static void sweep_fill(uint8_t *buf, size_t n, uint32_t mix) {
    for (size_t i = 0; i < n; ++i) {
        uint32_t v = ((uint32_t)i * 37U + 13U) ^ mix;
        buf[i] = (uint8_t)(v & 0xffU);
    }
}

static int check_sweep(void) {
    int failures = 0;
    uint8_t key[16];
    uint8_t iv[12];
    uint8_t aad[128];
    uint8_t pt[128];
    uint8_t ref_ct[128];
    uint8_t ref_tag[16];
    uint8_t asm_ct[128];
    uint8_t asm_tag[16];
    uint8_t asm_pt[128];

    for (size_t ci = 0; ci < SWEEP_N; ++ci) {
        const struct sweep_case *c = &SWEEP_CASES[ci];
        uint32_t seed = (uint32_t)(ci * 0x9e3779b9U);
        sweep_fill(key, 16U, seed ^ 0x11111111U);
        sweep_fill(iv,  12U, seed ^ 0x22222222U);
        sweep_fill(aad, c->aad_len, seed ^ 0x33333333U);
        sweep_fill(pt,  c->pt_len,  seed ^ 0x44444444U);

        ref_gcm_encrypt(key, iv,
                        aad, c->aad_len,
                        pt, c->pt_len,
                        ref_ct, ref_tag);
        aes128_gcm_encrypt(key, iv,
                           aad, c->aad_len,
                           pt, c->pt_len,
                           asm_ct, asm_tag);

        if (!cmp_bytes(asm_tag, ref_tag, 16U)) {
            printf("FAIL  sweep %s: tag mismatch\n", c->label);
            print_bytes("  ref=", ref_tag, 16U);
            print_bytes("  asm=", asm_tag, 16U);
            ++failures;
        }
        if (c->pt_len > 0U &&
            !cmp_bytes(asm_ct, ref_ct, c->pt_len)) {
            printf("FAIL  sweep %s: ct mismatch\n", c->label);
            ++failures;
        }

        int rc = aes128_gcm_decrypt(key, iv,
                                    aad, c->aad_len,
                                    asm_ct, c->pt_len,
                                    asm_tag, asm_pt);
        if (rc != 0) {
            printf("FAIL  sweep %s: decrypt rejected valid tag rc=%d\n",
                   c->label, rc);
            ++failures;
        } else if (c->pt_len > 0U &&
                   !cmp_bytes(asm_pt, pt, c->pt_len)) {
            printf("FAIL  sweep %s: decrypt pt mismatch\n", c->label);
            ++failures;
        }
    }
    if (failures == 0) {
        printf("(sweep: %zu cases, asm vs reference byte-identical)\n",
               (size_t)SWEEP_N);
    }
    return failures;
}

/* Tag-forgery rejection. Flip each of the 128 bits of the tag on
 * every named vector; every flip MUST cause decrypt to return a
 * non-zero (tag-mismatch) result. A single-bit flip that sneaks
 * through would be the canonical AEAD authenticity failure — the
 * reason GCM specifies a constant-time OR-fold comparison in the
 * first place.
 *
 * Also verifies the defense-in-depth pt-zero-on-mismatch contract:
 * the scratch_pt buffer is pre-filled with a 0xAA sentinel before
 * each decrypt call. On mismatch, bytes [0..pt_len) must be
 * zero (under-zeroing would leak stale keystream / stale caller
 * data if the caller ignores the indeterminate-pt contract) and
 * bytes [pt_len..sizeof(scratch_pt)) must still be 0xAA
 * (over-zeroing would mean the impl wrote past the caller's
 * buffer — a would-be buffer overflow caught at test time). */
static int check_forgery(void) {
    int failures = 0;
    for (size_t i = 0; i < AES128_GCM_N_VECTORS; ++i) {
        const struct aes128_gcm_vector *v = &AES128_GCM_VECTORS[i];
        uint8_t scratch_pt[64];
        for (size_t bit = 0; bit < 128U; ++bit) {
            uint8_t forged[16];
            memcpy(forged, v->tag, 16U);
            size_t  byte_ix = bit >> 3U;
            uint8_t mask    = (uint8_t)(1U << (bit & 7U));
            forged[byte_ix] = (uint8_t)(forged[byte_ix] ^ mask);
            memset(scratch_pt, 0xAA, sizeof(scratch_pt));
            int rc = aes128_gcm_decrypt(v->key, v->iv,
                                        v->aad, v->aad_len,
                                        v->ct, v->pt_len,
                                        forged, scratch_pt);
            if (rc == 0) {
                printf("FAIL  forgery accepted on %s bit %zu\n",
                       v->name, bit);
                ++failures;
                continue;
            }
            /* Under-zeroing check: pt[0..pt_len) must be 0. */
            for (size_t j = 0; j < v->pt_len; ++j) {
                if (scratch_pt[j] != 0U) {
                    printf("FAIL  pt not zeroed on %s bit %zu "
                           "at offset %zu (got 0x%02x, "
                           "expected 0x00)\n",
                           v->name, bit, j,
                           (unsigned)scratch_pt[j]);
                    ++failures;
                    break;
                }
            }
            /* Over-zeroing check: bytes past pt_len must still be
             * the 0xAA sentinel. */
            for (size_t j = v->pt_len; j < sizeof(scratch_pt); ++j) {
                if (scratch_pt[j] != 0xAAU) {
                    printf("FAIL  pt over-zeroed on %s bit %zu "
                           "at offset %zu past pt_len %zu "
                           "(got 0x%02x, expected 0xAA)\n",
                           v->name, bit, j, (size_t)v->pt_len,
                           (unsigned)scratch_pt[j]);
                    ++failures;
                    break;
                }
            }
        }
    }
    if (failures == 0) {
        printf("(forgery: all %zu single-bit tag flips rejected "
               "across %zu vectors; pt zeroed on mismatch without "
               "overrun)\n",
               (size_t)128U,
               (size_t)AES128_GCM_N_VECTORS);
    }
    return failures;
}

int main(void) {
    if (!validate_reference()) {
        return 2;
    }

    /* x86_64: both AES-NI AND PCLMULQDQ are required. On a host
     * without either, skip cleanly rather than SIGILL on first
     * instruction (same pattern as aes128_test.c). AArch64 has
     * neither weak symbol defined and runs unconditionally. */
    if (aes128_has_aesni != NULL && aes128_has_aesni() == 0) {
        puts("SKIP  AES-NI not present on this host CPU");
        puts("PASS  (reference self-check ok; asm path skipped)");
        return 0;
    }
    if (aes128_gcm_has_pclmulqdq != NULL &&
        aes128_gcm_has_pclmulqdq() == 0) {
        puts("SKIP  PCLMULQDQ not present on this host CPU");
        puts("PASS  (reference self-check ok; asm path skipped)");
        return 0;
    }

    int total = 0;
    total += check_named_vectors();
    total += check_sweep();
    total += check_forgery();

    if (aes128_gcm_has_pclmulqdq != NULL) {
        printf("CPU has PCLMULQDQ: %d\n", aes128_gcm_has_pclmulqdq());
    }

    if (total == 0) {
        puts("PASS  all AES-128-GCM checks passed");
        return 0;
    }
    printf("FAIL  %d mismatch(es)\n", total);
    return 1;
}
