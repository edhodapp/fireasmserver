"""Integration tests for the CRC-32 IEEE 802.3 assembly modules.

These wrap the C test driver in tooling/crypto_tests/. Each test
invokes ``make`` to (re)build its arch's binary, then runs the binary
and asserts a zero exit code. The driver prints per-vector pass/fail
lines to stdout; we echo its output on failure.

The aarch64 binary is cross-built and runs under ``qemu-aarch64-static``
per the crypto_tests Makefile. If either toolchain is missing (e.g.
a CI runner without the cross-compiler), the test skips with a
descriptive message rather than failing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CRYPTO_TESTS_DIR = REPO_ROOT / "tooling" / "crypto_tests"

# Tools required per arch. If any are missing we skip rather than fail.
REQUIRED_TOOLS = {
    "x86_64": ("x86_64-linux-gnu-as", "x86_64-linux-gnu-gcc"),
    "aarch64": (
        "aarch64-linux-gnu-as",
        "aarch64-linux-gnu-gcc",
        "qemu-aarch64-static",
    ),
}


def _skip_if_missing(arch: str) -> None:
    missing = [t for t in REQUIRED_TOOLS[arch] if shutil.which(t) is None]
    if missing:
        pytest.skip(f"{arch} toolchain incomplete: missing {missing}")


def _run_driver(make_target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", make_target],
        cwd=str(CRYPTO_TESTS_DIR),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_crc32_ieee_802_3_vectors_pass(arch: str) -> None:
    _skip_if_missing(arch)
    target = f"test-{'x86' if arch == 'x86_64' else 'aarch64'}"
    result = _run_driver(target)
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{arch} CRC-32 driver failed (exit={result.returncode}):\n"
        f"{output}"
    )
    assert "PASS  all CRC-32 IEEE vectors match" in output, output
