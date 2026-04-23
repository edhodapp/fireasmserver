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

**Pre-push (local, blocking):** integration tests per cell — build + boot + `READY` marker on serial + branch-cov ratchet on aarch64/qemu. Pi-covered when the Pi is reachable. Named cleanup trap visible in every run.

**GitHub Actions (on push):**
- `python-gates.yml` — flake8 + mypy --strict + pylint + pytest with branch coverage, on Python 3.11 and 3.12.
- `cd-matrix.yml` — three-cell arch × platform build-plus-boot matrix: x86_64/firecracker and aarch64/qemu on `ubuntu-latest`, plus aarch64/firecracker on `ubuntu-24.04-arm`. Each cell builds the guest, launches it under its VMM, verifies the `READY` marker on serial, runs OSACA static pipeline analysis advisorily (D007), and executes an explicit cleanup step. The aarch64/qemu cell additionally captures a QEMU `-d exec -singlestep` trace and runs `branch-cov --baseline` against the boot — any new or closed gap relative to `tooling/branch_cov/baselines/aarch64-qemu.txt` fails the cell (the ratchet). x86_64/qemu is intentionally absent: QEMU's `-machine pc` boots through SeaBIOS, and running our 142-byte stub through a megabyte of BIOS emulation spends CI minutes exercising SeaBIOS, not our code. The x86_64/qemu stub itself is still in the tree for local use. The aarch64/firecracker cell cannot go beyond build-level in hosted CI because the free `ubuntu-24.04-arm` runner does not expose `/dev/kvm` (confirmed empirically 2026-04-18); VM-boot coverage for that cell lives in the local Pi tracer bullet instead.

## Development topology

Two hosts cooperate during development:

- **Laptop** (x86_64) — primary development machine, runs the x86_64 Firecracker path natively with `/dev/kvm`, builds the Pi 5 image via `pi-gen`, and hosts a laptop-side `apt-cacher-ng` proxy so the Pi can install packages (per D035).
- **Raspberry Pi 5** (AArch64, 16 GB RAM) — local-only Firecracker host on an isolated USB-NIC bridge (no internet route; D022/D024). Kernel custom-built with `CONFIG_KVM=y` (D023/D033). Not a GitHub Actions self-hosted runner — CI lives in the cloud; the Pi is for local integration work.

Snapshot strategy for the Pi (D036): `backup_pi_rsync.sh` for fast incremental hardlink snapshots, `backup_pi_dd.sh` for crash-consistent block-level images that `flash_sd_card.sh` can restore. Both run with the Pi up — no SD removal for routine backups.

See [DECISIONS.md](DECISIONS.md) for the full architecture decision log (D001–D043). Supersessions carry bidirectional cross-references: the new entry cites what it replaces, and the old entry is strike-through'd with a `DEPRECATED <ISO-8601>Z — see DNNN` marker (established convention since D042).

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
- **Assembly branch coverage** — [`tooling/src/branch_cov/`](tooling/src/branch_cov/) is a capstone + pyelftools tool that enumerates every conditional branch in a guest ELF, compares against a QEMU `-d exec` PC trace, and reports any (addr, outcome) pair that went unobserved. Per-cell baselines (`tooling/branch_cov/baselines/<arch>-<platform>.txt`) accept known-uncovered paths; any delta — new gap or closed gap — fails the cell. Fires in CI on aarch64/qemu, in pre-push locally, and as a CLI one-liner for ad-hoc analysis.
- **OSACA static pipeline analysis** — per-cell advisory step emitting port-pressure numbers for the guest's instruction stream (D007 / D040). Currently uses Neoverse-N1 as a proxy for Cortex-A76 and Skylake-X for Firecracker's typical Xeon host.
- **L2 planning artifacts** — [`docs/l2/REQUIREMENTS.md`](docs/l2/REQUIREMENTS.md) tracks IEEE 802.3 / 802.1Q / RFC 826 / Virtio v1.2 requirements with per-row status columns (`spec` / `tested` / `implemented` / `deviation` / `N/A`) for audit against implementation. Parallel to `DECISIONS.md` in shape. Arch-neutral. The L2 design note (D038 stage 2 / D039) that references these requirements is in flight.
- **Decision log** currently at D001–D043; all load-bearing architectural choices recorded as immutable entries in [DECISIONS.md](DECISIONS.md). Includes the L2 implementation methodology (D038), the L2 design-doc five-property rule (D039), the perf regression ratchet design (D040), the production deployment requirements (D041), the realistic interop matrix replacing "one enterprise switch" (D042), and the FSA runtime model (D043 — static per-type pools, cooperative dispatch, no heap).
- **Parallelization strategy** — self-contained modules (crypto primitives, CRC-32/FCS, perf tooling, future-layer requirements docs) can be handed off to a second Claude Code session via briefing files in [`docs/side_sessions/`](docs/side_sessions/). The main session stays on architecture and cross-cutting decisions; the side session takes directory-scoped implementation work. First briefing: CRC-32 IEEE 802.3 FCS.

Next milestones: the L2 design note, the pure-assembly primitives (CRC-32, crypto), then the L2 core (VMIO automaton, virtio-net driver) — starting on x86_64, with AArch64 following the same sequence. Once L2 is functionally and performance-green, real deployment capabilities per D041 (failure injection, observability, rollback, canary, graceful drain, backpressure, deterministic replay, signed SLSA-attested artifacts) and real deployment targets (fly.io first per the delivery-trigger).

## License

fireasmserver is licensed under the **GNU Affero General Public License, version 3 or any later version** (AGPL-3.0-or-later). See [COPYRIGHT](COPYRIGHT) for the short notice + single-author / commercial-license stance, and [LICENSE](LICENSE) for the full AGPLv3 text.

If your use case is incompatible with the AGPL, contact ed@hodapp.com about commercial licensing.

Copyright © 2026 Ed Hodapp.

## Contributions

Bug reports welcome. Pull requests are not accepted — fireasmserver is a single-author project. If you have a fix, file an issue and it may be reimplemented.

## Acknowledgements

Thanks to Chetan Venkatesh for pointing at Firecracker as the right deployment target for this kind of work.
