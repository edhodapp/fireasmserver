// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Ed Hodapp
//
// SHA-256 per FIPS 180-4, AArch64 ARMv8 SHA-2 extension (FEAT_SHA256).
//
// Contract (AAPCS64):
//   x0 = const void *data
//   x1 = size_t      len
//   x2 = uint8_t    *digest   // 32 bytes, big-endian per FIPS 180-4
//
// Approach: ARMv8 SHA-256 accelerator instructions compute four rounds
// per SHA256H/SHA256H2 pair, with SHA256SU0 + SHA256SU1 supplying the
// rolling message schedule. The state is held across a block in two
// 128-bit V-registers (ABEF / CDGH); the message schedule rotates
// through four V-regs. Feed-forward adds the saved ABEF/CDGH back at
// block end.
//
// Runtime CPU dispatch is not used on AArch64: D034 requires
// FEAT_SHA256 as a baseline for every production profile (Cortex-A76,
// Neoverse N1/V1/V2, Apple cores). A build-time .arch assertion
// below is sufficient. Cortex-A72 / A55 (no SHA-2) are out of scope.
//
// Padding (FIPS 180-4 §5.1.1): append 0x80, zero-pad, append a
// 64-bit big-endian total bit length. Digest output is big-endian
// per FIPS 180-4 §6.2.2.
//
// Constant-time by construction: no data-dependent branches and no
// data-dependent indexed memory access.
//
// V-register allocation inside _sha256_compress:
//   v0       STATE0 (ABEF)   lanes 0..3 = A, B, E, F
//   v1       STATE1 (CDGH)   lanes 0..3 = C, D, G, H
//   v2       ABEF_SAVE       (feed-forward)
//   v3       CDGH_SAVE       (feed-forward)
//   v4..v7   MSG0..MSG3      rolling message schedule (four quads)
//   v16      K[t..t+3]       reloaded from rodata per 4-round group
//   v17      STATE0 copy     snapshot before sha256h2 in each group
// Only v0..v7 and v16..v17 are touched, so the AAPCS64 low-half
// preservation of v8..v15 is trivially satisfied.
//
// References:
//   FIPS PUB 180-4, "Secure Hash Standard," August 2015.
//       §4.1.2, §4.2.2, §5.1.1, §5.3.3, §6.2.2.
//   RFC 6234, "US Secure Hash Algorithms," May 2011, §8.2.
//   Arm ARM for A-profile (ARM DDI 0487), §C7.2.370-373
//       (SHA256H, SHA256H2, SHA256SU0, SHA256SU1).

    .arch   armv8-a+crypto

    .section .rodata.sha256_consts, "a", %progbits
    .balign 64
    .globl  sha256_k
sha256_k:
    .word   0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5
    .word   0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5
    .word   0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3
    .word   0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174
    .word   0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc
    .word   0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da
    .word   0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7
    .word   0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967
    .word   0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13
    .word   0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85
    .word   0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3
    .word   0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070
    .word   0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5
    .word   0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3
    .word   0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208
    .word   0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    .size   sha256_k, . - sha256_k

    .globl  sha256_h0
sha256_h0:
    .word   0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a
    .word   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
    .size   sha256_h0, . - sha256_h0

    .text
    .globl  sha256
    .type   sha256, %function
    .balign 16

// Outer function register map:
//   x0  : data ptr (advances)
//   x1  : remaining length (decremented)
//   x2  : digest out
//   x3  : total length bytes (saved at entry)
//   x4  : K base  (preserved across _sha256_compress calls)
//   x5..x8 : scratch (padding construction)
//   x30 : link register (saved into stack with frame record)
sha256:
    // Frame layout (144 B total, 16-byte aligned):
    //   [sp+   0 .. sp+ 127]   pad scratch (128 B)
    //   [sp+ 128 .. sp+ 135]   saved x29
    //   [sp+ 136 .. sp+ 143]   saved x30
    sub     sp, sp, #144
    stp     x29, x30, [sp, #128]
    add     x29, sp, #128                       // frame pointer

    mov     x3, x1                              // total length

    // Resolve K base once.
    adrp    x4, sha256_k
    add     x4, x4, :lo12:sha256_k

    // Load initial state H[0..7] into v0, v1.
    adrp    x5, sha256_h0
    add     x5, x5, :lo12:sha256_h0
    ld1     {v0.4s, v1.4s}, [x5]

    // -------- full-block loop --------
.Lblk:
    cmp     x1, #64
    b.lo    .Lblk_done
    bl      _sha256_compress
    add     x0, x0, #64
    sub     x1, x1, #64
    b       .Lblk
.Lblk_done:

    // -------- pad tail --------
    // scratch ptr = sp; zero 128 bytes.
    movi    v4.16b, #0
    movi    v5.16b, #0
    movi    v6.16b, #0
    movi    v7.16b, #0
    mov     x5, sp
    st1     {v4.16b, v5.16b, v6.16b, v7.16b}, [x5], #64
    st1     {v4.16b, v5.16b, v6.16b, v7.16b}, [x5]
    // Copy residue (0..63 bytes) one byte at a time; non-secret bounds.
    mov     x5, xzr
.Lpad_copy:
    cmp     x5, x1
    b.hs    .Lpad_copy_done
    ldrb    w6, [x0, x5]
    strb    w6, [sp, x5]
    add     x5, x5, #1
    b       .Lpad_copy
.Lpad_copy_done:
    // Append 0x80 at residue_len.
    mov     w6, #0x80
    strb    w6, [sp, x1]
    // Determine 1 or 2 blocks (length field won't fit in block 1 if
    // residue_len >= 56).
    mov     w7, #1
    cmp     x1, #56
    b.lo    .Lone_block
    mov     w7, #2
.Lone_block:
    // Write big-endian total-bit-length at offset (64*w7 - 8).
    // Input-length domain: ≤ 2^61 - 1 bytes. A `size_t` on AArch64
    // admits values up to 2^64 - 1, so in principle an input at or
    // past 2^61 bytes (≈ 2 EiB) would silently drop high bits here;
    // FIPS 180-4 itself is only defined up to 2^64 - 1 bits
    // (≈ 2^61 - 1 bytes), so callers above that bound are already
    // outside the standard and the spec has no answer. We do not
    // add a runtime check: any realistic call (one-shot TLS
    // transcripts, HKDF blocks, filesystem objects in a Firecracker
    // guest) lands many orders of magnitude below the bound.
    lsl     x5, x3, #3                          // bytes → bits
    rev     x5, x5                              // big-endian
    lsl     x8, x7, #6                          // 64 * block_count
    sub     x8, x8, #8
    str     x5, [sp, x8]

    // Compress pad blocks.
    mov     x0, sp
.Lpad_loop:
    bl      _sha256_compress
    add     x0, x0, #64
    subs    w7, w7, #1
    b.ne    .Lpad_loop

    // Emit digest big-endian: byte-reverse each 32-bit lane, store 32 B.
    rev32   v0.16b, v0.16b
    rev32   v1.16b, v1.16b
    st1     {v0.4s, v1.4s}, [x2]

    ldp     x29, x30, [sp, #128]
    add     sp, sp, #144
    ret
    .size   sha256, . - sha256

// -----------------------------------------------------------------
// _sha256_compress
//   One-block compression. Reads 64 bytes from x0; updates v0 (ABEF)
//   and v1 (CDGH). Requires x4 = &sha256_k on entry.
//
//   Clobbers: v4..v7, v16, v17. Does not touch x0..x4.
// -----------------------------------------------------------------
    .type   _sha256_compress, %function
    .balign 16
_sha256_compress:
    // Save state for feed-forward.
    mov     v2.16b, v0.16b
    mov     v3.16b, v1.16b

    // Load 64 bytes and byte-reverse each 32-bit lane.
    ld1     {v4.16b, v5.16b, v6.16b, v7.16b}, [x0]
    rev32   v4.16b, v4.16b
    rev32   v5.16b, v5.16b
    rev32   v6.16b, v6.16b
    rev32   v7.16b, v7.16b

    // Rounds 0-3
    ldr     q16, [x4, #  0]                     // K[0..3]
    add     v16.4s, v16.4s, v4.4s               // K+W
    sha256su0 v4.4s, v5.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v4.4s, v6.4s, v7.4s

    // Rounds 4-7
    ldr     q16, [x4, # 16]
    add     v16.4s, v16.4s, v5.4s
    sha256su0 v5.4s, v6.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v5.4s, v7.4s, v4.4s

    // Rounds 8-11
    ldr     q16, [x4, # 32]
    add     v16.4s, v16.4s, v6.4s
    sha256su0 v6.4s, v7.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v6.4s, v4.4s, v5.4s

    // Rounds 12-15
    ldr     q16, [x4, # 48]
    add     v16.4s, v16.4s, v7.4s
    sha256su0 v7.4s, v4.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v7.4s, v5.4s, v6.4s

    // Rounds 16-19
    ldr     q16, [x4, # 64]
    add     v16.4s, v16.4s, v4.4s
    sha256su0 v4.4s, v5.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v4.4s, v6.4s, v7.4s

    // Rounds 20-23
    ldr     q16, [x4, # 80]
    add     v16.4s, v16.4s, v5.4s
    sha256su0 v5.4s, v6.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v5.4s, v7.4s, v4.4s

    // Rounds 24-27
    ldr     q16, [x4, # 96]
    add     v16.4s, v16.4s, v6.4s
    sha256su0 v6.4s, v7.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v6.4s, v4.4s, v5.4s

    // Rounds 28-31
    ldr     q16, [x4, #112]
    add     v16.4s, v16.4s, v7.4s
    sha256su0 v7.4s, v4.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v7.4s, v5.4s, v6.4s

    // Rounds 32-35
    ldr     q16, [x4, #128]
    add     v16.4s, v16.4s, v4.4s
    sha256su0 v4.4s, v5.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v4.4s, v6.4s, v7.4s

    // Rounds 36-39
    ldr     q16, [x4, #144]
    add     v16.4s, v16.4s, v5.4s
    sha256su0 v5.4s, v6.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v5.4s, v7.4s, v4.4s

    // Rounds 40-43
    ldr     q16, [x4, #160]
    add     v16.4s, v16.4s, v6.4s
    sha256su0 v6.4s, v7.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v6.4s, v4.4s, v5.4s

    // Rounds 44-47
    ldr     q16, [x4, #176]
    add     v16.4s, v16.4s, v7.4s
    sha256su0 v7.4s, v4.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s
    sha256su1 v7.4s, v5.4s, v6.4s

    // Rounds 48-51: schedule is complete; no more su0/su1.
    ldr     q16, [x4, #192]
    add     v16.4s, v16.4s, v4.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s

    // Rounds 52-55
    ldr     q16, [x4, #208]
    add     v16.4s, v16.4s, v5.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s

    // Rounds 56-59
    ldr     q16, [x4, #224]
    add     v16.4s, v16.4s, v6.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s

    // Rounds 60-63
    ldr     q16, [x4, #240]
    add     v16.4s, v16.4s, v7.4s
    mov     v17.16b, v0.16b
    sha256h   q0, q1, v16.4s
    sha256h2  q1, q17, v16.4s

    // Feed-forward.
    add     v0.4s, v0.4s, v2.4s
    add     v1.4s, v1.4s, v3.4s
    ret
    .size   _sha256_compress, . - _sha256_compress

    .section .note.GNU-stack, "", %progbits
