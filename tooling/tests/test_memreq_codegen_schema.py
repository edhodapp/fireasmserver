"""Tests for memreq_codegen.schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memreq_codegen.schema import RegionDecl, RegionFile


def _valid_region(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "smoke_test",
        "tier": "cold",
        "lifetime": "steady_state",
        "owner": 0,
        "writable": True,
        "size": 4096,
        "align": 4096,
    }
    base.update(overrides)
    return base


class TestRegionDecl:
    """Field validation on a single region."""

    def test_valid_minimal(self) -> None:
        r = RegionDecl.model_validate(_valid_region())
        assert r.name == "smoke_test"
        assert r.tier == "cold"
        assert r.doc == ""

    def test_name_rejects_uppercase(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(name="SmokeTest"))

    def test_name_rejects_leading_digit(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(name="9foo"))

    def test_name_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(name=""))

    def test_name_rejects_too_long(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(name="a" * 65))

    def test_tier_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(tier="warm"))

    def test_lifetime_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(lifetime="forever"))

    def test_owner_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(owner=-1))

    def test_owner_rejects_over_u16(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(owner=0x10000))

    def test_size_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(size=0))

    def test_size_rejects_over_u32(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(size=0x100000000))

    def test_align_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(align=0))

    def test_extra_field_rejected(self) -> None:
        # extra="forbid" — typos surface as errors, not silent drops.
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(
                _valid_region(tier_extra="cold"),
            )


class TestRegionFile:
    """Top-level YAML structure."""

    def test_empty_regions_list_ok(self) -> None:
        rf = RegionFile.model_validate({"regions": []})
        assert rf.regions == []

    def test_multiple_regions(self) -> None:
        rf = RegionFile.model_validate({
            "regions": [
                _valid_region(name="a"),
                _valid_region(name="b"),
            ],
        })
        assert [r.name for r in rf.regions] == ["a", "b"]

    def test_extra_top_level_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RegionFile.model_validate({
                "regions": [],
                "spurious": 1,
            })


class TestRegionDeclExpressions:
    """Non-literal size/align — discriminated-union op lists."""

    def test_lit_op_alone(self) -> None:
        r = RegionDecl.model_validate(_valid_region(
            size=[{"kind": "lit", "value": 524288}],
        ))
        assert isinstance(r.size, list)
        assert len(r.size) == 1

    def test_cpu_field_resolves(self) -> None:
        r = RegionDecl.model_validate(_valid_region(
            size=[{"kind": "cpu", "field": "l1d_line_bytes"}],
        ))
        assert isinstance(r.size, list)
        # field name preserved on the model for round-trip clarity;
        # encoder converts it to the positional id at emit time.
        assert r.size[0].field == "l1d_line_bytes"  # type: ignore[union-attr]

    def test_cpu_unknown_field_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="unknown CpuCharacteristics",
        ):
            RegionDecl.model_validate(_valid_region(
                size=[{"kind": "cpu", "field": "no_such_field"}],
            ))

    def test_tuning_field_resolves(self) -> None:
        r = RegionDecl.model_validate(_valid_region(
            align=[{"kind": "tuning", "field": "rx_queue_depth"}],
        ))
        assert isinstance(r.align, list)
        assert r.align[0].field == "rx_queue_depth"  # type: ignore[union-attr]

    def test_tuning_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown TuningProfile"):
            RegionDecl.model_validate(_valid_region(
                align=[{"kind": "tuning", "field": "bogus"}],
            ))

    def test_full_expression_lit_lit_mul(self) -> None:
        r = RegionDecl.model_validate(_valid_region(
            size=[
                {"kind": "lit", "value": 256},
                {"kind": "lit", "value": 2048},
                {"kind": "mul"},
            ],
        ))
        assert isinstance(r.size, list)
        assert len(r.size) == 3

    def test_cpu_mul_alignup_expression(self) -> None:
        # Realistic shape: cores_sharing_l3 * 4096, aligned up to 4 KiB.
        r = RegionDecl.model_validate(_valid_region(
            size=[
                {"kind": "cpu", "field": "cores_sharing_l3"},
                {"kind": "lit", "value": 4096},
                {"kind": "mul"},
                {"kind": "lit", "value": 4096},
                {"kind": "align_up"},
            ],
        ))
        assert len(r.size) == 5  # type: ignore[arg-type]

    def test_div_lit_op(self) -> None:
        r = RegionDecl.model_validate(_valid_region(
            size=[
                {"kind": "lit", "value": 4096},
                {"kind": "div_lit", "divisor": 4},
            ],
        ))
        assert len(r.size) == 2  # type: ignore[arg-type]

    def test_div_lit_zero_rejected(self) -> None:
        # divisor=0 is meaningless and the VM raises on it — catch
        # at YAML load instead of letting it reach the interpreter.
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(
                size=[
                    {"kind": "lit", "value": 4096},
                    {"kind": "div_lit", "divisor": 0},
                ],
            ))

    def test_call_thunk_op(self) -> None:
        r = RegionDecl.model_validate(_valid_region(
            size=[{"kind": "call_thunk", "thunk_id": 0x12345678}],
        ))
        assert len(r.size) == 1  # type: ignore[arg-type]

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(
                size=[{"kind": "xor"}],
            ))

    def test_extra_field_on_op_rejected(self) -> None:
        # `extra="forbid"` on each op model — typos surface clearly.
        with pytest.raises(ValidationError):
            RegionDecl.model_validate(_valid_region(
                size=[{"kind": "lit", "value": 4096, "extra": 1}],
            ))

    def test_empty_op_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="empty op list"):
            RegionDecl.model_validate(_valid_region(size=[]))

    def test_literal_shortcut_still_works(self) -> None:
        # Backward-compatible path: integer size unchanged.
        r = RegionDecl.model_validate(_valid_region(size=4096))
        assert r.size == 4096
