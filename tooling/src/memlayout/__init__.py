"""Init-time static memory layout reference (D059 + D060).

Pure-Python implementation of the bytecode VM and bump allocator
that the per-arch assembly interpreters (D060 step 3) must agree
with byte-for-byte. Doubles as the test oracle for differential
testing under user-mode QEMU and the audit backend the ontology
gate (D051 extension) runs to verify (profile, cpu_model)
combinations layout cleanly before they hit production.

The module is intentionally self-contained — no I/O, no logging,
no concurrency. Inputs are pure data structures; outputs are
pure data structures or exceptions. That makes it Hypothesis-
friendly and keeps the contract with the assembly side as
small as possible.
"""

from memlayout.bytecode import BytecodeError, run_bytecode
from memlayout.models import (
    AssignedRegion,
    CpuCharacteristics,
    Layout,
    MemoryRegion,
    TuningProfile,
)
from memlayout.reference import LayoutOverflow, allocate
from memlayout.types import Lifetime, Opcode

__all__ = [
    "AssignedRegion",
    "BytecodeError",
    "CpuCharacteristics",
    "Layout",
    "LayoutOverflow",
    "Lifetime",
    "MemoryRegion",
    "Opcode",
    "TuningProfile",
    "allocate",
    "run_bytecode",
]
