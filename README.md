# fireasmserver

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
      firecracker/          # Linux arm64 Image format — tracer-bullet
        boot.S              # PL011 UART at 0x09000000, WFE halt
        linker.ld
      qemu/                 # (future; Pi 5 is Firecracker-only in scope)
```

Two implementations, one design. Each arch is ISA-idiomatic — not "C in assembly."

## CD Pipeline

Every commit passes through a multi-gate pipeline before reaching `main`:

**Pre-commit (local, blocking):**
- flake8 (max complexity 5) + pylint (Google style) + mypy --strict + pytest with branch coverage
- Independent Gemini CLI code review (advisory)
- Independent clean Claude code review (advisory, within Claude Code sessions)

**GitHub CI (on push):**
- Lint + type check + pylint (collapsed job)
- Test matrix: Python 3.11 + 3.12 with branch coverage
- Arch × platform matrix (x86_64/qemu, x86_64/firecracker, aarch64/qemu, aarch64/firecracker) — planned

## Development topology

Two hosts cooperate during development:

- **Laptop** (x86_64) — primary development machine, runs the x86_64 Firecracker path natively with `/dev/kvm`, builds the Pi 5 image via `pi-gen`, and hosts a laptop-side `apt-cacher-ng` proxy so the Pi can install packages (per D035).
- **Raspberry Pi 5** (AArch64, 16 GB RAM) — local-only Firecracker host on an isolated USB-NIC bridge (no internet route; D022/D024). Kernel custom-built with `CONFIG_KVM=y` (D023/D033). Not a GitHub Actions self-hosted runner — CI lives in the cloud; the Pi is for local integration work.

Snapshot strategy for the Pi (D036): `backup_pi_rsync.sh` for fast incremental hardlink snapshots, `backup_pi_dd.sh` for crash-consistent block-level images that `flash_sd_card.sh` can restore. Both run with the Pi up — no SD removal for routine backups.

See [DECISIONS.md](DECISIONS.md) for the full architecture decision log (D001–D036).

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
- **AArch64** — Firecracker tracer-bullet stub built as a valid Linux arm64 Image (138 B, PL011 UART writer, MPIDR-gated, DSB-flushed). Integration onto the Pi 5 Firecracker host is the next milestone.
- **Pi 5 local test host** — PiOS Trixie 64-bit custom-built via pi-gen with a KVM-enabled kernel, static `10.0.0.2/24` on USB NIC, pubkey-only SSH, first-user password random-per-build. `apt-cacher-ng` proxy on the laptop makes the Pi's package world work through a single bridge hop (D035). Two-tier backup strategy (D036) keeps snapshots without SD removal.
- **Decision log** currently at D001–D036; all load-bearing architectural choices recorded as immutable entries in [DECISIONS.md](DECISIONS.md).

Next milestones: Firecracker cross-build or install on Pi 5, SSH-orchestrated AArch64 tracer-bullet run, branch-coverage tool, full arch × platform CI matrix, then the VMIO engine, virtio-net driver, TCP stack, and HTTP server in assembly.

## License

fireasmserver is licensed under the **GNU Affero General Public License, version 3 or any later version** (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

If your use case is incompatible with the AGPL, contact ed@hodapp.com about commercial licensing.

Copyright © 2026 Ed Hodapp.

## Contributions

Bug reports welcome. Pull requests are not accepted — fireasmserver is a single-author project. If you have a fix, file an issue and it may be reimplemented.

## Acknowledgements

Thanks to Chetan Venkatesh for pointing at Firecracker as the right deployment target for this kind of work.
