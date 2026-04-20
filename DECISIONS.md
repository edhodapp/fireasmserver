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

## 2026-04-19

### D038: L2 implementation methodology

**Decision:** L2 (Ethernet/MAC layer) — and by extension every subsequent
protocol layer — follows this fixed sequence. No skipping forward; no
mixing stages for the same layer.

1. **Requirements gather.** Extract conformance requirements from IEEE
   802.3/802.1 and relevant IETF RFCs into a tracking document that
   lives at `docs/l2/REQUIREMENTS.md` (analogous in spirit to
   `DECISIONS.md`). Each requirement gets an ID, a citation, and a
   status column: `spec` / `tested` / `implemented` / `deviation`.
   Arch-neutral — x86_64 and AArch64 share this doc.
2. **Design note.** Short architecture document at
   `docs/l2/DESIGN.md` covering: VMIO state/transition tables, buffer
   lifecycle, latency budget (see D039 family below), re-entrancy /
   atomicity ACL, DMA/cache-coherence model per arch, VLAN scope,
   observability hook contract. Test plan references concrete design
   decisions here rather than inventing them line-by-line.
3. **Test plan.** Behavioral (happy-path) tests first, per the
   Testability rule in `~/.claude/CLAUDE.md`. Each test cross-
   references a requirement ID.
4. **Functional implementation.** x86_64 first (per Ed, 2026-04-19).
   Assembly follows the design doc; deviations from the design doc
   update the design doc in the same commit.
5. **Adversarial.** Fuzz the wire boundary before performance work —
   malformed frames, runts, giants, bad FCS, crafted VLAN stacks, ARP
   flood, preamble attacks. Automated in CI as a gating step once in
   place.
6. ~~**Interop matrix.** At least: Linux mainline netdev, FreeBSD, OVS,
   one enterprise switch (Cisco or Juniper sample). Not exhaustive —
   representative.~~ **DEPRECATED 2026-04-19T08:10Z — see D042.** The
   "one enterprise switch" line implied real Cisco/Juniper hardware
   that is neither affordable nor relevant for firecracker cloud
   targets. D042 replaces it with a tiered free / on-demand / deferred
   plan that reflects what fireasmserver's deployment surface actually
   needs.
7. **Performance measurement.** Only after 3–6 are green. Measure
   cycles/frame, cache misses, pipeline stalls (OSACA predictions vs.
   `-icount` actuals vs. hardware PMC actuals).
8. **Second arch (AArch64).** Same sequence top-to-bottom. Design doc
   and requirements doc are *extended*, not forked.
9. **Production deployment.** After both arches complete. See D041.

**Why codified:** each stage catches a different class of bug. Skipping
adversarial because "we have unit tests" ships a parser that segfaults
on a runt. Skipping interop because "the packet looks right to us"
ships a NIC that nobody can plug into. The order is load-bearing.

### D039: L2 design-doc must explicitly state these five properties

**Decision:** The L2 design doc (`docs/l2/DESIGN.md`, per D038) must
state, upfront, each of the following — absence is a review-blocking
bug in the doc, not a runtime surprise to fix later.

1. **Latency and throughput budget.** A concrete target in
   cycles/frame (or ns/frame) at a stated link speed. "World-class"
   is not measurable; `sub-400 ns per 64-byte frame at 10 Gbps, single
   vCPU` is. Set before writing assembly.
2. **DMA and cache-coherence model.** On x86_64 the virtio rings are
   coherent-by-default; on AArch64 explicit `dsb`/`dmb` placement is
   correctness-critical. Document the model once per arch. Do not
   discover this late.
3. **VMIO re-entrancy / atomicity ACL.** Astier FSA is thread-free
   (per D012), but interrupts / exception dispatch can still arrive
   mid-handler. State which transitions are interruptible, which must
   be atomic, and how the automaton re-syncs.
4. **VLAN scope.** Which of 802.1Q, 802.1ad (Q-in-Q), 802.1Qau, 802.1Qbb
   are in scope. Leaving out is a choice to make deliberately; the
   product is aimed at enterprise deployments where VLAN is
   table-stakes.
5. **Observability hook contract.** How the hot path exposes
   diagnostic state without tanking throughput. Zero-overhead when
   disabled; bounded overhead when enabled. Ring-buffer tracing,
   per-state-counters, sampling. Designed before assembly is written
   around the hooks.

**Test:** the design-doc review gate asks "does the doc state each of
the five properties explicitly?" — yes/no per property.

### D040: Perf regression ratchet — baselines per cell

**Decision:** After L2 functional passes, establish per-cell
performance baselines that ratchet the same way branch-cov's
`tooling/branch_cov/baselines/<arch>-<platform>.txt` does today.
A `tooling/perf/baselines/<arch>-<platform>.<metric>.txt` file pins
the current measurement; CI fails on regression beyond a tolerance
band.

**Metrics initially in scope (all per L2 receive+transmit path):**
- cycles/frame (measured via `perf stat` on hosted runners,
  hardware PMCs on Pi 5)
- L1d cache miss rate
- branch-miss rate
- instructions retired (for cross-cell sanity checks)

**OSACA interplay:** OSACA's predicted throughput (the `CP` column)
becomes a secondary baseline. A divergence between OSACA prediction
and measured actuals is itself a signal — either OSACA's DB is
missing an opcode, or the real hardware is doing something different
from the microarch model. Ratchet on both. Don't silently accept a
large predicted/actual gap.

**Not yet decided:** tolerance band (±5%? ±2%? arch-dependent?).
Deferred until we have five consecutive runs' worth of baseline noise
to characterize.

**Cost/benefit motivation:** cycles/frame is the only metric that
separates "world-class" from "works." Measuring without ratcheting
lets regressions slip in one %-point at a time until the compounding
ships a product that is slow. Branch-cov's pattern worked — reuse it.

### D041: Production deployment pipeline requirements

**Decision:** The "real deployment" phase of the CD pipeline (unlocks
per the trigger in
`~/.claude/projects/-home-ed-fireasmserver/memory/project_cd_delivery_trigger.md`)
must include the following capabilities before any production
customer is onboarded. This is the "don't-put-this-off" list.

1. **Failure injection.** Configurable fault modes — dropped packets,
   reordered frames, corrupted FCS, allocator OOM, DMA stall. Injected
   in CI and in pre-release testing. Chaos-engineering-style. Fail-fast
   on regression, fail-loud on new failures.
2. **Observability.** Hot-path ring-buffer tracing, per-state counters,
   PMC sampling. Dumpable on fault. Compatible with the observability
   hook contract mandated by D039.
3. **Rollback confidence.** Every release is tag-pinned; rolling a
   specific customer back to `vX.Y.Z` is a one-command operation.
   Binary artifacts signed (see #6) and retained for at least the
   support window.
4. **Canary rollouts.** Percentage-based traffic split at the fly.io /
   Kata / EC2 level. Canary population observed with separate metrics;
   automatic rollback on threshold breach.
5. **Graceful drain.** Existing connections terminate cleanly on
   rolling deploy. Requires a drain protocol designed in; cannot be
   retrofitted.
6. **Backpressure + circuit breakers.** What happens when L2 can't
   keep up with offered load. Silent drops are the enemy; explicit
   backpressure signaling upstream is the answer.
7. **Deterministic replay.** When something breaks in prod, we can
   reconstruct what the automaton saw. Ring-buffer trace + state dump
   on fault, retrievable post-incident.
8. **Signed release artifacts + SBOM + SLSA attestation.** cosign
   signatures on every release, syft-generated SBOM, SLSA L3 build
   provenance (GitHub's `actions/attest-build-provenance` gets us
   most of the way). Required for regulated OEM markets per the
   market notes.

**Sequencing:** same "functional before perf" pattern. Failure
injection and observability first — if we can't see what's
happening, perf numbers lie. Rollback and canary next — they
presuppose signed, retained artifacts. Graceful drain, backpressure,
replay follow. All eight before first paying customer.

**Not yet decided:** tenancy model (single-tenant VM per customer vs.
multi-tenant per microVM), blast-radius containment at the microVM
level, cross-region coordination for fly.io. Defer to the deployment-
phase trigger.

### D042: L2 interop matrix — free / on-demand / deferred (supersedes D038 §6)

**Decision:** Replace D038 stage 6's single "one enterprise switch
(Cisco or Juniper sample)" line with a tiered plan that matches
fireasmserver's actual deployment surface:

**Tier 1 — In CI every push (free, containerized, automated):**
- Linux mainline netdev (`veth` pair in a Docker container)
- Open vSwitch (OVS) in a Docker container
- **Arista cEOS-lab** via `containerlab` — free-for-lab-use Docker
  image; real Arista EOS code path
- **Nokia SR Linux** via `containerlab` — free for non-production

These four cover ~80% of "does real vendor code accept our frames"
at L2 and run alongside the existing `cd-matrix.yml` cells at zero
recurring cost.

**Tier 2 — Pre-sale, OEM-gated (free, on-demand):**
- **Cisco DevNet Sandbox** (`developer.cisco.com/sandbox`) —
  reservation-based free access to real Catalyst 9000, Nexus 9000,
  ISR4000. Run once per customer engagement where Cisco is the stated
  target, not per release.
- **Juniper vLabs** — same pattern for Junos.
- **Arista EOS Central** — vEOS free non-prod (backup to cEOS-lab).

**Tier 3 — Deferred, customer-specific, potentially on-hardware:**
- Decommissioned physical gear (Catalyst 2960-X on eBay, ~$30–80) if
  a specific OEM deal demands desk-local silicon. Not before.
- Paid simulators (Cisco CML Personal Edition, ~$200/yr) if DevNet
  Sandbox reservation windows prove too constraining.

**Why the matrix looks like this:**
- fireasmserver's cloud deployment surface (fly.io, AWS EC2, Kata
  Containers, nested Firecracker) is entirely virtual-switched.
  Customer traffic never hits an enterprise Cisco.
- L2 is the most standardized networking layer. Ethernet II,
  802.1Q, LACP, STP are consistent across vendors in a way that
  L3+ protocols (BGP dialect quirks, OSPF opaque LSAs) are not.
  The interop risk at L2 is low enough that cEOS-lab + SR Linux +
  OVS catches the overwhelming majority of real issues.
- Real hardware matters for OEM appliance deployment (defense /
  SCADA / instrumentation per `~/fireasmserver_oem_market_notes.md`),
  but only as *engagement qualification*, not as per-release gating.
  Tier 2 (DevNet / vLabs) covers that cadence.

**Convention going forward (feedback-rule level):** when a new
decision supersedes part or all of an earlier one, strike-through
the superseded paragraph in the old entry, add a `**DEPRECATED
<ISO-8601>Z — see DNNN.**` marker with a one-line rationale, and
write the replacement as a new numbered entry that cites the
section being superseded. Bidirectional references beat unidirectional.

### D043: FSA runtime model — statically-allocated per-type pools, cooperative dispatch

**Decision:** fireasmserver's FSA runtime (the concrete realization of
the VMIO automaton engine per D012) uses **per-FSA-type static slot
pools sized at build time**. No heap. Dispatch is cooperative, not
preemptive. Allocator failure is a per-layer-defined backpressure
response, not a panic.

Applies cross-layer: L2 connection state, L3 ARP/reassembly, L4 TCB,
HTTP request state, TLS context, future timers. Each gets its own
pool; each pool's capacity is independent.

### Memory model

- **Per-FSA-type static pools.** Each FSA species has its own
  contiguous array of slots, sized by a build-time `.equ`:
  ```
  # arch/<arch>/config.S (pseudocode)
  .equ TCP_MAX_CONN,         8192
  .equ HTTP_MAX_REQ,         16384   # supports pipelining up to 2x
  .equ TLS_MAX_CTX,          8192
  .equ ARP_MAX_ENTRIES,      2048
  .equ REASSEMBLY_BUFS,      512
  ```
  The full RAM footprint is
  `sum_over_types(slots × slot_size_bytes)` — known exactly at link
  time. Builds fail loudly if the sum exceeds the configured RAM
  budget; no surprise OOMs.
- **No heap.** Zero `malloc`/`free` paths. Consistent with D003 and
  removes an entire attack surface and class of bugs (fragmentation,
  use-after-free, double-free, heap corruption).
- **Slot recycling zeroes.** On release, the slot's state block is
  `memset`-cleared before the allocator returns it. Cheap, predictable,
  closes the info-leak class of bugs that plague reused heap allocators.
- **Sizing relationships documented** alongside the `.equ` block, e.g.
  `HTTP_MAX_REQ >= TCP_MAX_CONN × pipeline_depth`,
  `TLS_MAX_CTX <= TCP_MAX_CONN` (one TLS context attaches to one TCP
  connection), `ARP_MAX_ENTRIES` sized for subnet breadth not
  connection count. OEM deployments can re-tune without inventing
  the relationships from scratch.

### Dispatch model

- **Cooperative.** A single dispatcher loop pulls the next pending
  event from the priority wait queues (per Astier FSA, D012) and
  invokes the matching transition handler. No preemption, no kernel
  scheduler, no context-switch cost.
- **Bounded-work transitions.** A transition has a wall-clock
  budget (nanoseconds, not microseconds) because it directly blocks
  every other FSA in the dispatcher. Anything that might exceed the
  budget must be split across multiple transitions with interleaved
  dispatches. This is the "transactional handler" discipline made
  concrete and enforceable (see Properties to Enforce).
- **Transition atomicity.** A transition either fully completes or
  didn't start. On fault, the slot rolls back to the pre-transition
  state. Basis for durability claims and for formal verification
  later.
- **Priority and QoS** live in the wait queues, not in slot
  allocation. Slots are fungible within a type; priority is per
  pending event (e.g., TLS handshake completion > keepalive refresh).

### Backpressure — allocator failure is per-layer behavior

Each layer defines its response to "pool full" at design time.
Silent drops are the enemy; every layer has a defined answer:

| Allocator | Response on full | Rationale |
|-----------|------------------|-----------|
| TCP (new TCB) | RST or silent drop of SYN (config-pinned per deployment) | TCP retransmit handles temporary unavailability cleanly; RST is more honest to well-behaved clients |
| TCP (established) | still has its slot; no alloc needed | — |
| HTTP request | 503 Service Unavailable + `Retry-After` | Gives the client a defined comeback window |
| TLS context | TCP RST at connection time (don't accept if we can't handshake) | Fail early; don't burn a TCB slot on an unservable connection |
| ARP | drop (ARP is already best-effort) | Next client request re-triggers |
| Reassembly | drop the fragment | Sender will retransmit |

Each per-layer decision recorded in that layer's design doc (the
`docs/<layer>/DESIGN.md` mandated by D038 stage 2 / D039).

### Why this wins over a thread pool

- **Bounded memory by construction.** Thread stacks are typically
  8 KB minimum (often 64 KB+); 10K threads is 80 MB of stack alone
  before any application state. FSA slots carry only the state the
  layer needs (TCB ~200 B, HTTP req state ~1 KB, TLS ctx ~16 KB) —
  a full 10K-connection server fits in ~200 MB.
- **No OS overhead.** No kernel thread table entries, no signal
  masks, no FPU state save areas, no scheduler queues.
- **Deterministic scheduling.** No preemption, no priority-inversion
  pathologies, no scheduler jitter. Latency bounds are a function of
  transition work, not kernel fairness heuristics.
- **Cache-friendly.** Contiguous per-type arrays accessed via base +
  index, bounded TLB pressure, no pointer-chasing through allocator
  metadata.
- **Predictable DoS envelope.** Attacker exhausts *slots*; cannot
  cause OOM kill, heap corruption, or fragmented-allocator stall.
  The security envelope equals the `.equ` sum.

### Properties the FSA runtime MUST enforce (invariants)

Each bullet is a testable invariant, not aspirational:

1. Every transition completes in ≤ `FSA_TRANSITION_BUDGET_NS`
   (initial budget to be set in D040's perf-regression baseline;
   design-doc states the number before implementation).
2. No transition allocates or frees heap memory (static-check
   in CI: grep for malloc/free symbol references in linked output).
3. A freed slot's state block is fully zeroed before the allocator
   returns it to another caller (unit-testable per allocator).
4. Pool capacity per type is a build-time constant visible in a
   single header; rebuild with different values produces a different
   binary, not a different runtime state.
5. Every per-layer "pool full" path is handled — no silent drops
   except where explicitly named above (ARP, reassembly).

### Runtime reconfiguration — deliberately out of scope

Hot-resizing a pool while connections are live is a future
decision, not MVP. Build-time `.equ` tuning per OEM deployment
covers today's needs without adding a config parser, string
handling, and early-boot complexity that 100%-assembly doesn't
want. If we ever need it, that's a deliberate new decision,
not a quiet feature addition.

### Cross-references

- `D003` — 100% assembly, no C stdlib
- `D012` — Astier VMIO automaton engine
- `D034` — per-arch profiles (including cache-line size, which
  informs slot-size alignment)
- `D038` stage 2 / `D039` — each L-layer design doc must state
  its pool-type, its pool size, and its allocator-full behavior
- `D040` — perf regression ratchet includes `FSA_TRANSITION_BUDGET_NS`
  as a measurable metric

### D044: VLAN (802.1Q and successors) — out of scope for fireasmserver L2 MVP

**~~DEPRECATED 2026-04-19T09:30Z — see D045.~~** Ed pushed back on
the deferral on assembly-retrofit-cost grounds: VLAN parsing
reshapes the hot-path EtherType dispatch (offset 12 untagged vs. 16
tagged), and retrofitting that later costs more than designing it in
at MVP. D045 reverses this decision to "designed in, runtime-inert
until configured." The reject-path described below is replaced by
unconditional parse-and-tolerate.

**Decision:** 802.1Q single-tag VLAN, 802.1ad Q-in-Q, 802.1Qau Congestion
Notification, and 802.1Qbb Priority Flow Control are **out of scope**
for the fireasmserver L2 MVP. The L2 layer MUST still gracefully reject
tagged frames (per `VLAN-005` in `docs/l2/REQUIREMENTS.md`): silent
discard with a dedicated counter increment, no attempt to parse.

**Rationale:**
- All current MVP deployment targets (fly.io, AWS EC2 with Firecracker,
  Kata Containers, nested Firecracker on Pi 5) present **untagged
  frames to the guest.** The hypervisor/VPC layer strips VLAN before
  hand-off. Implementing VLAN in the guest doesn't improve any
  current deployment path.
- VLAN handling has real complexity cost: variable-offset EtherType
  dispatch in the RX hot path, extended max-frame length (1522
  instead of 1518), tag-insertion logic on TX, control-queue
  plumbing for VLAN-filter updates. Every one of these adds budget
  against the D039 latency target without earning signal.
- OEM appliance deployments (security, SCADA, defense,
  instrumentation — per `~/fireasmserver_oem_market_notes.md`) may
  require VLAN. When a specific engagement surfaces the need, VLAN
  becomes a scope addition gated on that engagement (same pattern
  D042 uses for enterprise-switch interop).

**Implementation cost of the reject-path:** one comparison of the
EtherType field against `0x8100` (802.1Q) and `0x88A8` (802.1ad) in
the RX dispatch transition. Cheap and explicit.

**Affected requirement rows** (`docs/l2/REQUIREMENTS.md` section 2):
- `VLAN-001`..`VLAN-004`: flipped from `spec` to `deviation`, citing
  this D044.
- `VLAN-005`: stays `spec` — this IS the behavior we implement
  (silent reject with counter).
- `VLAN-006` (802.1ad Q-in-Q): `deviation`, D044.
- `VLAN-007` (802.1Qbb PFC): was already `deviation-candidate`;
  promoted to `deviation` citing D044.

**Revisit trigger (supersedes condition):** first OEM engagement
that requires tagged-frame handling, OR evidence of a production
cloud deployment arriving with tagged frames. Either would result
in a new D-entry marking D044's VLAN-out-of-scope decision
DEPRECATED per the D042 convention.

**Cross-references:**
- `docs/l2/DESIGN.md` §5 — the design-doc statement of this scope.
- `D039` §4 — the "VLAN scope" property the design doc must
  explicitly state; D044 records the resolution.
- `D042` — establishes the "defer vendor-specific scope until
  engagement" pattern this decision follows.

### D045: VLAN + other hot-path-shaping features — designed in, runtime-inert by default (supersedes D044, applies D046)

**Decision:** L2 is architected from MVP to accommodate every feature
whose later addition would reshape the hot path, even when the MVP
runtime disables the feature by default. The deferred-until-customer
model (D044) is reversed for this class. Ships-with-MVP runtime is
simple; architecture is production-capable.

**Applies to:**

| Feature | Hot-path shaping | MVP runtime default | Design obligation |
|---------|------------------|---------------------|-------------------|
| **802.1Q / 802.1ad VLAN RX** | EtherType offset dispatch (12 vs. 16 vs. 20) | Accept tagged frames, strip tag, log VID in per-frame metadata; no filter enforcement | Unconditional tag-peek after SA; tag-strip on RX; VID propagated to L3 handoff metadata |
| **802.1Q VLAN TX** | Optional 4-byte tag insert | `tx_request_t.vid == 0` → no tag inserted | `tx_request_t` carries `vid` field; TX path branches on non-zero |
| **virtio-net multi-queue** | Per-queue interrupt dispatch, pools, steering | `NUM_QUEUES = 1` build-time `.equ` | Dispatcher indexes by queue; RX/TX pools are per-queue arrays (size-1 in MVP) |
| **Checksum offload** | `virtio_net_hdr` flags / `csum_start` / `csum_offset` plumbing to L4 | Feature not negotiated; hdr fields zeroed | L2 always populates hdr; L4 reads + decides |
| **Jumbo frames** | `RX_BUF_SIZE` + every frame-length compare | `L2_MAX_FRAME = 1518` default | Sizes + compares derive from `L2_MAX_FRAME`; OEM overrides via `.equ` |
| **GSO / LRO metadata passthrough** | `virtio_net_hdr.gso_type/hdr_len/gso_size` | Reject non-GSO_NONE at L4 for MVP | L2 passes the metadata struct through to L4; L4 is the gate |

**Not in D045's scope (stay deferred per D044-class reasoning,
because they're additive on an already-designed interface):**
- VLAN filter management (`VIRTIO_NET_CTRL_VLAN_ADD`/`_DEL`) — control-queue feature; additive when wanted.
- Runtime MQ negotiation with the device — MVP is build-time pinned.
- Actual GSO segmentation on TX — L4 decision, not L2 architecture.
- Pause frames (802.3x), PFC (802.1Qbb), LACP (802.1AX) — each is its own module, orthogonal to L2 frame-parse dispatch. Keep deferred but add an `ETH-018` row in `REQUIREMENTS.md` so pause frames get an explicit reject in the RX parser (analogous to the old VLAN-005 pattern).

**Cost analysis that changed the decision:**

- RX hot-path cost when VLAN is runtime-inert: ~3–5 cycles for the
  untagged path (one compare, predict-not-taken branch over the
  skip). At the 10 Gbps / 67 ns target, ~1–2 ns — fits in budget.
- Retrofit cost of VLAN on 100% assembly: every caller of the RX
  parser assumes a fixed EtherType offset. Changing that is not a
  one-file edit; it's a full-pipeline rewrite with rebaseline of
  the D040 perf ratchet. Assembly amplifies.
- Same asymmetry applies to multi-queue, jumbo, checksum-offload,
  GSO metadata. Each would force a reshape-of-callers retrofit.

**Updates cascaded from this decision:**

- `docs/l2/DESIGN.md` §5 rewritten: VLAN parsing described; TX `vid`
  field described; design hot-path costs table added.
- `docs/l2/DESIGN.md` new §11 "Designed-in accommodations":
  describes the multi-queue/checksum/jumbo/GSO architectural shape
  that MVP runtime defaults away from.
- `docs/l2/REQUIREMENTS.md` VLAN-001..VLAN-007 flipped from
  `deviation` back to `spec`. New `ETH-018` row added for pause-
  frame reject.
- D044 gets a DEPRECATED marker citing D045 per the D042 convention.

### D046: Assembly-deferral bar — hot-path-shaping features are designed in at MVP

**Principle (meta-rule for future L-layer scoping decisions):**

> A deferral that would force a later reshape of hot-path data layout,
> dispatch structure, or inter-layer handoff API must be designed in
> at MVP, even when the MVP runtime defaults the feature off. Only
> deferrals that are purely additive on an already-designed interface
> stay deferred.

**Test each deferral proposal against:**

1. Does it change the **frame-parse dispatch** (offsets, EtherType,
   header layout)?
2. Does it change **buffer sizing** (max frame, max MTU, pool
   granularity)?
3. Does it change **ring / queue structure** (single-queue vs.
   per-queue arrays, interrupt dispatch)?
4. Does it change the **L2 ↔ L3 / L3 ↔ L4 handoff API** (metadata
   struct shape, flags passed through)?

A "yes" to any of the four means design in from the start.

**Rationale:**

- 100% assembly per D003 means every hot-path caller hand-rolls
  around the data structure it consumes. Retrofitting a structure
  change touches every caller — no "the compiler will re-emit the
  accesses" fallback.
- The D040 perf ratchet baselines are set on the current data
  layout. Changing layout invalidates the baselines and forces
  re-measurement across all cells.
- Review overhead compounds: each retrofit re-touches code that
  previously passed review. The second review is harder than the
  first because reviewers compare against the baseline they
  approved.
- The MVP runtime stays as simple as the deferred plan (config-
  time defaults turn the feature off). Customers who need the
  feature turn it on via build-time `.equ` change; no code churn.

**Application history:**

- D044 (VLAN out-of-scope) superseded by D045 on these grounds.
- Future L3/L4/HTTP design decisions should invoke D046 when
  evaluating a "let's do this later" proposal.

**Not a license to scope-creep:** the bar is "would retrofit
reshape hot-path or interface structure?" — not "might we want this
eventually?" Features that don't meet the bar (LACP, PFC,
background-control-queue operations) stay deferred.

### D047: GAS intel-syntax `OFFSET` convention for MOV sources (x86_64)

**~~DEPRECATED 2026-04-19T14:00Z — see D048.~~** The convention held
for a few hours and one commit (`f44e294`) before Ed and I concluded
that living with GAS's intel-syntax ambiguities is worse than a
one-time toolchain switch. D048 moves x86_64 to NASM, which has clean
intel-syntax semantics and needs no OFFSET convention. The asm-syntax
lint from this decision is removed under D048.

**Rule:**

> In `arch/x86_64/**/*.S`, any `mov <reg>, <bare-identifier>` source
> must be written as `mov <reg>, OFFSET <identifier>` for immediate
> loads or `mov <reg>, [<identifier>]` for explicit memory loads.
> Bare-symbol MOV sources are rejected at commit time.

**Why:** GAS under `.intel_syntax noprefix` is a partial MASM emulator,
not a spec-compliant one. A bare symbol as a MOV source defaults to a
memory reference, so `mov ecx, ready_len` — where `ready_len` is a
`.equ`-defined constant — silently assembles to `mov ecx, [<value>]`,
a load from the value-treated-as-address. The behavior also varies by
the symbol's defining expression: pure literal `.equ` symbols
sometimes pick the immediate encoding, `. - label` expression symbols
pick memory. That fragility is the problem — our rule makes the
intent explicit so GAS cannot guess.

**Discovery:** first-byte emission of the x86_64 virtio-MMIO probe
failed because `mov ecx, ready_len` assembled as `mov ecx, [6]` (load
from physical address 6). The emit loop ran with a garbage `ecx`,
`READY\n` never reached COM1, and control later fell through to
invalid instructions. Separately, an uninitialized ESP caused the
first `CALL` to push the return address into MMIO space at
`0xfffffffc`, so even the corrected MOV could not have produced a
stable probe — both issues fixed together in the D047-guarded commit.

**Scope — x86_64 only:**

- AArch64 uses a different mnemonic grammar (`mov` takes an
  immediate-encoded `#imm` or a register; no bracketed memory form),
  so the ambiguity does not arise. The lint filters to `arch/x86_64/`.
- Other x86_64 mnemonics (`cmp`, `add`, `sub`, `test`, ...) were not
  observed to hit this ambiguity for bare .equ sources — they pick the
  immediate encoding in every case we have disassembled. The rule
  stays narrowly focused on MOV. Expand only if a future disassembly
  shows the same symptom on another mnemonic.

**Alternatives considered:**

- **Switch x86_64 to NASM.** Clean intel-syntax semantics, no MOV
  ambiguity. Rejected because D006 (GNU as for both arches) is
  load-bearing for toolchain unity: one assembler, one directive set,
  one set of linker scripts, one apt package in CI. The MOV-source
  issue is a one-time tax payable with a four-line regex; a second
  assembler is an ongoing one.
- **Switch x86_64 to GAS AT&T syntax.** Native GAS, no ambiguity
  (immediates marked `$`). Rejected on readability — x86_64 assembly
  in fireasmserver is written by humans who reach for Intel's
  manuals as primary reference; AT&T's operand order and sigils add
  cognitive load without correctness benefit beyond what this rule
  already provides.

**Enforcement:** `tooling/hooks/asm_syntax_lint.sh` runs under
`.git/hooks/pre-commit` (via `tooling/hooks/pre_commit.sh`) and
matches the offending pattern on staged `arch/x86_64/**/*.S`. Failing
the lint blocks the commit with a remediation message pointing at
this decision. The check is also runnable ad-hoc on any file set for
CI or interactive use.

**Cross-refs:**

- `D006` — GAS chosen as the assembler for both arches; this rule
  pins the convention needed to use GAS's intel-syntax safely.
- `D003` — 100% assembly; no C compiler is available to paper over
  GAS's operand-interpretation quirks.

### D048: Switch x86_64 to NASM (supersedes D006 for x86_64; AArch64 stays on GNU as)

**Decision (2026-04-19):** x86_64 assembly sources move from
`x86_64-linux-gnu-as` (GAS `.intel_syntax noprefix`) to NASM. AArch64
continues to use `aarch64-linux-gnu-as` — no change there.

**Supersedes D006 for x86_64 only.** D006 ("GNU as, both arches") was
load-bearing for toolchain unity; we keep the principle on AArch64
(where the `mov` grammar is unambiguous and GAS is idiomatic) but
sacrifice unity on x86_64 in exchange for assembler semantics that
match the Intel SDM the code is written against.

**What broke the camel's back:**

- D047's OFFSET convention arose after a multi-hour debug session in
  which `mov ecx, ready_len` silently assembled to `mov ecx, [6]`
  (load from physical address 6). We wrote a lint to catch future
  recurrences of that specific shape.
- Gemini's independent review of the follow-up commit (`f44e294`)
  then flagged `cmp eax, VIRTIO_MAGIC` with the same rationale. The
  flag was empirically wrong — the CMP encoding picked the immediate
  form — but "empirically wrong in this codebase, right in principle"
  is not the kind of rule that scales. Every future x86_64 contributor
  (human or AI) would have to re-derive the MOV-vs-CMP asymmetry from
  disassembly, and the lint would have to grow in step.
- The cleaner invariant is: **don't use an assembler whose operand
  interpretation is ambiguous.** NASM is that assembler.

**What NASM gives us:**

- **Unambiguous bare-symbol semantics.** `mov ecx, ready_len` is
  always immediate; `mov ecx, [ready_len]` is always memory; the
  assembler never guesses.
- **Native intel syntax.** No `.intel_syntax noprefix` directive, no
  partial-MASM emulation layer. What the Intel SDM reads, NASM
  assembles.
- **ELF section types explicit.** `section .note.Xen note alloc
  align=4` produces SHT_NOTE directly, matching the PVH requirement
  without relying on name-based heuristics.
- **Documented macros.** `%define`, `%macro` are first-class; GAS's
  `.macro` is fine but NASM's is more commonly cited in x86 reference
  material.

**What NASM costs:**

- **Second assembler in CI.** Install `nasm` for x86_64 cells;
  `binutils-*-linux-gnu` still needed for `ld` and the AArch64
  assembler. One extra apt package per cell.
- **Syntax deltas to absorb.** `.equ X, v` → `X equ v`, `.section`
  → `section`, `.code32` → `[bits 32]`, `.ascii` / `.asciz` → `db`
  forms, `.byte`/`.word`/`.long`/`.quad` → `db`/`dw`/`dd`/`dq`,
  `.globl` → `global`, and — the main thing — no `OFFSET` keyword
  (bare symbol = immediate always; brackets = memory always).
- **Re-learning for anyone reaching for GAS reflexively.** Tolerable
  because the reason (semantic unambiguity) stays front-of-mind.

**Files converted in this switch** (see commits C1 and C2 in this
series):

- `arch/x86_64/platform/firecracker/boot.S`
- `arch/x86_64/platform/qemu/boot.S`
- `arch/x86_64/crypto/crc32_ieee.S`

**Hook/lint cleanup under this decision:**

- `tooling/hooks/asm_syntax_lint.sh` — removed; not applicable to NASM.
- `tooling/hooks/pre_commit.sh` — simplified to just exec the shared
  cross-project hook (Python gates + Gemini review). Kept as a wrapper
  in case a future project-local pre-commit check lands.
- `tooling/hooks/install.sh` — still installs both `pre-commit` and
  `pre-push` symlinks; target-existence check retained.

**Cross-refs:**

- `D006` — load-bearing for AArch64; superseded for x86_64 only.
- `D047` — deprecated by this decision; the OFFSET convention and the
  lint that enforced it are obsolete under NASM.
- `D003` (100% assembly) — unaffected; we're changing assemblers, not
  languages.

### D049: Ontology as formal verifiable requirements — SysE-grade schema with preserved DAG history, git-cross-referenced

**Decision (2026-04-19):** the ontology at `tooling/qemu-harness.json`
(produced by `tooling/build_qemu_harness_ontology.py`) is the
project's formal verifiable-requirements artifact, not a planning
sketch. The **primary abstraction** is a **DAG of ontologies**:
every DAG node is a complete ontology snapshot, every DAG edge
carries a `Decision` record, and parallel designs coexist as
first-class sibling branches rather than being tracked in separate
documents.

**Prior-art scope (checked 2026-04-19):** the base shape —
node-is-ontology, edge-carries-decision, parallel-branches,
navigation API — is already Ed's own public prior art via
`github.com/edhodapp/python-agent` (BSD-3-Clause, first commit
2026-03-31). This decision carries forward that shape into
fireasmserver rather than claiming novelty over it. The
fireasmserver-specific additions — (a) embedding the git HEAD
SHA into snapshot labels, (b) idempotent content-hash-gated
snapshot append so no-op re-runs don't pollute the DAG, (c)
SysE traceability fields on `DomainConstraint` (rationale,
implementation_refs, verification_refs, status), (d)
`PerformanceConstraint` type with first-class numeric budget +
direction + measurement method, (e) the specific application
framing as formal verifiable requirements for a real
systems-engineering artifact — are the parts not in the
upstream. Whether that combination is meaningfully novel
against the broader literature we have not exhaustively
searched; treat the framing as "a useful composition" rather
than "a breakthrough" when discussing externally.

Standard SysE requirements-management tooling (DOORS, Rational,
Polarion, linear baseline docs) doesn't natively offer the DAG
branching + Decision-annotated forks combination either, so the
shape remains a differentiator against that class of tools even
after acknowledging the python_agent prior art — the
application-to-SysE is what's being done newly here, not the
DAG primitive itself.

Two related sub-decisions:

1. **SysE-grade schema.** `DomainConstraint` grows first-class
   traceability fields — `rationale` (decision pointer or
   requirement-row ID), `implementation_refs` (list of
   `file:symbol` strings), `verification_refs` (list of
   test/measurement/gate pointers), `status` (one of `spec`,
   `tested`, `implemented`, `deviation`, `n_a`). A new
   `PerformanceConstraint` type carries quantitative budgets as
   first-class numeric data (`metric`, `budget`, `unit`,
   `direction`, `measured_via`) rather than string-encoding them
   in description text.

2. **Preserved DAG history cross-referenced to git, with
   first-class support for parallel designs.** The builder loads
   the existing DAG on each run and appends a snapshot **only
   when the ontology content actually changed** (content hash
   compared against the selected parent node's ontology hash).
   Each new snapshot label embeds the git HEAD SHA plus a
   `dirty` marker if the working tree is dirty, so an auditor
   can locate the source context for any DAG generation with a
   single `git show` and can locate the DAG generation for any
   commit by walking the snapshot labels.

   **The DAG is not just a linear history.** Its branching
   structure is the point: multiple design explorations can
   coexist as sibling branches off the same parent. Each `DAGEdge`
   carries a `Decision` record that captures the question / options
   / choice / rationale for that fork, so an auditor reading a
   branched DAG sees not just "what did the ontology look like at
   node X" but "why did we fork here, what alternatives were we
   weighing." That lets us keep multiple designs in play at once —
   a baseline and an experimental refactor, two perf-budget
   scenarios, a conservative and an aggressive variant — and
   always have every variant at our fingertips for comparison.
   The branching API itself is inherited from python_agent per
   the prior-art scope above; what's done newly here is applying
   it to formal SysE requirements rather than agent planning.

   **Git preserves source-level history** with diff granularity;
   the **pydantic DAG preserves graph-of-constraints evolution**
   with structural granularity *and* parallel-design multiplicity.
   Complementary coverage of three dimensions: source changes
   (git), graph-shape changes (DAG), and alternative-design
   multiplicity (DAG branching).

   **Concurrent-process safety.** Parallel work across Claude
   sessions is already part of the workflow (a main session plus
   side sessions taking briefed modules — see
   `project_parallelization_strategy.md`), and any of them may
   trigger a builder run. The DAG file is a shared artifact; a
   naive load-modify-save across two concurrent builders would
   race and lose updates. O2b addresses this with a
   `dag_transaction` context manager in
   `tooling/src/ontology/dag.py` that acquires an exclusive
   `fcntl.flock` on a sidecar lock file (`<dag_path>.lock`)
   before loading, holds the lock across the modification, and
   releases it on exit after saving. Contenders serialize
   automatically — the second process's `flock` call blocks
   until the first's with-block exits, so loads always see the
   fully-saved state of the prior writer. No lost updates, no
   torn reads. On an exception inside the yielded block the DAG
   is NOT saved but the lock IS released; callers wanting to
   persist partial state must save explicitly before the
   exception propagates. The concurrent-safety invariants are
   tested under real OS processes in
   `tooling/tests/test_ontology_dag_concurrent.py` (two
   workers + three workers + five workers in parallel, all must
   land; exception path must roll back; crashed-worker lock
   release must not wedge the next worker).

**Why:**

- **External SysE expert review** is planned post-first-release
  (`project_sysengineering_expert_review.md`). Ad-hoc description
  text is not defensible as formal requirements; first-class
  traceability is.
- **D046 (assembly-deferral bar)** — SysE formalism reshapes how
  every new constraint is authored. Retrofitting across hundreds
  of future rows is not viable; the schema must be right at first
  iteration.
- **Can't measure what you can't see, can't fix what you can't
  measure** (Ed, 2026-04-19). Perf constraints pinned as first-class
  numeric data make the measurement-vs-budget gap surfaceable by a
  single audit tool; description-text budgets do not.
- **History preservation** — a single snapshot destroys the
  evolution record. For external review, "how did we arrive at
  this constraint set" is as material as "what is the current
  constraint set." Git alone can't show the graph-structural
  delta; the pydantic DAG gives us that dimension.

**Not in this decision's scope** (separate D-entries as they land):

- The **audit tool** that reads the ontology + the repo + the
  perf-ratchet artifacts and emits a requirement → impl →
  verification matrix with gap flags. Drafted as `O5` in the
  commit series; will get its own D-entry once it stabilizes.
- **Schema extensions beyond the ones above** (e.g., attaching
  `status` and verification_refs to `Property` or `ModuleSpec`).
  Add as needed under this same decision's umbrella — not each a
  new D-entry.
- **Ontology forking from `python_agent`** — captured
  in the O1 commit message and the fork docstrings; not a
  decision-log-worthy choice on its own.

**Cross-refs:**

- `D040` — perf-regression ratchet. `PerformanceConstraint.budget`
  values become the baselines the ratchet enforces once both are
  wired together.
- `D046` — assembly-deferral bar. Formal-requirements formalism is
  the thing being "designed in first iteration" here.
- `D043` — FSA runtime model. The `fsa_transition_ns` budget
  (≤ 100 ns) is one of the first `PerformanceConstraint` entries
  under this schema.
- `docs/observability.md` — that proposal's status renumbers from
  "pre-D049" to "pre-D050" (or later) since D049 is now this
  ontology decision.
- `project_sysengineering_expert_review.md` (memory) — the
  downstream audit reader.

### D050: Fold-by-N with `pslldq 4` reuses fold-by-1 reduction constants

**Decision (2026-04-19):** the x86_64 PCLMULQDQ fold-by-N CRC-32
path in `arch/x86_64/crypto/crc32_ieee.S` uses the 33-bit constant
form with `pslldq xmm, 4` as its alignment step, and under that
form the **reduction chain after the main loop is fold-by-1, not
fold-by-N**. Consequence: the on-chip constant table has four
exponents — `x^576 mod P`, `x^512 mod P`, `x^192 mod P`, and
`x^128 mod P` — **not** the six exponents the fold-by-4 briefing
initially hinted at (`{512, 448, 384, 320, 192, 128}`).

**Math (abbreviated — full derivation in
`tooling/crypto_tests/derive_fold_constants.py` with 312 test
cases per fold factor verified against `zlib.crc32`):**

In the `pclmulqdq + pslldq 4` form used here, each multiply-and-
shift step advances one accumulator's "running polynomial" by
exactly 128 bits. When N parallel accumulators are initialized
from consecutive 16-byte chunks of input, they start staggered:
accumulator `i ∈ {0..N-1}` holds the contribution of a running
polynomial at position `128·i` bits ahead of accumulator 0. Each
main-loop iteration advances **all N accumulators simultaneously**
by 128 bits — so after any number of iterations, the N
accumulators remain staggered at the same 128-bit offsets from
each other. The stagger is preserved end-to-end by the loop.

The post-loop reduction collapses the N accumulators into one.
Because the stagger between any two adjacent accumulators is
exactly 128 bits, every step of the reduction chain is a
fold-by-1 — multiply one accumulator by `x^128 mod P`, XOR into
the next, repeat. The reduction never needs a fold-by-2 or
fold-by-4 constant.

The constants actually used:

- **Main loop (fold-by-4 step):** `x^576 mod P` and `x^512 mod P`.
  Advances each accumulator by 4 × 128 = 512 bits per iteration;
  the 576 = 512+64 exponent handles the half of the accumulator's
  internal 128-bit state that's shifted by the pclmul's own
  alignment.
- **Reduction chain (fold-by-1):** `x^192 mod P` and `x^128 mod P`.
  Same two constants the existing fold-by-1 path uses; the
  reduction reuses the fold-by-1 step three times to collapse
  four accumulators to one.

**Why this matters:**

- **Smaller constants table.** 4 × 16 B = 64 B of constants, fits
  in a single cache line. 6 exponents would need 96 B — still one
  line, but less headroom for future additions (fold-by-8 constants
  if we ever go there) without straddling a line boundary.
- **Reduction is branch-free and compact.** Three inline
  fold-by-1 steps (12 instructions total: 4 × pclmul + 4 × pslldq
  + 4 × pxor) vs. a mixed fold-by-2-then-fold-by-1 chain that
  would need different constants per step.
- **Parallel accumulators** for future fold-by-8 reuse the same
  reduction path. Adding N=8 would introduce new main-loop
  constants (`x^1152 mod P`, `x^1088 mod P`) but **not** new
  reduction constants — the reduction stays fold-by-1 regardless
  of N, since the post-loop stagger is always 128 bits per
  accumulator regardless of how many there are.

**Non-obvious corollary (briefing erratum):** the briefing
suggested "{512, 448, 384, 320, 192, 128}" as the fold-by-4
exponent set. That enumeration is what you'd need under a
*different* pclmul form — one that uses `psrldq` or a 64-bit-
aligned constant — where the N accumulators converge during
reduction rather than staying staggered. Under our form, that
enumeration is wrong; the derivation script caught the mismatch
empirically (zero-mismatch against `zlib.crc32` only for the
4-exponent set).

**Implementation:**

- `arch/x86_64/crypto/crc32_ieee.S` — fold-by-4 main loop and
  reduction, constants laid out in `.rodata.crc32_pclmul_consts`.
- `tooling/crypto_tests/derive_fold_constants.py` — derives the
  constants from the polynomial via GF(2) modular math, verifies
  against `zlib.crc32` across 312 length points per fold factor,
  emits NASM-ready `dq` literals.
- `tooling/tests/test_derive_fold_constants.py` — 361 pytest
  tests locking down the derivation.

**Verification:**

- The CRC-32 host-side driver runs 264 lengths × 3 code paths
  (slice8, pclmulqdq, dispatcher) per arch under the pre-push
  integration gate, plus the cross-path equivalence check. All
  green since `b655543`.
- The 312-length sweep inside the derivation script is
  redundant with the driver but exercises boundary points the
  driver doesn't (fold-chunk-stride aligned lengths 64, 128,
  256, 512, 1024, 4096, 8192).

**Cross-refs:**

- `ETH-005` (CRC-32 IEEE 802.3 — now `implemented`).
- `D034` — `HAS_PCLMULQDQ` profile flag; this decision is the
  math specifically for the fold-by-4 path taken when the flag
  is on.
- `D040` — perf ratchet; the fold-by-4 throughput baseline lives
  here once measured.
- `D048` — NASM-on-x86_64 — the constants are declared via
  `dq` literals, NASM-idiomatic.

**Attribution:** math derivation + implementation by the
2026-04-19 fold-by-N side session (briefing at
`docs/side_sessions/2026-04-19_crc32_pclmul_foldbyn.md`). Content
shipped on `origin/main` in commits `4fcfc3e` (derivation
script), `da98b0d` (rename cleanup), and asm content absorbed
into `b655543` (main-session wide-staging incident per
`feedback_explicit_git_add_during_parallel_sessions.md`; leave-
as-is per Ed's disposition).

### D051: Ontology audit as closing pre-push gate

**Decision (2026-04-19):** every push to `origin/main` must pass
`audit-ontology --exit-nonzero-on-gap`, which verifies that
every `implementation_refs` / `verification_refs` entry in the
committed ontology resolves against the working tree and that
status ↔ refs fields are internally consistent. Enforcement
lives in `tooling/hooks/pre_push.sh` alongside the existing
tracer bullets, CRC vectors, and full pytest suite. Bypassing
with `--no-verify` remains Ed's prerogative but carries the same
"you own the broken CI" penalty the other pre-push gates do.

**Why:** the ontology's value as formal requirements depends on
its refs pointing at code that actually exists. The O4 back-fill
(`6fe19c7`) technically shipped two latent symbol-resolution
bugs — `vm_launcher.py:_proc_registry` and
`vm_launcher.py:_proc_lock` — where refs pointed at module-level
variables that the initial resolver matched only via substring
coincidence. They surfaced only when the audit tool's resolver
was expanded to match module-level assigns properly. Without an
enforced gate, ontology entries can claim traceability they
don't have, and the claim rots silently between commits. The
gate closes that loop.

**Why pre-push not pre-commit:** the audit re-reads the whole
ontology and resolves all refs — cheap (milliseconds) but not
free, and the class of drift it catches is cross-commit
coherence, not per-file correctness. Consistent with the
existing split: quality gates pre-commit; integration and
coherence pre-push.

**Why `--exit-nonzero-on-gap` is opt-in rather than the
default:** the human-readable invocation `audit-ontology` (no
flag) stays exit-0 so manual inspection of the matrix is
friction-free. Scripts and hooks opt in via the explicit flag.

**Implementation:**
- `run_ontology_audit` function added to
  `tooling/hooks/pre_push.sh` immediately after
  `run_pytest_suite`.
- Console-script entry `audit-ontology = "audit_ontology.cli:main"`
  added to `pyproject.toml` `[project.scripts]`.
- Running `pip install -e .[dev]` once installs the script into
  the venv alongside the existing `qemu-harness` and `branch-cov`
  entries.

**Follow-up:** `.github/workflows/cd-matrix.yml` should grow an
equivalent audit step so the gate fires on every PR, not just
every local push. One-line addition after the existing
`pip install -e .[dev]` setup step. Left for the next CI touch.

**Cross-refs:**
- `D049` — the ontology schema this gate enforces.
- `D017` — two-tier quality gates; this adds a coherence gate
  at the pre-push tier.
- `D040` — perf ratchet; sibling pre-push gate with the same
  "fail on drift" posture.

**Attribution:** audit tool built by the 2026-04-19
`audit_ontology` side session (briefing at
`docs/side_sessions/2026-04-19_audit_ontology.md`); policy +
wire-up landed by the main session in the same window.

## Future decisions (not yet made)
- virtio-net driver design
- TCP state machine implementation
- HTTP parser design
- ~~Assembly branch coverage tooling~~ **DEPRECATED 2026-04-19T07:15Z** — implemented as `tooling/src/branch_cov/` (MVP in commit `e3aa166`, 2026-04-18) with capstone + pyelftools disassembly, QEMU-trace ingestion, per-cell baselines, and a ratchet-mode `--baseline` flag wired into `cd-matrix.yml` via `run_local.sh`.
- PICT combinatorial testing integration
- OSACA CI integration
- ~~Pi 5 self-hosted runner setup~~ **DEPRECATED 2026-04-17T00:00Z** — superseded by D022 (Pi 5 is a local-only AArch64 test host; CI lives in GitHub Actions, not on the Pi).
