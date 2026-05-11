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
