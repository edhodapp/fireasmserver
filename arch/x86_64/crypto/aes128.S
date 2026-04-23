; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; AES-128 block primitive per FIPS PUB 197 using Intel AES-NI
; instructions (aeskeygenassist, aesenc, aesenclast, aesdec,
; aesdeclast, aesimc). x86_64 SysV AMD64 ABI; entries are
; host-callable leaf routines with no stack frame or callee-saved
; register usage beyond what cpuid requires.
;
; Contracts:
;   aes128_expand_key(const uint8_t key[16],
;                     uint8_t round_keys[176])
;       rdi = key, rsi = round_keys
;   aes128_encrypt_block(const uint8_t round_keys[176],
;                        const uint8_t in[16], uint8_t out[16])
;       rdi = round_keys, rsi = in, rdx = out   (in may alias out)
;   aes128_decrypt_block(const uint8_t round_keys[176],
;                        const uint8_t in[16], uint8_t out[16])
;       rdi = round_keys, rsi = in, rdx = out   (in may alias out)
;   aes128_has_aesni(void) -> int
;       0 if CPU lacks AES-NI, 1 otherwise
;
; No alignment requirements on caller buffers: all SIMD loads and
; stores are movdqu. The briefing's 16-byte-aligned claim was more
; restrictive than needed; dropping that claim (rather than tightening
; the code to movdqa) gives callers the less-demanding contract.
;
; No scalar fallback. D034's x86_64 profile targets CPUs with
; AES-NI (Westmere+ server, Goldmont+ Atom); a table-lookup AES
; would violate constant-time (D032), so on a CPU without AES-NI
; the correct behavior is to refuse the work, not to fall back.
; Callers MUST probe via aes128_has_aesni and surface a hard
; error on zero — see tooling/crypto_tests/aes128_test.c for the
; reference skip-clean pattern.
;
; Constant-time: aesenc/aesenclast/aesdec/aesdeclast/aesimc and
; aeskeygenassist are data-independent in timing per Intel SDM
; Vol. 3A §15.17 / Vol. 2A. No secret-dependent branches; round
; keys are read in a fixed sequential order.
;
; Byte order (FIPS 197 §3.3): the first byte of the input array
; is byte 0 of the 4x4 state (state[0,0]). AES-NI's xmm byte
; ordering matches this convention directly — byte 0 of an xmm
; register (the low byte) holds state[0,0] when you load via
; movdqu/movdqa. No byte-swap is applied on load or store; the
; primitive passes bytes through under the FIPS 197 convention
; the caller already uses.
;
; Decrypt key-schedule policy: aes128_expand_key produces the
; raw forward-direction schedule (same schedule used by encrypt).
; aes128_decrypt_block applies aesimc inline to the inner round
; keys 1..9 as it walks the schedule; the caller's round_keys
; buffer is never mutated by decrypt. Trades 9 aesimc's per
; decrypt for a simpler shared schedule — immaterial for
; single-block use.
;
; CPUID AES-NI probe: leaf 1 (EAX=1), ECX bit 25 per Intel SDM
; Vol. 2A §3.2 "CPUID - CPU Identification" feature flag
; description (verified 2026-04-22; not to be confused with leaf
; 7 / EBX[29] which is SHA-NI).
;
; References:
;   FIPS PUB 197, "Advanced Encryption Standard (AES),"
;       November 2001. §5.1 (Cipher), §5.2 (Key Expansion),
;       §5.3 (Inverse Cipher / Equivalent Inverse Cipher),
;       Appendix C.1 (AES-128 test vector).
;   S. Gueron, "Intel Advanced Encryption Standard (AES) New
;       Instructions Set," Intel white paper, rev 3.01, 2012.
;       Figure 24 (AES-128 key expansion macro), Figure 25
;       (encrypt round sequence), Figure 26 (decrypt with aesimc
;       applied at decrypt time).
;   Intel SDM Vol. 2A, CPUID AES-NI feature flag; Vol. 2A/2B
;       instruction references for AESENC/AESENCLAST/AESDEC/
;       AESDECLAST/AESIMC/AESKEYGENASSIST.

default rel

; ---------------------------------------------------------------
; Text
; ---------------------------------------------------------------
section .text

global aes128_expand_key
global aes128_encrypt_block
global aes128_decrypt_block
global aes128_has_aesni

; aes_128_assist macro — Intel AES-NI whitepaper Figure 24.
;   On entry: xmm1 = previous round key v[i-1];
;             xmm2 = output of aeskeygenassist (expected to hold
;                    Rcon-xor'd rotated SubWord in lane 3)
;   On exit:  xmm1 = next round key v[i]
;   Clobbers: xmm2, xmm3
;
; The three cumulative (pslldq 4, pxor) pairs implement the
; FIPS 197 §5.2 rolling XOR: v[i][j] = v[i-1][j] XOR v[i][j-1]
; for j = 1..3, with v[i][0] = v[i-1][0] XOR t where t is the
; broadcast of RotWord(SubWord(v[i-1][3])) XOR Rcon[i].
%macro AES128_KEY_ASSIST 0
    pshufd      xmm2, xmm2, 0xff
    movdqa      xmm3, xmm1
    pslldq      xmm3, 4
    pxor        xmm1, xmm3
    movdqa      xmm3, xmm1
    pslldq      xmm3, 4
    pxor        xmm1, xmm3
    movdqa      xmm3, xmm1
    pslldq      xmm3, 4
    pxor        xmm1, xmm3
    pxor        xmm1, xmm2
%endmacro

; ===============================================================
; aes128_has_aesni
;   CPUID leaf 1 ECX[25]. Leaf 1 exists on every x86_64 CPU so
;   no max-leaf gate is required (contrast sha256_has_shani which
;   must probe leaf 7 for existence first).
; ===============================================================
align 16
aes128_has_aesni:
    push    rbx
    mov     eax, 1
    xor     ecx, ecx
    cpuid
    xor     eax, eax
    bt      ecx, 25
    setc    al
    pop     rbx
    ret

; ===============================================================
; aes128_expand_key(const uint8_t key[16], uint8_t round_keys[176])
;   rdi = key, rsi = round_keys
;
; Produces 11 round keys (rk[0..10]) = 176 bytes. rk[0] is the
; input key verbatim; rk[1..10] are derived via aeskeygenassist
; with Rcon values 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
; 0x80, 0x1b, 0x36 (FIPS 197 §5.2, AES-128 instance).
; Fully unrolled — no loop counter to mislead a reader into
; thinking the round-count is runtime-dependent.
; ===============================================================
align 16
aes128_expand_key:
    movdqu      xmm1, [rdi]
    movdqu      [rsi +   0], xmm1                ; rk[0]

    aeskeygenassist xmm2, xmm1, 0x01
    AES128_KEY_ASSIST
    movdqu      [rsi +  16], xmm1                ; rk[1]

    aeskeygenassist xmm2, xmm1, 0x02
    AES128_KEY_ASSIST
    movdqu      [rsi +  32], xmm1                ; rk[2]

    aeskeygenassist xmm2, xmm1, 0x04
    AES128_KEY_ASSIST
    movdqu      [rsi +  48], xmm1                ; rk[3]

    aeskeygenassist xmm2, xmm1, 0x08
    AES128_KEY_ASSIST
    movdqu      [rsi +  64], xmm1                ; rk[4]

    aeskeygenassist xmm2, xmm1, 0x10
    AES128_KEY_ASSIST
    movdqu      [rsi +  80], xmm1                ; rk[5]

    aeskeygenassist xmm2, xmm1, 0x20
    AES128_KEY_ASSIST
    movdqu      [rsi +  96], xmm1                ; rk[6]

    aeskeygenassist xmm2, xmm1, 0x40
    AES128_KEY_ASSIST
    movdqu      [rsi + 112], xmm1                ; rk[7]

    aeskeygenassist xmm2, xmm1, 0x80
    AES128_KEY_ASSIST
    movdqu      [rsi + 128], xmm1                ; rk[8]

    aeskeygenassist xmm2, xmm1, 0x1b
    AES128_KEY_ASSIST
    movdqu      [rsi + 144], xmm1                ; rk[9]

    aeskeygenassist xmm2, xmm1, 0x36
    AES128_KEY_ASSIST
    movdqu      [rsi + 160], xmm1                ; rk[10]

    ret

; ===============================================================
; aes128_encrypt_block(round_keys, in, out)
;   rdi = round_keys, rsi = in, rdx = out
;
; FIPS 197 §5.1 cipher:
;   state = in XOR rk[0]
;   for r = 1..9:  state = MixColumns(ShiftRows(SubBytes(state)))
;                          XOR rk[r]                        (aesenc)
;   state = ShiftRows(SubBytes(state)) XOR rk[10]           (aesenclast)
; ===============================================================
align 16
aes128_encrypt_block:
    movdqu      xmm0, [rsi]
    movdqu      xmm1, [rdi +   0]
    pxor        xmm0, xmm1

    movdqu      xmm1, [rdi +  16]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi +  32]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi +  48]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi +  64]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi +  80]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi +  96]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi + 112]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi + 128]
    aesenc      xmm0, xmm1
    movdqu      xmm1, [rdi + 144]
    aesenc      xmm0, xmm1

    movdqu      xmm1, [rdi + 160]
    aesenclast  xmm0, xmm1

    movdqu      [rdx], xmm0
    ret

; ===============================================================
; aes128_decrypt_block(round_keys, in, out)
;   rdi = round_keys, rsi = in, rdx = out
;
; FIPS 197 §5.3.5 equivalent inverse cipher. The schedule is the
; raw forward schedule; the inverse-MixColumns transform on rk[1..9]
; is applied inline via aesimc, which trades 9 extra uops per
; decrypt for a shared expand_key path between encrypt and decrypt
; and, critically, leaves the caller's round_keys buffer unmodified.
;
; AES-NI semantics (Intel SDM):
;   aesdec     xmm1, m128:   xmm1 = InvMixColumns(InvSubBytes(
;                                     InvShiftRows(xmm1))) XOR m128
;   aesdeclast xmm1, m128:   xmm1 = InvSubBytes(InvShiftRows(xmm1))
;                                   XOR m128
; InvMixColumns is linear, so InvMC(state) XOR InvMC(rk) ==
;   InvMC(state XOR rk); passing InvMC(rk[r]) to aesdec yields
; the equivalent-inverse-cipher round exactly.
; ===============================================================
align 16
aes128_decrypt_block:
    movdqu      xmm0, [rsi]
    movdqu      xmm1, [rdi + 160]
    pxor        xmm0, xmm1                       ; state ^= rk[10]

    movdqu      xmm1, [rdi + 144]
    aesimc      xmm1, xmm1                       ; dw[9]
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi + 128]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi + 112]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi +  96]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi +  80]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi +  64]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi +  48]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi +  32]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi +  16]
    aesimc      xmm1, xmm1
    aesdec      xmm0, xmm1

    movdqu      xmm1, [rdi +   0]
    aesdeclast  xmm0, xmm1

    movdqu      [rdx], xmm0
    ret

section .note.GNU-stack noalloc noexec nowrite progbits
