"""Shared pytest fixtures for the L2 integration tests.

Per `docs/l2/HARNESS.md` §3.2 — the lifecycle is per-test
(clean Firecracker boot, clean ARP cache) for MVP isolation.
"""

from __future__ import annotations

import os
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
from l2_harness.tap0 import (
    flush_arp_cache,
    host_mtu_of,
    require_tap0,
)


TAP_IFACE_ENV_VAR = "FIREASM_TAP_IFACE"
TAP_IFACE_DEFAULT = "tap0"
"""Default tap interface name; override via FIREASM_TAP_IFACE.

Lets CI runners or parallel test executions point at a
different tap device without editing per-test source. Default
is the developer-laptop setup wired in by ~/bin/fireasm-tap0-up.
"""

TAP0_RECOMMENDED_MTU = 1700
"""Tap MTU floor that lets every integration test actually run.

Tests that need to send frames above this (currently the
ETH-003 oversize test) skip cleanly if MTU is lower, but the
silent skip masks a real coverage gap. The session-level env
check below WARNS loudly when MTU is below this floor so a
silent regression is at least visible.
"""


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


@pytest.fixture(scope="session")
def tap_iface() -> str:
    """Tap interface name for all L2 tests.

    Defaults to TAP_IFACE_DEFAULT (`tap0`); override via the
    FIREASM_TAP_IFACE environment variable. Session-scoped
    because there's no use case for switching interfaces
    mid-run today; if/when there is, change scope to function
    + add a parametrize indirection.
    """
    return os.environ.get(TAP_IFACE_ENV_VAR, TAP_IFACE_DEFAULT)


@pytest.fixture(scope="session", autouse=True)
# pylint: disable-next=redefined-outer-name
def _check_environment(tap_iface: str) -> None:
    """Fail fast if the environment can't run integration tests.

    Three preconditions:
      1. firecracker binary on PATH
      2. raw-socket capability (effective uid 0)
      3. tap interface configured at the expected IP

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
    require_tap0(
        expected_host_ip=frames.HOST_DEFAULT_IP,
        iface=tap_iface,
    )
    # MTU floor: WARN loudly if below the recommended threshold.
    # Tests that need a bigger MTU SKIP individually rather than
    # failing the session, but the warning makes the regression
    # visible at session-start time instead of buried in per-test
    # skip messages. fireasm-tap0-up bumps this to 2000 on boot;
    # if it's lower here, either the operator skipped that step
    # or something reset the MTU after.
    mtu = host_mtu_of(tap_iface)
    if mtu is None or mtu < TAP0_RECOMMENDED_MTU:
        print(
            f"\nWARNING: {tap_iface} MTU is {mtu}; below "
            f"{TAP0_RECOMMENDED_MTU} recommended. Tests that "
            "send oversize stimuli will SKIP. Bump with:\n"
            f"    sudo ip link set {tap_iface} mtu 2000\n"
            "(See docs/l2/HARNESS.md §3.3a.)",
            flush=True,
        )


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
def artifact_dir(tmp_path: Path) -> Path:
    """Per-test artifact directory.

    pytest's `tmp_path` is per-test by default. We rename the
    fixture for clarity at the test call site.
    """
    return tmp_path


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
def frame_sender(
    artifact_dir: Path,
    tap_iface: str,    # pylint: disable=redefined-outer-name
) -> Iterator[FrameSender]:
    """Frame injector bound to the tap interface; logs to pcap."""
    pcap = artifact_dir / "sent.pcap"
    yield FrameSender(iface=tap_iface, pcap_path=pcap)
    flush_arp_cache(frames.GUEST_DEFAULT_IP, iface=tap_iface)
