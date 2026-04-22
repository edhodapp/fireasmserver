; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (c) 2026 Ed Hodapp
;
; SHA-256 per FIPS 180-4.
;
; Contract (SysV AMD64):
;   rdi = const void *data
;   rsi = size_t      len
;   rdx = uint8_t    *digest   ; 32 bytes, big-endian per FIPS 180-4
;
; Paths:
;   - sha256_shani     Intel SHA extensions. Two rounds per
;                      sha256rnds2; message schedule folded inline
;                      with sha256msg1/sha256msg2. Requires
;                      CPUID.(EAX=07H,ECX=0):EBX[bit 29] = 1.
;   - sha256_scalar    Plain-GPR 64-round fallback. Correct on any
;                      x86_64 CPU; constant-time by construction
;                      (no data-dependent branches or memory access).
;   - sha256           Dispatcher. One-time CPUID resolver caches
;                      the chosen implementation in sha256_ptr;
;                      subsequent calls are a single indirect jump.
;   - sha256_has_shani Public CPUID probe for test drivers.
;
; SHA-256 has no secret-dependent branches or data-dependent indexed
; memory accesses; both paths therefore run in data-independent
; time. No T-tables are used; round constants K[t] are read in a
; fixed sequence.
;
; Padding (FIPS 180-4 §5.1.1): append 0x80, zero-pad, append the
; 64-bit big-endian total bit length. Output digest bytes are
; big-endian (FIPS 180-4 §6.2.2 final state; matches RFC 6234 §8.1
; printout and TLS 1.3 / HKDF consumer expectation).
;
; References:
;   FIPS PUB 180-4, "Secure Hash Standard," August 2015.
;       §4.1.2 (Ch, Maj, Σ0, Σ1, σ0, σ1), §4.2.2 (K[0..63]),
;       §5.1.1 (padding), §5.3.3 (initial hash values H[0..7]),
;       §6.2.2 (hash computation, round function).
;   RFC 6234, "US Secure Hash Algorithms," May 2011, §8.2 (test
;       vectors).
;   Intel SDM Vol. 2 §3.2 "CPUID" (leaf 7 feature flags);
;       Intel SDM Vol. 2 SHA-NI instruction reference
;       (SHA256RNDS2, SHA256MSG1, SHA256MSG2).
;   S. Gulley, V. Gopal et al., "Intel SHA Extensions: New
;       Instructions Supporting the Secure Hash Algorithm on
;       Intel Architecture Processors," Intel white paper, 2013.

default rel

; ---------------------------------------------------------------
; Constants
; ---------------------------------------------------------------
section .rodata.sha256_consts
align 64
global sha256_k
sha256_k:
    dd 0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5
    dd 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5
    dd 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3
    dd 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174
    dd 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc
    dd 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da
    dd 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7
    dd 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967
    dd 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13
    dd 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85
    dd 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3
    dd 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070
    dd 0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5
    dd 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3
    dd 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208
    dd 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2

; Initial hash values H[0..7] — FIPS 180-4 §5.3.3.
global sha256_h0
sha256_h0:
    dd 0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a
    dd 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19

; SHA-NI byte-reverse mask: converts each 32-bit little-endian lane
; loaded from memory into big-endian form (SHA-256 interprets each
; 4-byte chunk big-endian).
align 16
sha256_bswap_mask:
    dq 0x0405060700010203, 0x0c0d0e0f08090a0b

; ---------------------------------------------------------------
; Text
; ---------------------------------------------------------------
section .text

global sha256
global sha256_shani
global sha256_scalar
global sha256_has_shani

; ===============================================================
; sha256_has_shani
;   Returns 1 if CPU supports SHA-NI, else 0. Leaf-7 existence is
;   gated on CPUID leaf 0 EAX >= 7 so parts predating leaf 7 do
;   not read stale register state. SHA-NI bit is EBX[29] of
;   leaf 7 subleaf 0 per Intel SDM Vol. 2 §3.2.
; ===============================================================
align 16
sha256_has_shani:
    push    rbx
    xor     eax, eax
    cpuid
    cmp     eax, 7
    jb      .no_shani
    mov     eax, 7
    xor     ecx, ecx
    cpuid
    xor     eax, eax
    bt      ebx, 29
    setc    al
    pop     rbx
    ret
.no_shani:
    xor     eax, eax
    pop     rbx
    ret

; ===============================================================
; sha256_pad_tail (internal helper)
;   Builds the padded final block(s) in a caller-provided 128-byte
;   scratch buffer per FIPS 180-4 §5.1.1.
;
; Args:
;   rdi = const uint8_t *residue       (0..63 bytes of trailing input)
;   rsi = size_t         residue_len   (0..63)
;   rdx = uint64_t       total_bytes
;   rcx = uint8_t       *scratch128    (16-byte aligned)
; Returns:
;   eax = 1 or 2 (number of 64-byte blocks written into scratch)
;
; Clobbers: eax, r8, r10, r11, rcx, xmm0, rbx (saved).
; Constant-time is not required for SHA-256 padding.
; ===============================================================
align 16
sha256_pad_tail:
    push    rbx
    mov     rbx, rsi                    ; residue_len
    mov     r8, rcx                     ; scratch base
    pxor    xmm0, xmm0
    movdqa  [r8 +  0], xmm0
    movdqa  [r8 + 16], xmm0
    movdqa  [r8 + 32], xmm0
    movdqa  [r8 + 48], xmm0
    movdqa  [r8 + 64], xmm0
    movdqa  [r8 + 80], xmm0
    movdqa  [r8 + 96], xmm0
    movdqa  [r8 +112], xmm0
    xor     ecx, ecx
.copy:
    cmp     rcx, rbx
    jae     .copy_done
    mov     al, [rdi + rcx]
    mov     [r8 + rcx], al
    inc     rcx
    jmp     .copy
.copy_done:
    mov     byte [r8 + rbx], 0x80
    mov     eax, 1
    cmp     rbx, 56
    jb      .one_block
    mov     eax, 2
.one_block:
    ; Length field lives in the last 8 bytes of the last block.
    ; 1-block tail ⇒ offset 56; 2-block tail ⇒ offset 120.
    mov     r10, rdx
    shl     r10, 3                      ; bytes → bits
    bswap   r10
    mov     r11d, eax
    shl     r11d, 6
    sub     r11d, 8
    mov     [r8 + r11], r10
    pop     rbx
    ret

; ===============================================================
; sha256_shani
;
;   Shuffling convention (Gulley/Gopal whitepaper; verified by lane
;   walkthrough from the pshufd + palignr sequence that produces it):
;     STATE0 (ABEF): lane 0=F, lane 1=E, lane 2=B, lane 3=A
;                    (bits [31:0]=F, [63:32]=E, [95:64]=B, [127:96]=A)
;     STATE1 (CDGH): lane 0=H, lane 1=G, lane 2=D, lane 3=C
;                    (bits [31:0]=H, [63:32]=G, [95:64]=D, [127:96]=C)
;
;   Frame: 128 B pad scratch at [rsp], 16-byte aligned.
; ===============================================================
align 16
sha256_shani:
    push    rbp
    mov     rbp, rsp
    push    rbx
    push    r12
    push    r13
    push    r14
    sub     rsp, 144
    and     rsp, -16

    ; State pins (across calls to sha256_pad_tail and
    ; _sha256_shani_compress, both of which may clobber r8..r11):
    ;   rbx = data ptr (advances through block loop)
    ;   r12 = remaining len (decremented through block loop)
    ;   r13 = digest ptr (used at end to store 32 B BE result)
    ;   r14 = total length in bytes (passed into pad_tail)
    mov     rbx, rdi
    mov     r12, rsi
    mov     r13, rdx
    mov     r14, rsi

    ; Load + shuffle initial state.
    movdqu  xmm0, [rel sha256_h0]
    movdqu  xmm2, [rel sha256_h0 + 16]
    pshufd  xmm0, xmm0, 0xb1
    pshufd  xmm2, xmm2, 0x1b
    movdqa  xmm1, xmm0
    palignr xmm1, xmm2, 8               ; STATE0 (ABEF)
    pblendw xmm2, xmm0, 0xf0            ; STATE1 (CDGH)

    movdqa  xmm7, [rel sha256_bswap_mask]

.blk:
    cmp     r12, 64
    jb      .blk_done
    mov     rax, rbx
    call    _sha256_shani_compress
    add     rbx, 64
    sub     r12, 64
    jmp     .blk
.blk_done:

    ; Pad tail. pad_tail may clobber r8..r11; our state lives in
    ; rbx/r12-r14 which are preserved across the call.
    mov     rdi, rbx
    mov     rsi, r12
    mov     rdx, r14
    mov     rcx, rsp
    call    sha256_pad_tail
    mov     ecx, eax
    mov     rbx, rsp                    ; scratch walk ptr (rbx is safe
                                        ; now — data ptr no longer needed)
.pad:
    mov     rax, rbx
    call    _sha256_shani_compress
    add     rbx, 64
    dec     ecx
    jnz     .pad

    ; Un-shuffle state and store digest big-endian. xmm7 (BE mask)
    ; survives the pad_tail/compress calls because pad_tail only
    ; touches xmm0 and compress does not write xmm7.
    pshufd  xmm0, xmm1, 0x1b
    pshufd  xmm2, xmm2, 0xb1
    movdqa  xmm3, xmm0
    pblendw xmm3, xmm2, 0xf0            ; {D,C,B,A} (lanes MSB→LSB)
    palignr xmm2, xmm0, 8               ; {H,G,F,E}
    pshufb  xmm3, xmm7                  ; BE-swap each dword
    pshufb  xmm2, xmm7
    movdqu  [r13 +  0], xmm3
    movdqu  [r13 + 16], xmm2

    lea     rsp, [rbp - 32]
    pop     r14
    pop     r13
    pop     r12
    pop     rbx
    pop     rbp
    ret

; Inner SHA-NI compress. One block at rax; updates STATE0=xmm1,
; STATE1=xmm2. Uses xmm0, xmm3..xmm6, xmm8..xmm10 as scratch and
; reads xmm7 (bswap mask). Does not touch any GPRs beyond what
; sha256rnds2/msg1/msg2 internally require.
align 16
_sha256_shani_compress:
    movdqa  xmm8, xmm1                  ; ABEF_SAVE
    movdqa  xmm9, xmm2                  ; CDGH_SAVE

    movdqu  xmm3, [rax +  0]
    pshufb  xmm3, xmm7                  ; MSG0
    movdqu  xmm4, [rax + 16]
    pshufb  xmm4, xmm7                  ; MSG1
    movdqu  xmm5, [rax + 32]
    pshufb  xmm5, xmm7                  ; MSG2
    movdqu  xmm6, [rax + 48]
    pshufb  xmm6, xmm7                  ; MSG3

    ; Rounds 0-3
    movdqa      xmm0, xmm3
    paddd       xmm0, [rel sha256_k + 0*16]
    sha256rnds2 xmm2, xmm1
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2

    ; Rounds 4-7
    movdqa      xmm0, xmm4
    paddd       xmm0, [rel sha256_k + 1*16]
    sha256rnds2 xmm2, xmm1
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm3, xmm4

    ; Rounds 8-11
    movdqa      xmm0, xmm5
    paddd       xmm0, [rel sha256_k + 2*16]
    sha256rnds2 xmm2, xmm1
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm4, xmm5

    ; Rounds 12-15
    movdqa      xmm0, xmm6
    paddd       xmm0, [rel sha256_k + 3*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm6
    palignr     xmm10, xmm5, 4
    paddd       xmm3, xmm10
    sha256msg2  xmm3, xmm6
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm5, xmm6

    ; Rounds 16-19
    movdqa      xmm0, xmm3
    paddd       xmm0, [rel sha256_k + 4*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm3
    palignr     xmm10, xmm6, 4
    paddd       xmm4, xmm10
    sha256msg2  xmm4, xmm3
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm6, xmm3

    ; Rounds 20-23
    movdqa      xmm0, xmm4
    paddd       xmm0, [rel sha256_k + 5*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm4
    palignr     xmm10, xmm3, 4
    paddd       xmm5, xmm10
    sha256msg2  xmm5, xmm4
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm3, xmm4

    ; Rounds 24-27
    movdqa      xmm0, xmm5
    paddd       xmm0, [rel sha256_k + 6*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm5
    palignr     xmm10, xmm4, 4
    paddd       xmm6, xmm10
    sha256msg2  xmm6, xmm5
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm4, xmm5

    ; Rounds 28-31
    movdqa      xmm0, xmm6
    paddd       xmm0, [rel sha256_k + 7*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm6
    palignr     xmm10, xmm5, 4
    paddd       xmm3, xmm10
    sha256msg2  xmm3, xmm6
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm5, xmm6

    ; Rounds 32-35
    movdqa      xmm0, xmm3
    paddd       xmm0, [rel sha256_k + 8*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm3
    palignr     xmm10, xmm6, 4
    paddd       xmm4, xmm10
    sha256msg2  xmm4, xmm3
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm6, xmm3

    ; Rounds 36-39
    movdqa      xmm0, xmm4
    paddd       xmm0, [rel sha256_k + 9*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm4
    palignr     xmm10, xmm3, 4
    paddd       xmm5, xmm10
    sha256msg2  xmm5, xmm4
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm3, xmm4

    ; Rounds 40-43
    movdqa      xmm0, xmm5
    paddd       xmm0, [rel sha256_k + 10*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm5
    palignr     xmm10, xmm4, 4
    paddd       xmm6, xmm10
    sha256msg2  xmm6, xmm5
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm4, xmm5

    ; Rounds 44-47
    movdqa      xmm0, xmm6
    paddd       xmm0, [rel sha256_k + 11*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm6
    palignr     xmm10, xmm5, 4
    paddd       xmm3, xmm10
    sha256msg2  xmm3, xmm6
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm5, xmm6

    ; Rounds 48-51
    ; (sha256msg1 xmm6, xmm3) is required here so xmm6 carries the
    ; σ0(W[45..48]) contribution that rounds 56-59's sha256msg2 needs
    ; to compute W[60..63]. Omitting it silently produces a wrong
    ; digest only on inputs whose compression reaches rounds 60-63
    ; (i.e., every real input, since every block goes through all 64
    ; rounds). A previous version of this file dropped the msg1 in
    ; the last four groups thinking the schedule was "complete"; it
    ; was not — the σ0 plumbing still has to land.
    movdqa      xmm0, xmm3
    paddd       xmm0, [rel sha256_k + 12*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm3
    palignr     xmm10, xmm6, 4
    paddd       xmm4, xmm10
    sha256msg2  xmm4, xmm3
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm6, xmm3

    ; Rounds 52-55
    movdqa      xmm0, xmm4
    paddd       xmm0, [rel sha256_k + 13*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm4
    palignr     xmm10, xmm3, 4
    paddd       xmm5, xmm10
    sha256msg2  xmm5, xmm4
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm3, xmm4

    ; Rounds 56-59
    movdqa      xmm0, xmm5
    paddd       xmm0, [rel sha256_k + 14*16]
    sha256rnds2 xmm2, xmm1
    movdqa      xmm10, xmm5
    palignr     xmm10, xmm4, 4
    paddd       xmm6, xmm10
    sha256msg2  xmm6, xmm5
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm4, xmm5

    ; Rounds 60-63
    ; The sha256msg1 xmm5, xmm6 here would compute σ0 for a
    ; hypothetical W[72..75] that is never referenced; kept for
    ; uniformity with the whitepaper macro and so a reader does not
    ; wonder whether it was omitted by mistake again.
    movdqa      xmm0, xmm6
    paddd       xmm0, [rel sha256_k + 15*16]
    sha256rnds2 xmm2, xmm1
    pshufd      xmm0, xmm0, 0x0e
    sha256rnds2 xmm1, xmm2
    sha256msg1  xmm5, xmm6

    paddd       xmm1, xmm8              ; feed-forward
    paddd       xmm2, xmm9
    ret

; ===============================================================
; sha256_scalar
;
;   Plain 64-round GPR implementation.
;
;   Register conventions inside _sha256_scalar_compress:
;     r8d  r9d  r10d r11d r12d r13d r14d r15d = a b c d e f g h
;     eax  = (W[t]+K[t]) then T1
;     ebx  = Σ* accumulator, then T2
;     edi  = third scratch (repurposed after message schedule)
;     ebp  = loop counter (saved / restored as callee-saved)
;     rdx  = W base pointer
;     rcx  = K base pointer
;     rsi  = &H buffer
;
;   Outer frame:
;     [rsp +   0 .. rsp + 255]   W[0..63]    256 B
;     [rsp + 256 .. rsp + 287]   H[0..7]      32 B
;     [rsp + 288 .. rsp + 415]   pad scratch 128 B
;   Rounded up + 16 B align slack = 432 B total.
; ===============================================================
%define SHA_FRAME   432
%define OFF_W         0
%define OFF_H       256
%define OFF_PAD     288

align 16
sha256_scalar:
    push    rbp
    mov     rbp, rsp
    push    rbx
    push    r12
    push    r13
    push    r14
    push    r15
    sub     rsp, SHA_FRAME
    and     rsp, -16

    mov     r12, rdi                    ; data (advances)
    mov     r13, rsi                    ; remaining len
    mov     r14, rdx                    ; digest
    mov     r15, rsi                    ; total length

    ; Initialize local H[] from H0 constants.
    lea     rdi, [rel sha256_h0]
    lea     rsi, [rsp + OFF_H]
    mov     ecx, 8
.init_H:
    mov     eax, [rdi]
    mov     [rsi], eax
    add     rdi, 4
    add     rsi, 4
    dec     ecx
    jnz     .init_H

.blk:
    cmp     r13, 64
    jb      .blk_done
    mov     rdi, r12                    ; block
    lea     rsi, [rsp + OFF_H]          ; &H
    lea     rdx, [rsp + OFF_W]          ; &W
    lea     rcx, [rel sha256_k]         ; &K
    call    _sha256_scalar_compress
    add     r12, 64
    sub     r13, 64
    jmp     .blk
.blk_done:

    ; Pad tail.
    mov     rdi, r12
    mov     rsi, r13
    mov     rdx, r15
    lea     rcx, [rsp + OFF_PAD]
    call    sha256_pad_tail
    mov     ebx, eax
    lea     r12, [rsp + OFF_PAD]
.pad:
    mov     rdi, r12
    lea     rsi, [rsp + OFF_H]
    lea     rdx, [rsp + OFF_W]
    lea     rcx, [rel sha256_k]
    call    _sha256_scalar_compress
    add     r12, 64
    dec     ebx
    jnz     .pad

    ; Emit digest big-endian.
    lea     rsi, [rsp + OFF_H]
    mov     rdi, r14
    mov     ecx, 8
.emit:
    mov     eax, [rsi]
    bswap   eax
    mov     [rdi], eax
    add     rsi, 4
    add     rdi, 4
    dec     ecx
    jnz     .emit

    lea     rsp, [rbp - 40]
    pop     r15
    pop     r14
    pop     r13
    pop     r12
    pop     rbx
    pop     rbp
    ret

; Inner scalar compress.
;   rdi = block, rsi = &H[8], rdx = &W[64], rcx = &K
;   Saves rbx, rbp, r12-r15 (5 callee-saved registers used as state).
align 16
_sha256_scalar_compress:
    push    rbx
    push    rbp
    push    r12
    push    r13
    push    r14
    push    r15

    ; Build W[0..15]: big-endian loads of 16 dwords from block.
    xor     ebp, ebp
.ws_lo:
    mov     eax, [rdi + rbp*4]
    bswap   eax
    mov     [rdx + rbp*4], eax
    inc     ebp
    cmp     ebp, 16
    jb      .ws_lo

    ; Build W[16..63].
    ;   σ0(x) = ROR(x,7)  ^ ROR(x,18) ^ SHR(x,3)
    ;   σ1(x) = ROR(x,17) ^ ROR(x,19) ^ SHR(x,10)
    mov     ebp, 16
.ws_hi:
    ; σ1(W[t-2])
    mov     eax, [rdx + rbp*4 -  8]
    mov     edi, eax
    ror     eax, 17
    mov     r8d, edi
    ror     r8d, 19
    xor     eax, r8d
    shr     edi, 10
    xor     eax, edi                    ; eax = σ1(W[t-2])
    add     eax, [rdx + rbp*4 - 28]     ; + W[t-7]
    ; σ0(W[t-15])
    mov     edi, [rdx + rbp*4 - 60]
    mov     r8d, edi
    ror     r8d, 7
    mov     r9d, edi
    ror     r9d, 18
    xor     r8d, r9d
    shr     edi, 3
    xor     r8d, edi                    ; r8d = σ0
    add     eax, r8d
    add     eax, [rdx + rbp*4 - 64]     ; + W[t-16]
    mov     [rdx + rbp*4], eax
    inc     ebp
    cmp     ebp, 64
    jb      .ws_hi

    ; Load H[] into working state a..h (r8d..r15d).
    mov     r8d,  [rsi +  0]
    mov     r9d,  [rsi +  4]
    mov     r10d, [rsi +  8]
    mov     r11d, [rsi + 12]
    mov     r12d, [rsi + 16]
    mov     r13d, [rsi + 20]
    mov     r14d, [rsi + 24]
    mov     r15d, [rsi + 28]

    ; 64 rounds. Counter in ebp.
    xor     ebp, ebp
.round:
    mov     eax, [rdx + rbp*4]          ; W[t]
    add     eax, [rcx + rbp*4]          ; + K[t]    ; eax = W+K

    ; Σ1(e), e = r12d.
    mov     ebx, r12d
    ror     ebx, 6
    mov     edi, r12d
    ror     edi, 11
    xor     ebx, edi
    mov     edi, r12d
    ror     edi, 25
    xor     ebx, edi                    ; ebx = Σ1(e)
    ; Ch(e,f,g) = g ^ (e & (f ^ g)).
    mov     edi, r13d                   ; f
    xor     edi, r14d                   ; f^g
    and     edi, r12d                   ; e & (f^g)
    xor     edi, r14d                   ; edi = Ch
    ; T1 = h + Σ1 + Ch + (W+K).
    add     eax, r15d                   ; + h  (h consumed; r15d now free
                                        ;       until the rotation step)
    add     eax, ebx
    add     eax, edi                    ; eax = T1

    ; Σ0(a), a = r8d.
    mov     ebx, r8d
    ror     ebx, 2
    mov     edi, r8d
    ror     edi, 13
    xor     ebx, edi
    mov     edi, r8d
    ror     edi, 22
    xor     ebx, edi                    ; ebx = Σ0(a)
    ; Maj(a,b,c) = (a & b) | (c & (a | b)).
    mov     edi, r8d
    or      edi, r9d                    ; a | b
    and     edi, r10d                   ; c & (a|b)
    mov     r15d, r8d                   ; (r15d reused as one-shot scratch;
    and     r15d, r9d                   ;  reassigned below as new h)
    or      edi, r15d                   ; Maj
    add     ebx, edi                    ; ebx = T2

    ; State rotation.
    mov     r15d, r14d                  ; h = g
    mov     r14d, r13d                  ; g = f
    mov     r13d, r12d                  ; f = e
    add     r11d, eax                   ; d + T1
    mov     r12d, r11d                  ; e = d + T1
    mov     r11d, r10d                  ; d = c
    mov     r10d, r9d                   ; c = b
    mov     r9d,  r8d                   ; b = a
    add     ebx, eax                    ; T1 + T2
    mov     r8d,  ebx                   ; a = T1 + T2

    inc     ebp
    cmp     ebp, 64
    jb      .round

    ; Accumulate back into H[].
    add     [rsi +  0], r8d
    add     [rsi +  4], r9d
    add     [rsi +  8], r10d
    add     [rsi + 12], r11d
    add     [rsi + 16], r12d
    add     [rsi + 20], r13d
    add     [rsi + 24], r14d
    add     [rsi + 28], r15d

    pop     r15
    pop     r14
    pop     r13
    pop     r12
    pop     rbp
    pop     rbx
    ret

; ===============================================================
; sha256 (dispatcher)
;
;   First call lands on sha256_resolve via the pointer cache; the
;   resolver overwrites the cache with the resolved target and
;   tail-jumps to it. Subsequent calls are a single indirect jump.
;   (Mirrors the crc32_ieee_802_3 dispatcher in this directory.)
; ===============================================================
align 16
sha256:
    jmp     [sha256_ptr]

align 16
sha256_resolve:
    push    rbx
    push    rdi
    push    rsi
    push    rdx
    xor     eax, eax
    cpuid
    cmp     eax, 7
    jb      .use_scalar
    mov     eax, 7
    xor     ecx, ecx
    cpuid
    bt      ebx, 29
    jnc     .use_scalar
    lea     rax, [sha256_shani]
    jmp     .install
.use_scalar:
    lea     rax, [sha256_scalar]
.install:
    mov     [sha256_ptr], rax
    pop     rdx
    pop     rsi
    pop     rdi
    pop     rbx
    jmp     rax

section .data
align 8
sha256_ptr:
    dq sha256_resolve

section .note.GNU-stack noalloc noexec nowrite progbits
