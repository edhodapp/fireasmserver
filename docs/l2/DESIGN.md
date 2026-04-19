# Layer-2 Design Note

**Scope:** the Ethernet/MAC layer of fireasmserver, targeting the
virtio-net device exposed by Firecracker. Arch-neutral except where
explicitly split per D039 §2 (DMA + cache-coherence model).

Cross-references:
- Requirements enumerated in [REQUIREMENTS.md](REQUIREMENTS.md).
- Architectural decisions: D012 (VMIO FSA engine), D022/D024 (Pi 5
  bridge topology), D038 (L2 methodology), D039 (design-doc five-
  property rule), D040 (perf ratchet), D042 (interop matrix), D043
  (FSA runtime model — static pools, cooperative dispatch).

---

## 1. Overview at a glance

Two primary flows. Neither holds per-frame state across transitions —
state is in pre-allocated buffer descriptors and in the pool allocator.

```
  RX path:
    virtio queue-notify →  RX dispatch transition
                          |
                          ├─ validate header (ETH-002, ETH-009, ETH-010, ETH-011)
                          ├─ dispatch by EtherType (ARP / IPv4 / IPv6 / other)
                          └─ return descriptor to avail ring

  TX path:
    L3 request_tx()      →  TX dispatch transition
                          |
                          ├─ populate descriptor chain (hdr + payload)
                          ├─ publish to avail ring (VIO-Q-008 barrier)
                          ├─ notify device if required (EVENT_IDX)
                          └─ mark request pending

  TX completion:
    virtio used-ring idx →  TX reclaim transition
                          |
                          ├─ walk new used-ring entries
                          ├─ return buffers to TX pool
                          └─ notify L3 of completion (per-request)
```

L2 has no connection state. What would be "per-connection" elsewhere
lives in L4's TCB pool (D043). L2's only state is the two virtqueue
ring cursors plus its pool free-lists.

---

## 2. D039 §1 — Latency and throughput budget

**Floor (must achieve):** 1 Gbps line rate, 64-byte frames, single
vCPU. = 1.488 Mpps. = **672 ns/frame** total budget across RX+TX.

**Design target:** 10 Gbps line rate, 64-byte frames, single vCPU.
= 14.88 Mpps. = **67 ns/frame** total budget across RX+TX.

**Stretch (future, multi-queue or hardware offload):** 25 Gbps.
= 37.2 Mpps. = 27 ns/frame. Not required for MVP.

**Per-path cycle budget on a 2.5 GHz reference core:**

| Phase | Cycle budget (10 Gbps) | Cycle budget (1 Gbps) |
|-------|------------------------|-----------------------|
| RX header validation | 30 | 300 |
| RX dispatch + L3 handoff | 50 | 500 |
| TX header build | 30 | 300 |
| TX descriptor publish + barrier | 40 | 400 |
| TX reclaim (amortized) | 15 | 150 |
| **Total (RX+TX)** | **165** | **1650** |

The 67 ns/frame target corresponds to ~170 cycles at 2.5 GHz — the
budget table is tight but not theoretical. Known 10 Gbps software
implementations (DPDK, XDP) achieve this without hand-rolled
assembly. We expect to match or beat them because we skip the
kernel boundary entirely.

**Ratcheted per D040.** `FSA_TRANSITION_BUDGET_NS` is initially
pinned at **100 ns** for L2 transitions; the actual per-transition
measurement becomes the baseline in
`tooling/perf/baselines/<arch>-<platform>.cycles.txt`. Regressions
fail the cell.

---

## 3. D039 §2 — DMA and cache-coherence model

### x86_64

Memory ordering: total-store-order (TSO). Stores from a single CPU
are ordered with each other by hardware; loads cannot be reordered
past earlier loads; stores cannot be reordered past earlier loads.

Implications for virtio:
- **Guest-to-device direction (driver writing descriptor, then
  bumping avail->idx).** The Intel/AMD hardware already orders these
  stores. No explicit fence needed between descriptor write and idx
  bump in the guest. A compiler barrier (`asm volatile("" ::: "memory")`
  equivalent — which for assembly means just not reordering source
  text) suffices.
- **Device-to-guest direction (driver reading used->idx, then
  reading used ring entry).** Same story in reverse: x86 won't
  reorder the load of the ring entry before the load of the idx.
  No explicit fence.
- **Only explicit fence needed:** before a cross-CPU wakeup if we
  ever add multi-vCPU. Not in MVP scope.

Conclusion: on x86_64 we rely on TSO; no `MFENCE`/`SFENCE`/`LFENCE`
in the L2 hot path. Verified at review time, not assumed.

### AArch64

Memory ordering: weak. Stores and loads can be freely reordered
unless constrained by explicit barriers or acquire/release semantics.

Implications for virtio (per Virtio 1.2 §2.7.11):
- **Guest-to-device direction (descriptor write → idx bump).**
  Requires a store-store barrier between the two. `DMB ISHST`
  (Inner Shareable, Store-before-Store) is the minimum-cost
  barrier that does the job: it serializes the descriptor store
  ahead of the idx store from the CPU's observable perspective.
- **Device-to-guest direction (idx load → descriptor load).**
  Requires a load-load barrier between reading the idx and reading
  the descriptor the idx points at. `DMB ISHLD` is sufficient.
- **Device observation of our stores.** Virtio devices in
  Firecracker see guest memory through the same coherent memory
  domain (inner shareable). `DMB ISH` is adequate; `DSB` is
  heavier and unnecessary for this path.

Alternative: use ARMv8's acquire/release load/store instructions
(`LDAR`, `STLR`) at the ring-index locations. These provide the
ordering without needing a separate barrier instruction. Slight
win: one instruction instead of two. Preferred when the assembly
is written specifically for ARMv8.1+.

**Where barriers live in our code (one line each, deliberately):**
```
TX publish path (AArch64):
    str  w_desc_idx, [avail_ring + ring_slot]   // descriptor visible
    dmb  ishst                                    // store-before-store
    str  w_new_idx,  [avail_ring + IDX_OFFSET]   // publish

RX consume path (AArch64):
    ldr  w_used_idx, [used_ring + IDX_OFFSET]   // snapshot
    dmb  ishld                                    // load-before-load
    ldr  x_desc,     [used_ring + ring_slot]    // safe to read entry
```

Reviewed as correctness, not perf. A missed barrier is a data-race
class bug that may not show up for months.

---

## 4. D039 §3 — VMIO re-entrancy / atomicity ACL

Per D043: the dispatcher is cooperative. Transitions run to
completion; no preemption mid-transition.

**Source of events (what can add to the pending queue):**

1. **Virtio queue-notify interrupt.** Delivered via MSI-X on x86
   (LAPIC), GIC-v3 PPI on AArch64. Handler is minimal: increments a
   "new events available on RX ring" counter and returns.
2. **Timer expirations.** Retransmit timers, ARP refresh, keepalive.
   L2 doesn't own these; L3/L4 do. L2 receives no timer events.
3. **L3-originated TX request.** Same-CPU, synchronous call into
   `l2_request_tx()`. Enqueues to the pending TX queue.

**Atomicity rules (load-bearing invariants):**

| Rule | Enforcement |
|------|-------------|
| An interrupt handler NEVER mutates FSA state beyond appending to a wait queue. | Code review + pre-commit static check (grep for FSA-state writes from handler files). |
| A transition, once started, runs to completion before the dispatcher pops the next event. | Dispatcher structure: no yield, no sleep. |
| A transition MUST NOT enable interrupts it has not specifically arranged. | Default IRQ state on transition entry is "as the dispatcher left it." |
| A transition fault rolls back the slot to pre-transition state (D043). | Slot header records entry-state; fault handler restores it before marking the slot as free. |
| A pending-queue enqueue from an interrupt is atomic w.r.t. the dispatcher's dequeue. | Single-producer/single-consumer MPMC is overkill; use a circular buffer with acquire/release semantics on head/tail. |

**Not re-entrant:** L2 transitions never call back into the L2
dispatcher. If RX processing wants to send an immediate reply
(e.g., ARP REPLY in response to an ARP REQUEST), it enqueues a TX
request to the pending queue and returns. The dispatcher picks up
the TX on the next loop iteration.

---

## 5. D039 §4 — VLAN scope

**MVP decision: 802.1Q + 802.1ad are DESIGNED IN, runtime-inert by
default.** Per D045 (which superseded D044 on assembly-retrofit-cost
grounds — see D046). Filter management and TX tagging are config-
driven; pure-cloud deployments that never see tagged frames pay
only the cost of one predict-not-taken branch per RX frame.

**RX behavior (always on, hot-path):**

The RX dispatch transition peeks at the two bytes at offset 12
(position of EtherType in an untagged frame):

```
  offset 12-13 == 0x8100  →  802.1Q single-tagged
                              extract VID (12 bits of bytes 14-15)
                              re-read EtherType from offset 16
  offset 12-13 == 0x88A8  →  802.1ad Q-in-Q
                              extract outer VID, inner VID from bytes 14-19
                              re-read EtherType from offset 20
  otherwise               →  untagged; EtherType at offset 12 as expected
```

Extracted VID(s) go into the per-frame metadata slot alongside the
buffer descriptor. L3 handoff passes the metadata; upper layers
that don't care about VID read 0 and proceed.

**Hot-path cost (measured against the D040 baseline):**

| Path | Cycles (2.5 GHz reference) |
|------|----------------------------|
| Untagged (compare + predict-not-taken branch) | +3 |
| 802.1Q single-tag (compare taken, skip 4, re-read) | +6 |
| 802.1ad Q-in-Q (nested compare, skip 8, re-read) | +9 |

At the 10 Gbps / 67 ns (≈170-cycle) budget, the untagged tax is
~2% of per-frame cycles. Acceptable; sits within the §2 table.

**TX behavior (opt-in per request):**

The TX request structure carries an optional VID:

```c
/* pseudocode */
struct tx_request {
    uint8_t   dst_mac[6];
    uint16_t  vid;          /* 0 = untagged; non-zero = insert tag */
    uint16_t  ethertype;
    ...
};
```

When `vid == 0` (the MVP default for every L3/L4 caller), the TX path
constructs an untagged frame with zero tag-insertion cost. When
`vid != 0`, a 4-byte `0x8100`-TPID tag is inserted between SA and
EtherType. TX-path branch is predict-not-taken on the untagged side.

**Filter management (VLAN-accept list):**

Deferred per D045's "stays deferred" list: `VIRTIO_NET_CTRL_VLAN_ADD`
/ `_DEL` control-queue plumbing is an additive feature on an already-
designed control-queue interface. Until it lands, L2 accepts any VID
it sees. OEM deployments that need strict VID filtering will add it
as a control-queue feature without reshaping the RX hot path.

**What we do NOT do:**

- We don't enforce a filter against the received VID (per above).
- We don't re-emit rejected tagged frames back to the device.
- We don't forward a received tagged frame as untagged to L3 without
  recording the VID — VID is always in the metadata slot.

**Revisit triggers (each would produce a new D-entry):**

- VLAN filter management becomes needed → design doc update + new D
  entry documenting the control-queue wiring.
- Production cost analysis shows the untagged hot-path tax (+3
  cycles) is above tolerance → revisit the design, possibly
  introduce a runtime-patched fast path.

---

## 6. D039 §5 — Observability hook contract

**Invariant:** observability is always designed in, never bolted on
after. Hot-path hooks compile to minimal instruction sequences
whether enabled or disabled.

### Three categories

1. **Always-on per-slot counters (free cost).** Every FSA slot in
   every pool has a fixed counter block: transitions-entered,
   transitions-completed, last-transition-cycles, errors. Writes are
   single store instructions in the transition body. No branch.
   Memory cost: ~32 B per slot × number of slots.

2. **Globally-gated trace ring (bounded cost).** A ring buffer of
   recent transition records (timestamp, slot ID, event type, result).
   Hot-path code tests a single global flag; when the flag is off,
   the code path is a single branch over the trace write. When on,
   the write is a bounded number of instructions (<10 cycles) to a
   pre-allocated ring slot.
   - MVP: branch-on-flag in the hot path.
   - Future optimization: patch the hot path at enable/disable time
     (live instruction patching) to turn the trace write into a NOP
     when disabled. Zero branch cost when off.

3. **PMC sampling (near-zero overhead, hardware).** Hardware
   performance counters (x86 `rdpmc`, ARM `PMEVCNTR`) expose cycle
   counts, cache misses, branch mispredicts, instructions retired
   per transition. Configuration is a one-time setup at dispatcher
   start; reads are rare (per-sample, not per-transition).

### Fault snapshot

On transition error OR assertion failure:
- Serialize the slot's state block (known size, known offset).
- Dump the last N trace ring entries (N = 256 for MVP).
- Dump the pending queue head state.

Serialized output writes to a dedicated diagnostic buffer the host
can retrieve via the virtio console device or (post-MVP) a dedicated
diagnostic virtqueue.

### What observability is NOT

- `printf`-style formatting in the hot path. Formatting is expensive;
  traces are structured binary records that the host decodes.
- Any dynamic allocation. Ring, snapshot buffers, counter blocks are
  all statically sized (D043).

---

## 7. RX state machine (informal)

Not a multi-state-per-frame machine — most frames touch exactly one
transition. States are "pipeline slots," not "connection states."

```
  DESCRIPTOR_READY   (virtio used-ring idx advanced; a frame is available)
        │
        │ RX-dispatch transition
        │
        ▼
  HEADER_VALIDATED   (DA matches filter; FCS valid per virtio flags;
                      length in bounds [ETH-003, ETH-004, ETH-010, ETH-011])
        │
        │ (same transition, no yield)
        │
        ▼
  DELIVERED          (handed to the appropriate upper layer based on
                      EtherType — ARP, IPv4, IPv6; unknown EtherType
                      → drop and count)
        │
        │ (same transition)
        │
        ▼
  RECYCLED           (descriptor returned to avail ring; counter updated)
```

One transition per frame is the common path. Invalid frames short-
circuit to RECYCLED with an error-counter increment.

## 8. TX state machine (informal)

```
  REQUEST_QUEUED     (L3 called l2_request_tx; pending queue holds
                      a TX intent record with ethertype + DA + payload
                      descriptor)
        │
        │ TX-dispatch transition
        │
        ▼
  DESCRIPTOR_BUILT   (virtio_net_hdr populated per [VIO-T-002..004];
                      descriptor chain constructed; avail ring updated
                      with the VIO-Q-008 barrier)
        │
        │
        ▼
  NOTIFIED           (queue notify register written unless EVENT_IDX
                      suppression said skip)
        │
        │ ... device operates asynchronously ...
        │ virtio used-ring idx advances; TX-reclaim transition fires
        │
        ▼
  RECLAIMED          (buffer returned to TX pool; L3 notified of
                      completion)
```

---

## 9. Buffer lifecycle

Two pools, both static per D043:

- **RX pool.** `RX_BUF_COUNT` buffers × `RX_BUF_SIZE` bytes. Populated
  into the RX virtqueue at init; refilled on consumption. `RX_BUF_COUNT`
  SHOULD equal the virtio RX-queue size so the queue never starves.
  Buffer size sized for max frame (1518 untagged; revisit if jumbo
  frames ever enter scope).
- **TX pool.** `TX_BUF_COUNT` buffers. L3 requests a TX by referencing
  one of these buffers (zero-copy in the common case; L3 fills it
  directly). On reclaim, buffer returns to the free list.

Capacity sizing relationships documented in `config.S`:
```
.equ RX_BUF_COUNT,     <queue_size from virtio>
.equ RX_BUF_SIZE,      1518 + slack
.equ TX_BUF_COUNT,     >= RX_BUF_COUNT   # conservative
.equ TX_BUF_SIZE,      1518 + virtio_net_hdr
```

---

## 10. Error handling and backpressure

Per D043, every allocator-full response is defined up front:

| Condition | Response |
|-----------|----------|
| RX descriptor validation fails (bad length / FCS bit) | Drop, increment counter, refill descriptor |
| RX buffer has unknown EtherType | Drop, increment counter, refill |
| RX tagged frame (VLAN) | Drop, increment `rx_vlan_dropped` counter |
| TX buffer pool empty | Return `TX_BUSY` to L3; L3 decides retry or drop |
| TX ring full (avail idx can't advance) | Same as above |

No silent drops without a named counter. That's the rule.

---

## 11. Designed-in accommodations (D045 / D046)

Features the MVP runtime disables by default but the architecture
accommodates so future enablement doesn't force a hot-path retrofit.
Shipping defaults produce the same runtime behavior the original
"out of scope" plan would have; the **structure** stays production-
capable.

### 11.1 Multi-queue

- `NUM_QUEUES` is a build-time `.equ` constant. MVP value: `1`.
- RX and TX pools are arrays indexed by queue: `rx_pool[NUM_QUEUES]`,
  `tx_pool[NUM_QUEUES]`. MVP instantiates size-1 arrays.
- The dispatcher indexes by queue when pulling events; pending-event
  queues are per-queue. No hard-coded "queue 0" special cases.
- `VIRTIO_NET_F_MQ` feature negotiation is gated on `NUM_QUEUES > 1`;
  MVP never negotiates it.
- When an OEM deployment wants MQ, the change is a `.equ` edit, a
  feature-negotiation branch flip, and a queue-steering rule. No
  reshape of the dispatcher or pool structure.

### 11.2 Checksum offload

- The L2 RX path always populates `virtio_net_hdr` fields
  (`flags.NEEDS_CSUM`, `csum_start`, `csum_offset`) into the
  per-frame metadata struct handed to L3.
- L4 (TCP) reads the metadata and decides whether to compute the
  checksum in software or trust the offload result.
- MVP does not negotiate `VIRTIO_NET_F_CSUM` / `F_GUEST_CSUM`,
  so the fields are always zeroed at L2. No hot-path cost.
- Enabling offload later is a feature-negotiation branch flip and an
  L4 consume-flag update. No L2 interface change.

### 11.3 Jumbo frames

- `ETH_MAX_FRAME` (the current L2 is Ethernet-only; a non-Ethernet
  L2, if ever added — e.g., PPP for a cellular gateway — would be
  its own module with its own constant) is a build-time `.equ`.
  MVP value: `1518` (or `1522` once VLAN-tag insertion runs).
- `RX_BUF_SIZE`, frame-length compares (`ETH-003`, `ETH-004`,
  `ETH-010`, `ETH-011`), and the TX descriptor sizing all derive
  from `ETH_MAX_FRAME`.
- OEM deployments that need 9000-byte jumbo override `ETH_MAX_FRAME`
  at build time. The RX hot path is structurally identical; only
  the compare constants change.
- Buffer-pool memory footprint scales linearly with `ETH_MAX_FRAME ×
  RX_BUF_COUNT`. Customers sizing jumbo need to co-tune
  `RX_BUF_COUNT` to stay within the RAM budget.

### 11.4 GSO / LRO metadata passthrough

- `virtio_net_hdr.gso_type`, `.hdr_len`, `.gso_size` fields are
  read by L2 on RX and written into the per-frame metadata struct
  passed to L3/L4.
- MVP does not negotiate GSO features, so the hdr fields are always
  `GSO_NONE` (0) at L2. Passthrough is unconditional; L4 is the gate.
- Future GSO enablement happens at L4 (TCP's segmentation decision
  and reassembly discipline). L2's interface is already shaped for it.

### 11.5 Pause-frame reject

Per `ETH-018`, the RX dispatch transition recognizes Ethernet PAUSE
frames (EtherType `0x8808`, opcode `0x0001` per IEEE 802.3x
§31B) and silently discards them with a `rx_pause_dropped` counter
increment. Pause frames are infrastructure-layer flow control the
MVP doesn't handle. Single compare in the RX parser.

---

## 12. Genuinely out of scope for MVP (explicit list)

These deferrals do NOT reshape the hot path on later addition, so
they stay deferred per D046:

- VLAN filter management (`VIRTIO_NET_CTRL_VLAN_ADD` / `_DEL`) —
  additive control-queue feature; implementable without touching
  RX dispatch.
- Actual GSO segmentation on TX — L4 decision, not L2 architecture.
- Runtime MQ negotiation with the device — MVP is `.equ`-pinned.
- Pause-frame *flow control response* (acting on received pause) —
  we reject pause frames, don't respond to them. Real flow control
  integration is its own module.
- PFC (802.1Qbb) — interacts with QoS queues we don't have.
- LACP (802.1AX) — bonding is above the L2 driver, orthogonal.
- Bridging — we are an endpoint (per D022 scope).
- PHY-layer anything (autonegotiation, WoL, EEE) — virtio abstracts.

Each becomes a new decision entry if/when reversed.

---

## 12. Open questions (recorded for the test plan to pin down)

1. **EVENT_IDX suppression.** Negotiate it or not? Affects TX
   notification frequency. Simpler without. Revisit when perf
   measurements show notification overhead mattering.
2. **Exact MSI-X / GIC setup per arch.** Firecracker documents this;
   we'll follow that doc when we wire up interrupts. Not a design
   question, just an implementation-time reference.
3. **Counter granularity.** Per-slot counters in D043 are explicit.
   Per-EtherType RX breakdown — necessary for observability, or
   overkill? Default "yes" (cheap, static); revisit if footprint
   becomes a concern.
4. **Test harness for malformed frames.** Side-session territory
   (packet generator, per parallelization strategy).

## Process notes

- This doc is the authority for L2 design questions. Code that
  disagrees with a statement here either updates the doc or gets
  rejected at review.
- Status updates in `REQUIREMENTS.md` reference sections of this
  doc by number when a design choice is the reason for a requirement's
  status (e.g., VLAN-001..007 all become `deviation` pointing at §5).
- Revisions to this doc are normal Git commits. Material design
  changes (changing the VLAN decision, changing the latency budget,
  changing the atomicity model) trigger a `DECISIONS.md` entry
  *in addition* to the doc update.
