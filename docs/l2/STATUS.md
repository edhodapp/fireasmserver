# L2 status — 2026-05-24

Snapshot of the L2 (Ethernet + ARP) layer's state at the close
of the fail-path coverage push. Reference document for "what
does L2 cover today?" — updated when L2 scope changes
materially, not on every commit.

## Bottom line

**L2 RX side is substantively complete.** Every spec row in
`REQUIREMENTS.md` §1 that doesn't depend on infrastructure we
don't have yet (TX API, virtio FCS offload negotiation,
control queue) is implemented in `arch/{x86_64,aarch64}/l2/
dispatcher.S` and covered by either integration tests
(`tooling/tests/integration/`) or build-time assertions or
fail-path stub tests.

**L2 TX side is design-only.** `docs/l2/TX_API.md` captures
the Vyukov MPSC ring + buffer-pool design chosen 2026-05-23;
implementation is deferred until L3 lands as the first real
consumer. Per-layer pieces of the design (ARP responder
through-queue migration, ETH-012/013 outgoing-padding tests)
ride along with the implementation pass.

**Layer is ready to be a stable foundation for L3.** The
contract L3 will consume — a real `l2_tx_enqueue` API and the
shape of the dispatcher's bounded transitions — is captured in
the TX_API doc. The L2 RX path is hardened enough that L3
code reading from L2 won't encounter "did the dispatcher
handle malformed frame X" questions; every malformed frame
class either gates out (drop with marker) or is type-system-
unrepresentable (fail-path defensive checks plus their
fail-path stub tests).

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
| ETH-012 | TX-side padding to 64 | ❌ deferred | needs TX API; see `docs/l2/TX_API.md` |
| ETH-013 | TX padding zero-fill | ❌ deferred | same |
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

27 integration tests, ~14 s suite runtime on x86_64 laptop:

```
$ pytest tooling/tests/integration/ -q
...........................                                              [100%]
27 passed in 14.14s
```

Files (all under `tooling/tests/integration/`):

- `test_arp_request_reply.py` — 3 cases (ARP-001, ARP-004, ARP-011)
- `test_eth_mac_filter.py` — 3 cases (ETH-006/008/007 + MAC-001)
- `test_eth_pause_drop.py` — 1 case (ETH-018)
- `test_eth_size_bounds.py` — 4 cases (ETH-003/004/010/011)
- `test_eth_src_mac.py` — 2 cases (ETH-015 + positive companion)
- `test_l2_fail_paths.py` — 3 cases (BAD_ID, NUM_BUFS, TX_BAD_ID; x86_64)
- `test_l2_gate_order.py` — 10 cases (PICT-style)
- `test_rx_budget_reentrance.py` — 1 case (FSA-4)

Plus the Pi-side tracer scripts:

- `tooling/tracer_bullet/pi_aarch64_firecracker.sh` — boot smoke
  + full marker chain through TX:RECLAIMED on aarch64
- `tooling/tracer_bullet/pi_aarch64_failpath.sh` — 3 fail-path
  scenarios on aarch64

Both run from `tooling/hooks/pre_push.sh` on every push (SKIP
cleanly when the Pi isn't reachable).

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

Per the 2026-05-24 working order, the queue from here is:

1. **TX API implementation** when L3 is ready to consume.
   `docs/l2/TX_API.md` is the design spec.
2. **L3 (IPv4)** scope discussion + implementation. Will
   consume the TX API; will revisit the ARP cache as a
   real downstream consumer.

Things deliberately not next:

- More L2 RX hardening for its own sake. The coverage map
  above is complete enough that additional rows would be
  marginal vs. moving up a layer.
- aarch64-specific dispatcher work without a forcing
  function. The cross-arch parity is good enough that
  feature work should land on both arches in one commit
  going forward.

## See also

- `REQUIREMENTS.md` — per-row spec
- `docs/l2/TEST_PLAN.md` — original test plan framing
- `docs/l2/HARNESS.md` — integration test harness internals
- `docs/l2/TX_API.md` — TX API design (sketch, not yet
  implemented)
- `arch/{x86_64,aarch64}/l2/dispatcher.S` — the actual
  implementation
- `DECISIONS.md` D067 — the formal closure entry
