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
and instead writes a malformed used-ring entry (or buffer
header) directly into memreq-allocated memory BEFORE calling
l2_dispatch. The dispatcher reads the malformed value, hits
the fail path, emits the standard RX:FAIL / TX:FAIL marker,
and returns nonzero. The stub catches the nonzero return and
emits `FAILPATH:DONE rc=<hex>` before halting.

Alternative considered and rejected: a Python `vhost-user-net`
backend running under QEMU, which would have meant migrating
the entire L2 harness off Firecracker. See task #40 + the
2026-05-24 discussion for the cost/benefit analysis.

Coverage in this file (one test per scenario, all
parametrised on the same fixture):

  BAD_ID    — RX UsedRing.ring[0].id = 0x100, expect
              `RX:FAIL bad_id=00000100`.
  NUM_BUFS  — RX buffer's virtio_net_hdr.num_buffers = 2,
              expect `RX:FAIL num_bufs=00000002`. Buffer dst
              MAC = GUEST_MAC and src is unicast and
              EtherType is non-PAUSE so the dispatcher
              reaches the num_buffers check (it lives AFTER
              the dst/src/PAUSE gates).
  TX_BAD_ID — valid RX completes through the full RX:FRAME
              path, then the pre-populated TX UsedRing's
              ring[0].id = 0x100 trips the TX bad_id check.
              Markers in order: RX:FRAME, RX:RETURNED,
              TX:SUBMITTED, TX:FAIL bad_id=00000100.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Scenario:
    """One fail-path scenario.

    scenario_name maps to the NASM `-DFAILPATH_SCENARIO_<NAME>=1`
    selector in the stub. expected_fail_marker is the dispatcher's
    fail-marker substring we assert on (the dispatcher emits hex
    in upper-case 8-digit form via emit_hex32). The required and
    forbidden lists let each scenario assert ordered intermediate
    markers + negatives (e.g., TX_BAD_ID must see RX:FRAME first;
    BAD_ID must NOT see RX:FRAME).
    """

    scenario_name: str
    expected_fail_marker: str
    required_markers: tuple[str, ...]
    forbidden_markers: tuple[str, ...]


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        scenario_name="BAD_ID",
        expected_fail_marker="RX:FAIL bad_id=00000100",
        required_markers=("READY", "FAILPATH:BOOT",
                          "RX:FAIL bad_id=00000100",
                          "FAILPATH:DONE rc=00000001"),
        # bad_id gate fires before per-frame processing, so the
        # normal RX:FRAME emit (which lives after) must NOT have
        # run.
        forbidden_markers=("RX:FRAME", "TX:SUBMITTED"),
    ),
    Scenario(
        scenario_name="NUM_BUFS",
        expected_fail_marker="RX:FAIL num_bufs=00000002",
        required_markers=("READY", "FAILPATH:BOOT",
                          "RX:FAIL num_bufs=00000002",
                          "FAILPATH:DONE rc=00000001"),
        # num_bufs gate fires before RX:FRAME emit. Same as
        # BAD_ID — no RX:FRAME, no TX phase.
        forbidden_markers=("RX:FRAME", "TX:SUBMITTED"),
    ),
    Scenario(
        scenario_name="HDR_FLAGS",
        expected_fail_marker="RX:FAIL hdr_flags=00000001",
        required_markers=("READY", "FAILPATH:BOOT",
                          "RX:FAIL hdr_flags=00000001",
                          "FAILPATH:DONE rc=00000001"),
        # hdr_flags gate is defense-in-depth — Firecracker's
        # virtio-net never sets these fields under our
        # negotiated feature set. Fires AFTER num_bufs but
        # BEFORE per-frame processing reaches RX:FRAME emit.
        forbidden_markers=("RX:FRAME", "TX:SUBMITTED"),
    ),
    Scenario(
        scenario_name="TX_BAD_ID",
        expected_fail_marker="TX:FAIL bad_id=00000100",
        required_markers=("READY", "FAILPATH:BOOT",
                          "RX:FRAME",            # valid RX must complete
                          "RX:RETURNED",         # RX recycle ran
                          "TX:SUBMITTED",        # TX submit ran
                          "TX:FAIL bad_id=00000100",
                          "FAILPATH:DONE rc=00000001"),
        # TX bad_id is the LAST marker before the fail return —
        # no specific forbidden marker beyond "no completed
        # TX:RECLAIMED."
        forbidden_markers=("TX:RECLAIMED",),
    ),
)


@pytest.fixture(scope="session")
def failpath_artifact_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared root for per-scenario failpath build artifacts.

    Each scenario gets its own subdirectory so pytest's
    auto-cleanup keeps them separate but our build itself
    overwrites arch/x86_64/build/failpath/ — that's fine
    because we sequence scenarios serially and copy each
    built ELF out to the per-scenario subdir.
    """
    return tmp_path_factory.mktemp("failpath_builds")


def _build_scenario(scenario_name: str,
                    out_dir: Path) -> Path:
    """Build the failpath stub for the named scenario, copy the
    ELF into `out_dir`, and return the copied ELF path.

    Cleans the build dir first so the previous scenario's
    objects don't conflict — the source includes a single
    %ifdef block per scenario, and a stale .o would still
    contain a different scenario's code.
    """
    build_dir = FAILPATH_BUILD_DIR
    if build_dir.exists():
        shutil.rmtree(build_dir)
    result = subprocess.run(
        ["make", "-C", str(FAILPATH_ARCH_DIR),
         "PLATFORM=failpath", f"SCENARIO={scenario_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"failpath stub build (SCENARIO={scenario_name}) "
            f"failed:\n{result.stdout}"
        )
    src_elf = build_dir / "guest.elf"
    if not src_elf.exists():
        pytest.fail(
            f"failpath build for {scenario_name} returned 0 "
            f"but {src_elf} doesn't exist"
        )
    dst_elf = out_dir / f"guest_{scenario_name}.elf"
    shutil.copy(src_elf, dst_elf)
    return dst_elf


@pytest.fixture(scope="session")
def scenario_elfs(
    failpath_artifact_root: Path,    # pylint: disable=redefined-outer-name
) -> dict[str, Path]:
    """Build every scenario once per pytest invocation.

    Session-scoped to amortise NASM + ld over the suite.
    Returns a dict mapping scenario_name → ELF path for
    test_fail_path to look up by case.
    """
    if not has_firecracker_binary():
        pytest.skip("firecracker binary not found on PATH")
    if not shutil.which("nasm"):
        pytest.skip("nasm not installed; cannot build failpath stub")
    elfs: dict[str, Path] = {}
    for sc in SCENARIOS:
        elfs[sc.scenario_name] = _build_scenario(
            sc.scenario_name, failpath_artifact_root,
        )
    return elfs


@pytest.mark.parametrize(
    "scenario", SCENARIOS,
    ids=lambda sc: sc.scenario_name.lower(),
)
def test_fail_path(
    scenario: Scenario,
    scenario_elfs: dict[str, Path],    # pylint: disable=redefined-outer-name
    artifact_dir: Path,
    tap_iface: str,    # pylint: disable=redefined-outer-name
) -> None:
    """Boot the per-scenario failpath stub and assert its marker
    chain. The expected sequence is in `scenario.required_markers`;
    the gate-skipping invariants are in `scenario.forbidden_markers`.
    """
    cfg = FirecrackerConfig(
        kernel_image_path=scenario_elfs[scenario.scenario_name],
        artifact_dir=artifact_dir,
        tap_iface=tap_iface,
    )
    with ExitStack() as stack:
        guest = stack.enter_context(launched_guest(cfg))
        serial_log = SerialLog(guest.serial_log_path)

        # Wait for the terminal marker — implies every earlier
        # required marker is already in the log.
        serial_log.assert_marker_observed(
            "FAILPATH:DONE rc=00000001",
            timeout=MARKER_TIMEOUT_SECONDS,
        )

        full = serial_log.text()
        for marker in scenario.required_markers:
            if marker not in full:
                raise AssertionError(
                    f"failpath {scenario.scenario_name}: required "
                    f"marker {marker!r} missing from serial log\n"
                    f"--- serial log ({guest.serial_log_path}) ---\n"
                    f"{full}\n"
                    "--- end ---"
                )

        leaked = [
            f for f in scenario.forbidden_markers if f in full
        ]
        if leaked:
            raise AssertionError(
                f"failpath {scenario.scenario_name}: forbidden "
                f"markers leaked through gate: {leaked!r}\n"
                f"--- serial log ({guest.serial_log_path}) ---\n"
                f"{full}\n"
                "--- end ---"
            )
