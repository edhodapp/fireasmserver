# Architecture Decision Log

Chronological record of design decisions for fireasmserver.
Each entry captures the decision, rationale, and date.

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

## Future decisions (not yet made)
- PVH boot protocol for Firecracker
- virtio-net driver design
- TCP state machine implementation
- HTTP parser design
- Assembly branch coverage tooling
- PICT combinatorial testing integration
- OSACA CI integration
- Pi 5 self-hosted runner setup
