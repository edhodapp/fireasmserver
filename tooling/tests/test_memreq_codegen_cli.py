"""Tests for memreq_codegen.cli."""

from __future__ import annotations

from pathlib import Path

import pytest

from memreq_codegen.cli import main


def _fixture_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "regions.yaml"
    p.write_text(body, encoding="utf-8")
    return p


_VALID_BODY = """
regions:
  - name: smoke_test
    tier: cold
    lifetime: steady_state
    owner: 0
    writable: true
    size: 4096
    align: 4096
"""


class TestMainHappyPath:
    """Successful runs return 0 and write both outputs."""

    def test_returns_zero_and_writes_files(
        self, tmp_path: Path,
    ) -> None:
        yaml_path = _fixture_yaml(tmp_path, _VALID_BODY)
        out_records = tmp_path / "out" / "records.inc"
        out_pins = tmp_path / "out" / "pins.inc"
        rc = main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(out_records),
            "--out-pins", str(out_pins),
        ])
        assert rc == 0
        assert out_records.exists()
        assert out_pins.exists()

    def test_records_output_contains_region(
        self, tmp_path: Path,
    ) -> None:
        yaml_path = _fixture_yaml(tmp_path, _VALID_BODY)
        out_records = tmp_path / "records.inc"
        out_pins = tmp_path / "pins.inc"
        main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(out_records),
            "--out-pins", str(out_pins),
        ])
        text = out_records.read_text(encoding="utf-8")
        assert "__memreq_rec__smoke_test:" in text

    def test_pins_output_shows_no_hot_when_cold_only(
        self, tmp_path: Path,
    ) -> None:
        yaml_path = _fixture_yaml(tmp_path, _VALID_BODY)
        out_records = tmp_path / "records.inc"
        out_pins = tmp_path / "pins.inc"
        main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(out_records),
            "--out-pins", str(out_pins),
        ])
        text = out_pins.read_text(encoding="utf-8")
        assert "no hot-tier regions" in text

    def test_creates_parent_directories(
        self, tmp_path: Path,
    ) -> None:
        # Output paths under a nested non-existent dir should be
        # auto-created.
        yaml_path = _fixture_yaml(tmp_path, _VALID_BODY)
        out_records = tmp_path / "nested" / "deep" / "r.inc"
        out_pins = tmp_path / "nested" / "deep" / "p.inc"
        rc = main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(out_records),
            "--out-pins", str(out_pins),
        ])
        assert rc == 0
        assert out_records.exists()

    def test_aarch64_emits_gnu_as_syntax(
        self, tmp_path: Path,
    ) -> None:
        yaml_path = _fixture_yaml(tmp_path, _VALID_BODY)
        out_records = tmp_path / "records.inc"
        out_pins = tmp_path / "pins.inc"
        rc = main([
            str(yaml_path),
            "--arch", "aarch64",
            "--out-records", str(out_records),
            "--out-pins", str(out_pins),
        ])
        assert rc == 0
        records_text = out_records.read_text(encoding="utf-8")
        # GNU-as `.global` / `.word` / `.byte` instead of NASM
        # `global` / `dd` / `db`.
        assert ".global __memreq_rec__smoke_test" in records_text
        assert ".word   0x" in records_text
        assert ".byte   0x" in records_text


class TestMainErrors:
    """Validation, budget, and parse errors return non-zero."""

    def test_empty_file_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        yaml_path = _fixture_yaml(tmp_path, "")
        rc = main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(tmp_path / "r.inc"),
            "--out-pins", str(tmp_path / "p.inc"),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "empty" in err.lower()

    def test_schema_violation_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Owner out of u16 range.
        yaml_path = _fixture_yaml(tmp_path, """
regions:
  - name: bad_owner
    tier: cold
    lifetime: steady_state
    owner: 70000
    writable: true
    size: 4096
    align: 4096
""")
        rc = main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(tmp_path / "r.inc"),
            "--out-pins", str(tmp_path / "p.inc"),
        ])
        assert rc == 1
        assert "memreq-codegen" in capsys.readouterr().err

    def test_duplicate_name_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        yaml_path = _fixture_yaml(tmp_path, """
regions:
  - name: dup
    tier: cold
    lifetime: steady_state
    owner: 0
    writable: true
    size: 4096
    align: 4096
  - name: dup
    tier: cold
    lifetime: steady_state
    owner: 0
    writable: true
    size: 4096
    align: 4096
""")
        rc = main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(tmp_path / "r.inc"),
            "--out-pins", str(tmp_path / "p.inc"),
        ])
        assert rc == 1
        assert "duplicate" in capsys.readouterr().err.lower()

    def test_hot_budget_exceedance_errors_x86_64(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # x86_64 budget is 1; two hot regions trip it.
        yaml_path = _fixture_yaml(tmp_path, """
regions:
  - name: hot_a
    tier: hot
    lifetime: steady_state
    owner: 0
    writable: true
    size: 4096
    align: 4096
  - name: hot_b
    tier: hot
    lifetime: steady_state
    owner: 0
    writable: true
    size: 4096
    align: 4096
""")
        rc = main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(tmp_path / "r.inc"),
            "--out-pins", str(tmp_path / "p.inc"),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "budget" in err.lower()
        assert "task #30" in err

    def test_invalid_yaml_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        yaml_path = _fixture_yaml(tmp_path, "regions: [not valid: yaml")
        rc = main([
            str(yaml_path),
            "--arch", "x86_64",
            "--out-records", str(tmp_path / "r.inc"),
            "--out-pins", str(tmp_path / "p.inc"),
        ])
        assert rc == 1
        assert "memreq-codegen" in capsys.readouterr().err
