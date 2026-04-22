"""Integration tests for the SHA-256 (FIPS 180-4) assembly modules.

These wrap the C test driver in ``tooling/crypto_tests/``. Each test
invokes ``make`` to (re)build the arch's driver binary, then runs the
binary and asserts a zero exit code. The driver prints per-path
pass/fail lines to stdout; we echo its output on failure.

Cells
-----
- ``test-x86_64-sha256-native``     native host run; on a laptop
  without SHA-NI silicon this exercises the scalar fallback path
  (the dispatcher picks it via CPUID).
- ``test-x86_64-sha256-shani``      runs the driver under the
  fork-qemu ``-cpu Denverton``; that CPU model advertises SHA-NI
  cleanly (D054 fork is required — stock Ubuntu Noble qemu 8.2.2
  does not TCG-emulate ``sha256rnds2``). Exercises both SHA-NI and
  scalar paths and the shani-vs-scalar cross-check.
- ``test-x86_64-sha256-shani-max``  same driver under the fork-qemu
  ``-cpu max``. Second-cell belt-and-braces to catch CPU-model-
  specific regressions in the fork's SHA-NI emulation.
- ``test-aarch64-sha256``           cross-built aarch64 driver under
  ``qemu-aarch64-static``; exercises the FEAT_SHA256 path.

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

# Tools required per cell. Missing tools cause a pytest.skip rather
# than a failure so CI cells with a reduced toolset still pass green
# for the cells they can run.
NATIVE_X86_TOOLS = ("nasm", "x86_64-linux-gnu-gcc")
SHANI_CELL_TOOLS = ("nasm", "x86_64-linux-gnu-gcc", str(QEMU_FORK))
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
    # timeout=60 — each driver completes well under a second natively,
    # and under qemu-user on a slow runner still under a few seconds;
    # 60 is conservative enough for an adverse runner yet tight enough
    # that a runaway loop in the asm cannot hang the suite.
    # errors="backslashreplace" — preserve any non-UTF-8 bytes the
    # driver or make emits (crash dumps, locale strings) rather than
    # masking a real failure with UnicodeDecodeError.
    return subprocess.run(
        ["make", "-s", make_target],
        cwd=str(CRYPTO_TESTS_DIR),
        capture_output=True,
        text=True,
        errors="backslashreplace",
        timeout=60,
        check=False,
    )


def _assert_pass(result: subprocess.CompletedProcess[str], label: str) -> None:
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} driver failed (exit={result.returncode}):\n{output}"
    )
    assert "PASS  all SHA-256 checks passed" in output, output


def test_sha256_x86_64_native() -> None:
    _skip_if_missing(NATIVE_X86_TOOLS)
    _assert_pass(_run_driver("test-x86_64-sha256-native"), "x86_64 native")


def test_sha256_x86_64_shani_denverton() -> None:
    _skip_if_missing(SHANI_CELL_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-sha256-shani"),
        "x86_64 fork-qemu -cpu Denverton",
    )


def test_sha256_x86_64_shani_max() -> None:
    _skip_if_missing(SHANI_CELL_TOOLS)
    _assert_pass(
        _run_driver("test-x86_64-sha256-shani-max"),
        "x86_64 fork-qemu -cpu max",
    )


def test_sha256_aarch64() -> None:
    _skip_if_missing(AARCH64_TOOLS)
    _assert_pass(_run_driver("test-aarch64-sha256"), "aarch64")
