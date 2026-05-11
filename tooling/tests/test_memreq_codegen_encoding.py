"""Tests for memreq_codegen.encoding."""

from __future__ import annotations

import pytest

from memreq_codegen.encoding import (
    ALIGN_BYTECODE_BYTES,
    SIZE_BYTECODE_BYTES,
    encode_lit_bytecode,
    fnv1a_32,
)


class TestFnv1a32:
    """FNV-1a 32-bit hash."""

    def test_empty_string_is_offset_basis(self) -> None:
        # Standard FNV-1a property: hash of empty string is the
        # offset basis.
        assert fnv1a_32("") == 0x811C9DC5

    def test_single_char_a(self) -> None:
        # Reference: FNV-1a("a") = 0xe40c292c per the canonical
        # implementation.
        assert fnv1a_32("a") == 0xE40C292C

    def test_deterministic(self) -> None:
        assert fnv1a_32("foo") == fnv1a_32("foo")

    def test_distinct_inputs_distinct_hashes(self) -> None:
        # Collisions exist but adjacent short strings shouldn't hit
        # one in practice — useful regression-canary.
        assert fnv1a_32("a") != fnv1a_32("b")

    def test_result_is_u32(self) -> None:
        # Run a few inputs through and verify the result never
        # exceeds the u32 range.
        for s in ("", "a", "ab", "smoke_test", "x" * 100):
            h = fnv1a_32(s)
            assert 0 <= h <= 0xFFFFFFFF

    def test_utf8_encoding_used(self) -> None:
        # Non-ASCII goes through utf-8 (4-byte sequence for é);
        # different from any ASCII input.
        assert fnv1a_32("é") != fnv1a_32("e")


class TestEncodeLitBytecode:
    """LIT-only bytecode encoding."""

    def test_lit_4096_in_size_buffer(self) -> None:
        out = encode_lit_bytecode(4096, SIZE_BYTECODE_BYTES)
        assert len(out) == SIZE_BYTECODE_BYTES
        # LIT (0x01) + LE u32(4096 = 0x1000) + END (0x00) + zero pad
        assert out[:6] == bytes([0x01, 0x00, 0x10, 0x00, 0x00, 0x00])
        assert all(b == 0 for b in out[6:])

    def test_lit_1_in_align_buffer(self) -> None:
        out = encode_lit_bytecode(1, ALIGN_BYTECODE_BYTES)
        assert len(out) == ALIGN_BYTECODE_BYTES
        assert out[:6] == bytes([0x01, 0x01, 0x00, 0x00, 0x00, 0x00])
        assert out[6] == 0 and out[7] == 0

    def test_lit_max_u32(self) -> None:
        out = encode_lit_bytecode(0xFFFFFFFF, SIZE_BYTECODE_BYTES)
        assert out[:6] == bytes([0x01, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])

    def test_lit_zero(self) -> None:
        out = encode_lit_bytecode(0, ALIGN_BYTECODE_BYTES)
        assert out[:6] == bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00])

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            encode_lit_bytecode(-1, SIZE_BYTECODE_BYTES)

    def test_rejects_over_u32(self) -> None:
        with pytest.raises(ValueError):
            encode_lit_bytecode(0x100000000, SIZE_BYTECODE_BYTES)
