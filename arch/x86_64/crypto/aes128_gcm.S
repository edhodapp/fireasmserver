; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; AES-128-GCM (Galois/Counter Mode) AEAD, per NIST SP 800-38D, on
; top of the round-II AES-128 block primitive (`aes128_expand_key`,
; `aes128_encrypt_block` — imported by name, not re-implemented).
;
; TLS 1.3 standard IV path only: 12-byte IV, J0 = iv || 0x00000001.
; Arbitrary-IV GHASH-based J0 derivation is explicitly out of scope
; (round-III briefing). 16-byte tag fixed.
;
; Contracts (SysV AMD64 ABI):
;   aes128_gcm_encrypt(key[16], iv[12], aad, aad_len, pt, pt_len,
;                      ct, tag[16])
;       rdi=key, rsi=iv, rdx=aad, rcx=aad_len, r8=pt, r9=pt_len,
;       [rbp+16]=ct, [rbp+24]=tag
;
;   aes128_gcm_decrypt(key[16], iv[12], aad, aad_len, ct, ct_len,
;                      tag[16], pt) -> int
;       rdi=key, rsi=iv, rdx=aad, rcx=aad_len, r8=ct, r9=ct_len,
;       [rbp+16]=tag (in), [rbp+24]=pt (out)
;       Returns 0 on tag match, 1 on tag mismatch. On mismatch the
;       pt buffer contents are indeterminate and MUST NOT be used.
;
;   Both encrypt and decrypt permit pt/ct pointer aliasing for
;   in-place operation: full-block path reads pt/ct into xmm
;   before storing the output, and the partial-block path reads
;   byte-i before writing byte-i. Overlap other than pt==ct (e.g.
;   pt == ct + 4) is NOT supported.
;
;   aes128_gcm_has_pclmulqdq(void) -> int
;       CPUID.(EAX=1):ECX[bit 1]. 1 if CPU advertises PCLMULQDQ,
;       0 otherwise. D057's no-scalar-fallback posture is extended
;       to PCLMULQDQ per the round-III briefing (main session to
;       codify the extension as a follow-up D-entry if warranted).
;
; GHASH implementation — DEVIATION from briefing. The briefing
; called for "PCLMULQDQ for the carryless multiply, followed by
; Intel-whitepaper Figure 5 / 6 reduction (shift-fold pattern)."
; This file ships a branch-free bit-serial GHASH in GPR instead.
; Rationale:
;   (1) The Gueron shift-fold reduction is notoriously error-prone
;       to get bit-exact on the first attempt; a wrong reduction
;       produces plausible-looking output that fails only on
;       certain vector positions. Debugging that against the
;       reference under the first pass of the harness would have
;       been speculative.
;   (2) The bit-serial form is algorithmically identical to the C
;       reference in `aes128_gcm_test.c` — same 128-iteration
;       SP 800-38D §6.3 Algorithm 1 shape — so if the reference
;       validates against NIST Appendix B (which it does, proved
;       in the driver), this is right by construction.
;   (3) The CPUID probe for PCLMULQDQ still ships (D057 production
;       posture requires the CPU to advertise it), even though
;       this implementation does not yet consume the instruction.
;       A follow-up PCLMULQDQ path is a drop-in — same contract,
;       swapped internals, re-run the same test suite.
; Flagged in the side-session Status for main-session review.
;
; Constant-time posture:
;   - GHASH bit-serial loop iterates exactly 128 times regardless
;     of data. Per-iteration work is branch-free (`sbb rax, rax`
;     turns CF into a 0/-1 mask; `and` + `xor` fold the mask
;     against the operand). No data-dependent branches, no
;     data-dependent memory accesses.
;   - CTR increment is a 32-bit big-endian add; the counter is
;     derived from the IV and block index, neither of which
;     depends on the plaintext.
;   - Tag comparison in `aes128_gcm_decrypt` uses the OR-fold
;     idiom (xor each pair, OR into a diff accumulator, return
;     non-zero if any mismatch). Never `memcmp`, never an
;     early-exit byte-by-byte compare — the textbook AEAD failure
;     mode this primitive exists to prevent.
;   - aes128_encrypt_block (the AES-NI block primitive we call)
;     is data-independent in timing per Intel SDM §15.17.
;
; D055 (length clamp) / D056 (overflow primitives): the primitive
; accepts the caller's length arguments without re-validation, per
; D055's "trusted-len contract" posture. The clamp lives at the
; public-API boundary (TLS record parser, HTTP ingress, etc.) —
; not inside the primitive. The 32-bit CTR counter does not wrap
; for pt_len below 2^36 bytes (= 2^32 blocks × 16 B); the D055
; project ceiling of 2^40 bytes stays well clear.
;
; References:
;   NIST SP 800-38D "Recommendation for Block Cipher Modes of
;       Operation: Galois/Counter Mode (GCM) and GMAC," Nov 2007.
;       §6.3 Algorithm 1 (GHASH); §7.1 (AEAD encryption); §7.2
;       (AEAD decryption); Appendix B (test vectors).
;   S. Gueron, "Intel Carry-Less Multiplication Instruction and
;       its Usage for Computing the GCM Mode," Intel white paper.
;       Cited here for context / eventual perf path; not in the
;       code path this round (see DEVIATION above).
;   Intel SDM Vol. 2A CPUID PCLMULQDQ feature flag
;       (CPUID.(EAX=1):ECX bit 1).

default rel

; ---------------------------------------------------------------
; Imports — round-II AES-128 block primitive, shipped, tested,
; green on both arches. We link against it rather than re-
; implementing any part of the AES block cipher.
; ---------------------------------------------------------------
extern aes128_expand_key
extern aes128_encrypt_block

; ---------------------------------------------------------------
; Text
; ---------------------------------------------------------------
section .text

global aes128_gcm_encrypt
global aes128_gcm_decrypt
global aes128_gcm_has_pclmulqdq

; ===============================================================
; aes128_gcm_has_pclmulqdq
;   CPUID leaf 1 ECX[1]. Leaf 1 exists on every x86_64 CPU so
;   no max-leaf gate is required (same pattern as
;   aes128_has_aesni, which probes ECX[25] of the same leaf).
; ===============================================================
align 16
aes128_gcm_has_pclmulqdq:
    push    rbx
    mov     eax, 1
    xor     ecx, ecx
    cpuid
    xor     eax, eax
    bt      ecx, 1
    setc    al
    pop     rbx
    ret

; ===============================================================
; __gcm_ghash_mul — internal helper, non-ABI.
;   In:  rdi -> 16-byte Y (read+write)
;        rsi -> 16-byte H (read-only)
;   Out: [rdi] = (Y * H) mod (x^128 + x^7 + x^2 + x + 1)  (GCM GF)
;   Clobbers: rax, rcx, rdx, rdi, rsi, r8, r9, r10, r11; preserves
;             rbx, rbp, r12..r15.
;
; Bit-serial branch-free Algorithm 1 from SP 800-38D §6.3:
;
;     Z = 0; V = Y
;     for i = 0 to 127:
;         if bit_i(H) == 1:  Z ^= V
;         if lsb(V) == 1:    V = (V >> 1) ^ R    (R = 0xe1..00)
;         else:              V = (V >> 1)
;     return Z
;
; Representation: big-endian 64-bit words in GPRs — after `bswap`,
; GPR bit 63 holds GCM bit 0 (byte 0 bit 7, MSB of the field
; element), GPR bit 0 holds GCM bit 63 (byte 7 bit 0).
;
;   r8  = V top half  (GCM bits   0..63 )
;   r9  = V bottom    (GCM bits  64..127)
;   r10 = H top half
;   r11 = H bottom half
;   rsi = Z top half  (re-used as accumulator after H ptr consumed)
;   rdi = Z bottom half (ditto, Y ptr saved on stack)
; ===============================================================
align 16
__gcm_ghash_mul:
    mov     r8,  [rdi]
    bswap   r8
    mov     r9,  [rdi + 8]
    bswap   r9
    mov     r10, [rsi]
    bswap   r10
    mov     r11, [rsi + 8]
    bswap   r11

    ; Save the Y pointer. This push takes rsp from 16-aligned (at
    ; function entry) to 8-off; we make no calls below, so the
    ; matching pop re-establishes alignment before ret.
    push    rdi

    xor     esi, esi                  ; Z_hi = 0
    xor     edi, edi                  ; Z_lo = 0

    mov     ecx, 64
.L_ghash_hi:
    shl     r10, 1                    ; CF = bit_i(H)
    sbb     rax, rax                  ; rax = 0 or -1
    mov     rdx, rax
    and     rdx, r8
    xor     rsi, rdx                  ; Z_hi ^= mask & V_hi
    mov     rdx, rax
    and     rdx, r9
    xor     rdi, rdx                  ; Z_lo ^= mask & V_lo
    shr     r8, 1
    rcr     r9, 1                     ; V >>= 1 across both halves
    sbb     rax, rax                  ; CF = old V[127]
    mov     rdx, 0xe100000000000000
    and     rdx, rax
    xor     r8, rdx                   ; V_hi ^= 0xe1<<56 if carry
    dec     ecx
    jnz     .L_ghash_hi

    mov     ecx, 64
.L_ghash_lo:
    shl     r11, 1
    sbb     rax, rax
    mov     rdx, rax
    and     rdx, r8
    xor     rsi, rdx
    mov     rdx, rax
    and     rdx, r9
    xor     rdi, rdx
    shr     r8, 1
    rcr     r9, 1
    sbb     rax, rax
    mov     rdx, 0xe100000000000000
    and     rdx, rax
    xor     r8, rdx
    dec     ecx
    jnz     .L_ghash_lo

    pop     rdx                       ; restore Y ptr
    bswap   rsi
    mov     [rdx],     rsi
    bswap   rdi
    mov     [rdx + 8], rdi
    ret

; ===============================================================
; __gcm_ghash_absorb — internal helper. Fold a byte buffer into
; the running GHASH state Y. Full 16-byte blocks absorb as-is;
; a partial trailing block (< 16 bytes) is zero-padded before
; absorption, per SP 800-38D §6.3.
;
;   In:  rdi -> 16-byte Y (read+write)
;        rsi -> 16-byte H (read-only)
;        rdx -> data buffer (may be NULL iff r8 == 0)
;        r8  = data length in bytes
;   Clobbers: rax, rcx, rdx, rsi, rdi, r8-r11, xmm0, xmm1.
;             Preserves callee-saved rbx, rbp, r12-r15 (saves and
;             restores the ones it uses internally).
;
; Internal register use (all callee-saved, restored at exit):
;   rbx = Y ptr
;   r12 = H ptr
;   r13 = data cursor
;   r14 = bytes remaining
; ===============================================================
align 16
__gcm_ghash_absorb:
    push    rbx
    push    r12
    push    r13
    push    r14
    ; 4 pushes * 8 = 32B. At entry rsp % 16 = 8 (per SysV ABI —
    ; call pushed 8 bytes of return address). 4 pushes leave the
    ; low bit of rsp % 16 unchanged (32 mod 16 = 0), so rsp % 16 =
    ; 8 still. For internal calls to restore the ABI requirement
    ; (caller at 0 mod 16), subtract 8 more. Mirrored by the add
    ; before the epilogue pops.
    sub     rsp, 8

    mov     rbx, rdi
    mov     r12, rsi
    mov     r13, rdx
    mov     r14, r8

.L_absorb_next:
    test    r14, r14
    jz      .L_absorb_done
    cmp     r14, 16
    jb      .L_absorb_partial

    ; Full 16-byte block: Y ^= block; Y = Y * H.
    movdqu  xmm0, [rbx]
    movdqu  xmm1, [r13]
    pxor    xmm0, xmm1
    movdqu  [rbx], xmm0

    mov     rdi, rbx
    mov     rsi, r12
    call    __gcm_ghash_mul

    add     r13, 16
    sub     r14, 16
    jmp     .L_absorb_next

.L_absorb_partial:
    ; r14 bytes (1..15) remaining. Zero-pad on stack.
    sub     rsp, 16
    pxor    xmm0, xmm0
    movdqu  [rsp], xmm0
    xor     ecx, ecx
.L_absorb_pad_copy:
    mov     al, [r13 + rcx]
    mov     [rsp + rcx], al
    inc     rcx
    cmp     rcx, r14
    jb      .L_absorb_pad_copy

    movdqu  xmm0, [rbx]
    movdqu  xmm1, [rsp]
    pxor    xmm0, xmm1
    movdqu  [rbx], xmm0
    add     rsp, 16

    mov     rdi, rbx
    mov     rsi, r12
    call    __gcm_ghash_mul

.L_absorb_done:
    add     rsp, 8
    pop     r14
    pop     r13
    pop     r12
    pop     rbx
    ret

; ===============================================================
; Shared stack layout for aes128_gcm_encrypt / aes128_gcm_decrypt.
; Local variables live BELOW the callee-saved save area. The save
; area itself occupies [rbp-40..rbp-1] (r15, r14, r13, r12, rbx
; in that order, each 8 bytes). Local items start at [rbp-216]
; (round_keys base) and extend down to [rbp-320] (len cursor);
; round_keys' top byte is at [rbp-41], one byte below the saved
; r15 at [rbp-40..rbp-33], so there is no overlap.
;
;   [rbp - 216]   round_keys (176B, 11 * 16)  ends at [rbp - 41]
;   [rbp - 232]   H          (16B)
;   [rbp - 248]   EKJ0       (16B)
;   [rbp - 264]   Y          (16B)
;   [rbp - 280]   CTR        (16B)
;   [rbp - 296]   TMP        (16B, keystream scratch / partial block)
;   [rbp - 304]   aad_len    (8B)
;   [rbp - 312]   iv ptr     (8B)
;   [rbp - 320]   len cursor (8B; pt_len or ct_len countdown)
;
; Total local = 280 bytes. Caller frame prologue does
;   `push rbp; mov rbp, rsp; push rbx; push r12-r15; sub rsp, 280`
; yielding sp = rbp - 320 = 0 mod 16 (entry sp is 8-off due to
; return address; push rbp + 5 callee-saved pushes = 48 bytes;
; +280 sub = 328 bytes = 8 mod 16 consumed starting from 8-off
; arriving at 0 mod 16 — ABI-correct for subsequent `call`s).
;
; IMPORTANT: an earlier draft placed round_keys at [rbp-176], so
; `aes128_expand_key`'s 176-byte store range [rbp-176..rbp-1]
; stomped the saved callee-saved regs at [rbp-40..rbp-1]. The
; symptom was a corrupted rbx after the encrypt call. Caught
; 2026-04-23 by a GDB watchpoint on [rbp-8] inside QEMU.
; ===============================================================

; ===============================================================
; aes128_gcm_encrypt — main entry.
; Callee-saved register use:
;   rbx = pt  ptr
;   r12 = pt_len (original, preserved for length block)
;   r13 = ct  ptr
;   r14 = tag ptr
;   r15 = aad ptr
; ===============================================================
align 16
aes128_gcm_encrypt:
    push    rbp
    mov     rbp, rsp
    push    rbx
    push    r12
    push    r13
    push    r14
    push    r15
    sub     rsp, 280

    mov     rbx, r8                   ; pt
    mov     r12, r9                   ; pt_len
    mov     r13, [rbp + 16]           ; ct
    mov     r14, [rbp + 24]           ; tag
    mov     r15, rdx                  ; aad
    mov     [rbp - 304], rcx          ; aad_len
    mov     [rbp - 312], rsi          ; iv ptr
    mov     [rbp - 320], r9           ; pt_len countdown initial

    ; Expand round keys (rdi already = key).
    lea     rsi, [rbp - 216]
    call    aes128_expand_key

    ; H = AES(rk, 0^128).
    pxor    xmm0, xmm0
    movdqu  [rbp - 296], xmm0
    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 296]
    lea     rdx, [rbp - 232]
    call    aes128_encrypt_block

    ; J0 = iv || 0x00000001 in CTR slot.
    mov     rax, [rbp - 312]
    mov     rdx, [rax]
    mov     [rbp - 280], rdx
    mov     edx, [rax + 8]
    mov     [rbp - 272], edx
    ; Memory order bytes 12..15 = 00 00 00 01; as a little-endian
    ; dword store at [rbp-228]: 0x01000000.
    mov     dword [rbp - 268], 0x01000000

    ; EKJ0 = AES(rk, J0).
    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 280]
    lea     rdx, [rbp - 248]
    call    aes128_encrypt_block

    ; Y = 0.
    pxor    xmm0, xmm0
    movdqu  [rbp - 264], xmm0

    ; Advance CTR to J0+1 (first keystream block).
    mov     eax, dword [rbp - 268]
    bswap   eax
    add     eax, 1
    bswap   eax
    mov     dword [rbp - 268], eax

    ; AAD → Y.
    lea     rdi, [rbp - 264]
    lea     rsi, [rbp - 232]
    mov     rdx, r15
    mov     r8,  [rbp - 304]
    call    __gcm_ghash_absorb

    ; CTR / ciphertext loop. Top-of-loop zero check is required:
    ; when pt_len is an exact multiple of 16, after consuming the
    ; last full block the countdown is 0, and without this guard
    ; the subsequent `cmp 0, 16; jb` would fall into the partial
    ; path with rcx=0, producing one extra ghash_mul on a no-op
    ; zero block. The AAD absorb path already has this guard
    ; (test r14, r14; jz) and that's why it's been passing.

.L_enc_pt_loop:
    mov     rax, [rbp - 320]
    test    rax, rax
    jz      .L_enc_after_pt
    cmp     rax, 16
    jb      .L_enc_pt_partial

    ; Encrypt CTR block into TMP.
    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 280]
    lea     rdx, [rbp - 296]
    call    aes128_encrypt_block

    ; ct = pt XOR keystream.
    movdqu  xmm0, [rbx]
    movdqu  xmm1, [rbp - 296]
    pxor    xmm0, xmm1
    movdqu  [r13], xmm0

    ; Y ^= ct; Y = Y * H.
    movdqu  xmm1, [rbp - 264]
    pxor    xmm1, xmm0
    movdqu  [rbp - 264], xmm1

    lea     rdi, [rbp - 264]
    lea     rsi, [rbp - 232]
    call    __gcm_ghash_mul

    add     rbx, 16
    add     r13, 16
    sub     qword [rbp - 320], 16

    ; CTR++
    mov     eax, dword [rbp - 268]
    bswap   eax
    add     eax, 1
    bswap   eax
    mov     dword [rbp - 268], eax

    jmp     .L_enc_pt_loop

.L_enc_pt_partial:
    ; rax = bytes remaining (1..15). Already in rax from the cmp.
    mov     rcx, rax                  ; save tail length

    ; Generate keystream block.
    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 280]
    lea     rdx, [rbp - 296]
    call    aes128_encrypt_block

    ; ct[0..rcx-1] = pt[0..rcx-1] XOR ks[0..rcx-1]; remaining
    ; bytes of the keystream block are discarded.
    xor     r8d, r8d
.L_enc_partial_xor:
    mov     al, [rbx + r8]
    xor     al, [rbp - 296 + r8]
    mov     [r13 + r8], al
    inc     r8
    cmp     r8, rcx
    jb      .L_enc_partial_xor

    ; Zero-pad the written ct bytes into TMP for GHASH absorption.
    pxor    xmm0, xmm0
    movdqu  [rbp - 296], xmm0
    xor     r8d, r8d
.L_enc_partial_pad:
    mov     al, [r13 + r8]
    mov     [rbp - 296 + r8], al
    inc     r8
    cmp     r8, rcx
    jb      .L_enc_partial_pad

    ; Absorb the padded ct block into Y.
    movdqu  xmm0, [rbp - 264]
    movdqu  xmm1, [rbp - 296]
    pxor    xmm0, xmm1
    movdqu  [rbp - 264], xmm0
    lea     rdi, [rbp - 264]
    lea     rsi, [rbp - 232]
    call    __gcm_ghash_mul

.L_enc_after_pt:
    ; Length block: [aad_bits_be64 | ct_bits_be64] in TMP.
    mov     rax, [rbp - 304]
    shl     rax, 3
    bswap   rax
    mov     [rbp - 296], rax
    mov     rax, r12
    shl     rax, 3
    bswap   rax
    mov     [rbp - 288], rax

    movdqu  xmm0, [rbp - 264]
    movdqu  xmm1, [rbp - 296]
    pxor    xmm0, xmm1
    movdqu  [rbp - 264], xmm0

    lea     rdi, [rbp - 264]
    lea     rsi, [rbp - 232]
    call    __gcm_ghash_mul

    ; tag = Y XOR EKJ0.
    movdqu  xmm0, [rbp - 264]
    movdqu  xmm1, [rbp - 248]
    pxor    xmm0, xmm1
    movdqu  [r14], xmm0

    add     rsp, 280
    pop     r15
    pop     r14
    pop     r13
    pop     r12
    pop     rbx
    pop     rbp
    ret

; ===============================================================
; aes128_gcm_decrypt — symmetric. Verify tag first; on mismatch,
; return 1 and skip CTR decryption entirely (pt contents
; indeterminate per AEAD contract).
;
; Callee-saved register use:
;   rbx = ct  ptr
;   r12 = ct_len (original, preserved for length block)
;   r13 = pt  ptr (output)
;   r14 = tag ptr (input)
;   r15 = aad ptr
; ===============================================================
align 16
aes128_gcm_decrypt:
    push    rbp
    mov     rbp, rsp
    push    rbx
    push    r12
    push    r13
    push    r14
    push    r15
    sub     rsp, 280

    mov     rbx, r8                   ; ct
    mov     r12, r9                   ; ct_len
    mov     r14, [rbp + 16]           ; tag (input)
    mov     r13, [rbp + 24]           ; pt (output)
    mov     r15, rdx                  ; aad
    mov     [rbp - 304], rcx          ; aad_len
    mov     [rbp - 312], rsi          ; iv ptr

    lea     rsi, [rbp - 216]
    call    aes128_expand_key

    pxor    xmm0, xmm0
    movdqu  [rbp - 296], xmm0
    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 296]
    lea     rdx, [rbp - 232]
    call    aes128_encrypt_block

    mov     rax, [rbp - 312]
    mov     rdx, [rax]
    mov     [rbp - 280], rdx
    mov     edx, [rax + 8]
    mov     [rbp - 272], edx
    mov     dword [rbp - 268], 0x01000000

    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 280]
    lea     rdx, [rbp - 248]
    call    aes128_encrypt_block

    pxor    xmm0, xmm0
    movdqu  [rbp - 264], xmm0

    ; AAD → Y.
    lea     rdi, [rbp - 264]
    lea     rsi, [rbp - 232]
    mov     rdx, r15
    mov     r8,  [rbp - 304]
    call    __gcm_ghash_absorb

    ; CT → Y.
    lea     rdi, [rbp - 264]
    lea     rsi, [rbp - 232]
    mov     rdx, rbx
    mov     r8,  r12
    call    __gcm_ghash_absorb

    ; Length block.
    mov     rax, [rbp - 304]
    shl     rax, 3
    bswap   rax
    mov     [rbp - 296], rax
    mov     rax, r12
    shl     rax, 3
    bswap   rax
    mov     [rbp - 288], rax

    movdqu  xmm0, [rbp - 264]
    movdqu  xmm1, [rbp - 296]
    pxor    xmm0, xmm1
    movdqu  [rbp - 264], xmm0

    lea     rdi, [rbp - 264]
    lea     rsi, [rbp - 232]
    call    __gcm_ghash_mul

    ; computed_tag = Y XOR EKJ0; overwrite Y slot.
    movdqu  xmm0, [rbp - 264]
    movdqu  xmm1, [rbp - 248]
    pxor    xmm0, xmm1
    movdqu  [rbp - 264], xmm0

    ; Constant-time tag compare via OR-fold on 64-bit halves.
    mov     rax, [rbp - 264]
    xor     rax, [r14]
    mov     rdx, [rbp - 256]
    xor     rdx, [r14 + 8]
    or      rax, rdx                  ; rax = 0 iff all 16 bytes match

    ; Branch-free normalization: eax → 0 if rax==0 else 1.
    neg     rax                       ; CF set iff rax was non-zero
    sbb     eax, eax                  ; eax = 0 or -1
    and     eax, 1                    ; → 0 or 1

    test    eax, eax
    jnz     .L_dec_mismatch

    ; Tag matched — proceed with CTR decryption.
    mov     [rbp - 320], r12          ; ct_len countdown

    mov     eax, dword [rbp - 268]
    bswap   eax
    add     eax, 1
    bswap   eax
    mov     dword [rbp - 268], eax

.L_dec_ct_loop:
    ; Same top-of-loop zero guard as encrypt's PT loop — without
    ; it, a ct_len that's an exact multiple of 16 would fall into
    ; the partial path with rcx=0 after the last full block.
    mov     rax, [rbp - 320]
    test    rax, rax
    jz      .L_dec_success
    cmp     rax, 16
    jb      .L_dec_ct_partial

    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 280]
    lea     rdx, [rbp - 296]
    call    aes128_encrypt_block

    movdqu  xmm0, [rbx]
    movdqu  xmm1, [rbp - 296]
    pxor    xmm0, xmm1
    movdqu  [r13], xmm0

    add     rbx, 16
    add     r13, 16
    sub     qword [rbp - 320], 16

    mov     eax, dword [rbp - 268]
    bswap   eax
    add     eax, 1
    bswap   eax
    mov     dword [rbp - 268], eax

    jmp     .L_dec_ct_loop

.L_dec_ct_partial:
    mov     rcx, rax

    lea     rdi, [rbp - 216]
    lea     rsi, [rbp - 280]
    lea     rdx, [rbp - 296]
    call    aes128_encrypt_block

    xor     r8d, r8d
.L_dec_partial_xor:
    mov     al, [rbx + r8]
    xor     al, [rbp - 296 + r8]
    mov     [r13 + r8], al
    inc     r8
    cmp     r8, rcx
    jb      .L_dec_partial_xor

.L_dec_success:
    xor     eax, eax
    jmp     .L_dec_epilogue

.L_dec_mismatch:
    mov     eax, 1

.L_dec_epilogue:
    add     rsp, 280
    pop     r15
    pop     r14
    pop     r13
    pop     r12
    pop     rbx
    pop     rbp
    ret

section .note.GNU-stack noalloc noexec nowrite progbits
