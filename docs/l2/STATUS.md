# L2 status — 2026-05-24

Snapshot of the L2 (Ethernet + ARP) layer's state. Reference
document for "what does L2 cover today?" — updated when L2
scope changes materially.

## Bottom line (revised 2026-05-24 per DECISIONS.md D068)

**L2's RX-side defensive validation is at production-bar
quality.** Size bounds, MAC filter, src MAC unicast, PAUSE
silent discard, ARP responder, FSA-4 reentrance, gate-order
invariants, fail-path defensive checks (bad_id, num_bufs,
hdr_flags, TX bad_id), virtio feature negotiation policy —
all explicit, all tested.

**TX API end-to-end proven (phase b of D068 working
order).** Producer (Vyukov MPSC enqueue), pool (generation
counter ring), consumer (dispatcher TX phase drain + frame
builder), Vyukov slot release after TX:RECLAIMED, pool free
— all wired on both arches. End-to-end e2e test wired on
x86 (`test_l2_tx_api`); aarch64 has the build infrastructure
but no Pi-side runner yet. Caught one producer bug in the
e2e (release-store clobber); fixed.

**L2 as a complete layer is roughly 40% built.** The RX-side
validation + the TX API are the parts that are done. The
upper-layer interface and operational visibility are missing:

  - **L3-callable receive surface** — dispatcher emits
    serial markers but doesn't hand inbound frames to a
    registered upper-layer consumer
  - **ARP initiator + cache** — we REPLY to ARP requests
    but can't SEND them; no cache state machine. The ARP
    responder still uses the legacy inline `tx_arp_buf`
    path; migrating it to the queue through-path is
    phase (c) of the D068 working order.
  - **Statistics counters** — qualitative markers only; no
    RX/TX bytes/frames/drops/errors for ops visibility
  - **Link state monitoring** — `VIRTIO_NET_F_STATUS`
    rejected in feature policy; we assume link is up

Plus the scope-specific items (VLAN, jumbo, IGMP/MLD,
hardware offloads, multi-core dispatch) — these are real
production-L2 features but their absence doesn't block L3
boot-up or basic two-way traffic.

**Critical-not-deferrable** for any real two-way traffic
support: TX API, L3-callable receive surface, ARP initiator
+ cache.

This document previously claimed L2 was "ready to be a stable
foundation for L3." That claim was wrong — see D068. The work
that LANDED is solid; the layer that REMAINS to build is
substantial.

## Coverage map vs REQUIREMENTS.md §1 (Ethernet framing)

| Row | Spec | Status | Where |
|---|---|---|---|
| ETH-001 | Frame format (dst+src+ethertype+payload) | implicit | implemented by every accepted frame's processing path |
| ETH-002 | Header field offsets | implicit | dispatcher reads dst MAC at +12, src MAC at +18, ethertype at +24 |
| ETH-003 | Minimum frame size accept (60 wire = 72 virtio) | ✅ test | `test_eth_size_bounds.test_min_size_frame_accepted` |
| ETH-004 | Maximum frame size accept (1518 wire = 1530 virtio) | ✅ test | `test_eth_size_bounds.test_max_size_frame_accepted` |
| ETH-005 | FCS primitive (CRC-32) | ⚙️ primitive only | `arch/{x86_64,aarch64}/crypto/crc32_ieee.S` + `tooling/crypto_tests/`; not wired into L2 RX (virtio strips FCS per Virtio 1.2 §5.1.6.1) |
| ETH-006 | Broadcast accept | ✅ test | `test_eth_mac_filter.test_multicast_destination_accepted` (covers broadcast as a subset of multicast bit set) + implicit in every ARP test |
| ETH-007 | Multicast accept | ✅ test | `test_eth_mac_filter.test_multicast_destination_accepted` |
| ETH-008 | Wrong-dst-unicast drop | ✅ test | `test_eth_mac_filter.test_unicast_to_wrong_mac_dropped` |
| ETH-009 | Bad FCS discard | ⊘ N/A | virtio-net offloads FCS verification; the device won't deliver bad-FCS frames to us |
| ETH-010 | Runt discard | ✅ test | `test_eth_size_bounds.test_runt_frame_dropped` |
| ETH-011 | Oversize discard | ✅ test | `test_eth_size_bounds.test_oversize_frame_dropped` |
| ETH-012 | TX-side padding to 64 | ✅ test | `test_l2_tx_api.test_txapi_pre_baked_frame_arrives_on_tap0` — pre-bake submits a 10-byte payload, asserts the on-wire frame is exactly 60 bytes |
| ETH-013 | TX padding zero-fill | ✅ test | same — asserts pad bytes after payload are all 0x00 |
| ETH-014 | Inter-frame gap | ⊘ N/A | virtio abstracts PHY |
| ETH-015 | RX source MAC unicast-bit check | ✅ test | `test_eth_src_mac.test_multicast_source_mac_dropped` |
| ETH-016 | TX source MAC unicast sanity | ✅ build-time | `.if GUEST_MAC_BYTE_0 & 1 / .error ...` in both arch dispatcher.S + boot.S; fires the build, not at runtime |
| ETH-017 | Locally-administered bit informational | ⊘ no behaviour | tracked as informational; no enforcement gate needed |
| ETH-018 | PAUSE frame silent discard | ✅ test | `test_eth_pause_drop.test_pause_frame_dropped` |

## ARP (`REQUIREMENTS.md` §3)

| Row | Spec | Status |
|---|---|---|
| ARP-001 | Request-to-our-IP → reply | ✅ test (`test_arp_request_reply.test_arp_request_for_guest_ip_gets_reply`) |
| ARP-004 | Request-to-other-IP → no reply | ✅ test |
| ARP-005..008 | ARP cache state machine | ❌ deferred until L3 needs to *send* (the cache has no consumer without outbound IP) |
| ARP-011 | Cross-subnet wrong IP → no reply | ✅ test |

## Beyond REQUIREMENTS — invariants and behaviours we added

| Item | Coverage |
|---|---|
| FSA-4 persistent shadow correctness (rx_next_avail / rx_used_shadow across dispatch calls) | `test_rx_budget_reentrance.test_rx_burst_exhausts_budget_and_continues` — 30-frame burst proves budget gate + reentrance both work |
| Dispatcher gate ORDER (size > dst MAC > src MAC > PAUSE > ARP > accept) | `test_l2_gate_order.test_gate_order` — 10 parametrised multi-violation cases verify the earlier gate fires |
| Fail-path defensive checks (RX bad_id, RX num_bufs, TX bad_id) | `test_l2_fail_paths.test_fail_path` x3 on x86_64 + `pi_aarch64_failpath.sh` x3 on Pi |
| virtio feature negotiation policy (REQUIRED/ACCEPTED bitmasks) | `arch/{x86_64,aarch64}/platform/firecracker/boot.S` per-bit comment block; runtime asserts at boot via the `VIRTIO_REQUIRED_FEATURES_HI` mask check |

## Test inventory

28 integration tests, ~14 s suite runtime on x86_64 laptop:

```
$ pytest tooling/tests/integration/ -q
............................                                             [100%]
28 passed in 14.06s
```

Files (all under `tooling/tests/integration/`):

- `test_arp_request_reply.py` — 3 cases (ARP-001, ARP-004, ARP-011)
- `test_eth_mac_filter.py` — 3 cases (ETH-006/008/007 + MAC-001)
- `test_eth_pause_drop.py` — 1 case (ETH-018)
- `test_eth_size_bounds.py` — 4 cases (ETH-003/004/010/011)
- `test_eth_src_mac.py` — 2 cases (ETH-015 + positive companion)
- `test_l2_fail_paths.py` — 4 cases (BAD_ID, NUM_BUFS, HDR_FLAGS, TX_BAD_ID; x86_64)
- `test_l2_gate_order.py` — 10 cases (PICT-style)
- `test_rx_budget_reentrance.py` — 1 case (FSA-4)

Plus the Pi-side tracer scripts:

- `tooling/tracer_bullet/pi_aarch64_firecracker.sh` — boot smoke
  + full marker chain through TX:RECLAIMED on aarch64
- `tooling/tracer_bullet/pi_aarch64_failpath.sh` — 3 fail-path
  scenarios on aarch64

Both run from `tooling/hooks/pre_push.sh` on every push (SKIP
cleanly when the Pi isn't reachable).

## What's deliberately deferred — RCA prior-knowledge record

Per Ed's 2026-05-24 RCA-discipline directive ("if we hit any
L2 issues while working on L3, I want a root-cause analysis
why the issue was not discovered now"), this section
exhaustively names every category of L2 issue that we KNOW
we could be missing today. Future RCAs traceable to one of
these categories should not surprise anyone — they were
listed as known gaps at this milestone.

### Categories of bug we are explicitly NOT testing for

  - **Fuzz / random-bytes RX**: every integration test
    sends a hand-crafted frame with controlled bytes. We
    have not run sustained random / structured-fuzz input
    against the dispatcher. A regression in input handling
    that's not on our test enumeration would surface only
    on real traffic.

  - **Adversarial multi-frame interleaving**: tests inject
    sequences of similar frames (e.g., 30-frame burst, or
    different scenarios via fresh boots). We have not
    tested interleaved bad + good + bad in a single burst.
    A race or state-leak between consume-loop iterations
    where the dispatcher's per-frame state isn't fully
    isolated would surface only on mixed traffic.

  - **Sustained traffic / soak**: tests boot, send a
    handful of frames, exit. We have not run hours of
    moderate-rate traffic to surface long-tail bugs
    (counter wrap, log buffer fill, accumulator drift,
    etc.). The 30-frame burst test in
    `test_rx_budget_reentrance.py` is the closest we have.

### Trust assumptions that could break

  - **UART jam → silent failure**. `emit_bytes` returns
    `CF=1` (x86) or never returns (aarch64 hang in
    THRE-poll loop) on UART timeout. No caller checks the
    return; no marker fires; no stats counter increments.
    Firecracker's emulated 8250 doesn't jam under normal
    conditions, so this trust assumption holds in our
    deployment — but it's NOT a graceful degradation
    story.

  - **virtio device contract**. Beyond the defensive
    checks the dispatcher does explicitly (id range,
    num_buffers=1, hdr flags+gso_type=0, frame size in
    [72, 1530]), we trust the device's data. A device
    that wrote past the buffer boundary, set
    flags/csum/hash fields we don't check, or violated
    the AvailRing/UsedRing protocol would have undefined
    behavior in our handling.

  - **AAPCS64 callee-saved discipline**. The aarch64
    dispatcher relies on `emit_bytes` / `emit_hex32`
    preserving the documented register set. The Pi
    bring-up of the failpath stub surfaced one case where
    that contract was easy to violate (x0 = UART_BASE
    clobbered by `mov w0, #1` in `.Lfail`); the
    `emit_bytes` self-load fix (task #44) closes the
    foot-gun for that one register, but the broader
    "every caller must understand emit_*'s
    preserve/clobber contract" is human-discipline.

  - **Single-caller invariant**. `l2_dispatch` assumes
    one caller at a time on one core. Multi-core wake
    (post-D066-step-6+) without per-core dispatch
    discipline would have undefined behavior.

### Features rejected in feature negotiation we'd need to
### re-open for full production

`VIRTIO_NET_F_*` bits we explicitly reject today are listed
in `arch/{x86_64,aarch64}/platform/firecracker/boot.S`'s
feature-negotiation-policy comment block. If a future
deployment needs any of them — CSUM, GSO, MQ, STATUS, MAC
config-space read, CTRL_VQ, etc. — re-opening the policy
means re-running the negotiation testing AND adding the
corresponding consuming code. The current policy is
correct for our scope; expanding it is a real
architectural change.

## What's out of scope today

Deferred-with-rationale:

- **TX API implementation** — design captured in
  `docs/l2/TX_API.md`. Build when L3 lands as the first real
  consumer. Until then, ETH-012/013 (outgoing padding tests)
  and the ARP-responder-through-queue migration ride with the
  implementation pass.
- **ARP cache** (ARP-005..008) — premature without an
  outbound IP consumer. L3 will pull this in.
- **VLAN** — out of scope until we have a use case (no
  expected multi-tenant deployment yet).
- **FCS computation wire-in** — the primitive exists in
  `arch/{x86_64,aarch64}/crypto/crc32_ieee.S` with full test
  coverage in `tooling/crypto_tests/`; virtio-net offloads
  FCS for us today, so L2's TX path doesn't need to compute.
  When/if we move to a transport without offload, wire it in.
- **Multicast joined-group filtering** (RFC 1112 / 3376
  IGMP, RFC 2710 MLD) — accept-all-multicast today; refining
  to subscribed-groups-only needs higher-layer subscription
  state.
- **Multi-queue (`VIRTIO_NET_F_MQ`)** — explicitly rejected
  in the feature negotiation policy. Single RX+TX queue pair
  is enough for current scope; reconsider when L3 + multi-
  core land.

## Why "RX substantively complete" not "RX done"

Three flavours of follow-on work that don't change the
substantive completeness claim but are real:

- **Other-arch parity gaps**. The fail-path stub on aarch64
  runs on the Pi only (pre_push SKIPs when Pi is down);
  there's no GitHub Actions cell that runs aarch64 hardware.
  Acceptable today — the dispatcher code is mirrored
  line-by-line between arches and the test scenarios are
  identical, so an x86_64 fail-path regression would surface
  in CI and an aarch64-only regression would surface in
  local pre-push when the Pi is up.
- **Real-traffic stress**. The integration tests inject
  controlled stimulus. We've never run sustained adversarial
  traffic against the dispatcher (fuzzed frame contents,
  back-to-back oversize bursts, etc.). Belongs in a longer-
  running fuzz / chaos test infrastructure when one is
  warranted.
- **Multi-core / parallelism**. The dispatcher today runs
  on the boot core only. Multi-core wake (post-D066 step
  6+) will reopen ordering / barrier questions, especially
  on the producer side once the TX API has multiple L3
  workers feeding the queue.

None of the three blocks L3 development. They're "next
hardening pass" items that should land alongside specific
features that need them.

## What's next

Per the 2026-05-24 working order (after Ed's "most of the
deferred work completed" directive), the queue is:

1. ~~aarch64 emit_bytes self-load~~ ✅ done (task #44,
   commit 460dc2f)
2. ~~virtio_net_hdr defensive check~~ ✅ done (task #45,
   commit add9df4 — adds RX:FAIL hdr_flags fail-path)
3. ~~STATUS.md fuzz/UART notes + honest D068 reframe~~
   ✅ done (this commit + DECISIONS.md D068)
4. ~~TX API implementation~~ per `docs/l2/TX_API.md`
   ✅ done across phases b.1–b.4, (c) ARP migration, (d)
   ETH-012/013 tests. End-to-end on x86_64; aarch64 has
   the build infrastructure (Pi runner still pending).
5. ~~L3-callable receive surface~~ ✅ done. Dispatcher
   calls `l3_rx_dispatch(frame_addr, wire_len)` for each
   accepted non-ARP RX frame. Symbol lives in
   `arch/<arch>/l3/stub.S` today (emits `L3:RX_FRAME` and
   returns). Real L3 replaces stub.S without dispatcher
   changes. Test:
   `test_l3_rx_surface.test_l3_dispatch_fires_on_non_arp_unicast_frame`.
6. **ARP cache + initiator** — in progress (full state
   machine per the design pick on 2026-05-25). Sub-phases:
   - 6.a ✅ cache region + lookup/insert primitives in
     `arch/<arch>/l2/arp_cache.S`.
   - 6.b ✅ dispatcher RX recognition hook — ARP REPLY
     for our IP fires `arp_cache_insert`. Test:
     `test_arp_cache.test_arp_reply_inserts_into_cache`.
   - 6.c ✅ outbound ARP initiator —
     `arp_send_request(target_ip)` builds an ARP request
     body in a TX API pool buffer and enqueues with
     broadcast dst. Wired into the TXAPI_PREBAKE block
     for testing. Test:
     `test_arp_initiator.test_arp_initiator_sends_request_for_host_ip`
     (verifies the marker + the wire frame on tap0). Caveat:
     Linux's tap0 ARP reply is 42 wire bytes (no padding to
     60), so the dispatcher correctly drops it per ETH-003;
     round-trip cache update is exercised via padded scapy
     replies in test_arp_cache, not via the host's auto-reply.
   - 6.d ✅ `arp_resolve(ip, out_mac) → status` API for L3
     callers. Cache hit + REACHABLE → OK (MAC copied);
     cache hit + other state → PENDING (no re-send);
     cache miss → insert INCOMPLETE + send request + PENDING;
     send failure → FAILED. Tested via TXAPI_PREBAKE
     extension exercising both MISS and HIT paths. Test:
     `test_arp_resolve.test_arp_resolve_miss_then_hit`.
   - 6.e timer + state machine (REACHABLE → STALE →
     PROBE → REACHABLE/FAILED).
   - 6.f gratuitous ARP at boot.
6. **ARP cache + initiator** — outbound ARP, enables
   outbound IP traffic to non-cached peers
7. **Statistics / counters** — per-class drop counts, RX/TX
   bytes+frames, error categories
8. **Link state monitoring** — accept `VIRTIO_NET_F_STATUS`,
   read config-space link bit
9. **Hardware offloads** — start with `VIRTIO_NET_F_GUEST_CSUM`
   (RX checksum), then `F_CSUM` (TX). GSO/MQ are bigger
   architectural lifts.
10. **VLAN tagging** — protocol extension on RX + TX
11. **Jumbo frames** — parameterize size bounds,
    `VIRTIO_NET_F_MTU`
12. **IGMP/MLD multicast subscription** — joined-group state
13. **Multi-core dispatch + RSS** — biggest lift; intersects
    with D058 actor model

Items 4-6 are the "make this a real L2 layer" chunk that
turns the milestone claim from honest-30% to honest-substantively-
complete.

Items 7-9 are core operational + correctness for production
deployment.

Items 10-12 are scope-specific (deployment-dependent).

Item 13 is architectural and likely depends on the multi-
core wake from D066 step 6+.

## See also

- `REQUIREMENTS.md` — per-row spec
- `docs/l2/TEST_PLAN.md` — original test plan framing
- `docs/l2/HARNESS.md` — integration test harness internals
- `docs/l2/TX_API.md` — TX API design (sketch, not yet
  implemented)
- `arch/{x86_64,aarch64}/l2/dispatcher.S` — the actual
  implementation
- `DECISIONS.md` D067 — the formal closure entry
