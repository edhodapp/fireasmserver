# Architecture Decision Log

Chronological record of design decisions for fireasmserver.
Each entry captures: the decision, the justification, and the date/time.
Entries are numbered sequentially (D001, D002, ...) and never renumbered.
**Entries are immutable.** If a decision is revised or reversed, add a new
entry that references the original (e.g., "Supersedes D003"). Never edit
or delete a prior entry — the log is a historical record.
Review this log before making new decisions to avoid re-litigating settled questions.

## 2026-04-09

### D001: License — AGPL-3.0-or-later
Single-author project (SQLite model). No outside contributions accepted.
Private commercial licenses sold at Ed's discretion for OEM income.
Ed retains unconditional right to refuse commercial licenses.

### D002: Directory structure — arch-primary, platform-subordinate
`arch/<isa>/` is a self-contained project root with its own Makefile and
linker script. Platform-specific code (boot, virtio, device access) lives
under `arch/<isa>/platform/<vm>/`. Pure computation (HTTP parsing, TCP)
lives at the arch level. Each arch directory is analogous to ws_pi5's root.

### D003: 100% assembly — no C, no stdlib
The "Assembly of Fire" brand commits to this. ISA-idiomatic code that maps
ISA abstractions directly to the problem. Two implementations (x86_64 +
AArch64), one design.

### D004: Corporation name — Assembly of Fire, Inc.
Delaware C corp, structured for QSBS qualification. Name confirmed after
namespace saturation check: no USPTO marks, no Delaware entities, no GitHub
repos, no cultural uses, all major domain variants unregistered.

### D005: CD pipeline — two-tier local + CI
Pre-commit hook gates local commits (quality gates blocking, reviews
advisory). GitHub Actions runs arch x platform matrix. Functional gates
block; performance gates advise.

### D006: GNU as for both arches
x86_64 uses Intel syntax via `.intel_syntax noprefix`. AArch64 uses UAL.
No NASM, no LLVM integrated assembler.

### D007: OSACA for static pipeline analysis
Forked to edhodapp/OSACA. Covers both x86_64 and AArch64.
llvm-mca as x86_64 fallback. Both are CI-only advisory.

### D008: Python >= 3.11, always venv
Pi 5 PiOS Bookworm ships 3.11; laptop has 3.12. All Python packages
installed in project venv, never system-wide.

### D009: Coding-agent workflow (blocked on API billing)
Python tooling handed off to coding-agent from python_agent via devpi.
`--dag-file` added to pass ontology as design context. Blocked until
Agent SDK supports Max plan auth.

### D010: Brand family — fireasm
fireasm is the house mark, fireasmserver is the first product.
Future fireasm[X] products use the same namespace.

## 2026-04-10

### D011: Two-reviewer code review pipeline
Independent Gemini CLI review + clean Claude subagent review before
every commit. Both are advisory but both are required. Agreement between
reviewers is high-confidence signal. Findings addressed before committing.

### D012: VMIO automaton engine architecture (Astier)
fireasmserver's core is a VMIO automaton engine per J.Y. Astier's FSA
I/O Container paper. Main loop + priority wait queues + transition tables
per automaton (virtio driver, TCP protocol, HTTP service). Each connection
is a "way" with its own state variable and context. Handlers are
transactional (run to completion). No OS, no threads, no semaphores.

### D013: Foundational abstractions (Lextrait)
Build correct foundational abstractions first; the path upward composes.
Grothendieck's "rising sea" approach: bottom-up layered abstractions that
dissolve problems. Abstraction is not automation, not refactoring, not
conciseness. Get the event format, queue structure, transition table
layout, and context struct right from the start.

### D014: QEMU machine type — pc (not microvm)
Multiboot1 protocol for initial QEMU testing. microvm requires PVH boot.
Standard PC machine accepts multiboot ELF via -kernel. Will revisit when
PVH boot is implemented for Firecracker.

### D015: Toolchain class for arch-specific build flags
guest_builder uses a Toolchain object with per-arch as_flags and ld_flags.
x86_64 multiboot requires --32 and -m elf_i386. Clean Claude review
caught the mismatch between Makefile and Python harness.

### D016: Vendored pylintrc
Google Python Style Guide pylintrc vendored into repo as .pylintrc.
Clean Claude review flagged curling it at CI time as a HIGH severity
RCE vector via load-plugins. CI uses the vendored copy.

### D017: Quality gates in pre-commit (blocking)
flake8, pylint, mypy --strict, pytest run on staged Python files before
every commit. Both Claude Code hook and git pre-commit hook enforce this.
Commit blocked if any gate fails. Gemini review runs after gates pass
(advisory, not blocking).

### D018: Gemini batched review for speed
Gemini review script batches all files into a single prompt instead of
one API call per file. Reduces review time from minutes to seconds.

## 2026-04-11

### D019: FAT32 read-only for virtio-block content filesystem (04:15 UTC)
**Decision:** Web content served by fireasmserver is stored on a FAT32
filesystem on the Firecracker virtio-block device. The guest implements
a read-only FAT32 driver — no write support needed since the guest only
serves what's on the disk.

**Justification:**
- The guest has no OS and no filesystem — content must come from somewhere.
  Options considered: compiled into binary, custom binary format, virtio-block
  with real filesystem, virtio-vsock push, network fetch at boot.
- Compiled-in is too inflexible (rebuild to change content).
- Custom binary format requires our proprietary packing tool — friction for
  users who are not us.
- FAT32 is universally supported: every OS (Linux, macOS, Windows) can create
  FAT32 images with standard tools (`mkfs.fat -F 32`). No special tooling.
- FAT16 was considered but caps at 2GB partition / 2GB file size — too
  restrictive for sites with large media assets.
- FAT32 supports 2TB partition / 4GB file size — more than enough.
- Read-only implementation in assembly is straightforward: read BPB, compute
  offsets, walk directory entries, follow 32-bit cluster chains. No journaling,
  no allocation, no write logic.
- The delta from FAT16 to FAT32 in assembly is marginal (32-bit cluster
  entries, root dir as cluster chain instead of fixed region).
- Target audience (anyone deploying bare-metal assembly in Firecracker) is
  comfortable with disk image creation. For convenience, we'll ship a Python
  packing tool: `fireasmserver-pack ./www/ -o disk.img`.
- Fly.io (major Firecracker hosting platform) charges $0.08/GB/month for
  storage; a typical static site fits in the 10GB free tier.

### D020: PVH boot protocol for Firecracker (x86_64) — confirmed
**Decision:** Firecracker x86_64 guests boot via the PVH (Xen Para-Virtualized
Hardware) protocol. The guest ELF carries an `XEN_ELFNOTE_PHYS32_ENTRY` (type
18) note in a `PT_NOTE` program header pointing at a 32-bit protected-mode
entry. Linker emits both `PT_LOAD` (containing `.note.Xen` and `.text`) and
`PT_NOTE` (referencing `.note.Xen`) program headers.

**Justification:**
- D014 already committed to PVH for Firecracker; this entry locks in the
  concrete implementation now that the boot stub boots and writes "READY" to
  COM1 in ~5 ms after VMM startup.
- Firecracker's `linux-loader` accepts ELF binaries with PVH notes or Linux
  bzImages. PVH ELF is the path of least resistance for assembly guests:
  no Linux header struct, no real-mode entry, no boot params marshalling.
- The same `.code32` entry instructions work under both QEMU `-machine pc`
  (Multiboot1, ELF32) and Firecracker (PVH, ELF64). Only the ELF class and
  the loader-discovered entry note differ. The instruction stream is
  unchanged.
- ELF class is now a function of `(arch, platform)`: x86_64 qemu uses ELF32
  via `--32 -m elf_i386`; x86_64 firecracker uses ELF64 via the default
  `as`/`ld` flags. The Toolchain selector in `guest_builder.toolchain_for`
  encodes this.
- The 8250 UART at 0x3F8 (COM1) is identical between QEMU `-machine pc`
  and Firecracker — the serial diagnostics path is unified across both
  platforms with no per-platform `out` instruction differences.

**Reference implementation:**
- `arch/x86_64/platform/firecracker/boot.S` — PVH note + READY stub
- `arch/x86_64/platform/firecracker/linker.ld` — PHDRS with PT_LOAD + PT_NOTE
- `arch/x86_64/Makefile` — `PLATFORM=firecracker` builds ELF64
- `tooling/src/qemu_harness/vm_launcher.py:_launch_firecracker` — JSON config
  generation, `--no-api` invocation, stdout-to-serial redirection, VMM logger
  diverted to a sibling `.fc-log` file

## Future decisions (not yet made)
- virtio-net driver design
- TCP state machine implementation
- HTTP parser design
- Assembly branch coverage tooling
- PICT combinatorial testing integration
- OSACA CI integration
- Pi 5 self-hosted runner setup
