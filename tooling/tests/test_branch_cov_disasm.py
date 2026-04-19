"""Tests for branch_cov.disasm."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from branch_cov.disasm import (
    _capstone_for,
    _code_sections,
    _detect_arch,
    _is_conditional,
    _SHF_EXECINSTR,
    _to_branch,
    enumerate_branches,
)


class TestIsConditional:
    """Per-arch conditional-branch mnemonic classification."""

    @pytest.mark.parametrize(
        "mn", ["je", "jne", "jz", "jnz", "jg", "jle", "jrcxz"],
    )
    def test_x86_conditional_jumps(self, mn: str) -> None:
        assert _is_conditional("x86_64", mn)

    def test_x86_unconditional_jmp_is_false(self) -> None:
        assert not _is_conditional("x86_64", "jmp")

    def test_x86_non_branch_is_false(self) -> None:
        assert not _is_conditional("x86_64", "mov")

    @pytest.mark.parametrize(
        "mn", ["cbz", "cbnz", "tbz", "tbnz"],
    )
    def test_aarch64_single_mnemonic_branches(self, mn: str) -> None:
        assert _is_conditional("aarch64", mn)

    @pytest.mark.parametrize(
        "mn", ["b.eq", "b.ne", "b.lt", "b.ge"],
    )
    def test_aarch64_conditional_b_dot(self, mn: str) -> None:
        assert _is_conditional("aarch64", mn)

    def test_aarch64_unconditional_b_is_false(self) -> None:
        assert not _is_conditional("aarch64", "b")

    def test_aarch64_non_branch_is_false(self) -> None:
        assert not _is_conditional("aarch64", "mov")

    def test_unknown_arch_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported arch"):
            _is_conditional("mips", "beq")

    def test_case_insensitive(self) -> None:
        assert _is_conditional("x86_64", "JNE")
        assert _is_conditional("aarch64", "CBZ")


class TestCapstoneFor:
    """Factory mapping arch → capstone disassembler."""

    def test_x86_64_returns_disassembler(self) -> None:
        cs = _capstone_for("x86_64")
        assert cs is not None

    def test_aarch64_returns_disassembler(self) -> None:
        cs = _capstone_for("aarch64")
        assert cs is not None

    def test_unknown_arch_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported arch"):
            _capstone_for("riscv")


class TestDetectArch:
    """ELF e_machine → arch string."""

    def test_x86_64(self) -> None:
        fake_elf = MagicMock()
        fake_elf.header = {"e_machine": "EM_X86_64"}
        assert _detect_arch(fake_elf) == "x86_64"

    def test_aarch64(self) -> None:
        fake_elf = MagicMock()
        fake_elf.header = {"e_machine": "EM_AARCH64"}
        assert _detect_arch(fake_elf) == "aarch64"

    def test_unsupported_raises(self) -> None:
        fake_elf = MagicMock()
        fake_elf.header = {"e_machine": "EM_RISCV"}
        with pytest.raises(ValueError, match="Unsupported e_machine"):
            _detect_arch(fake_elf)


def _make_section(
    data: bytes, addr: int, flags: int,
) -> MagicMock:
    """Build a pyelftools-section-shaped mock."""
    section = MagicMock()
    section.__getitem__.side_effect = lambda k: {
        "sh_flags": flags,
        "sh_addr": addr,
    }[k]
    section.data.return_value = data
    return section


class TestCodeSections:
    """Executable-flag filtering over ELF sections."""

    def test_only_exec_sections_returned(self) -> None:
        exec_sec = _make_section(b"\x90\x90", 0x100, _SHF_EXECINSTR)
        data_sec = _make_section(b"\x00\x00", 0x200, 0x2)  # SHF_WRITE
        elf = MagicMock()
        elf.iter_sections.return_value = [exec_sec, data_sec]
        result = _code_sections(elf)
        assert result == [(b"\x90\x90", 0x100)]

    def test_empty_returns_empty(self) -> None:
        elf = MagicMock()
        elf.iter_sections.return_value = []
        assert not _code_sections(elf)

    def test_mixed_flags_with_exec_bit_passes(self) -> None:
        # Exec + alloc flags combined (0x6).
        sec = _make_section(b"\x90", 0x100, _SHF_EXECINSTR | 0x2)
        elf = MagicMock()
        elf.iter_sections.return_value = [sec]
        assert _code_sections(elf) == [(b"\x90", 0x100)]


def _fake_operand(imm: int) -> Any:
    op = MagicMock()
    op.imm = imm
    return op


def _fake_insn(
    addr: int, size: int, mnemonic: str, target: int,
) -> Any:
    insn = MagicMock()
    insn.address = addr
    insn.size = size
    insn.mnemonic = mnemonic
    insn.operands = [_fake_operand(target)]
    return insn


class TestToBranch:
    """Mapping capstone insn → ConditionalBranch."""

    def test_taken_target_from_last_operand(self) -> None:
        insn = _fake_insn(0x100, 4, "je", 0x200)
        b = _to_branch(insn)
        assert b.addr == 0x100
        assert b.insn_size == 4
        assert b.target_taken == 0x200
        assert b.target_not_taken == 0x104
        assert b.mnemonic == "je"


# Integration tests against real guest.elf build artifacts (when present).
# The x86_64 stub is branch-free; the aarch64 stub contains exactly three
# conditional branches: one cbnz (MPIDR gate), one cbz (end-of-message),
# and one tbz (TX-ready poll).
_X86_ELF = Path(
    "/home/ed/fireasmserver/arch/x86_64/build/firecracker/guest.elf",
)
_AARCH64_ELF = Path(
    "/home/ed/fireasmserver/arch/aarch64/build/firecracker/guest.elf",
)


@pytest.mark.skipif(
    not _X86_ELF.exists(),
    reason="x86_64 firecracker build artifact not present",
)
def test_enumerate_branches_on_x86_tracer_stub() -> None:
    """The x86_64 tracer-bullet stub has zero conditional branches."""
    assert not enumerate_branches(_X86_ELF)


@pytest.mark.skipif(
    not _AARCH64_ELF.exists(),
    reason="aarch64 firecracker build artifact not present",
)
def test_enumerate_branches_on_aarch64_tracer_stub() -> None:
    """The aarch64 stub has cbnz + cbz + tbz as its three branches."""
    branches = enumerate_branches(_AARCH64_ELF)
    mnemonics = sorted(b.mnemonic for b in branches)
    assert mnemonics == ["cbnz", "cbz", "tbz"]
