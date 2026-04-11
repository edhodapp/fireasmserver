"""Assemble and link guest images using GNU as + ld."""

from __future__ import annotations

import subprocess
from pathlib import Path


class Toolchain:
    """Assembler, linker, and arch-specific flags."""

    def __init__(
        self,
        assembler: str,
        linker: str,
        as_flags: list[str],
        ld_flags: list[str],
    ) -> None:
        self.assembler = assembler
        self.linker = linker
        self.as_flags = as_flags
        self.ld_flags = ld_flags


def toolchain_for(arch: str, platform: str) -> Toolchain:
    """Return the toolchain for the (arch, platform) target.

    ELF class is a function of (arch, platform): qemu boots
    x86_64 via Multiboot1 (ELF32), firecracker via PVH (ELF64).
    """
    toolchains: dict[tuple[str, str], Toolchain] = {
        ("x86_64", "qemu"): Toolchain(
            assembler="x86_64-linux-gnu-as",
            linker="x86_64-linux-gnu-ld",
            as_flags=["--32"],
            ld_flags=["-m", "elf_i386"],
        ),
        ("x86_64", "firecracker"): Toolchain(
            assembler="x86_64-linux-gnu-as",
            linker="x86_64-linux-gnu-ld",
            as_flags=[],
            ld_flags=[],
        ),
        ("aarch64", "qemu"): Toolchain(
            assembler="aarch64-linux-gnu-as",
            linker="aarch64-linux-gnu-ld",
            as_flags=[],
            ld_flags=[],
        ),
    }
    result = toolchains.get((arch, platform))
    if result is None:
        msg = f"Unsupported target: {arch}/{platform}"
        raise ValueError(msg)
    return result


def build_guest(
    arch: str,
    platform: str,
    source_dir: str,
    build_dir: str | None = None,
) -> Path:
    """Assemble and link the guest image.

    Assembles all .S files in source_dir, links with
    linker.ld if present, and returns the output path.
    """
    src = Path(source_dir)
    out = Path(build_dir) if build_dir else src / "build"
    out.mkdir(parents=True, exist_ok=True)
    tc = toolchain_for(arch, platform)
    sources = sorted(src.glob("*.S"))
    if not sources:
        msg = f"No .S files in {source_dir}"
        raise FileNotFoundError(msg)
    objects: list[Path] = []
    for s_file in sources:
        obj = out / s_file.with_suffix(".o").name
        subprocess.run(
            [tc.assembler, *tc.as_flags, "-o", str(obj), str(s_file)],
            check=True,
            capture_output=True,
        )
        objects.append(obj)
    binary = out / "guest.elf"
    link_cmd: list[str] = [tc.linker, *tc.ld_flags]
    linker_script = src / "linker.ld"
    if linker_script.exists():
        link_cmd.extend(["-T", str(linker_script)])
    link_cmd.extend(["-o", str(binary)])
    link_cmd.extend(str(o) for o in objects)
    subprocess.run(
        link_cmd,
        check=True,
        capture_output=True,
    )
    return binary
