"""Shared pytest fixtures for the L2 integration tests.

Per `docs/l2/HARNESS.md` §3.2 — the lifecycle is per-test
(clean Firecracker boot, clean ARP cache) for MVP isolation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from l2_harness import frames
from l2_harness.capture import FrameSender
from l2_harness.firecracker import (
    FirecrackerConfig,
    FirecrackerGuest,
    has_firecracker_binary,
    has_root_or_capability,
    launched_guest,
)
from l2_harness.serial import SerialLog
from l2_harness.tap0 import flush_arp_cache, require_tap0


REPO_ROOT = Path(__file__).resolve().parents[3]
GUEST_ELF = (
    REPO_ROOT
    / "arch" / "x86_64" / "build" / "firecracker" / "guest.elf"
)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register --keep-artifacts per HARNESS.md §8."""
    parser.addoption(
        "--keep-artifacts",
        action="store_true",
        default=False,
        help=(
            "Keep the per-test artifact directory even on test "
            "pass. By default it's kept only on failure."
        ),
    )


@pytest.fixture(scope="session", autouse=True)
def _check_environment() -> None:
    """Fail fast if the environment can't run integration tests.

    Three preconditions:
      1. firecracker binary on PATH
      2. raw-socket capability (effective uid 0)
      3. tap0 configured at the expected IP

    Per HARNESS.md §3.3, sudo elevation is the operator's choice;
    we don't try to acquire root automatically.
    """
    if not has_firecracker_binary():
        pytest.skip(
            "firecracker binary not found on PATH — install per "
            "tooling/tracer_bullet/run_local.sh expectations"
        )
    if not has_root_or_capability():
        pytest.skip(
            "L2 integration tests need raw-socket capability. "
            "Either run via sudo, or grant the venv Python "
            "CAP_NET_RAW once with:\n"
            "    sudo setcap cap_net_raw+eip "
            "$(readlink -f .venv/bin/python3)\n"
            "and re-run pytest."
        )
    require_tap0(expected_host_ip=frames.HOST_DEFAULT_IP)


@pytest.fixture(scope="session", autouse=True)
def _check_guest_built() -> None:
    """Skip if the x86_64/firecracker guest hasn't been built.

    Building isn't this fixture's job — it lives one layer
    up (CI pre-push pipeline or the developer's `make` step).
    """
    if not GUEST_ELF.exists():
        pytest.skip(
            f"guest ELF not found at {GUEST_ELF}; run\n"
            f"    make -C arch/x86_64 PLATFORM=firecracker\n"
            f"before invoking the integration tests"
        )


@pytest.fixture()
def artifact_dir(tmp_path: pytest.TempPathFactory) -> Path:
    """Per-test artifact directory.

    pytest's `tmp_path` is per-test by default. We rename the
    fixture for clarity at the test call site.
    """
    return Path(tmp_path)  # type: ignore[arg-type]


# pylint: disable=redefined-outer-name
# Pytest fixtures reference other fixtures by parameter name; the
# redefinition warning here is the standard noise that comes with
# pytest's dependency-injection style and isn't a real shadowing
# bug.
@pytest.fixture()
def firecracker_guest(artifact_dir: Path) -> Iterator[FirecrackerGuest]:
    """Boot a fresh Firecracker guest for one test."""
    cfg = FirecrackerConfig(
        kernel_image_path=GUEST_ELF,
        artifact_dir=artifact_dir,
    )
    with launched_guest(cfg) as guest:
        yield guest


@pytest.fixture()
def serial_log(firecracker_guest: FirecrackerGuest) -> SerialLog:
    """Reader for the guest's serial log."""
    return SerialLog(firecracker_guest.serial_log_path)


@pytest.fixture()
def frame_sender(artifact_dir: Path) -> Iterator[FrameSender]:
    """Frame injector bound to tap0; logs sent frames to pcap."""
    pcap = artifact_dir / "sent.pcap"
    yield FrameSender(iface="tap0", pcap_path=pcap)
    flush_arp_cache(frames.GUEST_DEFAULT_IP, iface="tap0")
