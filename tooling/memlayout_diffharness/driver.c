/* SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Driver for the per-arch bytecode VM differential test.
 *
 * Reads a stream of test cases from stdin, runs each one
 * through memlayout_run_bytecode (linked from the per-arch
 * .S), and writes (rc, result) pairs to stdout.
 *
 * Wire format on stdin (all little-endian):
 *   u32   code_len
 *   bytes [code_len]   bytecode
 *   u32   cpu_count
 *   u64   [cpu_count]  cpu_values
 *   u32   tun_count
 *   u64   [tun_count]  tun_values
 *
 * (repeated until EOF)
 *
 * Wire format on stdout (per case, all little-endian):
 *   i32   rc_out
 *   u64   result_out
 *
 * The Python harness on the laptop side launches this binary
 * under qemu-<arch>-static, writes test cases to stdin,
 * reads results from stdout, asserts every (rc, result) tuple
 * matches the Python reference's verdict on the same input.
 */

#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bcvm_abi.h"

/* Hard cap on per-case input sizes. Anything larger is a
 * harness bug. */
#define MAX_CODE_LEN    65536
#define MAX_FIELD_COUNT 256

static int read_exact(void *buf, size_t n) {
    size_t got = 0;
    while (got < n) {
        size_t r = fread((char *)buf + got, 1, n - got, stdin);
        if (r == 0) {
            return (got == 0) ? 0 : -1;
        }
        got += r;
    }
    return 1;
}

static int write_exact(const void *buf, size_t n) {
    size_t put = 0;
    while (put < n) {
        size_t w = fwrite((const char *)buf + put, 1,
                          n - put, stdout);
        if (w == 0) {
            return -1;
        }
        put += w;
    }
    return 0;
}

static int read_u32(uint32_t *out) {
    uint8_t buf[4];
    int r = read_exact(buf, 4);
    if (r <= 0) {
        return r;
    }
    *out = (uint32_t)buf[0]
        | ((uint32_t)buf[1] << 8)
        | ((uint32_t)buf[2] << 16)
        | ((uint32_t)buf[3] << 24);
    return 1;
}

static int read_u64_array(uint64_t *out, uint32_t n) {
    /* Wire format is little-endian; on x86_64 + aarch64 LE
     * hosts a raw read is fine. We could byte-swap defensively
     * but the asm and the Python harness both fix LE on the
     * wire, so divergence would be a different bug. */
    return read_exact(out, (size_t)n * 8) > 0 ? 0 : -1;
}

static int run_one_case(void) {
    uint32_t code_len = 0;
    int got = read_u32(&code_len);
    if (got == 0) {
        return 0;  /* clean EOF */
    }
    if (got < 0 || code_len > MAX_CODE_LEN) {
        return -1;
    }
    static uint8_t code_buf[MAX_CODE_LEN];
    if (code_len > 0) {
        if (read_exact(code_buf, code_len) <= 0) {
            return -1;
        }
    }

    uint32_t cpu_count = 0;
    if (read_u32(&cpu_count) <= 0
        || cpu_count > MAX_FIELD_COUNT) {
        return -1;
    }
    static uint64_t cpu_buf[MAX_FIELD_COUNT];
    if (cpu_count > 0
        && read_u64_array(cpu_buf, cpu_count) < 0) {
        return -1;
    }

    uint32_t tun_count = 0;
    if (read_u32(&tun_count) <= 0
        || tun_count > MAX_FIELD_COUNT) {
        return -1;
    }
    static uint64_t tun_buf[MAX_FIELD_COUNT];
    if (tun_count > 0
        && read_u64_array(tun_buf, tun_count) < 0) {
        return -1;
    }

    bcvm_call_t call = {
        .code        = code_len > 0 ? code_buf : NULL,
        .code_len    = code_len,
        .cpu_values  = cpu_count > 0 ? cpu_buf : NULL,
        .cpu_count   = cpu_count,
        .tun_values  = tun_count > 0 ? tun_buf : NULL,
        .tun_count   = tun_count,
        .rc_out      = -1,
        ._pad        = 0,
        .result_out  = 0,
    };
    memlayout_run_bytecode(&call);

    if (write_exact(&call.rc_out, 4) < 0) {
        return -1;
    }
    if (write_exact(&call.result_out, 8) < 0) {
        return -1;
    }
    fflush(stdout);
    return 1;
}

int main(void) {
    /* Unbuffered stdout so the Python harness reads results
     * as soon as we produce them. */
    setvbuf(stdout, NULL, _IONBF, 0);
    for (;;) {
        int r = run_one_case();
        if (r == 0) {
            return 0;
        }
        if (r < 0) {
            fprintf(stderr,
                    "driver: malformed input (errno=%d)\n",
                    errno);
            return 1;
        }
    }
}
