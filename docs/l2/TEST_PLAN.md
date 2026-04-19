# Layer-2 Test Plan

**Purpose:** describe the tests that verify fireasmserver's L2
implementation against the requirements in
[`REQUIREMENTS.md`](REQUIREMENTS.md) and the design in
[`DESIGN.md`](DESIGN.md). Corresponds to D038 stage 3; functional
passes before adversarial, adversarial before interop, interop
before performance.

This document is living. Tests are added here before they are
written; as a test transitions from "planned" to "passing," its
status column in section 9 flips. The plan is the single source of
truth for "what does it mean for L2 to be done?"

**Cross-references:**
- Requirement IDs (e.g., `ETH-003`) — from `REQUIREMENTS.md`.
- Design-doc sections (e.g., `DESIGN.md §5`) — for behaviors specified
  there rather than in an external standard.
- Decision log: D005 (functional gates block, perf advises), D012
  (VMIO), D038 (methodology), D039 (design-doc properties), D040
  (perf ratchet), D041 (deployment reqs), D042 (interop matrix),
  D043 (FSA runtime), D045+D046 (designed-in feature architecture).

---

## 0. Test harness architecture

Four tiers. Every test belongs to exactly one. The tier determines
when in the CD flow the test runs.

### Tier A — Host-side unit tests

Individual `.S` modules linked with a small C driver on the host
(not inside a VM). Fast (milliseconds), no VMM, no DMA, no interrupts.
Exercised via `pytest` wrapping the C driver's exit code.

**When to use:** primitives (CRC, checksum), parser state machines
given a fixed-size in-memory buffer, arithmetic hot paths, hash
functions.

**What it cannot test:** DMA coherence, interrupt timing, virtio
ring semantics, end-to-end RX/TX integration.

### Tier B — QEMU integration tests

Boot fireasmserver as a Firecracker guest on QEMU (for the
KVM-viable cells), inject frames via a tap device configured as
the virtio-net backend, observe the guest's serial output + any
host-side tap capture (`tcpdump` on the tap).

**When to use:** virtio-net device init, ring-level semantics,
RX/TX paths end-to-end, ARP request/reply, per-frame observability
counters, graceful handling of malformed frames.

**When it cannot run:** the aarch64/firecracker cell in hosted
CI (no `/dev/kvm`); that cell relies on the Pi-local tracer bullet
for the same coverage.

### Tier C — Adversarial / fuzz tests

`libFuzzer`- or `AFL++`-harnessed parser runs with synthetic random
byte streams shaped by a grammar definition (scapy-generated frame
corpus as seeds). Run in CI on a time budget; run longer pre-release.

**When to use:** the RX parser boundary — every frame shape the
wire can deliver. Also virtio descriptor handling (rings with
out-of-range indices, loops, oversized lengths).

### Tier D — Interop tests (per D042 Tier 1)

Containerlab topologies with fireasmserver attached to one of the
free-tier vendor images (Linux netdev, OVS, Arista cEOS-lab, Nokia
SR Linux). Exercises full-stack interop from the other side of the
wire.

**When to use:** anything that verifies "a real implementation of
the other side accepts / produces what we do." VLAN tag handling,
jumbo frames, ARP compatibility, common corner cases.

**Runtime:** runs as a cd-matrix job separate from the build+boot
matrix (doesn't block a push; nightly cadence is sufficient).

---

## 1. Functional tests — Ethernet II framing

All against `REQUIREMENTS.md` section 1 (`ETH-001` … `ETH-018`).

### 1.1 Frame layout parser — `ETH-001`, `ETH-002`, `ETH-015`, `ETH-017`

**Tier:** A (host-side unit). Feed well-formed frames into the
parser; verify DA/SA/EtherType extraction into the per-frame
metadata struct.

**Cases:**
1. Minimum-size Ethernet II frame (60-byte payload, no VLAN): parser
   yields expected DA, SA, EtherType = `0x0800`.
2. EtherType < `0x0600` (legacy 802.3 length): `ETH-002` says we
   reject; parser returns "unsupported framing" error.
3. Unicast SA (LSb=0): accepted.
4. Multicast SA (LSb=1): `ETH-015` reject, error counter increments.
5. Locally-administered SA (2nd LSb=1 but unicast): accepted per
   `ETH-017`.

### 1.2 Size bounds — `ETH-003`, `ETH-004`, `ETH-010`, `ETH-011`, `ETH-012`, `ETH-013`

**Tier:** A + B. Host-side exercises the size-compare path; QEMU
integration verifies the runt/oversized paths actually drop at the
device boundary.

**Cases:**
1. Exactly 64 bytes (including FCS): accepted.
2. 63 bytes (runt): `ETH-010` reject + counter.
3. Exactly 1518 bytes untagged: accepted.
4. 1519 bytes untagged: `ETH-011` reject + counter.
5. 1522 bytes with one VLAN tag (per D045 VLAN design): accepted.
6. `ETH_MAX_FRAME = 9018` build variant (jumbo override): frames up
   to 9018 bytes accepted; 9019+ rejected.
7. Short outgoing frame (<64 bytes payload + headers): `ETH-012`
   pad-to-64; `ETH-013` pad bytes are zero. Assertable from the
   tap-side capture in Tier B.

### 1.3 FCS handling — `ETH-005`, `ETH-009`

**Tier:** A (CRC primitive) + B (virtio FCS flag).

**Cases:**
1. Primitive test for `ETH-005`: the seven CRC-32 test vectors from
   the side-session briefing (`docs/side_sessions/2026-04-19_crc32_ieee.md`).
   Host-side unit tests, linked with the side-session's crc32_ieee.S.
2. `ETH-009`: inject a frame with the virtio RX descriptor's
   `VIRTIO_NET_HDR_F_DATA_VALID` bit clear; verify the guest drops
   and counts it.

### 1.4 MAC filter — `ETH-006`, `ETH-007`, `ETH-008`, `ETH-016`, `MAC-001`..`MAC-005`

**Tier:** B (QEMU). Configure the guest's MAC, send frames with
various DAs, verify accept/drop.

**Cases:**
1. Unicast DA == guest MAC → accept (`ETH-008`).
2. Unicast DA != guest MAC → drop.
3. Broadcast DA (`FF:FF:FF:FF:FF:FF`) → accept (`ETH-006`).
4. IPv4 multicast DA (`01:00:5E:...` per `MAC-004`) → accept if in
   filter list, else drop (`ETH-007`).
5. IPv6 multicast DA (`33:33:...` per `MAC-005`) → same.
6. Outgoing frame with broadcast-MAC SA → reject at TX
   (`ETH-016`). Host-side assertion in the L2 TX submit path.

### 1.5 Pause-frame reject — `ETH-018`

**Tier:** B. Inject an Ethernet PAUSE frame (EtherType `0x8808`,
opcode `0x0001`). Verify silent drop + `rx_pause_dropped` counter
increment.

---

## 2. Functional tests — VLAN (`VLAN-001` … `VLAN-008`)

Per D045, VLAN is designed in at MVP. All tests target the
runtime-inert-by-default implementation.

### 2.1 VLAN RX parsing — `VLAN-001`..`VLAN-004`, `VLAN-006`

**Tier:** A (parser offset math) + B (end-to-end).

**Cases:**
1. Untagged frame → no tag detected, EtherType at offset 12, VID in
   metadata = 0.
2. 802.1Q single-tag frame (TPID `0x8100`, VID=100) → tag stripped,
   EtherType re-read from offset 16, metadata VID = 100.
3. 802.1ad Q-in-Q (outer `0x88A8` VID=200, inner `0x8100` VID=300)
   → both VIDs in metadata, EtherType at offset 20.
4. PCP and DEI bits correctly extracted into metadata
   (`VLAN-003`, `VLAN-004`).
5. VID `0x000` priority-tagged frame → metadata VID = 0 with
   "priority-only" flag set.
6. VID `0xFFF` reserved → reject with `rx_vlan_reserved_vid` counter.

### 2.2 VLAN TX tagging

**Tier:** B. Submit a TX request with `vid != 0`; verify the
emitted frame (captured on tap) has the tag inserted with correct
TPID + VID.

**Cases:**
1. `tx_request.vid = 100` → 802.1Q tag inserted, frame 4 bytes longer.
2. `tx_request.vid = 0` → no tag; frame untagged and identical to
   pre-D045 behavior.

### 2.3 VLAN filter management — `VLAN-008`

**Tier:** N/A — status is `deviation` per D045. Omit tests; add
when the control-queue filter landing happens (new D-entry).

### 2.4 VLAN-005 historical behavior

`VLAN-005` is now `deviation` (D045 reversed it). Remove any
prior test that asserts the old "discard tagged frames" behavior;
new test 2.1.2 supersedes it.

---

## 3. Functional tests — ARP (`ARP-001` … `ARP-011`)

**Tier:** B primarily. Inject ARP requests/replies from the host
tap; observe guest responses on the tap.

### 3.1 Basic request/reply

**Cases:**
1. `ARP-001`, `ARP-002`, `ARP-003`: well-formed ARP REQUEST targeting
   the guest's IP → observe REPLY with correct HTYPE/PTYPE/HLEN/PLEN,
   OP=2.
2. `ARP-004`: REQUEST with target != guest IP → no REPLY.
3. `ARP-011`: REQUEST with target IP not local → no REPLY.
4. `ARP-005`: send REQUEST from guest, inject matching REPLY → verify
   cache update (observable via observability counter for cache hits).

### 3.2 Cache behavior

**Cases:**
1. `ARP-006`: inject an unsolicited REPLY whose Sender HW/Protocol
   are already in the cache → cache entry updates.
2. `ARP-007`: observe non-ARP traffic from an unknown IP → no cache
   entry added.
3. `ARP-008`: set short TTL in test build; verify entries age out
   after TTL elapses (observable via cache-miss counter for the aged
   IP).

### 3.3 Address conflict detection — `ARP-009`, `ARP-010`

**Cases:**
1. Guest emits gratuitous ARP on IP-assignment (`ARP-009`) — capture
   on tap.
2. Inject a conflicting ARP announcement for the guest's IP → verify
   guest emits a defense (`ARP-010`) per RFC 5227 §2.4 timing rules.

---

## 4. Functional tests — virtio-net device (`VIO-*`)

Device initialization, feature negotiation, and ring semantics.
Most tests here are Tier B; a handful are Tier A (data structure
validation on mock memory).

### 4.1 Device init sequence — `VIO-001`..`VIO-009`

**Tier:** B. Observe the guest's writes to the virtio Common
Configuration MMIO region during startup.

**Case:** full-sequence correctness: reset → ACKNOWLEDGE → DRIVER →
feature select → FEATURES_OK → re-read check → virtqueue setup →
DRIVER_OK. Each status bit transition is a checkpoint the test
asserts on. If the host side signals FEATURES_OK rejection
(`VIO-006`), the test verifies the guest aborts cleanly.

### 4.2 Feature negotiation — `VIO-F-001`..`VIO-F-007`

**Tier:** B. Host advertises various feature bit combinations; guest
negotiates expected subset.

**Cases:**
1. Host offers only `VERSION_1`: guest accepts.
2. Host offers `VERSION_1` + `MAC`: guest accepts; MAC read from
   config space.
3. Host does NOT offer `VERSION_1`: guest MUST refuse (we don't
   support legacy).
4. Host offers `CSUM`: guest does not select it (MVP default).
5. `VIO-F-004` MQ: not negotiated when `NUM_QUEUES=1`.
6. `VIO-F-007` EVENT_IDX: not negotiated in MVP.

### 4.3 Virtqueue layout — `VIO-Q-001`..`VIO-Q-008`

**Tier:** A for data structures; B for barrier correctness on real
hardware.

**Cases:**
1. Descriptor flags (`NEXT`, `WRITE`, `INDIRECT`) read/written correctly.
2. Power-of-2 queue size enforced at init (`VIO-Q-006`).
3. Alignment of descriptor table / avail / used (`VIO-Q-007`):
   static assertion at build time.
4. `VIO-Q-008` memory barriers: code review + targeted race test in
   Tier B that tries to trip barrier ordering on AArch64 under load.

### 4.4 Receive path — `VIO-R-001`..`VIO-R-007`

**Tier:** B. Inject frames, observe the avail-ring pre-population
and used-ring consumption.

**Cases:**
1. Pre-populated avail ring at init (`VIO-R-002`).
2. `virtio_net_hdr` correctly parsed from each received frame
   (`VIO-R-003`, `VIO-R-004`).
3. Multi-descriptor RX (`VIO-R-005`, `VIRTIO_NET_F_MRG_RXBUF`):
   currently `spec` and MVP may or may not negotiate it. If not
   negotiated, inject a single-descriptor frame and verify; if
   negotiated, inject `num_buffers > 1` and verify chain walk.
4. Descriptor returned to avail ring (`VIO-R-006`).
5. Queue notify register correctly written (`VIO-R-007`).

### 4.5 Transmit path — `VIO-T-001`..`VIO-T-006`

**Tier:** B. Submit TX requests; observe the tap-side frame stream.

**Cases:**
1. `virtio_net_hdr` prefix correctly populated (`VIO-T-002`).
2. CSUM/GSO fields zero (`VIO-T-003`, `VIO-T-004`).
3. Descriptor chain construction with multiple payload fragments
   (`VIO-T-005`): submit a multi-descriptor TX via L3 and verify
   the on-wire frame reconstructs correctly.
4. Used-ring reclaim (`VIO-T-006`): inject a used-ring advance,
   verify the buffer returns to the TX pool.

### 4.6 Control path — `VIO-C-001`..`VIO-C-004`

**Tier:** B, conditional on `VIRTIO_NET_F_CTRL_VQ` negotiation (MVP
may skip). If negotiated, verify command class + cmd + ack flow.

---

## 5. Adversarial / fuzz tests

### 5.1 RX parser fuzz

**Tier:** C. `libFuzzer` linked against the RX parser `.S` +
`virtio_net_hdr` consumer; feed random byte streams up to
`ETH_MAX_FRAME + virtio_net_hdr` bytes.

**Goal:** every state in the parser's state machine reachable; no
crashes, no out-of-bounds reads beyond the frame buffer, no
uninitialized-metadata exposure to L3.

**Seed corpus:** scapy-generated valid Ethernet II frames across
the full EtherType dispatch, VLAN-tagged, Q-in-Q-tagged, pause,
ARP, ICMP-inside-IP. Dictionary entries for `0x8100`, `0x88A8`,
`0x0806`, `0x0800`, `0x86DD`, `0x8808`.

**Runtime:** 5 minutes per cell in CI (budgeted against the cell's
total walltime); 24 hours nightly against main.

### 5.2 Virtqueue fuzz

**Tier:** C. Fuzz the descriptor ring / avail ring / used ring
layout from the device side — malformed indices, lengths beyond
buffer, `next` fields forming loops, `INDIRECT` chains of
pathological depth.

**Goal:** the guest's virtio driver never reads past a descriptor's
length, never follows a `next` into an invalid descriptor, never
loops indefinitely.

### 5.3 Frame-corpus adversarial

**Tier:** C / B hybrid. A curated corpus of known-pathological
frames: runt with valid FCS but malformed header, giant with valid
FCS but payload overflow, crafted VLAN stacks (128-deep
theoretical), ARP with misaligned field lengths, ARP flood (> ARP
cache capacity — D043 backpressure path).

**Goal:** each counter increments as expected; no state corruption;
the L2 FSA dispatcher continues processing after every rejection.

---

## 6. Interop tests — D042 Tier 1

Containerlab-hosted. Each topology is a CI job under a separate
workflow file (`cd-interop.yml`) that runs on nightly schedule +
pre-release tag.

### 6.1 Linux netdev interop

**Topology:** fireasmserver in one network namespace,
Linux-netdev endpoint in another, `veth` bridge.

**Cases:**
1. ARP resolution completes bidirectionally.
2. Ping (ICMP echo) succeeds (depends on L3; probes the L2 seam).
3. Raw Ethernet frame round-trip: Linux sends frames with a custom
   EtherType; fireasmserver counts them; Linux receives
   fireasmserver's replies.

### 6.2 OVS interop

**Topology:** OVS bridge between fireasmserver and a traffic source.

**Cases:**
1. Same basic tests as 6.1, with OVS in the path.
2. VLAN tag pass-through: OVS configured to tag with VID=42; verify
   fireasmserver extracts VID=42 into metadata.
3. Broadcast domain: multiple "clients" (traffic sources) behind OVS;
   fireasmserver sees each.

### 6.3 Arista cEOS-lab interop

**Topology:** fireasmserver attached to an Arista cEOS-lab switch
port configured as an access port (single VLAN).

**Cases:**
1. LLDP neighbor discovery (if LLDP in scope; probably not MVP).
2. MAC learning on the switch: switch correctly learns
   fireasmserver's MAC after the first frame.
3. VLAN access port: fireasmserver sees untagged frames;
   cEOS strips inbound and adds outbound.

### 6.4 Nokia SR Linux interop

**Topology:** Similar to 6.3 against SR Linux.

**Cases:** MAC learning, untagged frame exchange.

---

## 7. Performance tests — per D040

### 7.1 Cycles per frame

**Tier:** B for measurement; baseline stored at
`tooling/perf/baselines/<arch>-<platform>.cycles.txt`.

**Method:** warm up with 1024 frames, measure with `rdtsc` /
`cntvct_el0` for the next 65536 frames, report mean + p50 / p99 /
p99.9 / max.

**Initial target:** 670 ns / frame at 1 Gbps line rate (floor);
67 ns / frame at 10 Gbps (design target, per DESIGN.md §2).

**Ratchet:** CI fails the cell if measured cycles exceed the stored
baseline by more than the tolerance band (initial band: ±5%;
revisit after 5 consecutive runs characterize noise, per D040).

### 7.2 Cache miss rate

**Tier:** B via `perf stat` on the runner (x86) or hardware PMC on
the Pi 5 (aarch64).

**Metric:** L1d miss rate per RX + TX cycle. Goal: miss rate below
some threshold (pin in design doc update after first measurement).

### 7.3 Branch miss rate

**Tier:** B. Same measurement harness.

**Metric:** branch mispredict rate in the RX hot path. Goal: under
2% for the common untagged case.

### 7.4 Instructions retired

**Tier:** B. Sanity check: if the cycle count regresses but
instructions-retired is stable, the slowdown is memory/cache;
if instructions-retired is up, it's a code-path change.

### 7.5 OSACA predicted vs. measured divergence

**Tier:** B. OSACA predicted throughput from `tooling/osaca/run.sh`
vs. measured cycles/frame. Baseline the divergence; a large gap
means either OSACA's DB is wrong for our microarch or real
hardware is doing something the model doesn't capture.

---

## 8. Test tooling we need to build

- **`tooling/l2_test/`** — QEMU-based integration test harness
  with tap injection + capture. Python harness shell-outs to
  `qemu-system-*`; scapy for frame crafting.
- **`tooling/l2_fuzz/`** — libFuzzer harness linked with the RX
  parser; seed-corpus generator.
- **`tooling/perf/`** — baseline + ratchet tool per D040; already
  scoped as a side-session candidate.
- **`tooling/interop/`** — containerlab topologies for Tier 1
  interop; nightly workflow `cd-interop.yml`.

Each of these is a potential side-session task. `tooling/perf/` is
the obvious next hand-off after CRC-32 closes; `tooling/l2_fuzz/`
after that.

---

## 9. Test-to-requirement coverage matrix

Living table; rows added as tests are planned / written. Columns:

| Test ID | Tier | Covers | Status |
|---------|------|--------|--------|
| `eth-layout-minimal` | A | `ETH-001`, `ETH-002`, `ETH-015`, `ETH-017` | planned |
| `eth-size-bounds` | A+B | `ETH-003`, `ETH-004`, `ETH-010`..`ETH-013` | planned |
| `eth-fcs-primitive` | A | `ETH-005` (via crc32 side session) | planned |
| `eth-fcs-virtio` | B | `ETH-009` | planned |
| `eth-mac-filter` | B | `ETH-006`..`ETH-008`, `ETH-016`, `MAC-001`..`MAC-005` | planned |
| `eth-pause-reject` | B | `ETH-018` | planned |
| `vlan-rx-parse` | A+B | `VLAN-001`..`VLAN-004`, `VLAN-006` | planned |
| `vlan-tx-insert` | B | `VLAN-001` (TX path) | planned |
| `arp-request-reply` | B | `ARP-001`..`ARP-004`, `ARP-011` | planned |
| `arp-cache` | B | `ARP-005`..`ARP-008` | planned |
| `arp-conflict-detect` | B | `ARP-009`, `ARP-010` | planned |
| `virtio-init-sequence` | B | `VIO-001`..`VIO-009` | planned |
| `virtio-feature-negotiation` | B | `VIO-F-001`..`VIO-F-007` | planned |
| `virtio-queue-layout` | A+B | `VIO-Q-001`..`VIO-Q-008` | planned |
| `virtio-rx-path` | B | `VIO-R-001`..`VIO-R-007` | planned |
| `virtio-tx-path` | B | `VIO-T-001`..`VIO-T-006` | planned |
| `virtio-control-path` | B | `VIO-C-001`..`VIO-C-004` | planned (conditional) |
| `fuzz-rx-parser` | C | (broad — `ETH-*`, `VLAN-*`) | planned |
| `fuzz-virtqueue` | C | `VIO-Q-*`, `VIO-R-*`, `VIO-T-*` | planned |
| `interop-linux-netdev` | D | cross-arch regression | planned |
| `interop-ovs` | D | `VLAN-*` pass-through | planned |
| `interop-ceos-lab` | D | enterprise-switch parity | planned |
| `interop-sr-linux` | D | enterprise-switch parity | planned |
| `perf-cycles-per-frame` | B | D040 ratchet | planned |
| `perf-cache-miss-rate` | B | D040 ratchet | planned |
| `perf-branch-miss-rate` | B | D040 ratchet | planned |
| `perf-osaca-divergence` | B | D040 ratchet | planned |

Status legend: `planned` / `written` / `passing` / `superseded`.
Flipped by the author of the test commit in the same PR (single-
author repo — same commit).

---

## 10. Exit criteria for "L2 done"

All of the following must be true before L2 is declared
production-ready for x86_64 (then repeated for AArch64):

1. Every row in `REQUIREMENTS.md` has status `implemented`,
   `deviation` (with a cited D-entry), or `N/A`. No `spec` rows,
   no `tested` rows.
2. Every row in section 9 of this document has status `passing`,
   except `deviation`-linked tests that are marked `N/A`.
3. Fuzz (Tier C) has run for at least 72 cumulative hours without
   discovering a crash or unhandled error path.
4. Interop (Tier D) has a green run against all four Tier 1
   topologies on the current commit.
5. Perf baselines (section 7) are established and ratcheted in CI
   against the current commit; no outstanding "tolerance band
   exceeded" signal.
6. Observability hooks per `DESIGN.md` §6 emit expected counter
   deltas in every functional test scenario — the counters are
   themselves tested.

---

## 11. What this plan does not cover

- **L3 behaviors** (IP routing, fragmentation, ICMP) — separate
  test plan, separate requirements.
- **L4 behaviors** (TCP state machine, retransmit, congestion).
- **TLS + HTTP** — same; separate plans per layer.
- **Host hardware failure modes** (NIC disconnect, PCI error
  recovery) — those belong to the production-deployment test plan
  per D041.
- **Security hardening beyond protocol conformance** (spectre,
  side-channel, timing attacks on crypto primitives) — separate
  security review, cited from `DESIGN.md` when the relevant
  decision is made.

## Process notes

- This plan is revised in-commit with the code and tests it
  describes. Drift between plan and tests is a review-blocking bug.
- Adding or removing a requirement (`REQUIREMENTS.md`) ripples
  here: add / remove / re-map the covering test.
- Each "revisit trigger" in the requirements or design doc
  corresponds to a potential test-plan update when triggered.
