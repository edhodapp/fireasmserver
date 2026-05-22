"""Firecracker subprocess lifecycle for L2 integration tests.

Boots a fireasmserver guest under Firecracker, captures its serial
output to a file in the per-test artifact directory, and tears
down cleanly on test completion. Mirrors the invocation pattern
of `tooling/tracer_bullet/run_local.sh`'s
`launch_firecracker_x86_64` but exposes a Python context-manager
interface for pytest fixtures.

Per `docs/l2/HARNESS.md` §3.1.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

from pydantic import BaseModel


READY_MARKER_DEFAULT = "READY"
READY_TIMEOUT_SECONDS_DEFAULT = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 2.0


class FirecrackerConfig(BaseModel):
    """Inputs to a single Firecracker launch.

    Frozen so a test cannot accidentally mutate the config of an
    already-running guest. The artifact_dir is the per-test
    directory pytest hands us; the serial log, config JSON, and
    Firecracker stderr all land there.
    """

    model_config = {"frozen": True}

    kernel_image_path: Path
    artifact_dir: Path
    tap_iface: str = "tap0"
    vcpu_count: int = 1
    mem_size_mib: int = 128
    instance_id: str = "l2-harness"


class FirecrackerGuest:
    """Running Firecracker instance bound to one test.

    Constructed by the `firecracker_guest` fixture and yielded to
    the test. Callers interact via the `serial_log_path` (read
    via `l2_harness.serial`) and via tap0 frame I/O — this class
    only owns the subprocess lifetime + filesystem artifacts.
    """

    def __init__(self, cfg: FirecrackerConfig) -> None:
        self._cfg = cfg
        self._proc: subprocess.Popen[bytes] | None = None
        self._serial_path = cfg.artifact_dir / "serial.log"
        self._stderr_path = cfg.artifact_dir / "firecracker-stderr.log"
        self._config_path = cfg.artifact_dir / "firecracker-config.json"
        # We track the redirected stdout/stderr file handles
        # explicitly because Popen does NOT expose them via
        # proc.stdout / proc.stderr when we pass an already-open
        # file object (those attributes stay None unless
        # PIPE/stdout was used). Without explicit tracking, the
        # handles leak — caught by clean-Claude on 2026-05-22.
        self._stdout_f: IO[bytes] | None = None
        self._stderr_f: IO[bytes] | None = None

    @property
    def serial_log_path(self) -> Path:
        """Where Firecracker writes the guest's serial output."""
        return self._serial_path

    @property
    def stderr_log_path(self) -> Path:
        """Where Firecracker writes its own framework messages."""
        return self._stderr_path

    def start(self,
              ready_marker: str = READY_MARKER_DEFAULT,
              ready_timeout: float = READY_TIMEOUT_SECONDS_DEFAULT,
              ) -> None:
        """Launch the guest and block until READY appears.

        Raises:
            RuntimeError: if the READY marker does not appear in
                the serial log within `ready_timeout` seconds.
        """
        if self._proc is not None:
            raise RuntimeError("Firecracker already started")
        self._write_config()
        self._serial_path.touch()
        # pylint: disable=consider-using-with — file handles are
        # owned across scope by this class and closed in stop().
        # Stored on self so _close_redirected_streams can find them
        # (Popen doesn't expose them via proc.stdout/.stderr when
        # we pass an already-open file).
        self._stdout_f = open(self._serial_path, "wb")
        self._stderr_f = open(self._stderr_path, "wb")
        self._proc = subprocess.Popen(
            [
                "firecracker",
                "--no-api",
                "--config-file", str(self._config_path),
                "--id", self._cfg.instance_id,
            ],
            stdout=self._stdout_f,
            stderr=self._stderr_f,
            cwd=self._cfg.artifact_dir,
        )
        self._wait_for_marker(ready_marker, ready_timeout)

    def stop(self) -> None:
        """Terminate the guest cleanly. Idempotent."""
        if self._proc is None:
            return
        self._terminate_running()
        self._close_redirected_streams()
        self._proc = None

    def _terminate_running(self) -> None:
        """Send SIGTERM, escalate to SIGKILL on timeout."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def _close_redirected_streams(self) -> None:
        """Close stdout/stderr file handles we opened."""
        if self._stdout_f is not None:
            self._stdout_f.close()
            self._stdout_f = None
        if self._stderr_f is not None:
            self._stderr_f.close()
            self._stderr_f = None

    def _write_config(self) -> None:
        """Emit the Firecracker config JSON to the artifact dir."""
        cfg = {
            "boot-source": {
                "kernel_image_path": str(self._cfg.kernel_image_path),
            },
            "machine-config": {
                "vcpu_count": self._cfg.vcpu_count,
                "mem_size_mib": self._cfg.mem_size_mib,
            },
            "drives": [],
            "network-interfaces": [
                {
                    "iface_id": "eth0",
                    "host_dev_name": self._cfg.tap_iface,
                },
            ],
        }
        self._config_path.write_text(json.dumps(cfg, indent=2))

    def _wait_for_marker(self, marker: str, timeout: float) -> None:
        """Block until `marker` appears in the serial log."""
        deadline = time.monotonic() + timeout
        marker_bytes = marker.encode("ascii")
        while time.monotonic() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                raise RuntimeError(
                    f"Firecracker exited before {marker!r} "
                    f"appeared; see {self._stderr_path}"
                )
            data = self._serial_path.read_bytes()
            if marker_bytes in data:
                return
            time.sleep(0.05)
        raise RuntimeError(
            f"timed out after {timeout}s waiting for {marker!r} "
            f"in {self._serial_path}"
        )


@contextmanager
def launched_guest(cfg: FirecrackerConfig) -> Iterator[FirecrackerGuest]:
    """Context manager that starts the guest + cleans up on exit.

    Use from a pytest fixture rather than directly from a test —
    this gives the fixture's `yield` the FirecrackerGuest instance
    and the teardown happens reliably even if the test raises.
    """
    cfg.artifact_dir.mkdir(parents=True, exist_ok=True)
    guest = FirecrackerGuest(cfg)
    try:
        guest.start()
        yield guest
    finally:
        guest.stop()


def has_firecracker_binary() -> bool:
    """True if `firecracker` is on PATH and runnable.

    Tests gate on this so the suite SKIPs cleanly when run in an
    environment without Firecracker (developer workstation
    without it installed; CI runner that hasn't provisioned it).
    """
    try:
        result = subprocess.run(
            ["firecracker", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=2.0,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def has_root_or_capability() -> bool:
    """True if the current process can do raw-socket frame I/O.

    Required by scapy.sendp/sniff on tap0. Two acceptable
    configurations:
      - euid 0 (sudo elevation), or
      - the running Python binary has CAP_NET_RAW set via
        `setcap cap_net_raw+eip <python>`.

    We probe by trying to open an AF_PACKET raw socket. EPERM
    means we lack the privilege; success (after immediate close)
    means we have it. This is more robust than reading from
    /proc/self/status because it tests the actual operation
    pytest needs, not a proxy for it.

    Per `docs/l2/HARNESS.md` §3.3 — the operator chooses how
    to grant the privilege; the harness only checks.
    """
    if os.geteuid() == 0:
        return True
    return _can_open_raw_socket()


def _can_open_raw_socket() -> bool:
    """Probe AF_PACKET raw-socket creation; True iff allowed."""
    try:
        sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003)
        )
    except PermissionError:
        return False
    except OSError:
        return False
    sock.close()
    return True
