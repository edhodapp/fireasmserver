"""Tests for test_runner module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from qemu_harness.test_runner import (
    SuiteResult,
    TestCase,
    TestResult,
    TestSuite,
    _boot_and_test,
    _build_image,
    _run_case,
    _should_skip,
    check_http,
    check_serial,
    run_suite,
)
from qemu_harness.vm_launcher import VMHandle


class TestCheckSerial:
    """Tests for check_serial()."""

    def test_found(self, tmp_path: object) -> None:
        p = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        Path(p).write_text("boot OK\nREADY\n")
        r = check_serial(p, "READY")
        assert r.passed is True

    def test_not_found(self, tmp_path: object) -> None:
        p = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        Path(p).write_text("boot OK\n")
        r = check_serial(p, "READY")
        assert r.passed is False
        assert "not found" in r.message


class TestCheckHttp:
    """Tests for check_http()."""

    @patch("qemu_harness.test_runner.urllib.request.urlopen")
    def test_success(
        self, mock_open: MagicMock,
    ) -> None:
        resp = MagicMock()
        resp.read.return_value = b"Hello World"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        r = check_http("localhost", 8080, "Hello")
        assert r.passed is True

    @patch("qemu_harness.test_runner.urllib.request.urlopen")
    def test_mismatch(
        self, mock_open: MagicMock,
    ) -> None:
        resp = MagicMock()
        resp.read.return_value = b"Goodbye"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        mock_open.return_value = resp
        r = check_http("localhost", 8080, "Hello")
        assert r.passed is False

    @patch("qemu_harness.test_runner.urllib.request.urlopen")
    def test_connection_error(
        self, mock_open: MagicMock,
    ) -> None:
        mock_open.side_effect = ConnectionError("refused")
        r = check_http("localhost", 8080, "Hello")
        assert r.passed is False
        assert "failed" in r.message


class TestRunCase:
    """Tests for _run_case()."""

    def test_serial_check(self, tmp_path: object) -> None:
        p = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        Path(p).write_text("READY")
        handle = VMHandle(
            pid=1, serial_path=p, stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        case = TestCase(
            name="boot_marker", check_type="serial",
            expected="READY",
        )
        r = _run_case(case, handle)
        assert r.passed is True
        assert r.name == "boot_marker"

    def test_unknown_type(self) -> None:
        handle = VMHandle(
            pid=1, serial_path="/s", stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        case = TestCase(
            name="bad", check_type="grpc",
            expected="ok",
        )
        r = _run_case(case, handle)
        assert r.passed is False
        assert "Unknown" in r.message


class TestShouldSkip:
    """Tests for _should_skip()."""

    def test_qemu_never_skips(self) -> None:
        suite = TestSuite(
            arch="x86_64", platform="qemu",
            source_dir="/src",
        )
        assert _should_skip(suite) is None

    @patch("qemu_harness.test_runner.has_kvm")
    def test_firecracker_no_kvm_skips(
        self, mock_kvm: MagicMock,
    ) -> None:
        mock_kvm.return_value = False
        suite = TestSuite(
            arch="x86_64", platform="firecracker",
            source_dir="/src",
        )
        assert _should_skip(suite) is not None

    @patch("qemu_harness.test_runner.has_kvm")
    def test_firecracker_with_kvm_runs(
        self, mock_kvm: MagicMock,
    ) -> None:
        mock_kvm.return_value = True
        suite = TestSuite(
            arch="x86_64", platform="firecracker",
            source_dir="/src",
        )
        assert _should_skip(suite) is None


class TestBuildImage:
    """Tests for _build_image()."""

    @patch("qemu_harness.test_runner.build_guest")
    def test_success(
        self, mock_build: MagicMock,
    ) -> None:
        mock_build.return_value = Path("/out/guest.elf")
        suite = TestSuite(
            arch="x86_64", platform="qemu",
            source_dir="/src",
        )
        result = _build_image(suite, None)
        assert isinstance(result, Path)

    @patch("qemu_harness.test_runner.build_guest")
    def test_failure(
        self, mock_build: MagicMock,
    ) -> None:
        mock_build.side_effect = FileNotFoundError("no .S")
        suite = TestSuite(
            arch="x86_64", platform="qemu",
            source_dir="/src",
        )
        result = _build_image(suite, None)
        assert isinstance(result, TestResult)
        assert result.passed is False


class TestBootAndTest:
    """Tests for _boot_and_test()."""

    @patch("qemu_harness.test_runner.wait_for_ready")
    def test_timeout(
        self, mock_wait: MagicMock,
    ) -> None:
        mock_wait.return_value = False
        handle = VMHandle(
            pid=1, serial_path="/s", stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        suite = TestSuite(
            arch="x86_64", platform="qemu",
            source_dir="/src",
        )
        results = _boot_and_test(handle, suite)
        assert len(results) == 1
        assert results[0].passed is False

    @patch("qemu_harness.test_runner.wait_for_ready")
    def test_runs_cases(
        self, mock_wait: MagicMock, tmp_path: object,
    ) -> None:
        mock_wait.return_value = True
        p = str(tmp_path) + "/serial.log"  # type: ignore[operator]
        Path(p).write_text("READY")
        handle = VMHandle(
            pid=1, serial_path=p, stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        suite = TestSuite(
            arch="x86_64", platform="qemu",
            source_dir="/src",
            cases=[TestCase(
                name="marker", check_type="serial",
                expected="READY",
            )],
        )
        results = _boot_and_test(handle, suite)
        assert len(results) == 1
        assert results[0].passed is True


class TestSuiteResult:
    """Tests for SuiteResult.all_passed."""

    def test_all_pass(self) -> None:
        sr = SuiteResult(
            arch="x86_64", platform="qemu",
            results=[
                TestResult(name="a", passed=True),
                TestResult(name="b", passed=True),
            ],
        )
        assert sr.all_passed is True

    def test_one_fails(self) -> None:
        sr = SuiteResult(
            arch="x86_64", platform="qemu",
            results=[
                TestResult(name="a", passed=True),
                TestResult(name="b", passed=False),
            ],
        )
        assert sr.all_passed is False

    def test_empty(self) -> None:
        sr = SuiteResult(
            arch="x86_64", platform="qemu",
        )
        assert sr.all_passed is True


class TestRunSuite:
    """Tests for run_suite()."""

    @patch("qemu_harness.test_runner.has_kvm")
    def test_skip_firecracker_no_kvm(
        self, mock_kvm: MagicMock,
    ) -> None:
        mock_kvm.return_value = False
        suite = TestSuite(
            arch="x86_64", platform="firecracker",
            source_dir="/src",
        )
        result = run_suite(suite)
        assert result.all_passed is True
        assert "skipped" in result.results[0].message

    @patch("qemu_harness.test_runner.build_guest")
    def test_build_failure(
        self, mock_build: MagicMock,
    ) -> None:
        mock_build.side_effect = FileNotFoundError("no .S")
        suite = TestSuite(
            arch="x86_64", platform="qemu",
            source_dir="/src",
        )
        result = run_suite(suite)
        assert result.all_passed is False

    @patch("qemu_harness.test_runner.kill_vm")
    @patch("qemu_harness.test_runner.wait_for_ready")
    @patch("qemu_harness.test_runner.launch_vm")
    @patch("qemu_harness.test_runner.build_guest")
    def test_full_pass(
        self, mock_build: MagicMock,
        mock_launch: MagicMock,
        mock_wait: MagicMock,
        mock_kill: MagicMock,
        tmp_path: object,
    ) -> None:
        mock_build.return_value = Path("/img")
        serial = str(tmp_path) + "/s.log"  # type: ignore[operator]
        Path(serial).write_text("READY")
        mock_launch.return_value = VMHandle(
            pid=1, serial_path=serial, stderr_path="/s.err",
            arch="x86_64", platform="qemu",
        )
        mock_wait.return_value = True
        suite = TestSuite(
            arch="x86_64", platform="qemu",
            source_dir="/src",
            cases=[TestCase(
                name="marker", check_type="serial",
                expected="READY",
            )],
        )
        result = run_suite(suite, serial_path=serial)
        assert result.all_passed is True
        mock_kill.assert_called_once()
