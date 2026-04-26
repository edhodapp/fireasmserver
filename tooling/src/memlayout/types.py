"""Enums and integer constants shared across the memlayout module.

Opcode values and Lifetime values are wire-level — they appear
verbatim in the .memreq records the per-arch assembly emits, so
changing one of these integers is a binary-incompatible change.
"""

from enum import IntEnum

# Word size used by the allocator's bump pointers and the
# assigned_addr / assigned_size fields in each record. Matches
# the 64-bit ISA invariant from D003 / D060 layer 0.
WORD_BITS = 64
MAX_U64 = (1 << WORD_BITS) - 1

# Bytecode-buffer sizes from D060. The macro definitions in
# arch/<isa>/memory/memreq.inc emit exactly these many bytes
# per record; the interpreter MUST stop at the END opcode
# regardless of buffer size, but trailing junk past END is
# meaningless.
SIZE_BYTECODE_BYTES = 16
ALIGN_BYTECODE_BYTES = 8

# Stack machine depth. Matches the per-arch interpreter's
# register count: rax/rcx/rdx/r8 on x86_64 and x0..x3 on
# aarch64.
STACK_DEPTH = 4


class Opcode(IntEnum):
    """Bytecode opcodes for D060 layer-3 expressions.

    Each opcode is one byte. Some opcodes carry a payload of
    1 or 4 additional bytes immediately after the opcode byte.
    Numeric values are wire-level — coordinated with each arch's
    assembly interpreter and with the .memreq macro emitters.
    """

    END = 0x00
    LIT = 0x01         # +4 bytes payload (u32 little-endian)
    TUNING = 0x02      # +1 byte payload (field id)
    CPU = 0x03         # +1 byte payload (field id)
    MUL = 0x04
    DIV_LIT = 0x05     # +1 byte payload (small divisor)
    ALIGN_UP = 0x06
    CALL_THUNK = 0x07  # +4 bytes payload (function id)


class Lifetime(IntEnum):
    """Region lifetime tags from D059.

    Wire-level: stored in the lifetime byte at offset 30 of
    each .memreq record. Allocator dispatch on this value
    decides forward-bump vs reverse-bump and whether the region
    qualifies for write-protection at the init_complete fence.
    """

    STEADY_STATE = 0
    INIT_ONLY = 1
    IMMUTABLE_AFTER_INIT = 2
    STACK = 3
