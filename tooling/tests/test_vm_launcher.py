"""Tests for vm_launcher module."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from qemu_harness.vm_launcher import (
    _firecracker_args,
    _firecracker_config_dict,
    _firecracker_config_path,
    _firecracker_log_path,
    _firecracker_vm_id,
    _get_proc,
    _kill_via_pid,
    _kill_via_proc,
    _proc_registry,
    _qemu_args,
    _qemu_binary,
    _register_proc,
    _signal_pid,
    _try_waitpid,
    _unregister_proc,
    has_kvm,
    kill_vm,
    launch_vm,
    VMConfig,
    VMHandle,
    wait_for_ready,
)


@pytest.fixture(autouse=True)
def _clean_registry():  # type: ignore[no-untyped-def]
    """Clear the proc registry before and after each test."""
    _proc_registry.clear()
    yield
    _proc_registry.clear()


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
        assert args[idx + 1] == "pc"

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


class TestExtraArgsValidation:
    """Tests for blocked QEMU argument validation."""

    def test_safe_args_accepted(self) -> None:
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path="/s",
            extra_args=["-m", "128", "-smp", "2"],
        )
        assert config.extra_args == ["-m", "128", "-smp", "2"]

    def test_monitor_blocked(self) -> None:
        with pytest.raises(ValidationError, match="Blocked"):
            VMConfig(
                image_path="/img", arch="x86_64",
                platform="qemu", serial_path="/s",
                extra_args=["-monitor", "tcp::4444"],
            )

    def test_vnc_blocked(self) -> None:
        with pytest.raises(ValidationError, match="Blocked"):
            VMConfig(
                image_path="/img", arch="x86_64",
                platform="qemu", serial_path="/s",
                extra_args=["-vnc", ":0"],
            )

    def test_chardev_blocked(self) -> None:
        with pytest.raises(ValidationError, match="Blocked"):
            VMConfig(
                image_path="/img", arch="x86_64",
                platform="qemu", serial_path="/s",
                extra_args=["-chardev", "socket,id=foo"],
            )

    def test_extra_args_rejected_on_firecracker(self) -> None:
        with pytest.raises(ValidationError, match="qemu-only"):
            VMConfig(
                image_path="/img", arch="x86_64",
                platform="firecracker", serial_path="/s",
                extra_args=["-m", "256"],
            )

    def test_empty_extra_args_ok_on_firecracker(self) -> None:
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="firecracker", serial_path="/s",
        )
        assert not config.extra_args


class TestPathValidation:
    """Tests for path traversal rejection."""

    def test_clean_path_accepted(self) -> None:
        config = VMConfig(
            image_path="/tmp/guest.elf", arch="x86_64",
            platform="qemu", serial_path="/tmp/serial.log",
        )
        assert "guest.elf" in config.image_path

    def test_image_path_traversal_rejected(self) -> None:
        with pytest.raises(ValidationError, match="traversal"):
            VMConfig(
                image_path="/tmp/../etc/shadow", arch="x86_64",
                platform="qemu", serial_path="/tmp/s.log",
            )

    def test_serial_path_traversal_rejected(self) -> None:
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
        with pytest.raises(ValidationError):
            VMConfig(
                image_path="/img", arch="x86_64",
                platform="docker", serial_path="/s",  # type: ignore[arg-type]
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

    def test_get_missing(self) -> None:
        assert _get_proc(88888) is None

    def test_unregister_cleans_up(self) -> None:
        proc = MagicMock(pid=66666)
        _register_proc(proc)
        _unregister_proc(66666)
        assert _get_proc(66666) is None


class TestLaunchVm:
    """Tests for launch_vm()."""

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_qemu_launch(
        self, mock_popen: MagicMock, tmp_path: Path,
    ) -> None:
        serial = str(tmp_path / "serial.log")
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

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_stderr_captured_to_file(
        self, mock_popen: MagicMock, tmp_path: Path,
    ) -> None:
        serial = str(tmp_path / "serial.log")
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

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_truncates_serial_on_launch(
        self, mock_popen: MagicMock, tmp_path: Path,
    ) -> None:
        serial = str(tmp_path / "serial.log")
        Path(serial).write_text("STALE READY MARKER", encoding="utf-8")
        mock_proc = MagicMock(pid=33333)
        mock_popen.return_value = mock_proc
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path=serial,
        )
        launch_vm(config)
        content = Path(serial).read_text(encoding="utf-8")
        assert content == ""

    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_registers_proc(
        self, mock_popen: MagicMock, tmp_path: Path,
    ) -> None:
        serial = str(tmp_path / "serial.log")
        mock_proc = MagicMock(pid=11111)
        mock_popen.return_value = mock_proc
        config = VMConfig(
            image_path="/img", arch="x86_64",
            platform="qemu", serial_path=serial,
        )
        launch_vm(config)
        assert _get_proc(11111) is mock_proc

    def test_firecracker_no_kvm_raises(
        self, tmp_path: Path,
    ) -> None:
        serial = str(tmp_path / "serial.log")
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

    @patch("qemu_harness.vm_launcher.has_kvm", return_value=True)
    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_firecracker_launch(
        self, mock_popen: MagicMock,
        mock_kvm_unused: MagicMock, tmp_path: Path,
    ) -> None:
        del mock_kvm_unused
        serial = str(tmp_path / "fc.log")
        mock_proc = MagicMock(pid=77777)
        mock_popen.return_value = mock_proc
        config = VMConfig(
            image_path=str(tmp_path / "guest.elf"),
            arch="x86_64",
            platform="firecracker", serial_path=serial,
        )
        handle = launch_vm(config)
        assert handle.pid == 77777
        assert handle.platform == "firecracker"
        assert handle.serial_path == serial
        assert handle.stderr_path == serial + ".stderr"
        # Config and stderr files materialize on disk.
        assert Path(serial + ".fc-config.json").exists()
        assert Path(serial + ".fc-log").exists()
        assert Path(serial + ".stderr").exists()
        # firecracker invoked with --no-api and --config-file.
        invoked = mock_popen.call_args[0][0]
        assert invoked[0] == "firecracker"
        assert "--no-api" in invoked
        assert "--config-file" in invoked
        cf_idx = invoked.index("--config-file")
        assert invoked[cf_idx + 1] == serial + ".fc-config.json"

    @patch("qemu_harness.vm_launcher.has_kvm", return_value=True)
    @patch("qemu_harness.vm_launcher.subprocess.Popen")
    def test_firecracker_config_file_content(
        self, mock_popen: MagicMock,
        mock_kvm_unused: MagicMock, tmp_path: Path,
    ) -> None:
        del mock_kvm_unused
        serial = str(tmp_path / "fc.log")
        mock_popen.return_value = MagicMock(pid=88888)
        image = str(tmp_path / "guest.elf")
        config = VMConfig(
            image_path=image, arch="x86_64",
            platform="firecracker", serial_path=serial,
        )
        launch_vm(config)
        cfg_doc = json.loads(
            Path(serial + ".fc-config.json").read_text(
                encoding="utf-8",
            ),
        )
        assert cfg_doc["boot-source"]["kernel_image_path"] == image
        assert not cfg_doc["drives"]
        assert (
            cfg_doc["logger"]["log_path"] == serial + ".fc-log"
        )
        assert cfg_doc["machine-config"]["vcpu_count"] == 1


class TestFirecrackerHelpers:
    """Tests for firecracker config/path helpers."""

    def test_log_path_co_located(self) -> None:
        assert (
            _firecracker_log_path("/tmp/x.log")
            == "/tmp/x.log.fc-log"
        )

    def test_config_path_co_located(self) -> None:
        assert (
            _firecracker_config_path("/tmp/x.log")
            == "/tmp/x.log.fc-config.json"
        )

    def test_vm_id_from_stem(self) -> None:
        assert (
            _firecracker_vm_id("/tmp/test-foo.log") == "test-foo"
        )

    def test_config_dict_shape(self) -> None:
        config = VMConfig(
            image_path="/tmp/g.elf", arch="x86_64",
            platform="firecracker",
            serial_path="/tmp/s.log",
        )
        doc = _firecracker_config_dict(config)
        assert doc["boot-source"]["kernel_image_path"] == "/tmp/g.elf"
        assert not doc["drives"]
        assert doc["machine-config"]["vcpu_count"] == 1
        assert doc["machine-config"]["mem_size_mib"] == 128
        assert doc["logger"]["log_path"] == "/tmp/s.log.fc-log"
        assert doc["logger"]["level"] == "Info"

    def test_args_shape(self) -> None:
        args = _firecracker_args("/tmp/c.json", "fc-test")
        assert args[0] == "firecracker"
        assert "--no-api" in args
        assert "--config-file" in args
        assert "/tmp/c.json" in args
        assert "--id" in args
        assert "fc-test" in args


class TestWaitForReady:
    """Tests for wait_for_ready()."""

    def test_marker_found(self, tmp_path: Path) -> None:
        serial = str(tmp_path / "serial.log")
        Path(serial).write_bytes(b"booting...\nREADY\n")
        handle = VMHandle(
            pid=1, serial_path=serial, stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        assert wait_for_ready(handle, "READY", 1.0) is True

    def test_timeout(self, tmp_path: Path) -> None:
        serial = str(tmp_path / "serial.log")
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


class TestSignalPid:
    """Tests for _signal_pid()."""

    @patch("qemu_harness.vm_launcher.os.kill")
    def test_signal_delivered(self, mock_kill: MagicMock) -> None:
        mock_kill.return_value = None
        assert _signal_pid(123, 15) is True
        mock_kill.assert_called_once_with(123, 15)

    @patch("qemu_harness.vm_launcher.os.kill")
    def test_process_gone_returns_false(
        self, mock_kill: MagicMock,
    ) -> None:
        mock_kill.side_effect = ProcessLookupError
        assert _signal_pid(123, 15) is False

    @patch("qemu_harness.vm_launcher.os.kill")
    def test_permission_denied_treats_as_existing(
        self, mock_kill: MagicMock,
    ) -> None:
        # PID recycled to another user; we cannot signal it,
        # but it exists, so contract is "process exists" -> True.
        mock_kill.side_effect = PermissionError
        assert _signal_pid(123, 15) is True


class TestTryWaitpid:
    """Tests for _try_waitpid()."""

    @patch("qemu_harness.vm_launcher.os.waitpid")
    def test_reaped(self, mock_wait: MagicMock) -> None:
        mock_wait.return_value = (123, 0)
        assert _try_waitpid(123) is True

    @patch("qemu_harness.vm_launcher.os.waitpid")
    def test_not_exited(self, mock_wait: MagicMock) -> None:
        mock_wait.return_value = (0, 0)
        assert _try_waitpid(123) is False

    @patch("qemu_harness.vm_launcher.os.waitpid")
    def test_not_child(self, mock_wait: MagicMock) -> None:
        mock_wait.side_effect = ChildProcessError
        assert _try_waitpid(123) is True


class TestKillViaPid:
    """Tests for _kill_via_pid()."""

    @patch("qemu_harness.vm_launcher._try_waitpid")
    @patch("qemu_harness.vm_launcher.os.kill")
    def test_already_dead(
        self, mock_kill: MagicMock,
        mock_wait_unused: MagicMock,
    ) -> None:
        del mock_wait_unused
        mock_kill.side_effect = ProcessLookupError
        _kill_via_pid(1)
        mock_kill.assert_called_once_with(1, 15)

    @patch("qemu_harness.vm_launcher._try_waitpid")
    @patch("qemu_harness.vm_launcher.time.sleep")
    @patch("qemu_harness.vm_launcher.time.monotonic")
    @patch("qemu_harness.vm_launcher.os.kill")
    def test_exits_after_sigterm(
        self, mock_kill: MagicMock,
        mock_time: MagicMock,
        mock_sleep_unused: MagicMock,
        mock_wait: MagicMock,
    ) -> None:
        del mock_sleep_unused
        mock_wait.return_value = True
        mock_kill.return_value = None
        mock_time.side_effect = [0.0, 0.0]
        _kill_via_pid(1)
        mock_kill.assert_called_with(1, 15)
        mock_wait.assert_called()


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
