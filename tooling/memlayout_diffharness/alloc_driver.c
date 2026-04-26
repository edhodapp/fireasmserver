/* SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Driver for the per-arch bump allocator differential test.
 *
 * Reads a stream of test cases from stdin, runs each one
 * through memlayout_run_allocator, and writes results to
 * stdout.
 *
 * Wire format on stdin (all little-endian):
 *   u32   record_count
 *   bytes [record_count * 48]   memreq table
 *   u32   cpu_count
 *   u64   [cpu_count]           cpu_values
 *   u32   tun_count
 *   u64   [tun_count]           tun_values
 *   u64   heap_start
 *   u64   ram_top
 *
 * Wire format on stdout (per case, all little-endian):
 *   i32   rc_out
 *   u64   forward_end_out
 *   u64   reverse_end_out
 *   bytes [record_count * 48]   updated memreq table (with
 *                               assigned_addr/size populated
 *                               on success)
 */

#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bcvm_abi.h"

#define MAX_RECORDS     128
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

static int run_one_case(void) {
    uint32_t record_count = 0;
    int got = read_u32(&record_count);
    if (got == 0) {
        return 0;  /* clean EOF */
    }
    if (got < 0 || record_count > MAX_RECORDS) {
        return -1;
    }
    static uint8_t records[MAX_RECORDS * MEMREQ_RECORD_BYTES];
    size_t records_bytes = (size_t)record_count
        * MEMREQ_RECORD_BYTES;
    if (records_bytes > 0
        && read_exact(records, records_bytes) <= 0) {
        return -1;
    }

    uint32_t cpu_count = 0;
    if (read_u32(&cpu_count) <= 0
        || cpu_count > MAX_FIELD_COUNT) {
        return -1;
    }
    static uint64_t cpu_buf[MAX_FIELD_COUNT];
    if (cpu_count > 0
        && read_exact(cpu_buf, (size_t)cpu_count * 8) <= 0) {
        return -1;
    }

    uint32_t tun_count = 0;
    if (read_u32(&tun_count) <= 0
        || tun_count > MAX_FIELD_COUNT) {
        return -1;
    }
    static uint64_t tun_buf[MAX_FIELD_COUNT];
    if (tun_count > 0
        && read_exact(tun_buf, (size_t)tun_count * 8) <= 0) {
        return -1;
    }

    uint64_t heap_start = 0;
    if (read_exact(&heap_start, 8) <= 0) {
        return -1;
    }
    uint64_t ram_top = 0;
    if (read_exact(&ram_top, 8) <= 0) {
        return -1;
    }

    memlayout_call_t call = {
        .memreq_start = records_bytes > 0 ? records : NULL,
        .memreq_end   = records_bytes > 0
            ? records + records_bytes : NULL,
        .cpu_values   = cpu_count > 0 ? cpu_buf : NULL,
        .cpu_count    = cpu_count,
        .tun_values   = tun_count > 0 ? tun_buf : NULL,
        .tun_count    = tun_count,
        .heap_start   = heap_start,
        .ram_top      = ram_top,
        .rc_out       = -1,
        ._pad         = 0,
        .forward_end_out = 0,
        .reverse_end_out = 0,
    };
    memlayout_run_allocator(&call);

    if (write_exact(&call.rc_out, 4) < 0) {
        return -1;
    }
    if (write_exact(&call.forward_end_out, 8) < 0) {
        return -1;
    }
    if (write_exact(&call.reverse_end_out, 8) < 0) {
        return -1;
    }
    if (records_bytes > 0
        && write_exact(records, records_bytes) < 0) {
        return -1;
    }
    fflush(stdout);
    return 1;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    for (;;) {
        int r = run_one_case();
        if (r == 0) {
            return 0;
        }
        if (r < 0) {
            fprintf(stderr,
                    "alloc_driver: malformed input "
                    "(errno=%d)\n", errno);
            return 1;
        }
    }
}
