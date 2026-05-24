"""L2 dispatcher fail-path verification — synthetic-stimulus stub.

The dispatcher's defensive validation checks (RX bad_id,
num_buffers != 1, TX bad_id) are unreachable under any normal
load because Firecracker's virtio-net is spec-compliant and
won't produce malformed descriptor data. The 2026-05-23 L2
test-coverage review's #5 item ("fail-path tests") flagged
this as a production-bar gap.

Approach (chosen 2026-05-24): a "guest-side stub" — a
separate guest binary built from
`arch/x86_64/platform/failpath/boot.S` that links the
production dispatcher.o unchanged, but skips real virtio init
and instead writes a malformed used-ring entry directly into
the VIRTQ memreq region BEFORE calling l2_dispatch. The
dispatcher reads the malformed value, hits the fail path,
emits the expected RX:FAIL marker, and returns nonzero. The
stub catches the nonzero return and emits
`FAILPATH:DONE rc=<hex>` before halting.

Alternative considered and rejected: a Python `vhost-user-net`
backend running under QEMU, which would have meant migrating
the entire L2 harness off Firecracker. See task #40 + the
2026-05-24 discussion for the cost/benefit analysis.

Current coverage: scenario BAD_ID only (RX bad_id fail path).
The stub source supports `%ifdef FAILPATH_SCENARIO_<NAME>`
sentinels so adding num_bufs and tx_bad_id is mostly a matter
of dropping in new memory-pre-population blocks and new test
cases here.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path

import pytest

from l2_harness.firecracker import (
    FirecrackerConfig,
    has_firecracker_binary,
    launched_guest,
)
from l2_harness.serial import SerialLog


REPO_ROOT = Path(__file__).resolve().parents[3]
FAILPATH_BUILD_DIR = (
    REPO_ROOT / "arch" / "x86_64" / "build" / "failpath"
)
FAILPATH_ARCH_DIR = REPO_ROOT / "arch" / "x86_64"

MARKER_TIMEOUT_SECONDS = 3.0
"""Generous upper bound on how long the dispatcher takes from
boot to its FAILPATH:DONE marker. The boot path is ~100-150 ms,
init_memory_layout + queue-fill is sub-millisecond, and the
fail-path emit chain is microseconds. 3 s absorbs any host
load."""


@pytest.fixture(scope="session")
def failpath_guest_elf() -> Path:
    """Build (if needed) the x86_64 failpath stub guest.

    Session-scoped so the build runs once per pytest invocation
    even if multiple fail-path scenarios get added. Each
    scenario today produces the SAME binary (only BAD_ID is
    implemented); when num_bufs / tx_bad_id scenarios land,
    each will need its own build and this fixture will
    parametrise on the scenario name.
    """
    if not has_firecracker_binary():
        pytest.skip(
            "firecracker binary not found on PATH — install per "
            "tooling/tracer_bullet/run_local.sh expectations"
        )
    if not shutil.which("nasm"):
        pytest.skip("nasm not installed; cannot build failpath stub")
    # The Makefile rebuilds incrementally; just invoke it and
    # surface its output on failure.
    result = subprocess.run(
        ["make", "-C", str(FAILPATH_ARCH_DIR),
         "PLATFORM=failpath", "SCENARIO=BAD_ID"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            "failpath stub build failed:\n"
            f"{result.stdout}"
        )
    elf = FAILPATH_BUILD_DIR / "guest.elf"
    if not elf.exists():
        pytest.fail(
            f"failpath build returned 0 but {elf} doesn't exist"
        )
    return elf


def test_rx_bad_id_fail_path(
    failpath_guest_elf: Path,    # pylint: disable=redefined-outer-name
    artifact_dir: Path,
    tap_iface: str,    # pylint: disable=redefined-outer-name
) -> None:
    """RX UsedRing entry with id >= VIRTQ_MAX_SIZE triggers
    `.l2_rx_bad_id_fail`; dispatcher emits the bad_id marker
    and returns rc=1.

    Expected marker chain (in order):
      1. READY               — PVH prologue executed
      2. FAILPATH:BOOT       — kernel_main_64 reached, alloc + kstack ok
      3. RX:FAIL bad_id=00000100 — dispatcher's bad_id check fired
                                   (0x100 == 256, the malformed id
                                   the stub wrote into ring[0].id)
      4. FAILPATH:DONE rc=00000001 — dispatcher returned 1 (fail);
                                     stub caught the return value

    Negative assertion: no RX:FRAME marker — the bad_id check
    fires BEFORE any RX:FRAME emit; if RX:FRAME also appears,
    the dispatcher continued past the fail point (regression).
    """
    cfg = FirecrackerConfig(
        kernel_image_path=failpath_guest_elf,
        artifact_dir=artifact_dir,
        tap_iface=tap_iface,
    )
    with ExitStack() as stack:
        guest = stack.enter_context(launched_guest(cfg))
        serial_log = SerialLog(guest.serial_log_path)

        # Wait for the terminal marker — covers the whole chain
        # because FAILPATH:DONE only fires after every earlier
        # step.
        serial_log.assert_marker_observed(
            "FAILPATH:DONE rc=00000001",
            timeout=MARKER_TIMEOUT_SECONDS,
        )

        # Earlier markers should already be in the log by now.
        full = serial_log.text()
        for marker in ("READY", "FAILPATH:BOOT",
                       "RX:FAIL bad_id=00000100"):
            if marker not in full:
                raise AssertionError(
                    f"failpath BAD_ID: expected marker "
                    f"{marker!r} missing from serial log\n"
                    f"--- serial log ({guest.serial_log_path}) ---\n"
                    f"{full}\n"
                    "--- end ---"
                )

        # Negative: dispatcher must NOT emit RX:FRAME — the
        # bad_id gate fires before per-frame processing reaches
        # RX:FRAME's emit chain. If RX:FRAME shows up, the
        # gate didn't actually short-circuit.
        if "RX:FRAME" in full:
            raise AssertionError(
                "failpath BAD_ID: dispatcher emitted RX:FRAME "
                "despite the bad_id gate — gate didn't "
                "short-circuit the consume body.\n"
                f"--- serial log ({guest.serial_log_path}) ---\n"
                f"{full}\n"
                "--- end ---"
            )
