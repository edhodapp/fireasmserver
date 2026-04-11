"""Orchestrate build, boot, test, kill, report."""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from pydantic import BaseModel

from qemu_harness.guest_builder import build_guest
from qemu_harness.vm_launcher import (
    Platform,
    VMConfig,
    VMHandle,
    has_kvm,
    kill_vm,
    launch_vm,
    wait_for_ready,
)


class TestCase(BaseModel):
    """A single verification against a booted guest."""

    name: str
    check_type: str
    expected: str
    http_port: int = 80


class TestResult(BaseModel):
    """Outcome of a single test case."""

    name: str
    passed: bool
    actual: str = ""
    message: str = ""


class TestSuite(BaseModel):
    """Collection of test cases for a target."""

    arch: str
    platform: Platform
    source_dir: str
    ready_marker: str = "READY"
    boot_timeout: float = 10.0
    cases: list[TestCase] = []


class SuiteResult(BaseModel):
    """Aggregate results for a test suite run."""

    arch: str
    platform: str
    results: list[TestResult] = []

    @property
    def all_passed(self) -> bool:
        """Return True if every test passed."""
        return all(r.passed for r in self.results)


def check_serial(
    serial_path: str,
    expected: str,
) -> TestResult:
    """Check serial output file for expected content."""
    raw = Path(serial_path).read_bytes()
    content = raw.decode(errors="replace")
    passed = expected in content
    return TestResult(
        name="serial_check",
        passed=passed,
        actual=content,
        message="" if passed else (
            f"Expected '{expected}' not found in serial"
        ),
    )


def check_http(
    host: str,
    port: int,
    expected: str,
) -> TestResult:
    """Make HTTP GET and verify response body."""
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
    except (OSError, urllib.error.URLError) as exc:
        return TestResult(
            name="http_check",
            passed=False,
            actual="",
            message=f"HTTP request failed: {exc}",
        )
    passed = expected in body
    return TestResult(
        name="http_check",
        passed=passed,
        actual=body,
        message="" if passed else (
            f"Expected '{expected}' not in response"
        ),
    )


def _run_case(
    case: TestCase,
    handle: VMHandle,
) -> TestResult:
    """Run a single test case against a booted VM."""
    if case.check_type == "serial":
        result = check_serial(
            handle.serial_path, case.expected,
        )
    elif case.check_type == "http":
        result = check_http(
            "localhost", case.http_port, case.expected,
        )
    else:
        result = TestResult(
            name=case.name,
            passed=False,
            message=f"Unknown check_type: {case.check_type}",
        )
    return TestResult(
        name=case.name,
        passed=result.passed,
        actual=result.actual,
        message=result.message,
    )


def _should_skip(suite: TestSuite) -> str | None:
    """Return skip reason, or None if runnable."""
    if suite.platform == "firecracker" and not has_kvm():
        return "Firecracker requires /dev/kvm (skipped)"
    return None


def _build_image(
    suite: TestSuite,
    build_dir: str | None,
) -> Path | TestResult:
    """Build the guest image. Return Path or failure."""
    try:
        return build_guest(
            suite.arch, suite.platform, suite.source_dir,
            build_dir=build_dir,
        )
    except (
        FileNotFoundError, subprocess.CalledProcessError,
    ) as exc:
        return TestResult(
            name="build",
            passed=False,
            message=f"Build failed: {exc}",
        )


def _boot_and_test(
    handle: VMHandle,
    suite: TestSuite,
) -> list[TestResult]:
    """Wait for ready, run cases, return results."""
    ready = wait_for_ready(
        handle, suite.ready_marker, suite.boot_timeout,
    )
    if not ready:
        return [TestResult(
            name="boot",
            passed=False,
            message="Boot timed out waiting for marker",
        )]
    return [_run_case(c, handle) for c in suite.cases]


def run_suite(
    suite: TestSuite,
    build_dir: str | None = None,
    serial_path: str | None = None,
) -> SuiteResult:
    """Run a complete test suite: build, boot, test, kill.

    Returns results even if boot fails or tests error.
    """
    result = SuiteResult(
        arch=suite.arch, platform=suite.platform,
    )
    skip_reason = _should_skip(suite)
    if skip_reason is not None:
        result.results.append(TestResult(
            name="skip", passed=True, message=skip_reason,
        ))
        return result
    image = _build_image(suite, build_dir)
    if isinstance(image, TestResult):
        result.results.append(image)
        return result
    s_path = serial_path or f"/tmp/serial_{suite.arch}.log"
    config = VMConfig(
        image_path=str(image),
        arch=suite.arch,
        platform=suite.platform,
        serial_path=s_path,
    )
    handle = launch_vm(config)
    try:
        result.results.extend(
            _boot_and_test(handle, suite),
        )
    finally:
        kill_vm(handle)
    return result
