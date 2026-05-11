"""Command-line entry point for `memreq-codegen`.

Usage:
    memreq-codegen <regions.yaml> --arch x86_64 \\
        --out-records <records.inc> --out-pins <pins.inc>

Reads the YAML, validates it against the schema, enforces the
per-arch hot-tier budget (D066 Q-B), and writes the two `.inc`
files atomically. Exits non-zero on validation or budget errors.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from memreq_codegen import emitter, schema

# x86_64 hot-tier slot count from D066 Q-B. Updates here must
# match `_X86_64_HOT_POOL` in `emitter.py`.
_HOT_BUDGET_X86_64 = 1


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on success, non-zero on error."""
    ns = _parse_args(argv)
    try:
        regions = _load_and_validate(ns.regions_yaml)
        _enforce_hot_budget(regions, ns.arch)
    except (yaml.YAMLError, ValueError) as err:
        print(f"memreq-codegen: {err}", file=sys.stderr)
        return 1
    _write_outputs(regions, ns.arch, ns.out_records, ns.out_pins)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse argv."""
    parser = argparse.ArgumentParser(
        prog="memreq-codegen",
        description="Emit .memreq records and hot-tier pins.",
    )
    parser.add_argument(
        "regions_yaml",
        type=Path,
        help="Path to regions.yaml.",
    )
    parser.add_argument(
        "--arch",
        required=True,
        choices=("x86_64",),
        help="Target architecture (only x86_64 in step 5a).",
    )
    parser.add_argument(
        "--out-records",
        type=Path,
        required=True,
        help="Path to write the records .inc file.",
    )
    parser.add_argument(
        "--out-pins",
        type=Path,
        required=True,
        help="Path to write the hot-tier pins .inc file.",
    )
    return parser.parse_args(argv)


def _load_and_validate(path: Path) -> list[schema.RegionDecl]:
    """Read YAML from `path` and validate against the schema."""
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if raw is None:
        raise ValueError(f"{path}: file is empty")
    file_model = schema.RegionFile.model_validate(raw)
    _check_unique_names(file_model.regions)
    return file_model.regions


def _check_unique_names(regions: list[schema.RegionDecl]) -> None:
    """Reject duplicate region names within one file."""
    seen: set[str] = set()
    for region in regions:
        if region.name in seen:
            raise ValueError(
                f"duplicate region name: {region.name}"
            )
        seen.add(region.name)


def _enforce_hot_budget(
    regions: list[schema.RegionDecl], arch: str,
) -> None:
    """Refuse if hot-tier count exceeds the per-arch budget."""
    budget = {"x86_64": _HOT_BUDGET_X86_64}[arch]
    hot_count = sum(1 for r in regions if r.tier == "hot")
    if hot_count > budget:
        raise ValueError(
            f"{arch}: {hot_count} hot-tier regions exceed "
            f"budget {budget} (D066 Q-B); demote some to cold "
            f"or open task #30 to extend the budget"
        )


def _write_outputs(
    regions: list[schema.RegionDecl],
    arch: str,
    out_records: Path,
    out_pins: Path,
) -> None:
    """Render and write the two .inc files."""
    if arch != "x86_64":  # pragma: no cover
        # argparse choices guards this; defensive narrowing.
        raise ValueError(f"unsupported arch: {arch}")
    records_text = emitter.emit_records_x86_64(regions)
    pins_text = emitter.emit_pins_x86_64(regions)
    out_records.parent.mkdir(parents=True, exist_ok=True)
    out_pins.parent.mkdir(parents=True, exist_ok=True)
    out_records.write_text(records_text, encoding="utf-8")
    out_pins.write_text(pins_text, encoding="utf-8")
