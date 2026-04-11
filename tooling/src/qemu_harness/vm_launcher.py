"""Launch, monitor, and kill QEMU/Firecracker VMs."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

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


def _register_proc(proc: subprocess.Popen[bytes]) -> None:
    """Register a Popen object for later lookup."""
    _proc_registry[proc.pid] = proc


def _get_proc(pid: int) -> subprocess.Popen[bytes] | None:
    """Look up a registered Popen by PID."""
    return _proc_registry.get(pid)


def _unregister_proc(pid: int) -> None:
    """Remove a Popen from the registry."""
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
        args.extend(["-machine", "microvm"])
    elif config.arch == "aarch64":
        args.extend(["-machine", "virt", "-cpu", "cortex-a76"])
    args.extend(config.extra_args)
    return args


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
    Path(config.serial_path).write_bytes(b"")
    if config.platform == "firecracker":
        if not has_kvm():
            msg = "Firecracker requires /dev/kvm"
            raise RuntimeError(msg)
        msg = "Firecracker launch not yet implemented"
        raise NotImplementedError(msg)
    stderr_path = config.serial_path + ".stderr"
    Path(stderr_path).write_bytes(b"")
    args = _qemu_args(config)
    stderr_file = open(stderr_path, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
    )
    stderr_file.close()
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
    deadline = time.monotonic() + timeout_sec
    with open(handle.serial_path, "rb") as f:
        buf = b""
        while time.monotonic() < deadline:
            chunk = f.read()
            if chunk:
                buf += chunk
                if marker_bytes in buf:
                    log.info("Marker '%s' found", marker)
                    return True
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
    """Send signal to PID. Return False if process gone."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    return True


def _poll_pid_exit(pid: int, timeout: float) -> bool:
    """Poll until PID exits or timeout. True if exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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
