"""Integration tests for the AES-128 (FIPS PUB 197) assembly modules.

These wrap the C test driver in ``tooling/crypto_tests/``. Each test
invokes ``make`` to (re)build the arch's driver binary, then runs the
binary and asserts a zero exit code. The driver prints per-path
pass/fail lines to stdout; we echo its output on failure.

Cells
-----
- ``test-x86_64-aes128-native``      native host run; on a laptop
  without AES-NI silicon the driver skips cleanly with a SKIP note
  (the primitive SIGILLs on the first aesenc without AES-NI; the
  driver probes CPUID first and bails before any asm runs).
- ``test-x86_64-aes128-aesni``       runs the driver under the
  fork-qemu ``-cpu Denverton``; that CPU model advertises
  ``CPUID.(EAX=1):ECX[bit 25]`` (AES-NI) cleanly. Exercises the full
  AES-NI path with all five named vectors + 8-block sweep.
- ``test-x86_64-aes128-aesni-max``   same driver under the fork-qemu
  ``-cpu max``. Second-cell belt-and-braces for CPU-model-specific
  regressions in the fork's AES-NI emulation.
- ``test-aarch64-aes128``            cross-built aarch64 driver under
  ``qemu-aarch64-static``; exercises the FEAT_AES path.

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
AESNI_CELL_TOOLS = ("nasm", "x86_64-linux-gnu-gcc", str(QEMU_FORK))
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
        timeout=60,
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
    # "PASS  all AES-128 checks passed" on a fully-exercised run;
    # on a no-AES-NI host the driver exits 0 with a SKIP note.
    ok_tokens = (
        "PASS  all AES-128 checks passed",
        "PASS  (reference self-check ok; asm path skipped)",
    )
    assert any(token in output for token in ok_tokens), output


def test_aes128_x86_64_native() -> None:
    _skip_if_missing(NATIVE_X86_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-aes128-native"),
        "x86_64 native",
    )


def test_aes128_x86_64_aesni_denverton() -> None:
    _skip_if_missing(AESNI_CELL_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-aes128-aesni"),
        "x86_64 fork-qemu -cpu Denverton",
    )


def test_aes128_x86_64_aesni_max() -> None:
    _skip_if_missing(AESNI_CELL_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-aes128-aesni-max"),
        "x86_64 fork-qemu -cpu max",
    )


def test_aes128_aarch64() -> None:
    _skip_if_missing(AARCH64_TOOLS)
    _assert_pass(_run_driver("test-aarch64-aes128"), "aarch64")
