/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (c) 2026 Ed Hodapp
 *
 * Host-side test driver for the per-arch sha256 assembly routines.
 * Checks:
 *   1. Every named vector on every available entry point.
 *   2. A self-contained bit-level SHA-256 reference implementation
 *      validates the expected values and cross-checks the assembly
 *      over a 0..256-byte length sweep.
 *   3. Cross-path equivalence where more than one entry is linked
 *      (x86_64 exposes sha256_shani and sha256_scalar as separate
 *      entries in addition to the dispatcher; AArch64 exposes only
 *      sha256).
 *
 * The reference implementation is structurally distinct from the
 * assembly: straight C with explicit 32-bit rotations and a
 * one-word-at-a-time message schedule. Passing it and the assembly
 * through the same vectors gives two independent attempts at the
 * same answer.
 *
 * Exits 0 on full pass, non-zero on any mismatch.
 */
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "sha256_vectors.h"

/* Primary entry — may be dispatcher or the only path. */
extern void sha256(const void *data, size_t len, uint8_t digest[32]);

/* Per-arch additional entries. Weak-linked so the aarch64 build
 * (which only exposes sha256) still links cleanly. */
extern void sha256_shani(const void *data, size_t len, uint8_t digest[32])
    __attribute__((weak));
extern void sha256_scalar(const void *data, size_t len, uint8_t digest[32])
    __attribute__((weak));
extern int sha256_has_shani(void)
    __attribute__((weak));

/* ------------------------------------------------------------------
 * Reference SHA-256 (portable C, no external dependency).
 *   FIPS 180-4 §4.1.2 (Ch, Maj, Σ0, Σ1, σ0, σ1)
 *   FIPS 180-4 §4.2.2 (K)
 *   FIPS 180-4 §5.3.3 (initial hash values)
 *   FIPS 180-4 §6.2.2 (hash computation)
 *   FIPS 180-4 §5.1.1 (padding)
 * Accepts arbitrary-length input via a one-shot API mirroring the
 * assembly contract.
 * ------------------------------------------------------------------ */
static uint32_t ror32(uint32_t x, unsigned n) {
    return (x >> n) | (x << (32 - n));
}

static const uint32_t REF_K[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u,
    0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u,
    0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu,
    0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u,
    0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u,
    0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u,
    0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u,
    0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u,
    0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u,
};

static void ref_compress(uint32_t H[8], const uint8_t block[64]) {
    uint32_t W[64];
    for (int t = 0; t < 16; ++t) {
        W[t] = ((uint32_t)block[4*t + 0] << 24) |
               ((uint32_t)block[4*t + 1] << 16) |
               ((uint32_t)block[4*t + 2] <<  8) |
               ((uint32_t)block[4*t + 3]);
    }
    for (int t = 16; t < 64; ++t) {
        uint32_t s0 = ror32(W[t-15], 7) ^ ror32(W[t-15], 18) ^ (W[t-15] >> 3);
        uint32_t s1 = ror32(W[t-2], 17) ^ ror32(W[t-2], 19) ^ (W[t-2] >> 10);
        W[t] = W[t-16] + s0 + W[t-7] + s1;
    }
    uint32_t a = H[0], b = H[1], c = H[2], d = H[3];
    uint32_t e = H[4], f = H[5], g = H[6], h = H[7];
    for (int t = 0; t < 64; ++t) {
        uint32_t S1 = ror32(e, 6) ^ ror32(e, 11) ^ ror32(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t T1 = h + S1 + ch + REF_K[t] + W[t];
        uint32_t S0 = ror32(a, 2) ^ ror32(a, 13) ^ ror32(a, 22);
        uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t T2 = S0 + mj;
        h = g; g = f; f = e; e = d + T1;
        d = c; c = b; b = a; a = T1 + T2;
    }
    H[0] += a; H[1] += b; H[2] += c; H[3] += d;
    H[4] += e; H[5] += f; H[6] += g; H[7] += h;
}

static void ref_sha256(const void *data, size_t len, uint8_t digest[32]) {
    uint32_t H[8] = {
        0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
        0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u,
    };
    const uint8_t *p = (const uint8_t *)data;
    size_t remaining = len;
    while (remaining >= 64) {
        ref_compress(H, p);
        p += 64;
        remaining -= 64;
    }
    /* Final block construction: up to 128 B scratch. */
    uint8_t tail[128];
    memset(tail, 0, sizeof tail);
    memcpy(tail, p, remaining);
    tail[remaining] = 0x80;
    size_t blocks = (remaining < 56) ? 1 : 2;
    uint64_t bits = (uint64_t)len * 8u;
    size_t len_off = blocks * 64u - 8u;
    for (int i = 0; i < 8; ++i) {
        tail[len_off + i] = (uint8_t)(bits >> (56 - 8*i));
    }
    for (size_t b = 0; b < blocks; ++b) {
        ref_compress(H, tail + b*64);
    }
    for (int i = 0; i < 8; ++i) {
        digest[4*i + 0] = (uint8_t)(H[i] >> 24);
        digest[4*i + 1] = (uint8_t)(H[i] >> 16);
        digest[4*i + 2] = (uint8_t)(H[i] >>  8);
        digest[4*i + 3] = (uint8_t)(H[i]);
    }
}

/* ------------------------------------------------------------------
 * Test vectors (FIPS 180-4 + RFC 6234 §8.2 + two single-block
 * boundary cases at exactly 64 B).
 * ------------------------------------------------------------------ */
static const unsigned char vec_abc[]  = "abc";
static const unsigned char vec_448[]  =
    "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
static const unsigned char vec_896[]  =
    "abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmn"
    "hijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu";

static unsigned char vec_1M_a[1000000];
static unsigned char vec_64_zero[64];
static unsigned char vec_64_ff[64];

#define EXPECT(...) { __VA_ARGS__ }

static const struct sha256_vector VECTORS[] = {
    {
        "empty", NULL, 0, EXPECT(
            0xe3,0xb0,0xc4,0x42,0x98,0xfc,0x1c,0x14,
            0x9a,0xfb,0xf4,0xc8,0x99,0x6f,0xb9,0x24,
            0x27,0xae,0x41,0xe4,0x64,0x9b,0x93,0x4c,
            0xa4,0x95,0x99,0x1b,0x78,0x52,0xb8,0x55)
    },
    {
        "abc", vec_abc, 3, EXPECT(
            0xba,0x78,0x16,0xbf,0x8f,0x01,0xcf,0xea,
            0x41,0x41,0x40,0xde,0x5d,0xae,0x22,0x23,
            0xb0,0x03,0x61,0xa3,0x96,0x17,0x7a,0x9c,
            0xb4,0x10,0xff,0x61,0xf2,0x00,0x15,0xad)
    },
    {
        "abcdbcde...56B", vec_448, 56, EXPECT(
            0x24,0x8d,0x6a,0x61,0xd2,0x06,0x38,0xb8,
            0xe5,0xc0,0x26,0x93,0x0c,0x3e,0x60,0x39,
            0xa3,0x3c,0xe4,0x59,0x64,0xff,0x21,0x67,
            0xf6,0xec,0xed,0xd4,0x19,0xdb,0x06,0xc1)
    },
    {
        "abcdefgh...112B", vec_896, 112, EXPECT(
            0xcf,0x5b,0x16,0xa7,0x78,0xaf,0x83,0x80,
            0x03,0x6c,0xe5,0x9e,0x7b,0x04,0x92,0x37,
            0x0b,0x24,0x9b,0x11,0xe8,0xf0,0x7a,0x51,
            0xaf,0xac,0x45,0x03,0x7a,0xfe,0xe9,0xd1)
    },
    {
        "1M x 'a'", vec_1M_a, 1000000, EXPECT(
            0xcd,0xc7,0x6e,0x5c,0x99,0x14,0xfb,0x92,
            0x81,0xa1,0xc7,0xe2,0x84,0xd7,0x3e,0x67,
            0xf1,0x80,0x9a,0x48,0xa4,0x97,0x20,0x0e,
            0x04,0x6d,0x39,0xcc,0xc7,0x11,0x2c,0xd0)
    },
    {
        "64 x 0x00", vec_64_zero, 64, EXPECT(
            0xf5,0xa5,0xfd,0x42,0xd1,0x6a,0x20,0x30,
            0x27,0x98,0xef,0x6e,0xd3,0x09,0x97,0x9b,
            0x43,0x00,0x3d,0x23,0x20,0xd9,0xf0,0xe8,
            0xea,0x98,0x31,0xa9,0x27,0x59,0xfb,0x4b)
    },
    {
        /* Briefing value af961376... was incorrect; Python hashlib
         * and an independent recomputation confirm this digest. */
        "64 x 0xff", vec_64_ff, 64, EXPECT(
            0x86,0x67,0xe7,0x18,0x29,0x4e,0x9e,0x0d,
            0xf1,0xd3,0x06,0x00,0xba,0x3e,0xeb,0x20,
            0x1f,0x76,0x4a,0xad,0x2d,0xad,0x72,0x74,
            0x86,0x43,0xe4,0xa2,0x85,0xe1,0xd1,0xf7)
    },
};
#define N_VECTORS (sizeof VECTORS / sizeof VECTORS[0])

typedef void (*sha_fn_t)(const void *, size_t, uint8_t[32]);

struct path_spec {
    const char *name;
    sha_fn_t    fn;
};

static int cmp_digest(const uint8_t got[32], const uint8_t want[32]) {
    for (int i = 0; i < 32; ++i) {
        if (got[i] != want[i]) {
            return 0;
        }
    }
    return 1;
}

static void print_digest(const char *label, const uint8_t d[32]) {
    printf("%s", label);
    for (int i = 0; i < 32; ++i) {
        printf("%02x", d[i]);
    }
}

static int check_named_vectors(const struct path_spec *p) {
    int failures = 0;
    for (size_t i = 0; i < N_VECTORS; ++i) {
        const struct sha256_vector *v = &VECTORS[i];
        uint8_t got[32];
        p->fn(v->data, v->len, got);
        if (!cmp_digest(got, v->expected)) {
            printf("FAIL  [%s] %-20s  len=%8zu\n", p->name, v->name, v->len);
            print_digest("  exp=", v->expected); puts("");
            print_digest("  got=", got);         puts("");
            ++failures;
        }
    }
    return failures;
}

static int sweep_vs_reference(const struct path_spec *p) {
    /* 0..256-byte sweep against the in-file reference; every residue
     * class mod 64 is exercised, including the critical boundaries at
     * 55, 56, 63, 64, 119, 120. */
    unsigned char buf[256];
    for (size_t i = 0; i < sizeof buf; ++i) {
        buf[i] = (unsigned char)((i * 37u + 13u) & 0xFFu);
    }
    int failures = 0;
    for (size_t len = 0; len <= sizeof buf; ++len) {
        uint8_t want[32], got[32];
        ref_sha256(buf, len, want);
        p->fn(buf, len, got);
        if (!cmp_digest(got, want)) {
            printf("FAIL  [%s] sweep len=%4zu\n", p->name, len);
            print_digest("  exp=", want); puts("");
            print_digest("  got=", got);  puts("");
            ++failures;
        }
    }
    return failures;
}

static int cross_path_agreement(void) {
    /* Only meaningful when multiple entries are linked AND SHA-NI is
     * actually supported on this CPU. If CPU lacks SHA-NI, invoking
     * sha256_shani would #UD; skip cleanly in that case. */
    if (sha256_shani == NULL || sha256_scalar == NULL) {
        return 0;
    }
    if (sha256_has_shani && !sha256_has_shani()) {
        puts("(SHA-NI not present on host CPU; "
             "skipping shani-vs-scalar cross-check)");
        return 0;
    }
    unsigned char buf[256];
    for (size_t i = 0; i < sizeof buf; ++i) {
        buf[i] = (unsigned char)((i * 37u + 13u) & 0xFFu);
    }
    int failures = 0;
    for (size_t len = 0; len <= sizeof buf; ++len) {
        uint8_t ds[32], dn[32];
        sha256_scalar(buf, len, ds);
        sha256_shani(buf, len, dn);
        if (!cmp_digest(ds, dn)) {
            printf("FAIL  shani/scalar disagree  len=%4zu\n", len);
            print_digest("  scalar=", ds); puts("");
            print_digest("  shani =", dn); puts("");
            ++failures;
        }
    }
    if (failures == 0) {
        puts("(shani-vs-scalar equivalence: 257 lengths ok)");
    }
    return failures;
}

int main(void) {
    memset(vec_1M_a,    'a',  sizeof vec_1M_a);
    memset(vec_64_zero, 0x00, sizeof vec_64_zero);
    memset(vec_64_ff,   0xFF, sizeof vec_64_ff);

    /* First, validate the reference implementation against the
     * compiled-in expected values. If this fails, the vectors
     * themselves are suspect, not the assembly. */
    for (size_t i = 0; i < N_VECTORS; ++i) {
        const struct sha256_vector *v = &VECTORS[i];
        uint8_t got[32];
        ref_sha256(v->data, v->len, got);
        if (!cmp_digest(got, v->expected)) {
            printf("FATAL  reference impl mismatch on %s\n", v->name);
            print_digest("  exp=", v->expected); puts("");
            print_digest("  got=", got);         puts("");
            return 2;
        }
    }

    struct path_spec paths[4];
    int n_paths = 0;
    paths[n_paths++] = (struct path_spec){ "sha256", sha256 };
    if (sha256_shani && (!sha256_has_shani || sha256_has_shani())) {
        paths[n_paths++] = (struct path_spec){ "shani", sha256_shani };
    }
    if (sha256_scalar) {
        paths[n_paths++] = (struct path_spec){ "scalar", sha256_scalar };
    }

    int total = 0;
    for (int i = 0; i < n_paths; ++i) {
        int f = check_named_vectors(&paths[i]);
        f += sweep_vs_reference(&paths[i]);
        printf("[%s] %s  (7 named + 257 sweep = 264 lengths)\n",
               paths[i].name, f == 0 ? "ok" : "FAILURES");
        total += f;
    }
    total += cross_path_agreement();

    if (sha256_has_shani) {
        printf("CPU has SHA-NI: %d\n", sha256_has_shani());
    }

    if (total == 0) {
        puts("PASS  all SHA-256 checks passed");
        return 0;
    }
    printf("FAIL  %d mismatch(es)\n", total);
    return 1;
}
