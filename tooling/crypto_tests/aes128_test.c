/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (c) 2026 Ed Hodapp
 *
 * Host-side test driver for the per-arch AES-128 assembly routines.
 * Checks:
 *   1. Self-contained portable-C reference AES-128 (straight scalar
 *      per FIPS 197: S-box + InvS-box + MixColumns via xtime +
 *      InvMixColumns via full GF(2^8) multiplication) validates each
 *      named vector's expected ciphertext before any assembly is
 *      exercised. A FATAL exit here means the vectors themselves
 *      are suspect, not the assembly.
 *   2. aes128_expand_key + aes128_encrypt_block + aes128_decrypt_block
 *      on each of five FIPS 197 / NIST KAT vectors, with byte-level
 *      equality against the expected ciphertext and round-trip to the
 *      original plaintext.
 *   3. An 8-block deterministic-input sweep where (key, plaintext)
 *      pairs come from the same LCG pattern as sha256_test.c
 *      ((i*37 + 13) & 0xFF). For each block:
 *        - asm encrypt vs reference encrypt (byte-identical)
 *        - asm decrypt of the ciphertext recovers the plaintext
 *      This catches paths where a constant-vector test might pass
 *      by coincidence but the general case diverges.
 *   4. On x86_64, aes128_has_aesni() is probed first; on a CPU without
 *      AES-NI the driver exits 0 cleanly with a skip note rather than
 *      faulting on the first aesenc. On AArch64 there is no counterpart
 *      (FEAT_AES is required by D034) so the weak symbol is NULL and
 *      every vector runs unconditionally.
 *
 * Exits 0 on full pass, non-zero on any mismatch.
 *
 * No external dependency on OpenSSL / libcrypto — the reference
 * implementation is internal, same pattern as crc32_test.c and
 * sha256_test.c.
 */
#include <stdalign.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "aes128_vectors.h"

/* ------------------------------------------------------------------
 * Assembly entry points.
 * ------------------------------------------------------------------ */
extern void aes128_expand_key(const uint8_t key[16],
                              uint8_t round_keys[176]);
extern void aes128_encrypt_block(const uint8_t round_keys[176],
                                 const uint8_t in[16],
                                 uint8_t out[16]);
extern void aes128_decrypt_block(const uint8_t round_keys[176],
                                 const uint8_t in[16],
                                 uint8_t out[16]);

/* Weak-linked on AArch64 (symbol not defined there — FEAT_AES is
 * required by D034, so no probe is needed). */
extern int aes128_has_aesni(void) __attribute__((weak));

/* ------------------------------------------------------------------
 * Reference AES-128 implementation.
 *   FIPS PUB 197:
 *     §5.1   Cipher
 *     §5.1.1 SubBytes (Figure 7 + Table 4 forward S-box)
 *     §5.1.2 ShiftRows
 *     §5.1.3 MixColumns
 *     §5.1.4 AddRoundKey
 *     §5.2   KeyExpansion (Figure 11)
 *     §5.3   Inverse Cipher (straight-inverse form; we use this, not
 *            the equivalent-inverse form, specifically so the
 *            reference is structurally distinct from the assembly's
 *            aesd-based equivalent-inverse path).
 *     §5.3.1 InvShiftRows
 *     §5.3.2 InvSubBytes (Table 6 inverse S-box)
 *     §5.3.3 InvMixColumns
 * State layout follows FIPS 197 §3.4: state[r + 4*c] for the byte
 * at row r, column c.
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

static const uint8_t REF_INVSBOX[256] = {
    0x52U, 0x09U, 0x6aU, 0xd5U, 0x30U, 0x36U, 0xa5U, 0x38U,
    0xbfU, 0x40U, 0xa3U, 0x9eU, 0x81U, 0xf3U, 0xd7U, 0xfbU,
    0x7cU, 0xe3U, 0x39U, 0x82U, 0x9bU, 0x2fU, 0xffU, 0x87U,
    0x34U, 0x8eU, 0x43U, 0x44U, 0xc4U, 0xdeU, 0xe9U, 0xcbU,
    0x54U, 0x7bU, 0x94U, 0x32U, 0xa6U, 0xc2U, 0x23U, 0x3dU,
    0xeeU, 0x4cU, 0x95U, 0x0bU, 0x42U, 0xfaU, 0xc3U, 0x4eU,
    0x08U, 0x2eU, 0xa1U, 0x66U, 0x28U, 0xd9U, 0x24U, 0xb2U,
    0x76U, 0x5bU, 0xa2U, 0x49U, 0x6dU, 0x8bU, 0xd1U, 0x25U,
    0x72U, 0xf8U, 0xf6U, 0x64U, 0x86U, 0x68U, 0x98U, 0x16U,
    0xd4U, 0xa4U, 0x5cU, 0xccU, 0x5dU, 0x65U, 0xb6U, 0x92U,
    0x6cU, 0x70U, 0x48U, 0x50U, 0xfdU, 0xedU, 0xb9U, 0xdaU,
    0x5eU, 0x15U, 0x46U, 0x57U, 0xa7U, 0x8dU, 0x9dU, 0x84U,
    0x90U, 0xd8U, 0xabU, 0x00U, 0x8cU, 0xbcU, 0xd3U, 0x0aU,
    0xf7U, 0xe4U, 0x58U, 0x05U, 0xb8U, 0xb3U, 0x45U, 0x06U,
    0xd0U, 0x2cU, 0x1eU, 0x8fU, 0xcaU, 0x3fU, 0x0fU, 0x02U,
    0xc1U, 0xafU, 0xbdU, 0x03U, 0x01U, 0x13U, 0x8aU, 0x6bU,
    0x3aU, 0x91U, 0x11U, 0x41U, 0x4fU, 0x67U, 0xdcU, 0xeaU,
    0x97U, 0xf2U, 0xcfU, 0xceU, 0xf0U, 0xb4U, 0xe6U, 0x73U,
    0x96U, 0xacU, 0x74U, 0x22U, 0xe7U, 0xadU, 0x35U, 0x85U,
    0xe2U, 0xf9U, 0x37U, 0xe8U, 0x1cU, 0x75U, 0xdfU, 0x6eU,
    0x47U, 0xf1U, 0x1aU, 0x71U, 0x1dU, 0x29U, 0xc5U, 0x89U,
    0x6fU, 0xb7U, 0x62U, 0x0eU, 0xaaU, 0x18U, 0xbeU, 0x1bU,
    0xfcU, 0x56U, 0x3eU, 0x4bU, 0xc6U, 0xd2U, 0x79U, 0x20U,
    0x9aU, 0xdbU, 0xc0U, 0xfeU, 0x78U, 0xcdU, 0x5aU, 0xf4U,
    0x1fU, 0xddU, 0xa8U, 0x33U, 0x88U, 0x07U, 0xc7U, 0x31U,
    0xb1U, 0x12U, 0x10U, 0x59U, 0x27U, 0x80U, 0xecU, 0x5fU,
    0x60U, 0x51U, 0x7fU, 0xa9U, 0x19U, 0xb5U, 0x4aU, 0x0dU,
    0x2dU, 0xe5U, 0x7aU, 0x9fU, 0x93U, 0xc9U, 0x9cU, 0xefU,
    0xa0U, 0xe0U, 0x3bU, 0x4dU, 0xaeU, 0x2aU, 0xf5U, 0xb0U,
    0xc8U, 0xebU, 0xbbU, 0x3cU, 0x83U, 0x53U, 0x99U, 0x61U,
    0x17U, 0x2bU, 0x04U, 0x7eU, 0xbaU, 0x77U, 0xd6U, 0x26U,
    0xe1U, 0x69U, 0x14U, 0x63U, 0x55U, 0x21U, 0x0cU, 0x7dU,
};

/* Rcon[1..10] for AES-128 key schedule. Rcon[0] unused. */
static const uint8_t REF_RCON[11] = {
    0x00U, 0x01U, 0x02U, 0x04U, 0x08U, 0x10U,
    0x20U, 0x40U, 0x80U, 0x1bU, 0x36U,
};

/* GF(2^8) double with reduction mod x^8 + x^4 + x^3 + x + 1 (0x1b). */
static uint8_t xtime(uint8_t x) {
    uint8_t hi = (uint8_t)(x & 0x80U);
    uint8_t r = (uint8_t)(x << 1);
    if (hi != 0U) {
        r = (uint8_t)(r ^ 0x1bU);
    }
    return r;
}

/* Full GF(2^8) multiplication; used only for InvMixColumns since the
 * {0e, 0b, 0d, 09} coefficients don't admit a clean xtime shortcut
 * like MixColumns' {02, 03, 01} do.
 * bugprone-easily-swappable-parameters suppressed below: GF(2^8)
 * multiplication is commutative by field axiom; swapping the
 * arguments yields the same value by construction. Not a
 * callsite hazard. */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static uint8_t gmul(uint8_t a, uint8_t b) {
    uint8_t p = 0U;
    uint8_t aa = a;
    uint8_t bb = b;
    for (size_t i = 0; i < 8U; ++i) {
        if ((bb & 1U) != 0U) {
            p = (uint8_t)(p ^ aa);
        }
        aa = xtime(aa);
        bb = (uint8_t)(bb >> 1);
    }
    return p;
}

static void ref_sub_bytes(uint8_t s[16]) {
    for (size_t i = 0; i < 16U; ++i) {
        s[i] = REF_SBOX[s[i]];
    }
}

static void ref_inv_sub_bytes(uint8_t s[16]) {
    for (size_t i = 0; i < 16U; ++i) {
        s[i] = REF_INVSBOX[s[i]];
    }
}

static void ref_shift_rows(uint8_t s[16]) {
    uint8_t t;
    /* Row 1: (s1,s5,s9,s13) -> (s5,s9,s13,s1) */
    t = s[1];
    s[1]  = s[5];
    s[5]  = s[9];
    s[9]  = s[13];
    s[13] = t;
    /* Row 2: shift by 2 == pair swap */
    t = s[2];  s[2]  = s[10]; s[10] = t;
    t = s[6];  s[6]  = s[14]; s[14] = t;
    /* Row 3: (s3,s7,s11,s15) -> (s15,s3,s7,s11) */
    t = s[3];
    s[3]  = s[15];
    s[15] = s[11];
    s[11] = s[7];
    s[7]  = t;
}

static void ref_inv_shift_rows(uint8_t s[16]) {
    uint8_t t;
    /* Row 1: (s1,s5,s9,s13) -> (s13,s1,s5,s9) */
    t = s[13];
    s[13] = s[9];
    s[9]  = s[5];
    s[5]  = s[1];
    s[1]  = t;
    /* Row 2: pair swap (self-inverse) */
    t = s[2];  s[2]  = s[10]; s[10] = t;
    t = s[6];  s[6]  = s[14]; s[14] = t;
    /* Row 3: (s3,s7,s11,s15) -> (s7,s11,s15,s3) */
    t = s[3];
    s[3]  = s[7];
    s[7]  = s[11];
    s[11] = s[15];
    s[15] = t;
}

static void ref_mix_columns(uint8_t s[16]) {
    for (size_t c = 0; c < 4U; ++c) {
        size_t o = c * 4U;
        uint8_t a = s[o];
        uint8_t b = s[o + 1U];
        uint8_t cc = s[o + 2U];
        uint8_t d = s[o + 3U];
        uint8_t t = (uint8_t)(a ^ b ^ cc ^ d);
        s[o]       = (uint8_t)(s[o]       ^ t ^ xtime((uint8_t)(a ^ b)));
        s[o + 1U]  = (uint8_t)(s[o + 1U]  ^ t ^ xtime((uint8_t)(b ^ cc)));
        s[o + 2U]  = (uint8_t)(s[o + 2U]  ^ t ^ xtime((uint8_t)(cc ^ d)));
        s[o + 3U]  = (uint8_t)(s[o + 3U]  ^ t ^ xtime((uint8_t)(d ^ a)));
    }
}

static void ref_inv_mix_columns(uint8_t s[16]) {
    for (size_t c = 0; c < 4U; ++c) {
        size_t o = c * 4U;
        uint8_t a = s[o];
        uint8_t b = s[o + 1U];
        uint8_t cc = s[o + 2U];
        uint8_t d = s[o + 3U];
        s[o]      = (uint8_t)(gmul(a, 0x0eU) ^ gmul(b, 0x0bU)
                            ^ gmul(cc, 0x0dU) ^ gmul(d, 0x09U));
        s[o + 1U] = (uint8_t)(gmul(a, 0x09U) ^ gmul(b, 0x0eU)
                            ^ gmul(cc, 0x0bU) ^ gmul(d, 0x0dU));
        s[o + 2U] = (uint8_t)(gmul(a, 0x0dU) ^ gmul(b, 0x09U)
                            ^ gmul(cc, 0x0eU) ^ gmul(d, 0x0bU));
        s[o + 3U] = (uint8_t)(gmul(a, 0x0bU) ^ gmul(b, 0x0dU)
                            ^ gmul(cc, 0x09U) ^ gmul(d, 0x0eU));
    }
}

static void ref_add_round_key(uint8_t s[16], const uint8_t rk[16]) {
    for (size_t i = 0; i < 16U; ++i) {
        s[i] = (uint8_t)(s[i] ^ rk[i]);
    }
}

static void ref_expand_key(const uint8_t key[16], uint8_t rk[176]) {
    memcpy(rk, key, 16U);
    uint8_t temp[4];
    for (size_t w = 4U; w < 44U; ++w) {
        temp[0] = rk[(w - 1U) * 4U + 0U];
        temp[1] = rk[(w - 1U) * 4U + 1U];
        temp[2] = rk[(w - 1U) * 4U + 2U];
        temp[3] = rk[(w - 1U) * 4U + 3U];
        if ((w % 4U) == 0U) {
            /* RotWord: cyclic left-shift by 1 byte. */
            uint8_t b = temp[0];
            temp[0] = temp[1];
            temp[1] = temp[2];
            temp[2] = temp[3];
            temp[3] = b;
            /* SubWord: S-box per byte. */
            for (size_t j = 0; j < 4U; ++j) {
                temp[j] = REF_SBOX[temp[j]];
            }
            /* Rcon XOR on high byte only (FIPS {RC[i],00,00,00}). */
            temp[0] = (uint8_t)(temp[0] ^ REF_RCON[w / 4U]);
        }
        for (size_t j = 0; j < 4U; ++j) {
            rk[w * 4U + j] =
                (uint8_t)(rk[(w - 4U) * 4U + j] ^ temp[j]);
        }
    }
}

/* bugprone-easily-swappable-parameters suppressed below: the three
 * uint8_t* arguments ARE swappable by the check's definition — we
 * accept the risk rather than removing it. Rationale: (round_keys,
 * in, out) is the canonical AES signature (FIPS 197, OpenSSL
 * AES_encrypt, libsodium crypto_core_aes128); reshaping for lint
 * would diverge from every AES reader's mental model. In this
 * file a mistaken argument swap would be caught immediately by
 * the round-trip test (decrypt of the wrong buffer wouldn't
 * recover the plaintext). */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static void ref_encrypt(const uint8_t rk[176],
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

/* Same rationale as ref_encrypt above: canonical AES signature. */
// NOLINTNEXTLINE(bugprone-easily-swappable-parameters)
static void ref_decrypt(const uint8_t rk[176],
                        const uint8_t in[16],
                        uint8_t out[16]) {
    uint8_t state[16];
    memcpy(state, in, 16U);
    ref_add_round_key(state, &rk[160U]);
    for (size_t r = 9U; r != 0U; --r) {
        ref_inv_shift_rows(state);
        ref_inv_sub_bytes(state);
        ref_add_round_key(state, &rk[r * 16U]);
        ref_inv_mix_columns(state);
    }
    ref_inv_shift_rows(state);
    ref_inv_sub_bytes(state);
    ref_add_round_key(state, rk);
    memcpy(out, state, 16U);
}

/* ------------------------------------------------------------------
 * Test vectors (FIPS 197 Appendix C.1 + NIST KAT AESAVS subset).
 * ------------------------------------------------------------------ */
static const struct aes128_vector VECTORS[] = {
    {
        "fips-197-c1",
        {
            0x00U, 0x01U, 0x02U, 0x03U, 0x04U, 0x05U, 0x06U, 0x07U,
            0x08U, 0x09U, 0x0aU, 0x0bU, 0x0cU, 0x0dU, 0x0eU, 0x0fU,
        },
        {
            0x00U, 0x11U, 0x22U, 0x33U, 0x44U, 0x55U, 0x66U, 0x77U,
            0x88U, 0x99U, 0xaaU, 0xbbU, 0xccU, 0xddU, 0xeeU, 0xffU,
        },
        {
            0x69U, 0xc4U, 0xe0U, 0xd8U, 0x6aU, 0x7bU, 0x04U, 0x30U,
            0xd8U, 0xcdU, 0xb7U, 0x80U, 0x70U, 0xb4U, 0xc5U, 0x5aU,
        },
    },
    {
        "nist-kat-key-000",
        {
            0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U,
            0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U,
        },
        {
            0xf3U, 0x44U, 0x81U, 0xecU, 0x3cU, 0xc6U, 0x27U, 0xbaU,
            0xcdU, 0x5dU, 0xc3U, 0xfbU, 0x08U, 0xf2U, 0x73U, 0xe6U,
        },
        {
            0x03U, 0x36U, 0x76U, 0x3eU, 0x96U, 0x6dU, 0x92U, 0x59U,
            0x5aU, 0x56U, 0x7cU, 0xc9U, 0xceU, 0x53U, 0x7fU, 0x5eU,
        },
    },
    {
        "nist-kat-key-ff",
        {
            0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU,
            0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU,
        },
        {
            0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U,
            0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U,
        },
        {
            0xa1U, 0xf6U, 0x25U, 0x8cU, 0x87U, 0x7dU, 0x5fU, 0xcdU,
            0x89U, 0x64U, 0x48U, 0x45U, 0x38U, 0xbfU, 0xc9U, 0x2cU,
        },
    },
    {
        "nist-kat-pt-000",
        {
            0x10U, 0xa5U, 0x88U, 0x69U, 0xd7U, 0x4bU, 0xe5U, 0xa3U,
            0x74U, 0xcfU, 0x86U, 0x7cU, 0xfbU, 0x47U, 0x38U, 0x59U,
        },
        {
            0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U,
            0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U, 0x00U,
        },
        {
            0x6dU, 0x25U, 0x1eU, 0x69U, 0x44U, 0xb0U, 0x51U, 0xe0U,
            0x4eU, 0xaaU, 0x6fU, 0xb4U, 0xdbU, 0xf7U, 0x84U, 0x65U,
        },
    },
    {
        /* Briefing value 6e292011... was incorrect; Python
         * cryptography (libcrypto backend) and the in-file
         * reference both produce the digest below on the
         * same (key, plaintext) pair. Same pattern as the
         * sha256_test.c "64 x 0xff" correction. */
        "nist-kat-pt-ff",
        {
            0xcaU, 0xeaU, 0x65U, 0xcdU, 0xbbU, 0x75U, 0xe9U, 0x16U,
            0x9eU, 0xcdU, 0x22U, 0xebU, 0xe6U, 0xe5U, 0x46U, 0x75U,
        },
        {
            0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU,
            0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU, 0xffU,
        },
        {
            0x35U, 0xc3U, 0x15U, 0xa0U, 0x7dU, 0x94U, 0x4dU, 0x46U,
            0x77U, 0x5aU, 0x95U, 0xd0U, 0xf8U, 0x78U, 0x47U, 0xedU,
        },
    },
};
#define N_VECTORS (sizeof VECTORS / sizeof VECTORS[0])

/* ------------------------------------------------------------------
 * Helpers.
 * ------------------------------------------------------------------ */
static int cmp_block(const uint8_t a[16], const uint8_t b[16]) {
    for (size_t i = 0; i < 16U; ++i) {
        if (a[i] != b[i]) {
            return 0;
        }
    }
    return 1;
}

static void print_block(const char *label, const uint8_t b[16]) {
    printf("%s", label);
    for (size_t i = 0; i < 16U; ++i) {
        printf("%02x", b[i]);
    }
}

static int validate_reference(void) {
    /* If the reference disagrees with a vector's expected ciphertext,
     * the vector is suspect — abort before running any assembly so a
     * bad vector can't masquerade as an asm bug. */
    for (size_t i = 0; i < N_VECTORS; ++i) {
        const struct aes128_vector *v = &VECTORS[i];
        uint8_t rk[176];
        uint8_t ct[16];
        uint8_t pt[16];
        ref_expand_key(v->key, rk);
        ref_encrypt(rk, v->plaintext, ct);
        if (!cmp_block(ct, v->ciphertext)) {
            printf("FATAL  reference encrypt mismatch on %s\n", v->name);
            print_block("  exp=", v->ciphertext); puts("");
            print_block("  got=", ct);           puts("");
            return 0;
        }
        ref_decrypt(rk, v->ciphertext, pt);
        if (!cmp_block(pt, v->plaintext)) {
            printf("FATAL  reference decrypt mismatch on %s\n", v->name);
            print_block("  exp=", v->plaintext); puts("");
            print_block("  got=", pt);           puts("");
            return 0;
        }
    }
    return 1;
}

static int check_named_vectors(void) {
    int failures = 0;
    for (size_t i = 0; i < N_VECTORS; ++i) {
        const struct aes128_vector *v = &VECTORS[i];
        alignas(16) uint8_t rk[176];
        uint8_t ct[16];
        uint8_t pt[16];

        aes128_expand_key(v->key, rk);

        aes128_encrypt_block(rk, v->plaintext, ct);
        if (!cmp_block(ct, v->ciphertext)) {
            printf("FAIL  encrypt %s\n", v->name);
            print_block("  exp=", v->ciphertext); puts("");
            print_block("  got=", ct);           puts("");
            ++failures;
        }

        aes128_decrypt_block(rk, v->ciphertext, pt);
        if (!cmp_block(pt, v->plaintext)) {
            printf("FAIL  decrypt %s\n", v->name);
            print_block("  exp=", v->plaintext); puts("");
            print_block("  got=", pt);           puts("");
            ++failures;
        }
    }
    return failures;
}

/* 8 deterministic (key, plaintext) pairs from the same LCG pattern
 * used by sha256_test.c's sweep. Checks asm encrypt vs reference
 * encrypt byte-for-byte AND asm decrypt recovers the plaintext. */
static int check_sweep(void) {
    unsigned char buf[16U * 2U * 8U];  /* 16B key + 16B pt per block, 8 blocks */
    for (size_t i = 0; i < sizeof buf; ++i) {
        buf[i] = (unsigned char)((i * 37U + 13U) & 0xFFU);
    }
    int failures = 0;
    for (size_t b = 0; b < 8U; ++b) {
        const uint8_t *key = &buf[b * 32U];
        const uint8_t *pt  = &buf[b * 32U + 16U];

        alignas(16) uint8_t asm_rk[176];
        uint8_t ref_rk[176];
        uint8_t asm_ct[16];
        uint8_t ref_ct[16];
        uint8_t asm_pt[16];

        ref_expand_key(key, ref_rk);
        ref_encrypt(ref_rk, pt, ref_ct);

        aes128_expand_key(key, asm_rk);
        aes128_encrypt_block(asm_rk, pt, asm_ct);
        if (!cmp_block(asm_ct, ref_ct)) {
            printf("FAIL  sweep block %zu: asm encrypt != ref encrypt\n", b);
            print_block("  ref=", ref_ct); puts("");
            print_block("  asm=", asm_ct); puts("");
            ++failures;
        }

        aes128_decrypt_block(asm_rk, asm_ct, asm_pt);
        if (!cmp_block(asm_pt, pt)) {
            printf("FAIL  sweep block %zu: asm decrypt != plaintext\n", b);
            print_block("  exp=", pt);     puts("");
            print_block("  got=", asm_pt); puts("");
            ++failures;
        }

        /* Also confirm asm expand_key produced a schedule that
         * matches the reference byte-for-byte. Catches a class of
         * bug where encrypt+decrypt are self-consistent but the
         * schedule diverged from FIPS 197. */
        if (memcmp(asm_rk, ref_rk, 176U) != 0) {
            printf("FAIL  sweep block %zu: asm schedule != ref schedule\n", b);
            ++failures;
        }
    }
    if (failures == 0) {
        puts("(8-block sweep: asm vs reference byte-identical)");
    }
    return failures;
}

int main(void) {
    if (!validate_reference()) {
        return 2;
    }

    if (aes128_has_aesni != NULL && aes128_has_aesni() == 0) {
        /* No AES-NI on this CPU. D034's production x86_64 profile
         * requires AES-NI, so hitting this in production is a
         * deployment bug — but for test harness purposes we skip
         * cleanly rather than SIGILLing on the first aesenc. */
        puts("SKIP  AES-NI not present on this host CPU");
        puts("PASS  (reference self-check ok; asm path skipped)");
        return 0;
    }

    int total = 0;
    total += check_named_vectors();
    total += check_sweep();

    if (aes128_has_aesni != NULL) {
        printf("CPU has AES-NI: %d\n", aes128_has_aesni());
    }

    if (total == 0) {
        puts("PASS  all AES-128 checks passed");
        return 0;
    }
    printf("FAIL  %d mismatch(es)\n", total);
    return 1;
}
