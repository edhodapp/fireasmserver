#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Ed Hodapp
"""Derive fold-by-N multiplier constants for CRC-32 IEEE 802.3.

Ground truth for the PCLMULQDQ fold constants in
arch/x86_64/crypto/crc32_ieee.S. Emits NASM-ready `dq` literals
verified end-to-end against zlib.crc32 across a length sweep that
exercises every fold-chunk boundary (16 through 8192 bytes) with
three payload shapes (zeros, 0xFF, mixed pattern).

POLYNOMIAL FORM
---------------
P(x) = 0x104C11DB7 (IEEE 802.3 CRC-32 generator, unreflected, 33 bits)

Each stored constant is `rev32(x^k mod P) << 1` — a 33-bit value
in a 64-bit slot. The reflection matches the reflected CRC-32
convention; the `<< 1` absorbs the "pclmul result is one degree
short" quirk described in Intel's "Fast CRC Computation Using
PCLMULQDQ Instruction" white paper. The residual `x^32` alignment
is applied in the asm fold body as an explicit `pslldq xmm, 4`.

FOLD FORM (how constants map to operations)
-------------------------------------------
For a 128-bit xmm state held as state_lo (low 64) | state_hi
(high 64), one fold step computes:

    new_state = (pclmul(state_lo, K_hi) << 32)
              ^ (pclmul(state_hi, K_lo) << 32)
              ^ chunk                         [128-bit XOR]

with K_hi in the constant's low 64 and K_lo in the high 64. The
advance represented by this step is determined by the K pair:

    K_hi = rev32(x^(N*128 + 64) mod P) << 1
    K_lo = rev32(x^(N*128)       mod P) << 1

gives an N*128-bit advance. For fold-by-1 the pair is
(x^192, x^128); for fold-by-4 main loop, (x^576, x^512).

Per main-loop iteration, N parallel accumulators each advance by
N*128 bits. Because they started staggered at 16-byte positions
{16, 32, ..., N*16}, they remain staggered exactly 16 bytes apart
after the main loop — so the N-1 reduction steps are all fold-by-1
(the standard x^192/x^128 pair, which is already in the asm and
reused rather than re-emitted).

This means fold-by-4 requires only ONE new constants pair beyond
what the existing fold-by-1 code already has: the (x^576, x^512)
main-loop pair. The result is emitted; the existing fold-by-1 pair
is left untouched by this script. Deviation from the briefing's
hinted {512, 448, 384, 320, 192, 128} set is intentional: that set
assumes a different main-loop formulation where accumulators stay
co-positioned and the reduction chain is fold-by-(N-1), ...,
fold-by-1. Empirically both formulations compute the same CRC;
this one needs fewer constants.

USAGE
-----
    python3 derive_fold_constants.py             # verify, exit 0 on pass
    python3 derive_fold_constants.py --emit      # emit NASM block to stdout
    python3 derive_fold_constants.py --verbose   # chatty verify
    python3 derive_fold_constants.py --fold-n N  # verify up through fold-by-N
"""

from __future__ import annotations

import argparse
import sys
import zlib
from typing import Callable

from pydantic import BaseModel


# IEEE 802.3 CRC-32 generator, 33 bits, unreflected.
POLY_UNREFLECTED: int = 0x104C11DB7

# Reflected 32-bit polynomial mask for byte-at-a-time reference.
POLY_REFLECTED: int = 0xEDB88320

MASK64: int = (1 << 64) - 1
MASK128: int = (1 << 128) - 1
CRC_IV: int = 0xFFFFFFFF


class FoldPair(BaseModel):
    """One fold-by-N multiplier pair packed into a 128-bit xmm constant."""

    fold_n: int
    exp_hi: int  # exponent of the state_lo multiplier
    exp_lo: int  # exponent of the state_hi multiplier
    k_hi: int  # rev32(x^exp_hi mod P) << 1, at most 33 bits
    k_lo: int  # rev32(x^exp_lo mod P) << 1, at most 33 bits

    def packed_128(self) -> int:
        """Return the 128-bit xmm layout: low 64 = K_hi, high 64 = K_lo."""
        return (self.k_lo << 64) | self.k_hi


# -- GF(2)[x] arithmetic --------------------------------------------------


def gf2_mul(a: int, b: int) -> int:
    """Polynomial multiply over GF(2)."""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a <<= 1
        b >>= 1
    return result


def gf2_mod(a: int, p: int) -> int:
    """Polynomial mod over GF(2)[x]."""
    p_deg = p.bit_length() - 1
    while a.bit_length() - 1 >= p_deg:
        a ^= p << (a.bit_length() - 1 - p_deg)
    return a


def gf2_mul_mod(a: int, b: int, p: int) -> int:
    return gf2_mod(gf2_mul(a, b), p)


def x_power_mod_p(k: int) -> int:
    """x^k mod P via square-and-multiply. Returns at most 32 bits."""
    result = 1
    base = 2  # x^1
    while k:
        if k & 1:
            result = gf2_mul_mod(result, base, POLY_UNREFLECTED)
        base = gf2_mul_mod(base, base, POLY_UNREFLECTED)
        k >>= 1
    return result


def reflect32(x: int) -> int:
    """Bit-reverse the low 32 bits of x."""
    result = 0
    for i in range(32):
        if (x >> i) & 1:
            result |= 1 << (31 - i)
    return result


def derive_pair(fold_n: int) -> FoldPair:
    """Derive the fold-by-N K_hi/K_lo pair."""
    exp_hi = fold_n * 128 + 64
    exp_lo = fold_n * 128
    return FoldPair(
        fold_n=fold_n,
        exp_hi=exp_hi,
        exp_lo=exp_lo,
        k_hi=reflect32(x_power_mod_p(exp_hi)) << 1,
        k_lo=reflect32(x_power_mod_p(exp_lo)) << 1,
    )


# -- PCLMULQDQ simulation (matches the asm fold body exactly) -------------


def fold_step(state: int, const128: int, chunk: int) -> int:
    """One fold step, matching the asm `pclmul + pslldq 4 + xor` body.

    Names match FoldPair field roles (hi/lo = exponent tier, NOT xmm
    bit position): k_hi = x^(N*128+64) multiplier, held in the LOW 64
    bits of const128 by convention, multiplies state_lo.
    """
    state_lo = state & MASK64
    state_hi = (state >> 64) & MASK64
    k_hi = const128 & MASK64
    k_lo = (const128 >> 64) & MASK64
    p_lo = (gf2_mul(state_lo, k_hi) << 32) & MASK128
    p_hi = (gf2_mul(state_hi, k_lo) << 32) & MASK128
    return (p_lo ^ p_hi ^ chunk) & MASK128


# -- Byte-at-a-time reflected CRC-32 (reduction tail + short-input path) --


def crc32_byte_update(crc: int, byte: int) -> int:
    """Fold one byte into the reflected CRC (no init, no final XOR)."""
    crc ^= byte
    for _ in range(8):
        crc = (crc >> 1) ^ (POLY_REFLECTED if crc & 1 else 0)
    return crc


def crc32_reduce_128(state_128: int, tail: bytes) -> int:
    """Reduce a 128-bit state + tail bytes to a 32-bit CRC (no final XOR).

    Treats state_128 as 16 little-endian bytes (matches storing the xmm
    register to stack with `movdqu`), runs byte-at-a-time reflected CRC
    with init=0, then processes the tail with the accumulator preserved.
    Mirrors the asm's reduce path (two back-to-back `_crc32_update_slice8`
    calls on state bytes then tail bytes).
    """
    crc = 0
    for i in range(16):
        crc = crc32_byte_update(crc, (state_128 >> (i * 8)) & 0xFF)
    for byte in tail:
        crc = crc32_byte_update(crc, byte)
    return crc


def crc32_bytewise(data: bytes) -> int:
    """Full byte-at-a-time reflected CRC-32 with init + final XOR."""
    crc = CRC_IV
    for byte in data:
        crc = crc32_byte_update(crc, byte)
    return crc ^ CRC_IV


# -- Fold-by-N reference implementation -----------------------------------


def crc32_fold_by_n(data: bytes, fold_n: int) -> int:
    """Pure-Python fold-by-N CRC-32, matching the asm form.

    For inputs shorter than fold_n*16 bytes, falls back to fold-by-1 (if
    >= 16 bytes) or pure byte-at-a-time (if < 16). That mirrors how the
    asm dispatches short inputs to `crc32_ieee_802_3_slice8`.
    """
    if len(data) < fold_n * 16:
        if len(data) < 16:
            return crc32_bytewise(data)
        return _fold_engine(data, 1, derive_pair(1).packed_128())
    return _fold_engine(data, fold_n, derive_pair(fold_n).packed_128())


def _fold_engine(data: bytes, fold_n: int, main_const: int) -> int:
    """The fold-by-N engine, parametrized by main-loop advance constant."""
    states = _load_initial_states(data, fold_n)
    pos = _run_main_loop(data, fold_n, main_const, states)
    combined = _reduce_states(states)
    return crc32_reduce_128(combined, data[pos:]) ^ CRC_IV


def _load_initial_states(data: bytes, fold_n: int) -> list[int]:
    """Load fold_n * 16 bytes of data as fold_n accumulator states."""
    states = [
        int.from_bytes(data[i * 16:(i + 1) * 16], "little")
        for i in range(fold_n)
    ]
    states[0] ^= CRC_IV
    return states


def _run_main_loop(
    data: bytes,
    fold_n: int,
    main_const: int,
    states: list[int],
) -> int:
    """Main fold loop; mutates `states` in place; returns ending pos."""
    pos = fold_n * 16
    step = fold_n * 16
    while pos + step <= len(data):
        for i in range(fold_n):
            chunk = int.from_bytes(
                data[pos + i * 16:pos + (i + 1) * 16], "little",
            )
            states[i] = fold_step(states[i], main_const, chunk)
        pos += step
    return pos


def _reduce_states(states: list[int]) -> int:
    """Reduce N accumulators to one 128-bit state via N-1 fold-by-1 steps.

    After the main loop, state[i] sits at virtual position (i+1)*16
    relative to the final pos — they are exactly 16 bytes apart. Each
    reduction step advances the running combined state by 16 bytes and
    XORs in the next accumulator; the standard fold-by-1 constants
    (x^192/x^128 pair) are the advance factor.
    """
    fold_by_1 = derive_pair(1).packed_128()
    combined = states[0]
    for i in range(1, len(states)):
        combined = fold_step(combined, fold_by_1, states[i])
    return combined


# -- Self-test -------------------------------------------------------------


def _sweep_lengths() -> list[int]:
    """Lengths exercised by the self-test.

    Covers every length 0..256 (hits every residue class mod 16 plus
    every fold-chunk boundary up through fold-by-16), plus the
    fold-by-4 main-loop boundaries 512, 1024, 4096, 8192 with their
    off-by-one neighbors, plus larger scales to shake out any main-
    loop iteration-count miscount.
    """
    explicit = [
        63, 65, 127, 129, 255, 256, 257,
        511, 512, 513, 1023, 1024, 1025,
        2047, 2048, 4095, 4096, 4097, 8191, 8192,
    ]
    return sorted(set(list(range(257)) + explicit))


PatternFn = Callable[[int], bytes]

_PAYLOAD_PATTERNS: tuple[tuple[str, PatternFn], ...] = (
    ("zeros", lambda n: b"\x00" * n),
    ("ones", lambda n: b"\xFF" * n),
    ("mixed", lambda n: bytes((i * 37 + 13) & 0xFF for i in range(n))),
)


def run_self_test(fold_n: int, verbose: bool) -> int:
    """Run the boundary sweep against zlib.crc32. Returns mismatches."""
    mismatches = 0
    for length in _sweep_lengths():
        for name, fn in _PAYLOAD_PATTERNS:
            payload = fn(length)
            want = zlib.crc32(payload)
            got = crc32_fold_by_n(payload, fold_n)
            if got != want:
                print(
                    f"FAIL fold-{fold_n} {name:>5} len={length:>5} "
                    f"want=0x{want:08X} got=0x{got:08X}",
                    file=sys.stderr,
                )
                mismatches += 1
            elif verbose:
                print(
                    f"ok   fold-{fold_n} {name:>5} len={length:>5} "
                    f"crc=0x{got:08X}",
                )
    return mismatches


# -- NASM emission ---------------------------------------------------------


EMISSION_HEADER = """\
; SPDX-License-Identifier: AGPL-3.0-or-later
; Derived by tooling/crypto_tests/derive_fold_constants.py.
; DO NOT EDIT BY HAND — rerun the script to regenerate.
;
; Fold-by-N PCLMULQDQ multiplier constants for CRC-32 IEEE 802.3.
; Each pair packs two 33-bit values into a 128-bit xmm constant:
;   low  64 bits = multiplier for state_lo  (xmm bits [0..63])
;   high 64 bits = multiplier for state_hi  (xmm bits [64..127])
; Values are (rev32(x^k mod P(x)) << 1) where P(x) = 0x104C11DB7.
; The reduction chain after the main loop is N-1 fold-by-1 steps
; (the standard x^192/x^128 pair, declared elsewhere) — only the
; main-loop pair is emitted here.
"""


def emit_one_pair(pair: FoldPair, out: list[str]) -> None:
    """Emit a single `align 16` / label / two `dq` lines for one pair."""
    out.append("align 16")
    out.append(f"crc32_pclmul_fold_by_{pair.fold_n}_constants:")
    out.append(
        f"    dq 0x{pair.k_hi:016X}"
        f"          ; rev32(x^{pair.exp_hi} mod P) << 1"
        f"  — state_lo mult"
    )
    out.append(
        f"    dq 0x{pair.k_lo:016X}"
        f"          ; rev32(x^{pair.exp_lo} mod P) << 1"
        f"  — state_hi mult"
    )


def format_emission(fold_n: int) -> str:
    """Return the NASM constants block for the fold-by-N main loop."""
    lines: list[str] = [EMISSION_HEADER.rstrip(), ""]
    emit_one_pair(derive_pair(fold_n), lines)
    lines.append("")
    return "\n".join(lines)


# -- CLI -------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Derive and verify CRC-32 fold constants.",
    )
    parser.add_argument(
        "--emit", action="store_true",
        help="emit the NASM constants block for --fold-n to stdout",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="print per-case pass lines during self-test",
    )
    parser.add_argument(
        "--fold-n", type=int, default=4,
        help="fold factor to derive/verify (default: 4)",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    mismatches = 0
    for n in range(1, args.fold_n + 1):
        mismatches += run_self_test(n, args.verbose)
    if mismatches:
        print(f"FAIL: {mismatches} mismatch(es)", file=sys.stderr)
        return 1
    if args.emit:
        sys.stdout.write(format_emission(args.fold_n))
        return 0
    print("PASS: all fold-by-N variants match zlib.crc32")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
