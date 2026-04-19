"""ELF disassembly and conditional-branch enumeration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from capstone import (  # type: ignore[import-untyped]
    CS_ARCH_ARM64,
    CS_ARCH_X86,
    CS_MODE_64,
    CS_MODE_LITTLE_ENDIAN,
    Cs,
)
from elftools.elf.elffile import ELFFile
from pydantic import BaseModel

# SHF_EXECINSTR — ELF section flag bit for executable code.
_SHF_EXECINSTR = 0x4

_X86_UNCOND = frozenset({"jmp"})
_AARCH64_SINGLE_MN = frozenset({"cbz", "cbnz", "tbz", "tbnz"})


class ConditionalBranch(BaseModel):
    """A conditional branch found in the guest's code."""

    addr: int
    insn_size: int
    target_taken: int
    target_not_taken: int
    mnemonic: str


def _is_conditional(arch: str, mnemonic: str) -> bool:
    """Return True for conditional branches on the given arch."""
    mnl = mnemonic.lower()
    if arch == "x86_64":
        return mnl.startswith("j") and mnl not in _X86_UNCOND
    if arch == "aarch64":
        if mnl in _AARCH64_SINGLE_MN:
            return True
        return mnl.startswith("b.")
    msg = f"Unsupported arch: {arch}"
    raise ValueError(msg)


def _capstone_for(arch: str) -> Cs:
    """Return a capstone disassembler for the arch."""
    if arch == "x86_64":
        return Cs(CS_ARCH_X86, CS_MODE_64)
    if arch == "aarch64":
        # CS_MODE_64 is x86-only. For ARM64 the valid modes are the
        # endian flags; default little-endian matches fireasmserver.
        return Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
    msg = f"Unsupported arch: {arch}"
    raise ValueError(msg)


def _detect_arch(elf: ELFFile) -> str:
    """Map an ELF e_machine header to our arch string."""
    em = elf.header["e_machine"]
    if em == "EM_X86_64":
        return "x86_64"
    if em == "EM_AARCH64":
        return "aarch64"
    msg = f"Unsupported e_machine: {em}"
    raise ValueError(msg)


def _code_sections(elf: ELFFile) -> list[tuple[bytes, int]]:
    """Yield (data, base_addr) for each executable section."""
    out: list[tuple[bytes, int]] = []
    # pyelftools lacks type stubs; iter_sections is untyped.
    for section in elf.iter_sections():  # type: ignore[no-untyped-call]
        if section["sh_flags"] & _SHF_EXECINSTR:
            out.append((section.data(), section["sh_addr"]))
    return out


def _to_branch(insn: Any) -> ConditionalBranch:
    """Build a ConditionalBranch from a capstone instruction.

    `operands[-1].imm` is safe for every conditional branch in our
    supported ISAs: x86 Jcc family all take rel8/rel16/rel32 immediate
    displacements; aarch64 B.cond / CBZ / CBNZ / TBZ / TBNZ all end on
    a label operand. There is no conditional indirect branch in either
    ISA. If a new arch with register-indirect conditional branches is
    ever added, revisit and add an X86_OP_IMM / ARM64_OP_IMM filter.
    """
    return ConditionalBranch(
        addr=insn.address,
        insn_size=insn.size,
        target_taken=insn.operands[-1].imm,
        target_not_taken=insn.address + insn.size,
        mnemonic=insn.mnemonic,
    )


def _filter_branches(
    arch: str, insns: Any,
) -> list[ConditionalBranch]:
    """Return only the conditional branches that have operands."""
    return [
        _to_branch(i)
        for i in insns
        if _is_conditional(arch, i.mnemonic) and i.operands
    ]


def enumerate_branches(elf_path: Path) -> list[ConditionalBranch]:
    """Enumerate every conditional branch in the ELF's code sections."""
    result: list[ConditionalBranch] = []
    with open(elf_path, "rb") as f:
        # pyelftools is untyped; the ELFFile constructor is untyped too.
        elf = ELFFile(f)  # type: ignore[no-untyped-call]
        arch = _detect_arch(elf)
        cs = _capstone_for(arch)
        cs.detail = True
        # skipdata lets capstone emit a .byte directive for undecodable
        # bytes (e.g., parts of the 64-byte Linux arm64 Image header that
        # precede real code in our aarch64 firecracker stubs) and keep
        # disassembling instead of bailing at the first invalid opcode.
        # The .byte mnemonic doesn't match our conditional-branch filters,
        # so those get dropped harmlessly.
        #
        # KNOWN LIMITATION: capstone still decodes any syntactically-valid
        # 4-byte word as a real instruction even if the bytes came from
        # image-header fields or padding. On AArch64 this means a future
        # header change could produce a phantom conditional branch whose
        # address never appears in the trace (permanent false-positive
        # gap). The current aarch64 stub has been audited and produces no
        # such phantoms; if a false-positive is reported in future, the
        # principled fix is ELF-symbol-based range restriction to the
        # actual code entry (e.g., _entry onwards) rather than the whole
        # SHF_EXECINSTR section.
        cs.skipdata = True
        for data, base in _code_sections(elf):
            result.extend(_filter_branches(arch, cs.disasm(data, base)))
    return result
