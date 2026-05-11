"""FNV-1a name hashing and D060 LIT-only bytecode encoding.

The functions here produce the byte sequences that go into the
48-byte memreq record: a u32 name_hash, a 16-byte size_bytecode
buffer, and an 8-byte align_bytecode buffer. Both bytecode buffers
are END-terminated and zero-padded to their fixed width.
"""

from __future__ import annotations

# FNV-1a 32-bit constants. The matching kernel-side hash lives in
# `tooling/src/memlayout/models.py:MemoryRegion.name_hash` per
# REQ MR-006. Standardized; do not retune.
_FNV1A_OFFSET_BASIS = 0x811C9DC5
_FNV1A_PRIME = 0x01000193
_U32_MASK = 0xFFFFFFFF

# Bytecode opcode values from `tooling/src/memlayout/types.py`.
# Replicated here so this module has no inbound dependency on
# memlayout; the constants are wire-level and shared with the
# per-arch interpreters by design.
_OP_END = 0x00
_OP_LIT = 0x01

# Field widths from D066 Q-C; matched by the existing 48-byte
# record layout in arch/<isa>/memory/memreq.inc.
SIZE_BYTECODE_BYTES = 16
ALIGN_BYTECODE_BYTES = 8


def fnv1a_32(name: str) -> int:
    """Compute the FNV-1a 32-bit hash of `name`'s UTF-8 encoding."""
    h = _FNV1A_OFFSET_BASIS
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * _FNV1A_PRIME) & _U32_MASK
    return h


def encode_lit_bytecode(value: int, buffer_bytes: int) -> bytes:
    """Encode `LIT <value>; END` and zero-pad to `buffer_bytes`.

    `value` must fit in u32 (the LIT payload width per
    `tooling/src/memlayout/types.py:Opcode.LIT`). The returned
    bytes are exactly `buffer_bytes` long.
    """
    if not 0 <= value <= _U32_MASK:
        raise ValueError(
            f"LIT value {value} out of u32 range "
            f"[0, {_U32_MASK}]"
        )
    payload = value.to_bytes(4, "little")
    body = bytes([_OP_LIT]) + payload + bytes([_OP_END])
    if len(body) > buffer_bytes:  # pragma: no cover
        # Defensive: LIT+u32+END is 6 bytes, well under both
        # SIZE_BYTECODE_BYTES (16) and ALIGN_BYTECODE_BYTES (8).
        # If a future caller passes buffer_bytes < 6 this guards
        # against silent truncation.
        raise ValueError(
            f"buffer_bytes={buffer_bytes} too small "
            f"for LIT/END sequence ({len(body)} bytes)"
        )
    return body + bytes(buffer_bytes - len(body))
