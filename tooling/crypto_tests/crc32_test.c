/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (c) 2026 Ed Hodapp
 *
 * Host-side test driver for the per-arch crc32_ieee_802_3 assembly
 * routines. Tests:
 *   1. Every named vector on every available entry point.
 *   2. A length-256 sweep comparing each path against a self-
 *      contained bit-at-a-time reference (the same polynomial/init/
 *      final-XOR parameters the assembly is specified against).
 *   3. Cross-path equivalence where more than one entry point is
 *      available (x86_64 dispatcher vs. slice8 vs. pclmulqdq).
 *
 * The independent bit-by-bit reference here eliminates the
 * Python-zlib cross-check from the test path — the C driver stands
 * alone as a correctness gate. Exits 0 on full pass, non-zero on any
 * mismatch.
 */
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

#include "vectors.h"

/* Primary public entry — may dispatch or may be the only path. */
extern uint32_t crc32_ieee_802_3(const void *data, size_t len);

/* Per-arch additional entries. The x86_64 module exposes three: the
 * dispatcher, the slice8 fallback, and the pclmulqdq fast path. The
 * aarch64 module exposes only the single hardware-accelerated entry
 * (crc32_ieee_802_3). We declare all three as weak so linking against
 * aarch64's single-entry object file is still valid. */
extern uint32_t crc32_ieee_802_3_slice8(const void *data, size_t len)
    __attribute__((weak));
extern uint32_t crc32_ieee_802_3_pclmulqdq(const void *data, size_t len)
    __attribute__((weak));
extern uint32_t crc32_ieee_802_3_has_pclmulqdq(void)
    __attribute__((weak));

/* Bit-at-a-time reference implementation. Polynomial-correct per
 * IEEE 802.3 §3.2.9. No external dependencies; self-contained proof
 * that the assembly matches the specification. */
static uint32_t crc32_ref(const void *data, size_t len) {
    const unsigned char *p = (const unsigned char *)data;
    uint32_t crc = 0xFFFFFFFFU;
    for (size_t i = 0; i < len; ++i) {
        crc ^= p[i];
        for (int k = 0; k < 8; ++k) {
            uint32_t mask = -(crc & 1U);
            crc = (crc >> 1) ^ (0xEDB88320U & mask);
        }
    }
    return crc ^ 0xFFFFFFFFU;
}

static const unsigned char vec_a[]          = "a";
static const unsigned char vec_abc[]        = "abc";
static const unsigned char vec_msg_digest[] = "message digest";
static const unsigned char vec_alphabet[]   = "abcdefghijklmnopqrstuvwxyz";

static unsigned char vec_1024_zero[1024];
static unsigned char vec_1024_ff[1024];

static const struct crc_vector VECTORS[] = {
    { "empty",          NULL,           0,    0x00000000U },
    { "a",              vec_a,          1,    0xE8B7BE43U },
    { "abc",            vec_abc,        3,    0x352441C2U },
    { "message digest", vec_msg_digest, 14,   0x20159D7FU },
    { "alphabet",       vec_alphabet,   26,   0x4C2750BDU },
    { "1024 x 0x00",    vec_1024_zero,  1024, 0xEFB5AF2EU },
    { "1024 x 0xFF",    vec_1024_ff,    1024, 0xB83AFFF4U },
};
#define N_VECTORS (sizeof VECTORS / sizeof VECTORS[0])

typedef uint32_t (*crc_fn_t)(const void *, size_t);

struct path_spec {
    const char *name;
    crc_fn_t fn;
};

static int check_named_vectors(const struct path_spec *p) {
    int failures = 0;
    for (size_t i = 0; i < N_VECTORS; ++i) {
        const struct crc_vector *v = &VECTORS[i];
        uint32_t got = p->fn(v->data, v->len);
        if (got != v->expected) {
            printf("FAIL  [%s] %-16s  len=%4zu  exp=0x%08X"
                   "  got=0x%08X\n",
                   p->name, v->name, v->len, v->expected, got);
            ++failures;
        }
    }
    return failures;
}

static int sweep_vs_reference(const struct path_spec *p) {
    /* 256 bytes of a mix-dependency pattern; every residue class mod
     * 16 is exercised, plus a wide range of fold iterations (up to
     * 16 folds at length 256). */
    unsigned char buf[256];
    for (size_t i = 0; i < sizeof buf; ++i) {
        buf[i] = (unsigned char)((i * 37U + 13U) & 0xFFU);
    }
    int failures = 0;
    for (size_t len = 0; len <= sizeof buf; ++len) {
        uint32_t want = crc32_ref(buf, len);
        uint32_t got = p->fn(buf, len);
        if (got != want) {
            printf("FAIL  [%s] sweep        len=%4zu  exp=0x%08X"
                   "  got=0x%08X\n", p->name, len, want, got);
            ++failures;
        }
    }
    return failures;
}

static int check_crossarch_agreement(void) {
    /* Only meaningful when more than one entry is linked in. */
    int tested = 0;
    int failures = 0;
    unsigned char buf[256];
    for (size_t i = 0; i < sizeof buf; ++i) {
        buf[i] = (unsigned char)((i * 37U + 13U) & 0xFFU);
    }
    if (crc32_ieee_802_3_slice8 && crc32_ieee_802_3_pclmulqdq) {
        for (size_t len = 0; len <= sizeof buf; ++len) {
            uint32_t s = crc32_ieee_802_3_slice8(buf, len);
            uint32_t p = crc32_ieee_802_3_pclmulqdq(buf, len);
            if (s != p) {
                printf("FAIL  slice8/pclmul disagree  len=%4zu"
                       "  slice8=0x%08X  pclmul=0x%08X\n",
                       len, s, p);
                ++failures;
            }
        }
        ++tested;
    }
    if (tested) {
        printf("(cross-path equivalence: %d pair(s) over 257 lengths)"
               "\n", tested);
    }
    return failures;
}

int main(void) {
    memset(vec_1024_zero, 0x00, sizeof vec_1024_zero);
    memset(vec_1024_ff,   0xFF, sizeof vec_1024_ff);

    /* Self-check the reference against the compiled-in expected
     * values — if this fails, something is wrong with the vectors
     * themselves, not the assembly. */
    for (size_t i = 0; i < N_VECTORS; ++i) {
        const struct crc_vector *v = &VECTORS[i];
        uint32_t ref_got = crc32_ref(v->data, v->len);
        if (ref_got != v->expected) {
            printf("FATAL  reference impl mismatch on %s:"
                   "  exp=0x%08X  got=0x%08X\n",
                   v->name, v->expected, ref_got);
            return 2;
        }
    }

    struct path_spec paths[3];
    int n_paths = 0;
    paths[n_paths++] = (struct path_spec){
        "crc32_ieee_802_3", crc32_ieee_802_3 };
    if (crc32_ieee_802_3_slice8) {
        paths[n_paths++] = (struct path_spec){
            "slice8", crc32_ieee_802_3_slice8 };
    }
    if (crc32_ieee_802_3_pclmulqdq) {
        paths[n_paths++] = (struct path_spec){
            "pclmulqdq", crc32_ieee_802_3_pclmulqdq };
    }

    int total_fail = 0;
    for (int i = 0; i < n_paths; ++i) {
        int f = check_named_vectors(&paths[i]);
        f += sweep_vs_reference(&paths[i]);
        printf("[%s] %s\n", paths[i].name,
               f == 0 ? "ok  (7 named + 257 sweep = 264 lengths)"
                      : "FAILURES");
        total_fail += f;
    }

    total_fail += check_crossarch_agreement();

    if (crc32_ieee_802_3_has_pclmulqdq) {
        printf("CPU has PCLMULQDQ: %u\n",
               crc32_ieee_802_3_has_pclmulqdq());
    }

    if (total_fail == 0) {
        puts("PASS  all CRC-32 IEEE checks passed");
        return 0;
    }
    printf("FAIL  %d mismatch(es)\n", total_fail);
    return 1;
}
