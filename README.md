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
      qemu/                 # Multiboot stub, QEMU-specific setup
        boot.S
        linker.ld
      firecracker/          # PVH boot (future)
  aarch64/                  # (future)
    platform/
      qemu/
      firecracker/
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

See [DECISIONS.md](DECISIONS.md) for the full architecture decision log.

## Building

```bash
# x86_64 QEMU stub
cd arch/x86_64
make PLATFORM=qemu
```

Requires: `binutils-x86-64-linux-gnu` (cross-assembler + linker).

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

Early implementation. The CD pipeline, QEMU test harness, and x86_64 multiboot verification stub are operational. Assembly implementation of the VMIO engine, virtio-net driver, TCP stack, and HTTP server is next.

## License

fireasmserver is licensed under the **GNU Affero General Public License, version 3 or any later version** (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

If your use case is incompatible with the AGPL, contact ed@hodapp.com about commercial licensing.

Copyright © 2026 Ed Hodapp.

## Contributions

Bug reports welcome. Pull requests are not accepted — fireasmserver is a single-author project. If you have a fix, file an issue and it may be reimplemented.

## Acknowledgements

Thanks to Chetan Venkatesh for pointing at Firecracker as the right deployment target for this kind of work.
