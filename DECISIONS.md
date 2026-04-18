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

## 2026-04-12

### D021: Ontology-driven gates with incremental constraint discovery

**Decision:** Domain constraints in the project ontology (`tooling/qemu-harness.json`)
drive the quality gate runner. The gate runner reads the ontology, extracts domain
constraints, and for each constraint verifies three things:

1. **Traceability** — a test exists whose name matches the constraint (naming convention:
   test function name contains the constraint's `name` field with hyphens converted to
   underscores).
2. **Structural coverage** — the linked test actually executes the code that enforces the
   constraint (coverage scoped to the enforcement function).
3. **Mutation verification** — mutating the enforcement code causes the linked test to fail,
   proving the test depends on the constraint being enforced, not just on nearby code running.

Verification is bidirectional: the gate runner also checks that every function declared in
the ontology's module specs exists in the implementation with a matching signature, and
flags functions in code that are not declared in the ontology. Drift in either direction
is surfaced at commit time.

**Ontology lifecycle — draft first, discover as you go:**

The ontology is NOT a waterfall spec written before implementation begins. The workflow is:

1. Draft an initial ontology with rough entities, relationships, and module specs based on
   what is known before coding starts. This provides direction, not certainty.
2. Implement incrementally toward the draft. Testability, measurement, and observability
   challenges emerge during this work and cannot be fully anticipated in the draft.
3. When implementation reveals a new constraint — a safety invariant, a platform quirk, a
   failure mode caught by review — crystallize it immediately: add a `DomainConstraint` to
   the ontology, write the enforcement code, write the test (named to match), regenerate
   the DAG via `build_qemu_harness_ontology.py`.
4. The gate runner picks up the new constraint automatically on the next commit. From that
   point forward it is verified on every commit, forever.
5. When the code changes in a way that makes the ontology inaccurate, the conformance
   check flags the delta. Update whichever side is lagging — ontology or code — and
   resolve the disagreement explicitly.

The ontology and the code co-evolve. Each reinforces the other: the ontology keeps the
code honest (constraints are checked); the code keeps the ontology honest (conformance
drift is detected). Neither leads permanently.

**Justification:**

- fireasmserver's existing 7 domain constraints were all discovered AFTER initial
  implementation, not predicted before it. `path-traversal-rejected` emerged from a
  security review. `clean-vm-kill` emerged from discovering QEMU's serial-flush behavior.
  `kvm-required-for-firecracker` emerged from CI failures on runners without KVM. The
  ontology is a record of discovered truth, not a prediction of future requirements.
- The three-level verification chain (traceability → coverage → mutation) is the same
  methodology used in aerospace systems engineering (DO-178C: requirements traceability
  matrix + structural coverage analysis + fault injection), adapted for a continuous
  integration context where verification runs on every commit rather than at milestone
  reviews.
- Constraints that are too abstract to verify automatically (e.g., D013 "build correct
  foundational abstractions") stay in the ontology as documentation for humans and LLMs
  but are not wired into the gate runner. Only constraints that are concrete enough to
  have a testable enforcement artifact participate in automated verification.

**Scope:** This decision governs fireasmserver's `qemu_harness` tooling and will inform
the design of `aofire-asm-agent`'s gate runner (`gates.py`), which will implement the
constraint-driven verification chain as a reusable tool consumable by all bare-metal
assembly projects in the product line.

**Implementation status:** The ontology and constraints exist. The gate runner that reads
them and performs the three-level verification does not yet exist — it is the next piece
to build in `aofire-asm-agent`.

## 2026-04-17

### D022: Pi 5 as local-only AArch64 test host

**Decision:** The Raspberry Pi 5 is a local-only AArch64 test platform, not a
GitHub Actions self-hosted runner. It sits on a direct Ethernet link to the
laptop via USB NIC, with static addresses: Pi 5 `10.0.0.2/24`, laptop
`10.0.0.1/24`. No GitHub access from the Pi. fireasmserver AArch64 build
artifacts are produced on the laptop and delivered to the Pi via `scp`. CI
continues to live in GitHub Actions. Supersedes the "Pi 5 self-hosted runner
setup" item previously listed under "Future decisions."

**Justification:**
- Removes network-path failure modes (LAN flakes, GitHub outages, PAT/deploy-
  key rotation) from the AArch64 integration test path.
- Keeps credentials off the device — simpler security model.
- The Pi's role is pre-push integration tests and perf measurements, not
  per-commit fan-out; GitHub Actions handles the latter.
- Direct Ethernet gives deterministic, isolated network behavior — no LAN
  broadcast traffic, no DHCP, no NAT between laptop and VMs.

### D023: Pi 5 image via pi-gen at pinned tag plus one custom stage

**Decision:** The Pi 5 boot image is PiOS Lite 64-bit, built via pi-gen pinned
to a specific git tag, running on the laptop (x86_64; pi-gen uses
`qemu-user-static` for chroot stages). A single custom pi-gen stage layered on
top of stages 0–2 performs:

- Installs a custom KVM-enabled kernel built from `raspberrypi/linux` at the
  branch matching the PiOS kernel version. Kernel config explicitly enables
  `CONFIG_KVM=y`, `CONFIG_VIRTUALIZATION=y`, `CONFIG_ARM64_VHE=y`.
- Sets hostname `fireasm-test`; creates user `ed` (SSH key only, password
  login disabled).
- Generates a fresh SSH keypair scoped to Pi-5-access during the image build.
  Public key baked into `/home/ed/.ssh/authorized_keys`; private key written
  to laptop `~/.ssh/` with a distinct filename.
- `PasswordAuthentication no` in `sshd_config` from first boot.
- Single ext4 rootfs filling the 128 GB card; no disk-usage optimization.
- No DNS configured on the Pi at runtime (per D022's runtime-isolation scope).

**Migration path:** after the image is in real use and we know what we
actually need, revisit moving to a from-scratch debootstrap assembly (option
(i) from the 2026-04-17 design discussion). That migration earns its own
decision entry if/when it happens.

**Justification:**
- pi-gen encodes Pi-boot correctness that would otherwise be rediscovered by
  breaking things: firmware blob placement, `config.txt`, `/boot/firmware`
  layout, `kernel_2712.img` path, DTB + overlay placement.
- Pinned tag gives reproducibility; a single custom stage keeps all project-
  specific customization in one readable place.
- Custom kernel (not stock) removes ambiguity about whether the shipped
  kernel has KVM enabled, and allows offline KVM-readiness verification
  (extract kernel config, check modules) before burning a boot cycle.
- First-time Pi 5 bring-up from scratch trades fireasmserver progress for
  image-assembly expertise we don't currently need.

### D024: VM network — isolated bridge, routed, no NAT

**Decision:** Firecracker guests on the Pi 5 attach to a Linux bridge `br1`
(network `10.0.1.0/24`, Pi as gateway at `10.0.1.1`). The Pi sets
`net.ipv4.ip_forward=1`. The laptop adds a static route
`ip route add 10.0.1.0/24 via 10.0.0.2`. No MASQUERADE/NAT between `br1` and
`eth0`. Guests get stable per-test IPs assigned by the harness; the laptop
reaches each guest directly by IP.

**Justification:**
- Rejected option A (bridge `eth0` directly, guests on `10.0.0.0/24`): weak
  isolation, guest ARP/DHCP leaks onto the wire, silent `br_netfilter` cost
  if the module is loaded.
- Rejected option B (isolated bridge + MASQUERADE NAT): conntrack per-packet
  cost is real at high PPS on a Pi-class host; per-VM port-forwarding needed
  for laptop→guest reachability adds friction for parallel test VMs.
- Option C (chosen): per-VM stable addressability, direct laptop↔guest paths,
  no conntrack overhead, no port-forward gymnastics. Cost is one static route
  on the laptop and one sysctl on the Pi.

### D025: Flexible VM parallelism with opt-in CPU pinning

**Decision:** The number of concurrent Firecracker VMs is a runtime parameter
of the test harness, not baked into the image. Functional tests run unpinned
— kernel scheduler places vCPUs across the Pi's four cores. Performance tests
pin vCPUs per-run via cgroups v2 and `taskset`. The host kernel command line
does **not** set `isolcpus`.

**Justification:**
- Matches the separation between functional and performance test regimes
  (per ~/.claude/CLAUDE.md).
- Keeps the image single-purpose; perf-vs-functional is a runtime decision,
  not an image-build decision.
- `isolcpus` would make the Pi unsuitable for anything but pinned-core
  workloads and waste host cores when not running perf tests. Cgroups +
  `taskset` provide the same isolation on demand without the host-wide cost.

### D026: Firecracker built from upstream source, pinned tags, multi-version

**Decision:** Firecracker is built from `github.com/firecracker-microvm/
firecracker` at specific release tags. Both architectures are produced on the
laptop:

- **x86_64**: native `cargo build --release`.
- **aarch64**: cross-compiled against `aarch64-unknown-linux-musl` for a
  static binary (no runtime libc dependency on the Pi).

Multiple versioned binaries are installed side-by-side at
`/opt/firecracker/v<ver>/firecracker`. The test harness selects a version per
test. Starting pin = upstream stable at first build (exact tag recorded in
the build script once chosen). "Keep up to date" = adding a new
`/opt/firecracker/v<newver>/` alongside existing ones, never replacing the
pinned primary. Pi 5 receives built binaries via `scp`; the Rust toolchain
stays off the Pi.

**Justification:**
- Precise version control across the two-arch × multi-version matrix.
- Patch capability if we ever need to fix or instrument Firecracker itself.
- Rust builds are reproducible with a locked `Cargo.lock`; the same source
  tree yields the same binary.
- Laptop-only build keeps the Pi lean.
- Prebuilt GitHub Release binaries (the simpler alternative) were rejected
  because the multi-version + future-patch requirements are better served by
  a first-class source build from the start.

### D027: Alpine minimal rootfs for meta-testing, both arches

**Decision:** The minimal guest Linux rootfs used for meta-testing is Alpine
Linux (musl libc, busybox userspace), built for both AArch64 (Firecracker on
Pi 5) and x86_64 (Firecracker on laptop). Image built from `alpine-minirootfs`
tarballs at a pinned Alpine version.

**Scope:** Applies only to the meta-test rootfs. fireasmserver guests remain
bare-metal assembly images per the arch-specific boot decisions (D014, D020,
and the forthcoming AArch64 ARM64 Linux boot protocol decision).

**Justification:**
- Alpine is essentially init + shell + networking tools — exactly the surface
  meta-tests exercise (bridge, routing, virtio-net, CPU pinning).
- Small footprint (~5 MB compressed, ~15 MB uncompressed) keeps VM boot
  times low and memory cost minimal when running many in parallel.
- Clean separation from "application" behavior: nothing in Alpine resembles
  fireasmserver, so passing meta-tests cleanly indicate the host/VMM/network
  path is healthy, independent of any assembly code.
- Debian slim considered and rejected for this role: too large, too much
  userspace to discount when diagnosing failures. Debian could re-enter as a
  second meta-rootfs if glibc-specific behavior ever needs to be tested.

## Future decisions (not yet made)
- virtio-net driver design
- TCP state machine implementation
- HTTP parser design
- Assembly branch coverage tooling
- PICT combinatorial testing integration
- OSACA CI integration
- Pi 5 self-hosted runner setup
