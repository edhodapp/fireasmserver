# fireasmserver

[![CD matrix](https://github.com/edhodapp/fireasmserver/actions/workflows/cd-matrix.yml/badge.svg)](https://github.com/edhodapp/fireasmserver/actions/workflows/cd-matrix.yml)
[![Python Quality Gates](https://github.com/edhodapp/fireasmserver/actions/workflows/python-gates.yml/badge.svg)](https://github.com/edhodapp/fireasmserver/actions/workflows/python-gates.yml)

A bare-metal x86_64/AArch64 HTTP server written in 100% assembly language, booting directly as a [Firecracker](https://firecracker-microvm.github.io/) microVM guest. No Linux kernel, no userspace, no runtime — the kernel image *is* the HTTP server.

`fireasmserver` is the first product in the **fireasm** family of bare-metal Firecracker-hosted services. Downstream of [ws_pi5](https://github.com/edhodapp/ws_pi5)'s protocol stack, retargeted from AArch64 + GENET to x86_64 + virtio-net.

## Architecture

The core is a **VMIO automaton engine** — a main loop that dispatches events from priority wait queues through transition tables to transactional handlers. Each protocol layer (virtio-net driver, TCP, HTTP) is an automaton with its own transition table. Each connection is a "way" with its own state variable and context. No OS, no threads, no semaphores.

```
arch/
  x86_64/                   # Self-contained project root
    Makefile
    platform/
      qemu/                 # Multiboot1 stub — live
        boot.S
        linker.ld
      firecracker/          # PVH ELF64, boots under --no-api — live
        boot.S
        linker.ld
  aarch64/
    Makefile
    platform/
      firecracker/          # Linux arm64 Image format — tracer-bullet GREEN
        boot.S              # 8250 UART at 0x40002000, WFE halt
        linker.ld
      qemu/                 # Linux arm64 Image format — tracer-bullet GREEN
        boot.S              # PL011 UART at 0x09000000, WFE halt
        linker.ld
```

Two implementations, one design. Each arch is ISA-idiomatic — not "C in assembly."

## CD Pipeline

Every commit passes through a multi-gate pipeline before reaching `main`:

**Pre-commit (local, blocking):**
- flake8 (max complexity 5) + pylint (Google style) + mypy --strict + pytest with branch coverage
- Independent Gemini CLI code review (advisory)
- Independent clean Claude code review (advisory, within Claude Code sessions)

**GitHub Actions (on push):**
- `python-gates.yml` — flake8 + mypy --strict + pylint + pytest with branch coverage, on Python 3.11 and 3.12.
- `cd-matrix.yml` — four-cell arch × platform build matrix: x86_64/qemu, x86_64/firecracker, aarch64/qemu all on `ubuntu-latest` (x86_64 hosted), plus aarch64/firecracker on `ubuntu-24.04-arm`. Build-only today. aarch64/firecracker cannot go beyond build-level in hosted CI because the free `ubuntu-24.04-arm` runner does not expose `/dev/kvm` (confirmed empirically 2026-04-18); VM-boot coverage for that cell lives in the local Pi tracer bullet instead.

## Development topology

Two hosts cooperate during development:

- **Laptop** (x86_64) — primary development machine, runs the x86_64 Firecracker path natively with `/dev/kvm`, builds the Pi 5 image via `pi-gen`, and hosts a laptop-side `apt-cacher-ng` proxy so the Pi can install packages (per D035).
- **Raspberry Pi 5** (AArch64, 16 GB RAM) — local-only Firecracker host on an isolated USB-NIC bridge (no internet route; D022/D024). Kernel custom-built with `CONFIG_KVM=y` (D023/D033). Not a GitHub Actions self-hosted runner — CI lives in the cloud; the Pi is for local integration work.

Snapshot strategy for the Pi (D036): `backup_pi_rsync.sh` for fast incremental hardlink snapshots, `backup_pi_dd.sh` for crash-consistent block-level images that `flash_sd_card.sh` can restore. Both run with the Pi up — no SD removal for routine backups.

See [DECISIONS.md](DECISIONS.md) for the full architecture decision log (D001–D037).

## Building

```bash
# x86_64 QEMU stub (Multiboot1)
cd arch/x86_64
make PLATFORM=qemu

# x86_64 Firecracker stub (PVH ELF64)
cd arch/x86_64
make PLATFORM=firecracker

# AArch64 Firecracker tracer-bullet stub (Linux arm64 Image)
cd arch/aarch64
make PLATFORM=firecracker
```

Requires: `binutils-x86-64-linux-gnu` for x86_64, `binutils-aarch64-linux-gnu` for AArch64.

Pi 5 image build, SD flashing, and backup tooling lives under [`tooling/pi5_build/`](tooling/pi5_build/). Laptop-side `apt-cacher-ng` setup under [`tooling/apt_cache/`](tooling/apt_cache/).

## Testing

```bash
# Install tooling
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run Python quality gates
.venv/bin/flake8 tooling/src/ tooling/tests/
.venv/bin/mypy --strict tooling/src/qemu_harness/
.venv/bin/pylint --rcfile=.pylintrc tooling/src/qemu_harness/
.venv/bin/pytest --cov=qemu_harness --cov-branch -q
```

## Status

Early implementation. What's in place:

- **x86_64** — Multiboot1 stub (QEMU) and PVH ELF64 stub (Firecracker) both boot and emit `READY\n` on serial under their respective VMMs. VM launcher and test harness wire up launch + ready-marker polling + clean teardown. 100+ Python tests passing, CI green.
- **AArch64 tracer bullet GREEN** — 142-byte Linux arm64 Image stubs on both platforms. `aarch64/firecracker` boots under Firecracker on the Pi 5, writing `READY\n` to the emulated 8250 UART at `0x40002000`; the laptop sees the marker via SSH-captured serial in ~2 s wall-clock from `make`. Orchestration: [`tooling/tracer_bullet/pi_aarch64_firecracker.sh`](tooling/tracer_bullet/pi_aarch64_firecracker.sh). `aarch64/qemu` boots under `qemu-system-aarch64 -M virt`, writing to PL011 at `0x09000000` — proves the cross-toolchain + Image format + PC-relative code against a second VMM.
- **Pi 5 local test host** — PiOS Trixie 64-bit custom-built via pi-gen with a KVM-enabled kernel, static `10.0.0.2/24` on USB NIC, pubkey-only SSH, first-user password random-per-build. Firecracker v1.15.1 installed from the upstream prebuilt release (D037 amends D026 during bring-up). `apt-cacher-ng` proxy on the laptop makes the Pi's package world work through a single bridge hop (D035). Two-tier backup strategy (D036) keeps snapshots without SD removal.
- **Decision log** currently at D001–D037; all load-bearing architectural choices recorded as immutable entries in [DECISIONS.md](DECISIONS.md).

Next milestones: branch-coverage tool (capstone + pyelftools-based, run on the `guest.elf` against QEMU `-d exec` traces), full arch × platform CI matrix in GitHub Actions, then the VMIO automaton engine, virtio-net driver, TCP stack, and HTTP server in assembly — AArch64 pulled up from [ws_pi5](https://github.com/edhodapp/ws_pi5) at the L2/L3 boundary, x86_64 implemented in parallel.

## License

fireasmserver is licensed under the **GNU Affero General Public License, version 3 or any later version** (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

If your use case is incompatible with the AGPL, contact ed@hodapp.com about commercial licensing.

Copyright © 2026 Ed Hodapp.

## Contributions

Bug reports welcome. Pull requests are not accepted — fireasmserver is a single-author project. If you have a fix, file an issue and it may be reimplemented.

## Acknowledgements

Thanks to Chetan Venkatesh for pointing at Firecracker as the right deployment target for this kind of work.
