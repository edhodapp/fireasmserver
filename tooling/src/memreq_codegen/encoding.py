"""FNV-1a name hashing and D060 bytecode encoding.

The functions here produce the byte sequences that go into the
48-byte memreq record: a u32 name_hash, a 16-byte size_bytecode
buffer, and an 8-byte align_bytecode buffer. Both bytecode buffers
are END-terminated and zero-padded to their fixed width.

This module deliberately has no inbound dependency on memlayout;
the opcode constants are wire-level and replicated here so the
codegen side matches the per-arch assembly interpreters by
construction. Field-name → positional-id resolution lives in
`schema.py` (where it belongs — it's a YAML-validation concern,
not a wire-format concern).
"""

from __future__ import annotations

from dataclasses import dataclass

# FNV-1a 32-bit constants. The matching kernel-side hash lives in
# `tooling/src/memlayout/models.py:MemoryRegion.name_hash` per
# REQ MR-006. Standardized; do not retune.
_FNV1A_OFFSET_BASIS = 0x811C9DC5
_FNV1A_PRIME = 0x01000193
_U32_MASK = 0xFFFFFFFF
_U8_MASK = 0xFF

# Bytecode opcode values from `tooling/src/memlayout/types.py`.
# Replicated here so this module has no inbound dependency on
# memlayout; the constants are wire-level and shared with the
# per-arch interpreters by design. Keep in lockstep with the
# memlayout.Opcode enum (verified by test_memreq_codegen_encoding).
OP_END = 0x00
OP_LIT = 0x01
OP_TUNING = 0x02
OP_CPU = 0x03
OP_MUL = 0x04
OP_DIV_LIT = 0x05
OP_ALIGN_UP = 0x06
OP_CALL_THUNK = 0x07

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


# --- Bytecode emit ABI ---------------------------------------------
#
# An Op is a tagged value carrying (opcode, payload). Payload shape
# depends on opcode:
#   OP_LIT        : u32         in payload[0]
#   OP_TUNING     : u8 field id in payload[0]
#   OP_CPU        : u8 field id in payload[0]
#   OP_MUL        : ()
#   OP_DIV_LIT    : u8 divisor  in payload[0]
#   OP_ALIGN_UP   : ()
#   OP_CALL_THUNK : u32 fn id   in payload[0]
#   OP_END is NEVER passed in the op stream — it's appended by the
#   encoder so callers can't accidentally emit an END before their
#   expression is complete.


@dataclass(frozen=True)
class Op:
    """One bytecode opcode with its optional payload.

    `opcode` is one of the OP_* constants above. `payload` is the
    typed value the opcode consumes from the byte stream (u8 or
    u32 depending on opcode). For payload-less opcodes (MUL,
    ALIGN_UP) `payload` MUST be None.
    """

    opcode: int
    payload: int | None = None


# Per-opcode payload width in bytes (0 for payload-less). OP_END
# is excluded — it's never user-supplied.
_PAYLOAD_WIDTH: dict[int, int] = {
    OP_LIT: 4,
    OP_TUNING: 1,
    OP_CPU: 1,
    OP_MUL: 0,
    OP_DIV_LIT: 1,
    OP_ALIGN_UP: 0,
    OP_CALL_THUNK: 4,
}


def encode_lit_bytecode(value: int, buffer_bytes: int) -> bytes:
    """Encode `LIT <value>; END` and zero-pad to `buffer_bytes`.

    Backward-compatible shortcut for the common literal case.
    `value` must fit in u32. Equivalent to
    `encode_bytecode([Op(OP_LIT, value)], buffer_bytes)`.
    """
    return encode_bytecode([Op(OP_LIT, value)], buffer_bytes)


def _check_payload_range(opcode: int, payload: int, width: int) -> None:
    """Reject `payload` if it doesn't fit the opcode's payload width.

    Width is bytes (1 or 4 for the current opcode set). The asm
    interpreters consume exactly this many bytes after the opcode
    byte, so a Python-side range check catches authoring mistakes
    before they reach the VM. Explicit width dispatch — any future
    op width must be added here, never fall through to a default.
    """
    if width == 1:
        mask = _U8_MASK
    elif width == 4:
        mask = _U32_MASK
    else:  # pragma: no cover
        # Defensive: every entry in _PAYLOAD_WIDTH today is 0/1/4.
        # Reachable only via a dispatch-table edit that adds a new
        # width without updating this branch.
        raise ValueError(
            f"opcode 0x{opcode:02x} has unsupported payload "
            f"width {width}"
        )
    if not 0 <= payload <= mask:
        raise ValueError(
            f"opcode 0x{opcode:02x} payload {payload} out of "
            f"{width * 8}-bit unsigned range [0, {mask}]"
        )


def _validate_opcode(op: Op) -> int:
    """Reject ill-formed `op.opcode`; return its payload width.

    Centralizes the "is this an opcode at all" checks so `_encode_one`
    can focus on the payload-emit branch.
    """
    if op.opcode == OP_END:
        raise ValueError(
            "OP_END must not appear in the op stream; "
            "encode_bytecode appends it automatically"
        )
    if op.opcode not in _PAYLOAD_WIDTH:
        raise ValueError(f"unknown opcode 0x{op.opcode:02x}")
    return _PAYLOAD_WIDTH[op.opcode]


def _encode_one(op: Op, out: bytearray) -> None:
    """Emit one op's wire bytes (opcode + payload, if any) into `out`."""
    width = _validate_opcode(op)
    if width == 0:
        if op.payload is not None:
            raise ValueError(
                f"opcode 0x{op.opcode:02x} takes no payload, "
                f"got {op.payload!r}"
            )
        out.append(op.opcode)
        return
    if op.payload is None:
        raise ValueError(
            f"opcode 0x{op.opcode:02x} requires a "
            f"{width}-byte payload, got None"
        )
    _check_payload_range(op.opcode, op.payload, width)
    out.append(op.opcode)
    out.extend(op.payload.to_bytes(width, "little"))


def encode_bytecode(ops: list[Op], buffer_bytes: int) -> bytes:
    """Encode a list of `Op` into wire bytes, append END, zero-pad.

    Raises ValueError on:
      - empty `ops` (END alone is meaningless; a bytecode must
        leave one value on the stack — see memlayout.bytecode._finish)
      - unknown opcode (anything not in _PAYLOAD_WIDTH)
      - payload-present-when-not-expected or payload-missing-when-
        expected mismatch
      - payload out of range (u8 or u32 depending on opcode)
      - encoded length > buffer_bytes
    """
    if not ops:
        raise ValueError("empty op list (would yield END-only bytecode)")
    body = bytearray()
    for op in ops:
        _encode_one(op, body)
    body.append(OP_END)
    if len(body) > buffer_bytes:
        raise ValueError(
            f"encoded bytecode is {len(body)} bytes, exceeds "
            f"buffer_bytes={buffer_bytes}"
        )
    return bytes(body) + bytes(buffer_bytes - len(body))
