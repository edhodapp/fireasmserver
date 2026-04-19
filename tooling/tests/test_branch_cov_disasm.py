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
    _symbol_address,
    _to_branch,
    _trim_to_entry,
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


class TestSymbolAddress:
    """Resolve a named symbol from the ELF's .symtab."""

    def test_symbol_found(self) -> None:
        sym = MagicMock()
        sym.name = "_entry"
        sym.__getitem__.side_effect = lambda k: {"st_value": 0x40}[k]
        symtab = MagicMock()
        symtab.iter_symbols.return_value = [sym]
        elf = MagicMock()
        elf.get_section_by_name.return_value = symtab
        assert _symbol_address(elf, "_entry") == 0x40

    def test_symbol_missing_raises(self) -> None:
        symtab = MagicMock()
        symtab.iter_symbols.return_value = []
        elf = MagicMock()
        elf.get_section_by_name.return_value = symtab
        with pytest.raises(ValueError, match="Symbol not found"):
            _symbol_address(elf, "_entry")

    def test_no_symtab_raises(self) -> None:
        elf = MagicMock()
        elf.get_section_by_name.return_value = None
        with pytest.raises(ValueError, match="no .symtab"):
            _symbol_address(elf, "_entry")


class TestTrimToEntry:
    """Drop / trim sections so disassembly starts at entry_addr."""

    def test_section_entirely_before_entry_dropped(self) -> None:
        assert not _trim_to_entry([(b"\x00\x00", 0x0)], 0x100)

    def test_section_entirely_after_entry_kept(self) -> None:
        assert _trim_to_entry(
            [(b"\x90\x90", 0x200)], 0x100,
        ) == [(b"\x90\x90", 0x200)]

    def test_section_straddles_entry_trimmed(self) -> None:
        # Section spans 0x0..0x10; entry at 0x8 should keep bytes 8..16.
        data = bytes(range(16))
        result = _trim_to_entry([(data, 0x0)], 0x8)
        assert result == [(data[8:], 0x8)]

    def test_section_boundary_at_entry(self) -> None:
        # Section ends exactly at entry — entirely before; dropped.
        assert not _trim_to_entry([(b"\xaa\xbb", 0x0)], 0x2)

    def test_mixed_sections(self) -> None:
        sections = [
            (b"\x00\x00", 0x0),     # before entry → drop
            (b"\x11\x22\x33", 0x40),  # straddles → trim
            (b"\x44\x55", 0x100),    # after entry → keep
        ]
        result = _trim_to_entry(sections, 0x41)
        assert result == [
            (b"\x22\x33", 0x41),
            (b"\x44\x55", 0x100),
        ]


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
# The x86_64 stub grew conditional branches as the virtio-net L2 driver
# took shape (virtio probe, LSR-polled UART emit loop, hex-print helper);
# the aarch64 stub stays small but is still expected to grow. Both
# assertions are loose — non-emptiness plus mnemonic validity against
# a per-arch allowed set — so every boot.S edit doesn't re-pin.
#
# Paths resolve from this file's location, not a hardcoded absolute
# path. The prior form silently skipped in CI (artifact not at
# /home/ed/fireasmserver/...), which meant these integration checks
# had been running only on Ed's laptop — false confidence. Now they
# run anywhere the build artifacts exist.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_X86_ELF = _REPO_ROOT / "arch/x86_64/build/firecracker/guest.elf"
_AARCH64_ELF = _REPO_ROOT / "arch/aarch64/build/firecracker/guest.elf"

# Exhaustive-ish set of x86_64 conditional-branch mnemonics Capstone can
# emit for Jcc and loop variants. If the disassembler ever starts
# handing back a mnemonic outside this set, we want to know.
_X86_COND_BRANCHES = frozenset({
    "je", "jne", "jz", "jnz",
    "jb", "jnb", "jc", "jnc",
    "jbe", "ja", "jnbe", "jna",
    "jae", "jnae",
    "jl", "jnl", "jle", "jnle",
    "jg", "jng", "jge", "jnge",
    "js", "jns",
    "jo", "jno",
    "jp", "jnp", "jpe", "jpo",
    "jcxz", "jecxz", "jrcxz",
    "loop", "loope", "loopne", "loopz", "loopnz",
})


@pytest.mark.skipif(
    not _X86_ELF.exists(),
    reason="x86_64 firecracker build artifact not present",
)
def test_enumerate_branches_on_x86_tracer_stub() -> None:
    """The x86_64 stub has conditional branches across its virtio probe
    and UART / hex-emit helpers. Assert non-emptiness and mnemonic
    validity — catches disassembler regressions without pinning a
    brittle exact count."""
    branches = enumerate_branches(_X86_ELF)
    assert branches, (
        "expected conditional branches in the x86_64 tracer stub "
        "(virtio probe + LSR-polled UART loop + hex-emit helper)"
    )
    mnemonics = {b.mnemonic for b in branches}
    unexpected = mnemonics - _X86_COND_BRANCHES
    assert not unexpected, (
        f"unexpected x86 branch mnemonics from disassembler: {unexpected}"
    )


_AARCH64_COND_BRANCHES = frozenset({
    "b.eq", "b.ne", "b.cs", "b.hs", "b.cc", "b.lo", "b.mi", "b.pl",
    "b.vs", "b.vc", "b.hi", "b.ls", "b.ge", "b.lt", "b.gt", "b.le",
    "b.al", "b.nv",
    "cbz", "cbnz",
    "tbz", "tbnz",
})


@pytest.mark.skipif(
    not _AARCH64_ELF.exists(),
    reason="aarch64 firecracker build artifact not present",
)
def test_enumerate_branches_on_aarch64_tracer_stub() -> None:
    """The aarch64 stub has at least one conditional branch with valid
    AArch64 mnemonics. Loose form — same rationale as the x86_64 test:
    avoid re-pinning on every boot.S edit."""
    branches = enumerate_branches(_AARCH64_ELF)
    assert branches, "expected conditional branches in the aarch64 stub"
    mnemonics = {b.mnemonic for b in branches}
    unexpected = mnemonics - _AARCH64_COND_BRANCHES
    assert not unexpected, (
        f"unexpected aarch64 branch mnemonics from disassembler: {unexpected}"
    )


@pytest.mark.skipif(
    not _AARCH64_ELF.exists(),
    reason="aarch64 firecracker build artifact not present",
)
def test_enumerate_branches_with_entry_symbol_narrows_scope() -> None:
    """_entry-restricted enumeration still yields valid conditional
    branches (no bogus mnemonics, no empty result)."""
    branches = enumerate_branches(_AARCH64_ELF, entry_symbol="_entry")
    assert branches
    mnemonics = {b.mnemonic for b in branches}
    unexpected = mnemonics - _AARCH64_COND_BRANCHES
    assert not unexpected, (
        f"unexpected aarch64 branch mnemonics from disassembler: {unexpected}"
    )


@pytest.mark.skipif(
    not _AARCH64_ELF.exists(),
    reason="aarch64 firecracker build artifact not present",
)
def test_enumerate_branches_unknown_entry_symbol_raises() -> None:
    """A missing entry symbol surfaces as ValueError."""
    with pytest.raises(ValueError, match="Symbol not found"):
        enumerate_branches(_AARCH64_ELF, entry_symbol="__nope__")
