"""Launch, monitor, and kill QEMU/Firecracker VMs."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

log = logging.getLogger(__name__)

Platform = Literal["qemu", "firecracker"]


def _reject_traversal(path: str) -> str:
    """Reject paths containing '..' components."""
    if ".." in Path(path).parts:
        msg = f"Path traversal not allowed: {path}"
        raise ValueError(msg)
    return str(Path(path).resolve())


# QEMU flags that could open network services or escape the VM.
_BLOCKED_ARGS = frozenset({
    "-monitor", "-vnc", "-chardev",
    "-netdev", "-nic", "-spice",
})


# Registry of Popen objects keyed by PID.
# Popen is not Pydantic-serializable, so VMHandle stays
# as a BaseModel with bare pid, and we look up the Popen
# here for process management (wait, poll, terminate).
_proc_registry: dict[int, subprocess.Popen[bytes]] = {}
_proc_lock = threading.Lock()


def _register_proc(proc: subprocess.Popen[bytes]) -> None:
    """Register a Popen object for later lookup."""
    with _proc_lock:
        _proc_registry[proc.pid] = proc


def _get_proc(pid: int) -> subprocess.Popen[bytes] | None:
    """Look up a registered Popen by PID."""
    with _proc_lock:
        return _proc_registry.get(pid)


def _unregister_proc(pid: int) -> None:
    """Remove a Popen from the registry."""
    with _proc_lock:
        _proc_registry.pop(pid, None)


class VMConfig(BaseModel):
    """Immutable configuration for launching a VM."""

    image_path: str
    arch: str
    platform: Platform
    serial_path: str
    extra_args: list[str] = []

    @field_validator("image_path", "serial_path")
    @classmethod
    def no_traversal(cls, v: str) -> str:
        """Reject path traversal in file paths."""
        return _reject_traversal(v)

    @field_validator("extra_args")
    @classmethod
    def no_blocked_args(
        cls, v: list[str],
    ) -> list[str]:
        """Reject QEMU flags that could open services."""
        for arg in v:
            if arg in _BLOCKED_ARGS:
                msg = f"Blocked QEMU argument: {arg}"
                raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def extra_args_qemu_only(self) -> "VMConfig":
        """extra_args is QEMU-specific; firecracker config
        is generated from VMConfig fields and doesn't accept
        passthrough CLI flags."""
        if self.platform == "firecracker" and self.extra_args:
            msg = (
                "extra_args is qemu-only; firecracker takes "
                "configuration via the generated JSON"
            )
            raise ValueError(msg)
        return self


class VMHandle(BaseModel):
    """Handle to a running VM process."""

    pid: int
    serial_path: str
    stderr_path: str
    arch: str
    platform: Platform


def _qemu_binary(arch: str) -> str:
    """Return the QEMU system binary for the given arch."""
    binaries = {
        "x86_64": "qemu-system-x86_64",
        "aarch64": "qemu-system-aarch64",
    }
    result = binaries.get(arch)
    if result is None:
        msg = f"Unsupported arch: {arch}"
        raise ValueError(msg)
    return result


def _qemu_args(config: VMConfig) -> list[str]:
    """Build QEMU command-line arguments."""
    binary = _qemu_binary(config.arch)
    args = [
        binary,
        "-nographic",
        "-serial", f"file:{config.serial_path}",
        "-no-reboot",
        "-kernel", config.image_path,
    ]
    if config.arch == "x86_64":
        args.extend(["-machine", "pc"])
    elif config.arch == "aarch64":
        args.extend(["-machine", "virt", "-cpu", "cortex-a76"])
    args.extend(config.extra_args)
    return args


# Firecracker requires a logger file path that already exists
# at startup. We co-locate it with the serial output file so
# the launcher's path discipline (no traversal, abs paths)
# applies uniformly.
def _firecracker_log_path(serial_path: str) -> str:
    return serial_path + ".fc-log"


def _firecracker_config_path(serial_path: str) -> str:
    return serial_path + ".fc-config.json"


def _firecracker_vm_id(serial_path: str) -> str:
    """Derive a stable per-launch microVM id from serial_path.

    Tests use unique tmpdirs, so the stem is unique per test.
    Real deployments name serial files distinctly.
    """
    return Path(serial_path).stem


def _firecracker_config_dict(config: VMConfig) -> dict[str, Any]:
    """Build the JSON config Firecracker expects via --config-file.

    drives must be present (Firecracker rejects missing field)
    but may be empty for diagnostic boots that have no rootfs.
    Logger path diverts Firecracker's own log lines off stdout
    so the serial console stream stays mostly clean -- only the
    one-line boot header leaks before the logger is configured.
    """
    return {
        "boot-source": {
            "kernel_image_path": config.image_path,
        },
        "machine-config": {
            "vcpu_count": 1,
            "mem_size_mib": 128,
        },
        "drives": [],
        "logger": {
            "log_path": _firecracker_log_path(config.serial_path),
            "level": "Info",
        },
    }


def _firecracker_args(config_path: str, vm_id: str) -> list[str]:
    """Build the firecracker --no-api command line."""
    return [
        "firecracker",
        "--no-api",
        "--config-file", config_path,
        "--id", vm_id,
    ]


def has_kvm() -> bool:
    """Check if /dev/kvm is available."""
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def launch_vm(config: VMConfig) -> VMHandle:
    """Launch a VM in the background.

    Returns a handle with pid and serial output path.
    Does not block -- caller must poll for readiness.
    """
    log.info(
        "Launching %s/%s: %s",
        config.arch, config.platform, config.image_path,
    )
    if config.platform == "firecracker":
        return _launch_firecracker(config)
    return _launch_qemu(config)


def _launch_qemu(config: VMConfig) -> VMHandle:
    """Spawn QEMU and return a handle."""
    Path(config.serial_path).write_bytes(b"")
    stderr_path = config.serial_path + ".stderr"
    Path(stderr_path).write_bytes(b"")
    args = _qemu_args(config)
    with open(stderr_path, "w", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
    _register_proc(proc)
    log.info("VM launched, pid=%d", proc.pid)
    return VMHandle(
        pid=proc.pid,
        serial_path=config.serial_path,
        stderr_path=stderr_path,
        arch=config.arch,
        platform=config.platform,
    )


def _launch_firecracker(config: VMConfig) -> VMHandle:
    """Spawn Firecracker via --no-api and return a handle.

    Firecracker has no equivalent of QEMU's -serial file:<path>
    flag -- the 8250 UART always writes to stdout. We redirect
    stdout into the caller's serial_path so the same wait/read
    interface works for both platforms. The Firecracker logger
    is diverted to a sibling .fc-log file to keep VMM log lines
    off the serial stream.
    """
    if not has_kvm():
        msg = "Firecracker requires /dev/kvm"
        raise RuntimeError(msg)
    log_path = _firecracker_log_path(config.serial_path)
    config_path = _firecracker_config_path(config.serial_path)
    stderr_path = config.serial_path + ".stderr"
    Path(log_path).write_bytes(b"")
    Path(stderr_path).write_bytes(b"")
    Path(config_path).write_text(
        json.dumps(_firecracker_config_dict(config), indent=2),
        encoding="utf-8",
    )
    args = _firecracker_args(
        config_path, _firecracker_vm_id(config.serial_path),
    )
    with open(config.serial_path, "wb") as serial_file, \
            open(stderr_path, "w", encoding="utf-8") as stderr_file:
        proc = subprocess.Popen(
            args,
            stdout=serial_file,
            stderr=stderr_file,
        )
    _register_proc(proc)
    log.info("VM launched, pid=%d", proc.pid)
    return VMHandle(
        pid=proc.pid,
        serial_path=config.serial_path,
        stderr_path=stderr_path,
        arch=config.arch,
        platform=config.platform,
    )


def wait_for_ready(
    handle: VMHandle,
    marker: str,
    timeout_sec: float,
) -> bool:
    """Poll serial output until marker appears or timeout.

    Opens the file once, reads incrementally in binary mode.
    Returns True if ready, False if timed out.
    """
    log.debug(
        "Waiting for marker '%s' (timeout=%.1fs)", marker, timeout_sec,
    )
    marker_bytes = marker.encode()
    marker_len = len(marker_bytes)
    deadline = time.monotonic() + timeout_sec
    with open(handle.serial_path, "rb") as f:
        tail = b""
        while time.monotonic() < deadline:
            chunk = f.read(4096)
            if chunk:
                window = tail + chunk
                if marker_bytes in window:
                    log.info("Marker '%s' found", marker)
                    return True
                if len(window) >= marker_len:
                    tail = window[-marker_len:]
                else:
                    tail = window
            time.sleep(0.05)
    log.warning("Timeout waiting for marker '%s'", marker)
    return False


def _kill_via_proc(
    proc: subprocess.Popen[bytes],
) -> None:
    """Kill using Popen API. Immune to PID recycling."""
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)


def _signal_pid(pid: int, sig: int) -> bool:
    """Send signal to PID. Return False if process is gone.

    Returns True for the "process exists" case, including the
    sub-case where the PID has been recycled to a process owned
    by another user (PermissionError). The caller can't kill it
    in that case, but the process exists, which is what the bool
    contract reports. Logged so the caller can spot the
    pathological case in test output.
    """
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        log.warning(
            "PID %d exists but is not signalable by us "
            "(likely recycled to another user)", pid,
        )
        return True
    return True


def _try_waitpid(pid: int) -> bool:
    """Try to reap a child process. Return True if reaped.

    Returns False for the "not a child of ours" case
    (ChildProcessError) so the caller can fall through to a
    signal-based existence check via _signal_pid(pid, 0). The
    previous "True on ChildProcessError" behavior caused
    _poll_pid_exit to declare orphaned PIDs exited without
    actually checking.
    """
    try:
        result, _ = os.waitpid(pid, os.WNOHANG)
        return result != 0
    except ChildProcessError:
        return False


def _poll_pid_exit(pid: int, timeout: float) -> bool:
    """Poll until PID exits or timeout. True if exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _try_waitpid(pid):
            return True
        if not _signal_pid(pid, 0):
            return True
        time.sleep(0.1)
    return False


def _kill_via_pid(pid: int) -> None:
    """Fallback kill using bare PID (no Popen available)."""
    if not _signal_pid(pid, 15):
        return
    if _poll_pid_exit(pid, 5.0):
        return
    _signal_pid(pid, 9)
    _try_waitpid(pid)


def kill_vm(handle: VMHandle) -> None:
    """Kill the VM process cleanly.

    Uses Popen API if available (safe against PID
    recycling), falls back to bare PID signals.
    Reaps the child process to prevent zombies.
    """
    log.info("Killing VM pid=%d", handle.pid)
    proc = _get_proc(handle.pid)
    if proc is not None:
        _kill_via_proc(proc)
        _unregister_proc(handle.pid)
        log.info("VM pid=%d terminated via Popen", handle.pid)
    else:
        log.warning("No Popen for pid=%d, using bare PID", handle.pid)
        _kill_via_pid(handle.pid)
