"""Tests for vm_launcher module."""

from unittest.mock import MagicMock, patch

import pytest

from qemu_harness.vm_launcher import (
    VMConfig,
    VMHandle,
    _qemu_args,
    _qemu_binary,
    _send_signal,
    _wait_for_exit,
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
        mock_popen.assert_called_once()

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
        from pathlib import Path
        Path(serial).write_text("booting...\nREADY\n")
        handle = VMHandle(
            pid=1, serial_path=serial,
            arch="x86_64", platform="qemu",
        )
        assert wait_for_ready(handle, "READY", 1.0) is True

    def test_timeout(self, tmp_path: object) -> None:
        serial = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        from pathlib import Path
        Path(serial).write_text("booting...")
        handle = VMHandle(
            pid=1, serial_path=serial,
            arch="x86_64", platform="qemu",
        )
        assert wait_for_ready(handle, "READY", 0.1) is False


class TestSendSignal:
    """Tests for _send_signal()."""

    @patch("qemu_harness.vm_launcher.os.kill")
    def test_success(self, mock_kill: MagicMock) -> None:
        assert _send_signal(123, 15) is True
        mock_kill.assert_called_once_with(123, 15)

    @patch("qemu_harness.vm_launcher.os.kill")
    def test_gone(self, mock_kill: MagicMock) -> None:
        mock_kill.side_effect = ProcessLookupError
        assert _send_signal(123, 15) is False


class TestWaitForExit:
    """Tests for _wait_for_exit()."""

    @patch("qemu_harness.vm_launcher._send_signal")
    def test_exits_immediately(
        self, mock_send: MagicMock,
    ) -> None:
        mock_send.return_value = False
        assert _wait_for_exit(123, 1.0) is True

    @patch("qemu_harness.vm_launcher.time.sleep")
    @patch("qemu_harness.vm_launcher._send_signal")
    @patch("qemu_harness.vm_launcher.time.monotonic")
    def test_timeout(
        self, mock_time: MagicMock,
        mock_send: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        mock_time.side_effect = [0.0, 0.0, 1.0, 2.0]
        mock_send.return_value = True
        assert _wait_for_exit(123, 0.5) is False


class TestKillVm:
    """Tests for kill_vm()."""

    @patch("qemu_harness.vm_launcher._send_signal")
    def test_already_dead(
        self, mock_send: MagicMock,
    ) -> None:
        mock_send.return_value = False
        handle = VMHandle(
            pid=1, serial_path="/s",
            arch="x86_64", platform="qemu",
        )
        kill_vm(handle)
        mock_send.assert_called_once()

    @patch("qemu_harness.vm_launcher._send_signal")
    @patch("qemu_harness.vm_launcher._wait_for_exit")
    def test_sigterm_sufficient(
        self, mock_wait: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        mock_send.return_value = True
        mock_wait.return_value = True
        handle = VMHandle(
            pid=1, serial_path="/s",
            arch="x86_64", platform="qemu",
        )
        kill_vm(handle)
        assert mock_send.call_count == 1

    @patch("qemu_harness.vm_launcher._send_signal")
    @patch("qemu_harness.vm_launcher._wait_for_exit")
    def test_needs_sigkill(
        self, mock_wait: MagicMock,
        mock_send: MagicMock,
    ) -> None:
        mock_send.return_value = True
        mock_wait.return_value = False
        handle = VMHandle(
            pid=1, serial_path="/s",
            arch="x86_64", platform="qemu",
        )
        kill_vm(handle)
        import signal
        mock_send.assert_any_call(1, signal.SIGTERM)
        mock_send.assert_any_call(1, signal.SIGKILL)
