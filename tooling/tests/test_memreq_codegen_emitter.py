"""Tests for memreq_codegen.emitter."""

from __future__ import annotations

import pytest

from memreq_codegen.emitter import (
    emit_pins_aarch64,
    emit_pins_x86_64,
    emit_records_aarch64,
    emit_records_x86_64,
)
from memreq_codegen.schema import RegionDecl


def _region(**overrides: object) -> RegionDecl:
    base: dict[str, object] = {
        "name": "rx_buffer",
        "tier": "cold",
        "lifetime": "steady_state",
        "owner": 0,
        "writable": True,
        "size": 4096,
        "align": 4096,
    }
    base.update(overrides)
    return RegionDecl.model_validate(base)


class TestEmitRecordsX8664:
    """`memreq_records.inc` rendering."""

    def test_section_header_first(self) -> None:
        out = emit_records_x86_64([_region()])
        # Section directive must appear before any record.
        section_idx = out.index("[section .memreq")
        record_idx = out.index("__memreq_rec__rx_buffer")
        assert section_idx < record_idx

    def test_record_label_emitted(self) -> None:
        out = emit_records_x86_64([_region()])
        assert "global __memreq_rec__rx_buffer" in out
        assert "__memreq_rec__rx_buffer:" in out

    def test_assigned_label_emitted(self) -> None:
        out = emit_records_x86_64([_region()])
        assert "global __memreq_assigned__rx_buffer" in out
        assert "__memreq_assigned__rx_buffer:" in out

    def test_addr_alias_emitted(self) -> None:
        out = emit_records_x86_64([_region()])
        assert (
            "__memreq_addr__rx_buffer equ "
            "__memreq_assigned__rx_buffer"
        ) in out

    def test_size_alias_at_offset_8(self) -> None:
        out = emit_records_x86_64([_region()])
        # __memreq_size__ points to assigned+8 (assigned_size word).
        assert (
            "__memreq_size__rx_buffer equ "
            "__memreq_assigned__rx_buffer + 8"
        ) in out

    def test_lifetime_byte_matches_enum(self) -> None:
        # steady_state -> 0
        out = emit_records_x86_64([
            _region(name="a", lifetime="steady_state"),
        ])
        assert "db      0                          ; lifetime" in out

        # stack -> 3
        out2 = emit_records_x86_64([
            _region(name="b", lifetime="stack"),
        ])
        assert "db      3                          ; lifetime" in out2

    def test_writable_emits_one(self) -> None:
        out = emit_records_x86_64([_region(writable=True)])
        assert "db      1                          ; writable" in out

    def test_non_writable_emits_zero(self) -> None:
        out = emit_records_x86_64([_region(writable=False)])
        assert "db      0                          ; writable" in out

    def test_name_hash_format(self) -> None:
        # 8-hex-digit lowercase u32.
        out = emit_records_x86_64([_region(name="smoke_test")])
        # FNV-1a("smoke_test") = 0x9b6d2f4f
        assert "dd      0x9b6d2f4f" in out

    def test_size_bytecode_present(self) -> None:
        out = emit_records_x86_64([_region(size=4096)])
        # LIT 4096; END at the start of size_bc
        assert "0x01, 0x00, 0x10, 0x00, 0x00, 0x00" in out

    def test_align_bytecode_present(self) -> None:
        out = emit_records_x86_64([_region(align=8)])
        # LIT 8; END at the start of align_bc
        assert "0x01, 0x08, 0x00, 0x00, 0x00, 0x00" in out

    def test_multiple_regions_each_get_record(self) -> None:
        out = emit_records_x86_64([
            _region(name="a"),
            _region(name="b"),
            _region(name="c"),
        ])
        for name in ("a", "b", "c"):
            assert f"__memreq_rec__{name}:" in out

    def test_trailing_sect_restores_section(self) -> None:
        # Without trailing __SECT__, %include'ing this file inside
        # boot.S's .text would leave subsequent code in .memreq.
        out = emit_records_x86_64([_region()])
        assert out.rstrip().endswith("__SECT__")


class TestEmitPinsX8664:
    """`memreq_pin_hot.inc` rendering."""

    def test_empty_when_no_hot_regions(self) -> None:
        out = emit_pins_x86_64([_region(tier="cold")])
        assert "no hot-tier regions" in out
        assert "mov" not in out

    def test_one_hot_region_pins_r15(self) -> None:
        out = emit_pins_x86_64([_region(tier="hot")])
        assert "mov     r15, [__memreq_assigned__rx_buffer + 0]" in out

    def test_pin_skips_cold_and_init(self) -> None:
        # Only the hot region produces a pin; cold/init are absent.
        out = emit_pins_x86_64([
            _region(name="a", tier="cold"),
            _region(name="b", tier="hot"),
            _region(name="c", tier="init"),
        ])
        assert "__memreq_assigned__b" in out
        assert "__memreq_assigned__a" not in out
        assert "__memreq_assigned__c" not in out

    @pytest.mark.parametrize("tier", ["cold", "init"])
    def test_non_hot_tiers_dont_pin(self, tier: str) -> None:
        out = emit_pins_x86_64([_region(tier=tier)])
        assert "mov" not in out

    def test_hot_count_over_pool_raises(self) -> None:
        # x86_64 pool has 1 slot; two hot regions must raise even
        # when callers bypass the CLI's budget check.
        with pytest.raises(ValueError, match="pool size"):
            emit_pins_x86_64([
                _region(name="hot_a", tier="hot"),
                _region(name="hot_b", tier="hot"),
            ])


class TestEmitRecordsAarch64:
    """`memreq_records.inc` rendering on aarch64 (GNU-as syntax)."""

    def test_section_directive_uses_pushsection(self) -> None:
        out = emit_records_aarch64([_region()])
        assert ".pushsection .memreq" in out
        assert out.rstrip().endswith(".popsection")

    def test_record_label_emitted(self) -> None:
        out = emit_records_aarch64([_region()])
        assert ".global __memreq_rec__rx_buffer" in out
        assert "__memreq_rec__rx_buffer:" in out

    def test_assigned_label_emitted(self) -> None:
        out = emit_records_aarch64([_region()])
        assert ".global __memreq_assigned__rx_buffer" in out
        assert "__memreq_assigned__rx_buffer:" in out

    def test_addr_alias_emitted_as_equ(self) -> None:
        out = emit_records_aarch64([_region()])
        assert (
            ".equ __memreq_addr__rx_buffer, "
            "__memreq_assigned__rx_buffer"
        ) in out

    def test_size_alias_at_offset_8(self) -> None:
        out = emit_records_aarch64([_region()])
        assert (
            ".equ __memreq_size__rx_buffer, "
            "__memreq_assigned__rx_buffer + 8"
        ) in out

    def test_word_form_for_name_hash(self) -> None:
        # GNU-as uses .word for 32-bit (vs NASM's dd).
        out = emit_records_aarch64([_region(name="smoke_test")])
        # FNV-1a("smoke_test") = 0x9b6d2f4f
        assert ".word   0x9b6d2f4f" in out

    def test_byte_form_for_bytecode(self) -> None:
        out = emit_records_aarch64([_region(size=4096)])
        # LIT 4096; END at the start of size_bc, as .byte directives
        assert ".byte   0x01, 0x00, 0x10, 0x00, 0x00, 0x00" in out

    def test_short_form_for_owner_id(self) -> None:
        out = emit_records_aarch64([_region(owner=42)])
        assert ".short  42" in out

    def test_lifetime_byte_matches_enum(self) -> None:
        out = emit_records_aarch64([_region(lifetime="stack")])
        assert ".byte   3" in out

    def test_balign_8(self) -> None:
        # Records are 8-byte aligned (REQ MR-007).
        out = emit_records_aarch64([_region()])
        assert ".balign 8" in out


class TestEmitPinsAarch64:
    """`memreq_pin_hot.inc` rendering on aarch64."""

    def test_empty_when_no_hot_regions(self) -> None:
        out = emit_pins_aarch64([_region(tier="cold")])
        assert "no hot-tier regions" in out
        assert "ldr" not in out

    def test_one_hot_region_pins_x19(self) -> None:
        out = emit_pins_aarch64([_region(tier="hot")])
        assert "adrp    x19, __memreq_assigned__rx_buffer" in out
        assert (
            "add     x19, x19, :lo12:__memreq_assigned__rx_buffer"
        ) in out
        assert "ldr     x19, [x19]" in out

    def test_second_hot_region_pins_x20(self) -> None:
        out = emit_pins_aarch64([
            _region(name="a", tier="hot"),
            _region(name="b", tier="hot"),
        ])
        assert "adrp    x19, __memreq_assigned__a" in out
        assert "adrp    x20, __memreq_assigned__b" in out

    def test_hot_count_over_pool_raises(self) -> None:
        # aarch64 pool has 7 slots; 8 hot regions must raise.
        regions = [
            _region(name=f"h{i}", tier="hot") for i in range(8)
        ]
        with pytest.raises(ValueError, match="pool size"):
            emit_pins_aarch64(regions)

    def test_seven_hot_regions_ok(self) -> None:
        # Pool capacity, fully used: no overflow.
        regions = [
            _region(name=f"h{i}", tier="hot") for i in range(7)
        ]
        out = emit_pins_aarch64(regions)
        for slot in ("x19", "x20", "x21", "x22", "x23", "x24", "x25"):
            assert f"adrp    {slot}, __memreq_assigned__" in out
