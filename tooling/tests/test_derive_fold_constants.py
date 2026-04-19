# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Ed Hodapp
"""Pytest coverage for tooling/crypto_tests/derive_fold_constants.py.

The script lives in tooling/crypto_tests/ (alongside the C driver it
feeds) rather than under tooling/src/. It is not pip-installed and
not on the pytest `pythonpath`; we import it by file location so the
test reaches every branch directly. Every public and private
callable is exercised; the `if __name__ == '__main__':` block is
excluded from coverage via pyproject's `exclude_lines`.

The self-test's length-sweep overlaps the script's internal sweep by
design — the script's `run_self_test` is the one that ships with the
asm-side regeneration flow, so we keep an independent parametrized
sweep here that fails a named test node on any length (not just
"mismatches > 0"). Both are cheap and complementary.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import zlib


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPO_ROOT / "tooling" / "crypto_tests" / "derive_fold_constants.py"
)


def _load_under_test() -> ModuleType:
    """Import the script by file path (it's not on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "dfc_under_test", SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dfc = _load_under_test()


# -- GF(2)[x] primitives --------------------------------------------------


class TestGf2Mul:
    """gf2_mul: polynomial multiply over GF(2)."""

    def test_b_is_zero_skips_loop(self) -> None:
        assert dfc.gf2_mul(0xDEAD, 0) == 0

    def test_a_is_zero(self) -> None:
        assert dfc.gf2_mul(0, 0xBEEF) == 0

    def test_identity(self) -> None:
        assert dfc.gf2_mul(0xCAFE, 1) == 0xCAFE

    def test_x_times_x_is_x_squared(self) -> None:
        assert dfc.gf2_mul(0b10, 0b10) == 0b100

    def test_x_plus_one_squared_has_cross_terms_vanish(self) -> None:
        # (x+1)^2 = x^2 + 1 over GF(2) — the 2x term disappears.
        assert dfc.gf2_mul(0b11, 0b11) == 0b101


class TestGf2Mod:
    """gf2_mod: polynomial remainder over GF(2)[x]."""

    def test_smaller_degree_passes_through(self) -> None:
        assert dfc.gf2_mod(0x12345, 0x104C11DB7) == 0x12345

    def test_equal_to_p_reduces_to_zero(self) -> None:
        assert dfc.gf2_mod(0x104C11DB7, 0x104C11DB7) == 0

    def test_x_to_32_mod_p(self) -> None:
        # x^32 XOR P = 0x04C11DB7 (the low 32 bits of the generator).
        assert dfc.gf2_mod(0x100000000, 0x104C11DB7) == 0x04C11DB7


class TestGf2MulMod:
    """gf2_mul_mod: (a * b) mod p in one call."""

    def test_composition(self) -> None:
        # x^16 * x^16 = x^32; then mod P = 0x04C11DB7.
        assert dfc.gf2_mul_mod(
            1 << 16, 1 << 16, 0x104C11DB7,
        ) == 0x04C11DB7


class TestXPowerModP:
    """x_power_mod_p: x^k mod P via square-and-multiply."""

    def test_k_zero_is_one(self) -> None:
        assert dfc.x_power_mod_p(0) == 1

    def test_k_one_is_x(self) -> None:
        assert dfc.x_power_mod_p(1) == 2

    def test_k_thirty_two(self) -> None:
        assert dfc.x_power_mod_p(32) == 0x04C11DB7

    def test_k_128_matches_existing_fold_by_1_constant(self) -> None:
        # rev32(x^128 mod P) << 1 must equal 0x140D44A2E (K_lo33 in
        # the shipped asm).
        x128 = dfc.x_power_mod_p(128)
        assert (dfc.reflect32(x128) << 1) == 0x140D44A2E

    def test_k_192_matches_existing_fold_by_1_constant(self) -> None:
        # rev32(x^192 mod P) << 1 must equal 0x065673B46 (K_hi33 in
        # the shipped asm).
        x192 = dfc.x_power_mod_p(192)
        assert (dfc.reflect32(x192) << 1) == 0x065673B46


class TestReflect32:
    """reflect32: bit-reverse the low 32 bits."""

    @pytest.mark.parametrize("value,expected", [
        (0, 0),
        (1, 1 << 31),
        (0x80000000, 1),
        (0xFFFFFFFF, 0xFFFFFFFF),
        (0x12345678, 0x1E6A2C48),
    ])
    def test_values(self, value: int, expected: int) -> None:
        assert dfc.reflect32(value) == expected


class TestDerivePair:
    """derive_pair: produce a FoldPair for the given fold factor."""

    def test_fold_by_1_matches_existing_asm(self) -> None:
        pair = dfc.derive_pair(1)
        assert pair.fold_n == 1
        assert pair.exp_hi == 192
        assert pair.exp_lo == 128
        assert pair.k_hi == 0x065673B46
        assert pair.k_lo == 0x140D44A2E

    def test_packed_128_layout(self) -> None:
        pair = dfc.derive_pair(1)
        packed = pair.packed_128()
        # Low 64 = K_hi, high 64 = K_lo (per the asm xmm layout).
        assert (packed & ((1 << 64) - 1)) == 0x065673B46
        assert ((packed >> 64) & ((1 << 64) - 1)) == 0x140D44A2E

    def test_fold_by_4_main_constants(self) -> None:
        pair = dfc.derive_pair(4)
        assert pair.exp_hi == 576
        assert pair.exp_lo == 512
        assert pair.k_hi == 0x653D9822
        assert pair.k_lo == 0x111A288CE


# -- Byte-at-a-time primitives -------------------------------------------


class TestCrc32ByteUpdate:
    """crc32_byte_update: one-byte reflected CRC-32 fold step."""

    def test_zero_into_zero_stays_zero(self) -> None:
        # Exercises the crc&1==0 branch for all 8 iterations.
        assert dfc.crc32_byte_update(0, 0) == 0

    def test_first_byte_matches_slicing_table(self) -> None:
        # Value pinned to crc32_table_slice8[T[0]][1] in the shipped
        # asm. Exercises the crc&1==1 branch.
        assert dfc.crc32_byte_update(0, 0x01) == 0x77073096


class TestCrc32Reduce128:
    """crc32_reduce_128: 128-bit state + tail → 32-bit CRC (no final XOR)."""

    def test_zero_state_empty_tail(self) -> None:
        assert dfc.crc32_reduce_128(0, b"") == 0

    def test_zero_state_one_byte_tail(self) -> None:
        # Sixteen zero bytes then one 0x01 byte, init=0.
        expected = 0
        for _ in range(16):
            expected = dfc.crc32_byte_update(expected, 0)
        expected = dfc.crc32_byte_update(expected, 0x01)
        assert dfc.crc32_reduce_128(0, b"\x01") == expected


class TestCrc32Bytewise:
    """crc32_bytewise: end-to-end byte-at-a-time reference."""

    def test_empty_returns_zero(self) -> None:
        assert dfc.crc32_bytewise(b"") == 0

    @pytest.mark.parametrize("data", [
        b"a",
        b"abc",
        b"\x00" * 16,
        b"\xFF" * 64,
        bytes(range(100)),
    ])
    def test_matches_zlib(self, data: bytes) -> None:
        assert dfc.crc32_bytewise(data) == zlib.crc32(data)


# -- Fold-by-N engine: length branches + boundary sweep ------------------


class TestFoldByNDispatch:
    """Covers the length-class branches in crc32_fold_by_n."""

    def test_shorter_than_16_uses_bytewise(self) -> None:
        data = b"hello"
        assert dfc.crc32_fold_by_n(data, 4) == zlib.crc32(data)

    def test_mid_length_uses_fold_by_1_fallback(self) -> None:
        # 16 bytes: >= 16, < 4*16 → fold-by-1 fallback.
        data = b"abcdefghijklmnop"
        assert dfc.crc32_fold_by_n(data, 4) == zlib.crc32(data)

    def test_at_exact_fold_boundary_no_main_iter(self) -> None:
        # 4*16 = 64 → main engine with zero main-loop iterations.
        data = bytes(range(64))
        assert dfc.crc32_fold_by_n(data, 4) == zlib.crc32(data)

    def test_single_main_iter_zero_tail(self) -> None:
        # 128 bytes → one main iter, zero-byte tail.
        data = bytes(range(128))
        assert dfc.crc32_fold_by_n(data, 4) == zlib.crc32(data)

    def test_main_iter_plus_odd_tail(self) -> None:
        # 128+7 bytes → one main iter plus 7-byte tail.
        data = bytes(range(135))
        assert dfc.crc32_fold_by_n(data, 4) == zlib.crc32(data)


LENGTHS_SWEEP = [
    0, 1, 15, 16, 17, 31, 32, 48, 63, 64, 65, 95, 96, 127, 128, 129,
    200, 255, 256, 257, 511, 512, 1023, 1024, 4096, 8192,
]


@pytest.mark.parametrize("fold_n", [1, 2, 3, 4])
@pytest.mark.parametrize("length", LENGTHS_SWEEP)
@pytest.mark.parametrize("pattern", ["zeros", "ones", "mixed"])
def test_fold_by_n_matches_zlib(
    fold_n: int, length: int, pattern: str,
) -> None:
    if pattern == "zeros":
        data = b"\x00" * length
    elif pattern == "ones":
        data = b"\xFF" * length
    else:
        data = bytes((i * 37 + 13) & 0xFF for i in range(length))
    assert dfc.crc32_fold_by_n(data, fold_n) == zlib.crc32(data)


# -- Self-test runner ---------------------------------------------------


class TestRunSelfTest:
    """run_self_test: verbose/quiet paths and mismatch accounting."""

    def test_passes_cleanly_quiet(self) -> None:
        assert dfc.run_self_test(4, verbose=False) == 0

    def test_verbose_prints_per_case_lines(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert dfc.run_self_test(1, verbose=True) == 0
        assert "ok" in capsys.readouterr().out

    def test_mismatch_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(dfc, "crc32_fold_by_n", lambda _d, _n: 0)
        count = dfc.run_self_test(1, verbose=False)
        assert count > 0
        assert "FAIL" in capsys.readouterr().err


# -- NASM emission format -----------------------------------------------


class TestEmission:
    """format_emission: NASM-ready `dq` block for the main-loop pair."""

    def test_header_and_label_present(self) -> None:
        out = dfc.format_emission(4)
        assert "DO NOT EDIT BY HAND" in out
        assert "crc32_pclmul_fold_by_4_constants" in out

    def test_fold_by_2_label(self) -> None:
        assert "crc32_pclmul_fold_by_2_constants" in (
            dfc.format_emission(2)
        )

    def test_hex_literals_roundtrip_to_pair_values(self) -> None:
        pair = dfc.derive_pair(4)
        literals = re.findall(r"0x[0-9A-F]{16}", dfc.format_emission(4))
        assert len(literals) == 2
        assert int(literals[0], 16) == pair.k_hi
        assert int(literals[1], 16) == pair.k_lo


# -- main() direct invocation (covers both success and failure paths) ----


class TestMainFunction:
    """main(): argparse dispatch and success/failure return codes."""

    def test_emit_path_returns_zero(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert dfc.main(["--emit", "--fold-n", "1"]) == 0
        assert "crc32_pclmul_fold_by_1_constants" in (
            capsys.readouterr().out
        )

    def test_verify_path_returns_zero(self) -> None:
        assert dfc.main(["--fold-n", "1"]) == 0

    def test_verify_nonzero_on_forced_mismatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            dfc, "crc32_fold_by_n", lambda _d, _n: 0xDEADBEEF,
        )
        assert dfc.main(["--fold-n", "1"]) == 1
        assert "FAIL:" in capsys.readouterr().err

    def test_emit_blocked_by_forced_mismatch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # --emit must run the self-test first; a broken derivation
        # must exit non-zero with no NASM block on stdout, so bad
        # constants can't be piped to a file silently.
        monkeypatch.setattr(
            dfc, "crc32_fold_by_n", lambda _d, _n: 0xDEADBEEF,
        )
        assert dfc.main(["--emit", "--fold-n", "1"]) == 1
        captured = capsys.readouterr()
        assert "FAIL:" in captured.err
        assert "crc32_pclmul_fold_by_" not in captured.out


# -- CLI via subprocess -------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        capture_output=True, text=True, timeout=60, check=False,
    )


class TestCli:
    """End-to-end invocation via subprocess."""

    def test_default_exits_zero(self) -> None:
        result = _run_cli()
        assert result.returncode == 0, result.stderr
        assert "PASS" in result.stdout

    def test_emit_prints_nasm_block(self) -> None:
        result = _run_cli("--emit", "--fold-n", "4")
        assert result.returncode == 0
        assert "crc32_pclmul_fold_by_4_constants" in result.stdout
