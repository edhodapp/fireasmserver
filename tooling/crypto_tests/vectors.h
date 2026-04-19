/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (c) 2026 Ed Hodapp
 *
 * CRC-32 IEEE 802.3 test vector type. The vector table itself lives
 * in crc32_test.c alongside the large static buffers it points into,
 * so only one translation unit owns those BSS definitions.
 *
 * Expected values are polynomial-correct IEEE 802.3 CRCs (polynomial
 * 0xEDB88320 reflected, init/final XOR 0xFFFFFFFF), NOT on-the-wire
 * FCS bytes. Values are cross-checked against Python zlib.crc32 and
 * an independent bit-at-a-time reference implementation.
 */
#ifndef FIREASMSERVER_CRYPTO_TESTS_VECTORS_H
#define FIREASMSERVER_CRYPTO_TESTS_VECTORS_H

#include <stdint.h>
#include <stddef.h>

struct crc_vector {
    const char *name;
    const unsigned char *data;
    size_t len;
    uint32_t expected;
};

#endif /* FIREASMSERVER_CRYPTO_TESTS_VECTORS_H */
