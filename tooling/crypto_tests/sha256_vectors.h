/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (c) 2026 Ed Hodapp
 *
 * SHA-256 test vector type. Expected digests are per FIPS 180-4 +
 * RFC 6234 §8.2, cross-checked internally by a bit-level reference
 * implementation that ships with the test driver (no external
 * OpenSSL / libcrypto dependency). Stored as 32 raw digest bytes
 * (big-endian per FIPS 180-4 §6.2.2).
 */
#ifndef FIREASMSERVER_CRYPTO_TESTS_SHA256_VECTORS_H
#define FIREASMSERVER_CRYPTO_TESTS_SHA256_VECTORS_H

#include <stdint.h>
#include <stddef.h>

struct sha256_vector {
    const char          *name;
    const unsigned char *data;
    size_t               len;
    uint8_t              expected[32];
};

#endif /* FIREASMSERVER_CRYPTO_TESTS_SHA256_VECTORS_H */
