"""Tests for vm_launcher module."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qemu_harness.vm_launcher import (
    VMConfig,
    VMHandle,
    _get_proc,
    _kill_via_pid,
    _kill_via_proc,
    _qemu_args,
    _qemu_binary,
    _register_proc,
    _unregister_proc,
    has_kvm,
    kill_vm,
    launch_vm,
    wait_for_ready,
)


class TestQemuBinary:
    """Tests for _qemu_binary()."""

    def test_x86_64(self) -> None:
        assert _qemu_binary("x86_64") == "qemu-system-x86_64"

    def test_aarch64(self) -> None:
        result = _qemu_binary("aarch64")
        assert result == "qemu-system-aarch64"

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            _qemu_binary("mips")


class TestQemuArgs:
    """Tests for _qemu_args()."""

    def test_x86_64_args(self) -> None:
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path="/serial",
        )
        args = _qemu_args(config)
        assert args[0] == "qemu-system-x86_64"
        assert "-nographic" in args
        assert "file:/serial" in args
        assert "-machine" in args
        idx = args.index("-machine")
        assert args[idx + 1] == "microvm"

    def test_aarch64_args(self) -> None:
        config = VMConfig(
            image_path="/img", arch="aarch64",
            platform="qemu", serial_path="/serial",
        )
        args = _qemu_args(config)
        assert args[0] == "qemu-system-aarch64"
        idx = args.index("-machine")
        assert args[idx + 1] == "virt"

    def test_extra_args_appended(self) -> None:
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path="/serial",
            extra_args=["-m", "128"],
        )
        args = _qemu_args(config)
        assert "-m" in args
        assert "128" in args


class TestPathValidation:
    """Tests for path traversal rejection."""

    def test_clean_path_accepted(self) -> None:
        config = VMConfig(
            image_path="/tmp/guest.elf", arch="x86_64",
            platform="qemu", serial_path="/tmp/serial.log",
        )
        assert "guest.elf" in config.image_path

    def test_image_path_traversal_rejected(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="traversal"):
            VMConfig(
                image_path="/tmp/../etc/shadow", arch="x86_64",
                platform="qemu", serial_path="/tmp/s.log",
            )

    def test_serial_path_traversal_rejected(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="traversal"):
            VMConfig(
                image_path="/tmp/guest.elf", arch="x86_64",
                platform="qemu",
                serial_path="../../etc/passwd",
            )

    def test_paths_resolved(self) -> None:
        config = VMConfig(
            image_path="/tmp/./guest.elf", arch="x86_64",
            platform="qemu", serial_path="/tmp/./s.log",
        )
        assert "/tmp/guest.elf" == config.image_path
        assert "/tmp/s.log" == config.serial_path


class TestPlatformValidation:
    """Tests for Platform Literal validation."""

    def test_valid_qemu(self) -> None:
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path="/s",
        )
        assert config.platform == "qemu"

    def test_valid_firecracker(self) -> None:
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="firecracker", serial_path="/s",
        )
        assert config.platform == "firecracker"

    def test_invalid_rejects(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            VMConfig(
                image_path="/img", arch="x86_64",
                platform="docker", serial_path="/s",
            )


class TestHasKvm:
    """Tests for has_kvm()."""

    @patch("qemu_harness.vm_launcher.os.access")
    def test_available(self, mock_access: MagicMock) -> None:
        mock_access.return_value = True
        assert has_kvm() is True

    @patch("qemu_harness.vm_launcher.os.access")
    def test_unavailable(
        self, mock_access: MagicMock,
    ) -> None:
        mock_access.return_value = False
        assert has_kvm() is False


class TestProcRegistry:
    """Tests for the process registry."""

    def test_register_and_get(self) -> None:
        proc = MagicMock(pid=99999)
        _register_proc(proc)
        assert _get_proc(99999) is proc
        _unregister_proc(99999)

    def test_get_missing(self) -> None:
        assert _get_proc(88888) is None

    def test_unregister_missing(self) -> None:
        _unregister_proc(77777)

    def test_unregister_cleans_up(self) -> None:
        proc = MagicMock(pid=66666)
        _register_proc(proc)
        _unregister_proc(66666)
        assert _get_proc(66666) is None


class TestLaunchVm:
    """Tests for launch_vm()."""

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_qemu_launch(
        self, mock_popen: MagicMock, tmp_path: object,
    ) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        mock_proc = MagicMock(pid=12345)
        mock_popen.return_value = mock_proc
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path=serial,
        )
        handle = launch_vm(config)
        assert handle.pid == 12345
        assert handle.serial_path == serial
        assert handle.stderr_path == serial + ".stderr"
        mock_popen.assert_called_once()
        _unregister_proc(12345)

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_stderr_captured_to_file(
        self, mock_popen: MagicMock, tmp_path: object,
    ) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        mock_proc = MagicMock(pid=22222)
        mock_popen.return_value = mock_proc
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path=serial,
        )
        handle = launch_vm(config)
        assert Path(handle.stderr_path).exists()
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["stderr"] is not subprocess.DEVNULL
        _unregister_proc(22222)

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_truncates_serial_on_launch(
        self, mock_popen: MagicMock, tmp_path: object,
    ) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        Path(serial).write_text("STALE READY MARKER")
        mock_proc = MagicMock(pid=33333)
        mock_popen.return_value = mock_proc
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path=serial,
        )
        launch_vm(config)
        assert Path(serial).read_text() == ""
        _unregister_proc(33333)

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_registers_proc(
        self, mock_popen: MagicMock, tmp_path: object,
    ) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        mock_proc = MagicMock(pid=11111)
        mock_popen.return_value = mock_proc
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path=serial,
        )
        launch_vm(config)
        assert _get_proc(11111) is mock_proc
        _unregister_proc(11111)

    def test_firecracker_no_kvm_raises(
        self, tmp_path: object,
    ) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="firecracker", serial_path=serial,
        )
        with patch(
            "qemu_harness.vm_launcher.has_kvm",
            return_value=False,
        ):
            with pytest.raises(RuntimeError, match="kvm"):
                launch_vm(config)

    def test_firecracker_with_kvm_not_implemented(
        self, tmp_path: object,
    ) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="firecracker", serial_path=serial,
        )
        with patch(
            "qemu_harness.vm_launcher.has_kvm",
            return_value=True,
        ):
            with pytest.raises(NotImplementedError):
                launch_vm(config)


class TestWaitForReady:
    """Tests for wait_for_ready()."""

    def test_marker_found(self, tmp_path: object) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        Path(serial).write_bytes(b"booting...\nREADY\n")
        handle = VMHandle(
            pid=1, serial_path=serial, stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        assert wait_for_ready(handle, "READY", 1.0) is True

    def test_timeout(self, tmp_path: object) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        Path(serial).write_bytes(b"booting...")
        handle = VMHandle(
            pid=1, serial_path=serial, stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        assert wait_for_ready(handle, "READY", 0.1) is False


class TestKillViaProc:
    """Tests for _kill_via_proc()."""

    def test_terminate_succeeds(self) -> None:
        proc = MagicMock()
        proc.wait.return_value = 0
        _kill_via_proc(proc)
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5.0)
        proc.kill.assert_not_called()

    def test_terminate_timeout_then_kill(self) -> None:
        proc = MagicMock()
        proc.wait.side_effect = [
            subprocess.TimeoutExpired("qemu", 5.0),
            0,
        ]
        _kill_via_proc(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert proc.wait.call_count == 2


class TestKillViaPid:
    """Tests for _kill_via_pid()."""

    @patch("qemu_harness.vm_launcher.os.kill")
    def test_already_dead(
        self, mock_kill: MagicMock,
    ) -> None:
        mock_kill.side_effect = ProcessLookupError
        _kill_via_pid(1)
        mock_kill.assert_called_once_with(1, 15)

    @patch("qemu_harness.vm_launcher.time.sleep")
    @patch("qemu_harness.vm_launcher.time.monotonic")
    @patch("qemu_harness.vm_launcher.os.kill")
    def test_exits_after_sigterm(
        self, mock_kill: MagicMock,
        mock_time: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        mock_kill.side_effect = [
            None,
            ProcessLookupError,
        ]
        mock_time.side_effect = [0.0, 0.0]
        _kill_via_pid(1)
        assert mock_kill.call_count == 2


class TestKillVm:
    """Tests for kill_vm()."""

    def test_uses_proc_when_registered(self) -> None:
        proc = MagicMock(pid=44444)
        proc.wait.return_value = 0
        _register_proc(proc)
        handle = VMHandle(
            pid=44444, serial_path="/s", stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        kill_vm(handle)
        proc.terminate.assert_called_once()
        assert _get_proc(44444) is None

    @patch("qemu_harness.vm_launcher._kill_via_pid")
    def test_falls_back_to_pid(
        self, mock_kill_pid: MagicMock,
    ) -> None:
        handle = VMHandle(
            pid=55555, serial_path="/s", stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        kill_vm(handle)
        mock_kill_pid.assert_called_once_with(55555)
