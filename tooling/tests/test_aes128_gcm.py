"""Integration tests for the AES-128-GCM (NIST SP 800-38D) assembly modules.

These wrap the C test driver in ``tooling/crypto_tests/``. Each test
invokes ``make`` to (re)build the arch's driver binary, then runs
the binary and asserts a zero exit code. The driver prints per-path
pass/fail lines to stdout; we echo its output on failure.

Cells
-----
- ``test-x86_64-aes128-gcm-native``     native host run; on a laptop
  without AES-NI or PCLMULQDQ the driver skips cleanly (the AES-NI
  path SIGILLs on first aesenc otherwise; AES-NI covers the block
  cipher, PCLMULQDQ covers the GCM hash — D057's extended posture
  requires both).
- ``test-x86_64-aes128-gcm-pclmul``     runs the driver under the
  fork-qemu ``-cpu Denverton``, which advertises both
  ``CPUID.(EAX=1):ECX[bit 1]`` (PCLMULQDQ) and bit 25 (AES-NI).
- ``test-x86_64-aes128-gcm-pclmul-max`` same driver under
  ``-cpu max`` as a second CPU-model cell.
- ``test-aarch64-aes128-gcm``           cross-built aarch64 driver
  under ``qemu-aarch64-static``; FEAT_AES / PMULL are in the D034
  baseline and do not gate at runtime.

If either toolchain is missing (e.g. a CI runner without the
cross-compiler, or the QEMU fork not yet built), the affected test
skips with a descriptive message rather than failing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CRYPTO_TESTS_DIR = REPO_ROOT / "tooling" / "crypto_tests"

QEMU_FORK = Path(os.environ.get(
    "QEMU_X86_FORK",
    str(Path.home() / "opt" / "qemu-fork" / "bin" / "qemu-x86_64"),
))

NATIVE_X86_TOOLS = ("nasm", "x86_64-linux-gnu-gcc")
PCLMUL_CELL_TOOLS = ("nasm", "x86_64-linux-gnu-gcc", str(QEMU_FORK))
AARCH64_TOOLS = (
    "aarch64-linux-gnu-as",
    "aarch64-linux-gnu-gcc",
    "qemu-aarch64-static",
)


def _tool_present(tool: str) -> bool:
    if os.path.isabs(tool):
        return Path(tool).is_file() and os.access(tool, os.X_OK)
    return shutil.which(tool) is not None


def _skip_if_missing(tools: tuple[str, ...]) -> None:
    missing = [t for t in tools if not _tool_present(t)]
    if missing:
        pytest.skip(f"toolchain incomplete: missing {missing}")


def _run_driver(make_target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "-s", make_target],
        cwd=str(CRYPTO_TESTS_DIR),
        capture_output=True,
        text=True,
        errors="backslashreplace",
        timeout=120,
        check=False,
    )


def _assert_pass(
    result: subprocess.CompletedProcess[str],
    label: str,
) -> None:
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} driver failed (exit={result.returncode}):\n{output}"
    )
    ok_tokens = (
        "PASS  all AES-128-GCM checks passed",
        "PASS  (reference self-check ok; asm path skipped)",
    )
    assert any(token in output for token in ok_tokens), output


def test_aes128_gcm_x86_64_native() -> None:
    _skip_if_missing(NATIVE_X86_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-aes128-gcm-native"),
        "x86_64 native",
    )


def test_aes128_gcm_x86_64_pclmul_denverton() -> None:
    _skip_if_missing(PCLMUL_CELL_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-aes128-gcm-pclmul"),
        "x86_64 fork-qemu -cpu Denverton",
    )


def test_aes128_gcm_x86_64_pclmul_max() -> None:
    _skip_if_missing(PCLMUL_CELL_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-aes128-gcm-pclmul-max"),
        "x86_64 fork-qemu -cpu max",
    )


def test_aes128_gcm_aarch64() -> None:
    _skip_if_missing(AARCH64_TOOLS)
    _assert_pass(_run_driver("test-aarch64-aes128-gcm"), "aarch64")
