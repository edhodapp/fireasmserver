"""Tests for guest_builder module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qemu_harness.guest_builder import (
    build_guest,
    toolchain_for_arch,
)


class TestToolchainForArch:
    """Tests for toolchain_for_arch()."""

    def test_x86_64(self) -> None:
        tc = toolchain_for_arch("x86_64")
        assert tc.assembler == "x86_64-linux-gnu-as"
        assert tc.linker == "x86_64-linux-gnu-ld"
        assert "--32" in tc.as_flags
        assert "-m" in tc.ld_flags

    def test_aarch64(self) -> None:
        tc = toolchain_for_arch("aarch64")
        assert tc.assembler == "aarch64-linux-gnu-as"
        assert tc.linker == "aarch64-linux-gnu-ld"

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            toolchain_for_arch("riscv64")


class TestBuildGuest:
    """Tests for build_guest()."""

    def test_no_sources_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=".S"):
            build_guest("x86_64", "qemu", str(tmp_path))

    @patch("qemu_harness.guest_builder.subprocess.run")
    def test_assembles_and_links(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "boot.S").write_text(".global _start\n")
        build = tmp_path / "build"
        result = build_guest(
            "x86_64", "qemu", str(src), str(build),
        )
        assert str(result).endswith("guest.elf")
        assert mock_run.call_count == 2

    @patch("qemu_harness.guest_builder.subprocess.run")
    def test_passes_arch_flags(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "boot.S").write_text(".global _start\n")
        build = tmp_path / "build"
        build_guest("x86_64", "qemu", str(src), str(build))
        as_call = mock_run.call_args_list[0][0][0]
        assert "--32" in as_call
        ld_call = mock_run.call_args_list[1][0][0]
        assert "-m" in ld_call
        assert "elf_i386" in ld_call

    @patch("qemu_harness.guest_builder.subprocess.run")
    def test_uses_linker_script(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "boot.S").write_text(".global _start\n")
        (src / "linker.ld").write_text("ENTRY(_start)\n")
        build = tmp_path / "build"
        build_guest("x86_64", "qemu", str(src), str(build))
        link_call = mock_run.call_args_list[1]
        link_args = link_call[0][0]
        assert "-T" in link_args

    @patch("qemu_harness.guest_builder.subprocess.run")
    def test_no_linker_script(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "boot.S").write_text(".global _start\n")
        build = tmp_path / "build"
        build_guest("x86_64", "qemu", str(src), str(build))
        link_call = mock_run.call_args_list[1]
        link_args = link_call[0][0]
        assert "-T" not in link_args

    @patch("qemu_harness.guest_builder.subprocess.run")
    def test_multiple_sources(
        self, mock_run: MagicMock, tmp_path: Path,
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.S").write_text(".global _start\n")
        (src / "b.S").write_text(".global foo\n")
        build = tmp_path / "build"
        build_guest("x86_64", "qemu", str(src), str(build))
        assert mock_run.call_count == 3
