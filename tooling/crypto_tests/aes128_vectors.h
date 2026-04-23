/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Copyright (c) 2026 Ed Hodapp
 *
 * AES-128 test vector type. Each vector is a (key, plaintext,
 * ciphertext) triple per FIPS PUB 197 Appendix C.1 + the NIST KAT
 * "Known Answer Test" subset used for AES-128 implementation
 * verification (NIST SP 800-38A / AESAVS fixed-key + fixed-
 * plaintext single-block cases). Expected values are cross-checked
 * inside the driver against a self-contained portable-C reference
 * AES-128; no external OpenSSL/libcrypto dependency.
 *
 * Byte-order convention: FIPS 197 §3.3 — the first byte of each
 * array is byte 0 of the key/state. AES-NI's xmm byte ordering
 * and aarch64 FEAT_AES's 16-byte vector ordering both match this
 * convention directly; no byte-swap is applied on load or store.
 */
#ifndef FIREASMSERVER_CRYPTO_TESTS_AES128_VECTORS_H
#define FIREASMSERVER_CRYPTO_TESTS_AES128_VECTORS_H

#include <stdint.h>

struct aes128_vector {
    const char *name;
    uint8_t     key[16];
    uint8_t     plaintext[16];
    uint8_t     ciphertext[16];
};

#endif /* FIREASMSERVER_CRYPTO_TESTS_AES128_VECTORS_H */
