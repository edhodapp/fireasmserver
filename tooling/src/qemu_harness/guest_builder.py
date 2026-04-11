"""Assemble and link guest images using GNU as + ld."""

from __future__ import annotations

import subprocess
from pathlib import Path


def toolchain_for_arch(
    arch: str,
) -> tuple[str, str]:
    """Return (assembler, linker) for the target arch.

    Uses cross-toolchain names when the host arch
    differs from the target.
    """
    toolchains: dict[str, tuple[str, str]] = {
        "x86_64": (
            "x86_64-linux-gnu-as",
            "x86_64-linux-gnu-ld",
        ),
        "aarch64": (
            "aarch64-linux-gnu-as",
            "aarch64-linux-gnu-ld",
        ),
    }
    result = toolchains.get(arch)
    if result is None:
        msg = f"Unsupported arch: {arch}"
        raise ValueError(msg)
    return result


def build_guest(
    arch: str,
    platform: str,  # pylint: disable=unused-argument
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
    assembler, linker = toolchain_for_arch(arch)
    sources = sorted(src.glob("*.S"))
    if not sources:
        msg = f"No .S files in {source_dir}"
        raise FileNotFoundError(msg)
    objects: list[Path] = []
    for s_file in sources:
        obj = out / s_file.with_suffix(".o").name
        subprocess.run(
            [assembler, "-o", str(obj), str(s_file)],
            check=True,
            capture_output=True,
        )
        objects.append(obj)
    binary = out / "guest.elf"
    link_cmd: list[str] = [linker]
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
