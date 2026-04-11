"""Command-line interface for the QEMU test harness."""

from __future__ import annotations

import argparse
import json
import sys

from qemu_harness.test_runner import (
    SuiteResult,
    TestSuite,
    run_suite,
)

ARCHES = ["x86_64", "aarch64"]
PLATFORMS = ["qemu", "firecracker"]


def _print_result(result: SuiteResult) -> None:
    """Print test results for one suite."""
    label = f"{result.arch}/{result.platform}"
    for r in result.results:
        status = "PASS" if r.passed else "FAIL"
        line = f"  [{status}] {r.name}"
        if r.message:
            line += f" -- {r.message}"
        print(line)
    if result.all_passed:
        print(f"  {label}: ALL PASSED")
    else:
        print(f"  {label}: FAILED")


def _load_suite(path: str) -> TestSuite:
    """Load a test suite from a JSON file."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return TestSuite.model_validate(data)


def parse_args(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run QEMU test harness",
    )
    parser.add_argument(
        "--suite",
        required=True,
        help="Path to test suite JSON file",
    )
    parser.add_argument(
        "--arch",
        choices=ARCHES,
        help="Run only this architecture",
    )
    parser.add_argument(
        "--platform",
        choices=PLATFORMS,
        help="Run only this platform",
    )
    parser.add_argument(
        "--build-dir",
        default=None,
        help="Build output directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Run tests and return exit code."""
    args = parse_args(argv)
    suite = _load_suite(args.suite)
    if args.arch:
        suite.arch = args.arch
    if args.platform:
        suite.platform = args.platform
    result = run_suite(suite, build_dir=args.build_dir)
    _print_result(result)
    return 0 if result.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
