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

### D028: Python provisioning — apt + laptop-built aarch64 wheelhouse

**Decision:** Pi 5 image gets a working Python venv from first boot, populated
without ever needing internet on the Pi. Layered approach:

- pi-gen apt-installs `python3`, `python3-venv`, `python3-pip`, `python3-dev`
  during image build. PiOS Bookworm ships Python 3.11, meeting the CLAUDE.md
  project floor.
- pi-gen custom stage creates `/opt/pi5_harness/venv/` and seeds it from a
  laptop-built aarch64 wheelhouse bundled as a stage asset. The wheelhouse
  is produced inside an aarch64 Docker container on the laptop (via
  `qemu-user-static`) so the resolver picks correct ABI tags for C-extension
  packages like `pydantic-core`.
- Post-flash dep updates: scp a new wheelhouse to the Pi, run
  `pip install --upgrade --no-index --find-links=wheelhouse/ -r requirements.txt`
  inside the venv. No image rebuild needed.

**Justification:**
- venv is pure Python; no internet required to use one, only to populate.
- Cross-arch wheel resolution from x86_64 to aarch64 via plain `pip download`
  silently mis-resolves C-extension wheels; in-arch resolver via a Docker
  container is the robust fix.
- Hybrid (apt for the runtime, wheelhouse for venv contents) gives a working
  harness from first boot AND fast iteration on dependency changes.
- Bake-everything-into-image (alternative considered) was rejected: every dep
  change forces a full image rebuild.
- Pure-apt (alternative considered) was rejected: limits us to Debian-version
  Python packages, often well behind PyPI.

### D029: Layer-by-layer network bring-up toolchain on Pi 5

**Decision:** Pi 5 image includes a comprehensive network diagnostic toolchain
that exercises each OSI layer in isolation. Bring-up and debugging proceed
layer by layer; tools at layer N test only layer N.

**Tools by layer:**

| Layer | Verifies | Tools |
|-------|----------|-------|
| L1 physical | link state, speed, CRC errors | `ethtool`, `ip link` |
| L2 frame | bridge state, TAP carrier, ARP resolution | `bridge` (iproute2), `arping`, `tcpdump` |
| L3 IP | reachability both directions, routing | `iputils-ping`, `mtr-tiny`, `traceroute`, `ip route` |
| L4 TCP/UDP | listen/connect, payload integrity, port state | `ncat`, `socat`, `iperf3`, `nmap`, `ss` |
| L5/6 TLS | handshake, cert chain, cipher negotiation, alerts | `openssl`, `gnutls-bin`, `testssl.sh` (scp'd) |
| L7 HTTP(S) | GET/POST, keep-alive, chunked, status codes | `curl`, `wget` |

Plus `tcpdump` for capture at any layer (run on Pi to observe guest↔bridge,
on laptop to observe wire-level), with pcap files scp'd to laptop for
Wireshark offline. `scapy` lives in the `pi5_harness` Python venv (per D028)
for programmatic L2/L3/L4 packet crafting from harness code.

**Justification:**
- Direct application of CLAUDE.md "isolate what you test": when a test fails
  at layer N, the diagnostic at layer N tells you whether the problem is at
  N or below — not entangled with higher layers.
- All tools are standard Debian packages — installed via apt during pi-gen.
- `scapy` in Python (rather than shell-tool spawning) lets the harness craft
  precise frames inline, the same approach used on ws_pi5 for L2 testing.
- Comprehensive list rather than minimal set: 128 GB SD card per D023, no
  disk pressure, and missing tools at debug time cost more than the disk.

### D030: Full GNU dev env on Pi 5 + BPF observability

**Decision:** Pi 5 image includes a complete native dev environment, enabling
"scp source and build" as a recovery path for any tool we discover we need.
Installed via apt during pi-gen.

**Toolchain:**
- `build-essential` (gcc, g++, make, libc6-dev), `gdb`, `gdbserver`,
  `strace`, `ltrace`, `lsof`.
- Build systems: `cmake`, `ninja-build`, `meson`, `autoconf`, `automake`,
  `libtool`, `pkg-config`, `git`.

**Common dev libraries (headers for build-from-source workflow):**
- `libssl-dev`, `zlib1g-dev`, `liblzma-dev`, `libzstd-dev`,
  `libcurl4-openssl-dev`, `libsqlite3-dev`, `libreadline-dev`,
  `libedit-dev`, `python3-dev`.

**Profile and observability:**
- `linux-perf` (Pi-kernel-matched variant) for CPU profiling.
- `valgrind` for memory analysis.
- `bpftrace` for ad-hoc kernel tracing.
- `bpftool` for low-level BPF program inspection.
- `bpfcc-tools` — BCC's prebuilt suite (`tcpconnect`, `tcplife`, `execsnoop`,
  `bindsnoop`, `biolatency`, `trace`, etc.).

**Utilities:** `vim`, `tmux`, `less`, `rsync`, `jq`, `yq`.

**Justification:**
- Pi 5 is a DUT (device under test) — observability tooling earns its weight
  there in a way it wouldn't on a stripped-down production node.
- BCC and bpftrace give live kernel/process visibility essential when
  diagnosing bare-metal-guest failures from the host side.
- 128 GB SD card per D023 — disk usage is rounding error.
- "scp + build" as a recovery path: if we discover at debug time we need a
  tool that isn't installed, having the toolchain on the Pi means we can
  build it locally without bouncing back to the laptop and re-flashing.
- Compatible with D026: Firecracker still cross-built on the laptop per that
  decision; this dev env is for ad-hoc auxiliary tools, not production-
  pinned binaries.

### D031: TLS scope — production-ready TLS 1.2 + 1.3 stack, 2013-onwards compatibility

**Decision:** fireasmserver implements a production-ready TLS server stack in
100% assembly per D003. Full feature set from the start; no scope deferrals.
Client compatibility window: approximately 2013 onwards.

**Protocols:** TLS 1.3 (RFC 8446) and TLS 1.2 (RFC 5246), both fully
supported. Configuration profile per RFC 7525 + RFC 9325 BCP. Excluded:
SSLv3, TLS 1.0, TLS 1.1 (pre-2013 / actively unsafe).

**Key exchange:** ECDHE (x25519, secp256r1, secp384r1); FFDHE (ffdhe2048,
ffdhe3072, ffdhe4096; RFC 7919); RSA kex in 1.2 (still dominant in 2013;
required for OEM long-tail compat). Per-deployment profile configurable;
forward-secrecy-only is the recommended default.

**Cipher suites:**
- TLS 1.3: all five MTI suites (AES-128/256-GCM, ChaCha20-Poly1305,
  AES-128-CCM variants).
- TLS 1.2: ECDHE-ECDSA / ECDHE-RSA / DHE-RSA with AES-GCM, AES-CBC-HMAC-SHA,
  ChaCha20-Poly1305.
- Excluded: RC4, 3DES, EXPORT, anonymous, static-DH (all actively broken).

**Signature algorithms:** ECDSA (P-256/P-384/P-521), RSA-PSS, RSA-PKCS1 v1.5
(1.2 compat), Ed25519.

**Features:**
- mTLS (client certificate authentication).
- Session resumption: PSK (1.3), session ID + session ticket (1.2).
- 0-RTT early data with replay protection.
- SNI (RFC 6066), ALPN (RFC 7301), OCSP stapling, secure renegotiation
  (RFC 5746), key update (1.3).
- Extended Master Secret (RFC 7627) for 1.2 — required hygiene.
- Encrypt-then-MAC (RFC 7366) for 1.2 CBC suites — preferred when peer
  supports.
- Heartbeat extension (RFC 6520) explicitly NOT implemented (Heartbleed
  mitigation).

**Certificate lifecycle:**
- ACMEv2 client (RFC 8555) for Let's Encrypt and other ACME-compatible CAs.
- Private CA injection path (OEM provisioning scenarios).
- Full chain validation with configurable trust anchors.
- Hostname matching per RFC 6125.

**Hardware acceleration (per CLAUDE.md "check accelerations first"):**
- AArch64: ARMv8 AES (`AESE`/`AESD`/`AESMC`), SHA-256 (`SHA256H`/`SHA256SU0`),
  `PMULL`/`PMULL2` for GHASH, NEON for ChaCha20/Poly1305.
- x86_64: AES-NI (`AESENC`/`AESENCLAST`), `PCLMULQDQ` for GHASH, SHA-NI where
  available (SSSE3/AVX2 fallback), AVX2 for ChaCha20/Poly1305.
- Modular exponentiation (RSA, FFDHE): bignum routines optimized for each
  arch's multiply-accumulate primitives.

**Layer-by-layer bring-up:**

1. **Crypto primitives** — AES-{128,256}-{GCM,CBC,CCM}, SHA-{256,384,512},
   HMAC, ChaCha20-Poly1305, x25519, secp{256,384,521}r1 point ops, Ed25519,
   RSA modexp/PSS/PKCS1, HKDF, TLS 1.2 PRF, CSPRNG. Verified against NIST
   CAVP, Wycheproof, RFC test vectors.
2. **ASN.1/DER + X.509 parser** — DER decoder, certificate chain parser,
   signature verification, hostname matching. Hand-crafted certs + real
   Let's Encrypt certs as positive tests; Wycheproof corpus as negative.
3. **Record layer** — TLS 1.3 AEAD; TLS 1.2 AEAD + CBC-HMAC (Encrypt-then-
   MAC preferred, MAC-then-encrypt with constant-time anti-Lucky13 fallback).
   Fixed-key vectors from RFC 8448 (1.3) and RFC 5246 §6.2 (1.2).
4. **Handshake state machine** — full and resumed flows for both versions;
   mTLS path; 0-RTT path with replay protection; key update; secure
   renegotiation; Extended Master Secret. RFC 8448 byte-accurate tests for
   1.3; RFC 5246 + custom vectors for 1.2.
5. **Certificate lifecycle** — ACMEv2 issuance flow; private CA injection;
   OCSP stapling fetch + cache + serve; cert reload without service
   interruption.
6. **End-to-end interop** — `openssl s_client` (multi-version, multi-suite),
   `gnutls-cli`, `curl`, `testssl.sh` clean run, real browser interop matrix.

**Justification:**
- fireasmserver targets OEM commercial deployment; the OEM channel cannot
  dictate client compatibility windows. Full spec coverage within the
  2013-onwards window is a market requirement, not a luxury.
- Implementing the full stack from the start is cheaper than retrofitting
  later — TLS internals (record layer, key schedule, state machine) deeply
  shape higher-layer structure.
- 100% assembly + HW-accelerated crypto is a defensible commercial
  differentiator: the implementation effort is the moat. ISA-native
  AES/SHA/PMULL instructions map directly to crypto operations, per D003.
- RFC 8448 (1.3) and RFC 5246 (1.2) byte-accurate test vectors enable
  deterministic per-layer unit tests without a live peer, matching
  CLAUDE.md "repro before fix" discipline.
- Exclusions (SSLv3, TLS 1.0/1.1, RC4, 3DES, EXPORT, anonymous, static-DH,
  Heartbeat) are not deferrals — they are explicit exclusions on safety or
  compatibility-window grounds.

### D032: Crypto math implementation strategy — ISA-idiomatic, macros-first, constant-time, cache-aware

**Decision:** Cryptographic primitives for fireasmserver (supporting D031's
TLS stack) are implemented under four binding design principles:

**1. ISA-idiomatic code.** Each arch uses its full native primitive set
rather than a least-common-denominator interface across arches.
- **AArch64:** 31 GPRs; UMULH + MUL for 128-bit products; MADD/MSUB for
  multiply-accumulate; ADCS chains for carry propagation; ARMv8 crypto
  (AESE/AESD/AESMC, SHA256H/SHA256SU0, PMULL/PMULL2 for GHASH); NEON for
  ChaCha20/Poly1305 SIMD; LDP/STP paired load/store.
- **x86_64:** MULX (BMI2) + ADCX/ADOX (ADX) for two parallel carry chains
  per multiply step; AES-NI (AESENC/AESENCLAST); PCLMULQDQ for GHASH; SHA
  extensions where available (SSSE3/AVX2 fallback); AVX2 for ChaCha20/
  Poly1305 SIMD.

**2. Macros-first, subroutines later.** Bignum and other performance-
critical primitives are implemented as assembler macros that inline at each
call site, not as subroutines. A subroutine ABI would force both arches
into LCD register usage; macros let each arch use its full register file
and idiomatic instruction sequences without ABI constraints. If code size
becomes an issue (instruction-cache pressure, deployment footprint),
specific primitives can be refactored to subroutines — but optimize for
speed first, size later.

**3. Constant-time is mandatory.** No data-dependent branches in crypto
code. No data-dependent memory access patterns. No T-tables (use hardware
AES instructions instead). Scalar multiplication uses Montgomery ladder,
not sliding-window or other variable-time algorithms. Modular arithmetic
uses constant-time comparisons. This is a correctness requirement, not an
optimization — timing side channels (Bernstein 2005 on AES T-tables;
Tromer/Osvik/Shamir 2010) defeat TLS when violated.

**4. Cache-aware layout.** Hot crypto data is aligned to cache-line
boundaries (`.balign 64`) and structured to minimize working-set size:
- AES round keys (240 B for AES-256) → 4 contiguous cache lines.
- SHA state (32 B) → 1 cache line.
- Bignum operands (32 B for 256-bit) → 1 cache line.
- Hot/cold field separation in per-connection state.
- Prefetch instructions used deliberately (`PRFM PLDL1KEEP` on ARM,
  `PREFETCHT0` on x86).

**What we control inside a Firecracker guest:** alignment, structure
layout, prefetch-instruction behavior, working-set sizing. Cache geometry
is the host CPU's, not virtualized — Pi 5's Cortex-A76: 64 KB L1I +
64 KB L1D + 512 KB L2, 64-byte lines.

**What we do not control:** physical page mapping, host scheduling
preemption, co-tenant cache pressure, TLB shootdowns. We reason about
relative cache behavior, not absolute state from cycle 0.

**Justification:**
- ISA-idiomatic is the whole point of 100% assembly per D003. A compiler
  would emit generic code that sacrifices each arch's specific strengths.
  If we are writing assembly, we write each arch's *best* assembly.
- Macros expose the full register file to each primitive operation. A
  256-bit ECDHE keygen fully inlined is ~5000–10000 bignum ops, each
  saving ~30–50 instructions of call overhead; cumulative savings are
  significant. Code size grows proportionally but Cortex-A76's 64 KB L1I
  absorbs it.
- Constant-time is non-negotiable for production TLS. All cipher suites
  and protocols in D031 assume constant-time underlying primitives.
- Cache-awareness is simultaneously a performance concern (working-set
  fits in L1) and a security concern (no data-dependent cache misses).
  The same layout discipline satisfies both.
- Firecracker virtualizes the CPU but passes cache geometry through; we
  can reason about the Pi 5's specific cache hierarchy and design for it.

### D033: PiOS Trixie base image — supersedes D023's Bookworm clause

**Decision:** D023's "PiOS Lite 64-bit *Bookworm*" clause is superseded by
"PiOS Lite 64-bit *Trixie*" to follow the current-stable pi-gen, which
stopped advancing Bookworm tags in November 2025 and now targets only
Trixie. All other D023 terms (pi-gen at pinned tag, single custom stage,
custom KVM-enabled kernel, hostname `fireasm-test`, user `ed`, fresh scoped
SSH key, password-login disabled, single 128 GB ext4, no runtime DNS)
carry forward unchanged.

**Version pins (current stable at 2026-04-17):**
- pi-gen tag: `2026-04-13-raspios-trixie-arm64`
- raspberrypi/linux branch: `rpi-6.12.y` (project default; Linux 6.12 LTS)
- Alpine meta-rootfs (per D027): v3.23.4
- Firecracker (per D026): v1.15.1

**Knock-on effects:**
- Python on Pi shifts from Bookworm's 3.11 to Trixie's 3.13. Still above
  the CLAUDE.md >=3.11 floor. D008's "PiOS Bookworm ships 3.11" clause is
  now historical context, not a live pin.
- systemd, apt package versions, and kernel patches across the image
  advance forward. No expected compatibility breaks for the tooling in
  D028–D030.
- Wheelhouse (per D028) must be built against Python 3.13 ABI tags
  (`cp313` rather than `cp311`) — the aarch64 Docker container used to
  resolve wheels must run Python 3.13.

**Justification:**
- Follows Ed's "current stable" directive (2026-04-17).
- Bookworm pi-gen tags are stale (latest from Nov 2025); Trixie is
  actively maintained.
- Trixie has been Debian stable since mid-2025 — no bleeding-edge risk.
- Python 3.13 is a quiet upgrade with no project-level blockers.

### D034: Hardware platform profiles — parameterized cache and ISA features

**Decision:** Cache constants and ISA feature gates in fireasmserver crypto
code are parameterized by a build-time hardware *profile*, not hardcoded.
Supersedes the Pi 5-concrete cache numbers cited in D032 while keeping
D032's principles (ISA-idiomatic, macros-first, constant-time, cache-aware)
intact.

**Initial profile set:**

| Profile | CPU core | Role |
|---------|----------|------|
| `pi5` | Cortex-A76 | dev/test per D022 |
| `graviton2` | Neoverse N1 | AWS production |
| `graviton3` | Neoverse V1 | AWS production |
| `graviton4` | Neoverse V2 | AWS production; also Google Axion, Azure Cobalt, Ampere AltraMax |
| `intel-skylake` | Skylake-SP | Intel production (baseline) |
| `intel-icelake` | Ice Lake-SP | Intel production |
| `intel-sapphire-rapids` | Sapphire Rapids | Intel production |
| `intel-emerald-rapids` | Emerald Rapids | Intel production |
| `intel-granite-rapids` | Granite Rapids | Intel production |
| `amd-zen3` | Milan | AMD EPYC production |
| `amd-zen4` | Genoa | AMD EPYC production |
| `amd-zen5` | Turin | AMD EPYC production |
| `generic-aarch64` | — | conservative fallback |
| `generic-x86_64` | — | conservative fallback |

**Per-profile parameters** (exposed as `.equ` constants via a profile-
specific `.S` include file):
- `CACHE_LINE_SIZE` (bytes; 64 on every currently listed profile).
- `L1I_SIZE`, `L1D_SIZE`, `L2_SIZE` (bytes, per-core).
- `L1D_ASSOCIATIVITY`, `L2_ASSOCIATIVITY`.
- `PREFETCH_DISTANCE` (bytes ahead, tuned per microarchitecture).
- ISA feature flags:
  - **AArch64:** `HAS_ARMV8_AES`, `HAS_ARMV8_SHA256`, `HAS_ARMV8_SHA512`,
    `HAS_PMULL`, `HAS_SVE`, `HAS_SVE2`.
  - **x86_64:** `HAS_AES_NI`, `HAS_PCLMULQDQ`, `HAS_SHA_NI`, `HAS_AVX2`,
    `HAS_AVX512F`, `HAS_VAES`, `HAS_VPCLMULQDQ`, `HAS_BMI2`, `HAS_ADX`.

**Selection:** build-time via Makefile variable, e.g.
`make ARCH=aarch64 PROFILE=graviton3`. Default profiles: `pi5` for AArch64
dev, a locally-appropriate Intel/AMD profile for the x86_64 laptop;
`generic-aarch64` and `generic-x86_64` for "build once, run anywhere"
conservative builds.

**No runtime CPU dispatch in the initial implementation:**
- Adds branches to crypto hot paths.
- Typical Firecracker deployment is homogeneous (AWS spins only Gravitons;
  Fly chooses specific host pools). Build-time profile matches deployment
  reality.
- Runtime-selected dispatch stubs can be introduced later if deployment
  patterns demand it — that would be a separate decision.

**Profile conformance with D031 crypto requirements:**
- Every AArch64 profile must expose ARMv8 AES + SHA-256 + PMULL at minimum.
  Cortex-A55 and A72 (no SHA extensions) are explicitly excluded from
  server profiles.
- Every x86_64 profile must expose AES-NI + PCLMULQDQ at minimum. SHA-NI,
  AVX2, AVX-512, VAES, VPCLMULQDQ enable faster paths where present.

**Justification:**
- Hardcoding Pi 5's 64 KB L1D or Cortex-A76's prefetch distance into crypto
  code creates a false dependency and risks incorrect performance on
  production hardware.
- The production deployment surface is AWS Graviton + Intel/AMD server
  families. Pi 5 is a dev target per D022. D032's layout principles were
  right; its concrete numbers were parochial.
- Build-time profiling keeps generated code as tight as ISA-specific
  assembly should be, without runtime dispatch overhead.
- Cache line size is 64 bytes on every profile listed. Apple Silicon
  (128 B) and POWER (128 B) are not Firecracker deployment targets and
  therefore not in profile scope.
- ISA feature flags make the "use AVX-512 where present" pattern explicit
  at build time rather than relying on conditional macros scattered
  through the code.

## 2026-04-18

### D035: Pi 5 package source — apt-cacher-ng on the laptop

**Decision:** The Pi 5 gets Debian packages through an `apt-cacher-ng` proxy
running on the laptop at `10.0.0.1:3142`. The Pi's APT configuration
(`/etc/apt/apt.conf.d/00proxy`) points at that proxy. Laptop has internet;
Pi does not. Running `sudo apt install foo` on the Pi transparently flows
requests to the laptop, which fetches from `deb.debian.org`, caches locally,
and serves the `.deb` back to the Pi. D024's "no direct internet route from
the Pi" constraint is preserved — the Pi only ever talks to the laptop.

**What this replaces:** The implicit "rerun pi-gen to add a package" flow.
That's a 30–60 minute cycle per package change and is unsuitable for routine
development. apt-cacher-ng turns it into a normal `apt install` on the Pi
with no perceptible difference from a developer's perspective.

**Setup:**
1. Laptop: `sudo apt install apt-cacher-ng`; service listens on port 3142.
2. Laptop: firewall rule to allow only `10.0.0.0/24` on port 3142 (isolate
   the cache from the LAN).
3. Pi: write `/etc/apt/apt.conf.d/00proxy` with
   `Acquire::http::Proxy "http://10.0.0.1:3142";`.
4. Next pi-gen rebuild bakes the proxy config into the image so it's present
   from first boot. For the already-running Pi, the laptop ships the file in
   via `scp` once.

**Justification:**
- **Preserves D022 + D024.** Pi stays on the isolated bridge, no credentials,
  no direct internet route. Only new traffic is Pi → laptop on one TCP port.
- **Rejects direct `.deb` fetch + `scp` + `dpkg -i`.** Works for one-off
  cases but requires dependency resolution on the laptop against a
  Trixie-arm64 chroot — reimplementing apt's job. apt-cacher-ng is apt doing
  its own job via a proxy, which is strictly less code to own.
- **Rejects temporary NAT / MASQUERADE during apt ops.** Ugly toggle state,
  easy to forget on, contradicts D024 intermittently.
- **Caching is a bonus.** Repeated `apt install` across rebuilds hits the
  laptop cache, not the internet — faster, and works offline for anything
  seen before.

**Scope note:** D035 is about APT traffic only. Generic Pi → internet
reachability remains blocked per D024; apt-cacher-ng does not open a general
outbound path.

### D036: Pi 5 backup strategy — two-tier rsync + hot-dd over SSH

**Decision:** Pi 5 backups are taken over SSH from the laptop with the Pi
running. No SD card removal for routine backup operations. Two tools cover
different cadences:

1. `tooling/pi5_build/backup_pi_rsync.sh` — fast, file-level, hardlink-
   snapshotted under `build/pi-backup/snapshots/`. Run frequently. Covers
   "I broke something, put me back" via rsync-restore without reflashing.
2. `tooling/pi5_build/backup_pi_dd.sh` — slow, block-level hot SSH-dd,
   produces a sparse `.img` under `build/pi-backup/images/` that
   `flash_sd_card.sh` restores to a new SD card. Run weekly or after
   major changes. Covers SD card death.

**Why not cold-dd via SD-in-laptop-reader:** The Pi 5 sits in a Vilros case
with a fan shroud; SD removal requires smooth-jaw needle-nose pliers and
has real risk of damaging the card. Routine operations cannot require SD
pulling. Cold-dd is retained as a tool (`flash_sd_card.sh` is unchanged)
but only for the one-time restore-to-new-card path that an SD death or
replacement already implies.

**Consistency model:**
- rsync: file-level snapshot with rsync's normal "read-while-open"
  semantics. Active writes to specific files may produce inconsistent
  captures of those files; the rootfs as a whole is otherwise consistent
  enough for file-level restore.
- hot-dd: crash-consistent (equivalent to pulling power during the read).
  ext4 journal is whatever state it happened to be; normal fsck replay on
  restore boot handles it.

**Performance:**
- rsync after first sync: seconds to minutes (delta-only).
- hot-dd: full card-size transfer over the laptop↔Pi USB NIC, realistic
  throughput ~20–30 min for a 120 GB card.
- Periodic `--zerofill` on `backup_pi_dd.sh` fills the rootfs free space
  with zeros so `conv=sparse` produces a compact image. Run monthly, not
  per-backup — it writes the full card and consumes SD write endurance.

**Restore paths:**
- rsync: `rsync --delete <snapshot>/ ed@pi:/` (over SSH with sudo-rsync).
  Test by SSH first, then selectively restore paths, then whole-tree.
- hot-dd: `IMG_PATH=<.img> ./flash_sd_card.sh /dev/sdX` against a new or
  replacement card in a USB reader — the only SD-in-laptop event in the
  normal lifecycle.

**Rejected alternatives:**
- Cold-dd routine backup: mechanical risk in the Vilros case per above.
- Tar + partition-rebuild restore: complex restore script, fragile.
  Bit-perfect hot-dd plus existing `flash_sd_card.sh` handles the same
  failure mode more simply.
- rsync-only (no block-level path): cannot reconstruct a bootable card
  from file-level backup alone; SD-death recovery would still require a
  pi-gen rebuild, defeating the "avoid pi-gen" motivation.

### D037: Firecracker install — prebuilt binary for now, build-from-source deferred

**Decision:** Install Firecracker on the Pi 5 (and reuse the same upstream
tarball convention on the laptop when needed) from the **official GitHub
release tarball**, not from source. This amends — does not supersede —
D026's "build from upstream source, pinned tags, multi-version" policy.
D026's long-term target remains valid; the amendment is temporal.

**Version pin:** `v1.15.1` across both hosts for protocol symmetry during
tracer-bullet bring-up.

**Rationale:**
- Time-to-first-green matters while the AArch64 tracer bullet is the
  critical-path artifact. Prebuilt shaves ~30–60 min of Pi-side Rust
  toolchain install + cargo build off the cycle.
- The multi-version story D026 anticipates (running several Firecracker
  releases side-by-side in CI) isn't yet load-bearing. A single pinned
  prebuilt fulfills every current test.
- Patching Firecracker source isn't needed — upstream v1.15.1 exposes
  everything we use (PVH x86_64, Linux Image aarch64, `--no-api`,
  serial → file, `--config-file`).

**Migration trigger — revisit D026 when any applies:**
- CI needs >1 Firecracker version concurrently (e.g., compat matrix
  for downstream deployments).
- A Firecracker bug surfaces that requires a source-level patch or
  custom build-time config.
- Upstream stops providing binary tarballs for either target arch.

**Security notes:** The install script downloads from
`github.com/firecracker-microvm/firecracker/releases`, verifies SHA256
against the published checksum, and does a final `firecracker --version`
sanity check on the Pi post-install.

## Future decisions (not yet made)
- virtio-net driver design
- TCP state machine implementation
- HTTP parser design
- ~~Assembly branch coverage tooling~~ **DEPRECATED 2026-04-19T07:15Z** — implemented as `tooling/src/branch_cov/` (MVP in commit `e3aa166`, 2026-04-18) with capstone + pyelftools disassembly, QEMU-trace ingestion, per-cell baselines, and a ratchet-mode `--baseline` flag wired into `cd-matrix.yml` via `run_local.sh`.
- PICT combinatorial testing integration
- OSACA CI integration
- ~~Pi 5 self-hosted runner setup~~ **DEPRECATED 2026-04-17T00:00Z** — superseded by D022 (Pi 5 is a local-only AArch64 test host; CI lives in GitHub Actions, not on the Pi).
