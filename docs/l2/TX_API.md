# L2 TX API — design

**Status:** design sketch (not implemented). Decisions captured
2026-05-23 in a blue-sky session with Ed; build deferred until
L3 work creates the first real consumer.

**Audience:** anyone building an L3 layer that needs to send
frames, or anyone testing L2's outgoing-frame behavior
(ETH-012 padding to 64, ETH-013 zero-fill, ETH-016 src MAC
sanity). Also: anyone implementing the L2 dispatcher TX path
once this lands.

**Reference architecture:** Jean-Yves Astier's FSA engine
(`~/.claude/Finite_State_Automaton_for_Input_Output_Containers_*.pdf`)
— buffer pools, bounded transitions, transactional handlers,
ways. This API plays the role of Astier's "input container
producer side" for the L2 TX engine.

---

## 1. Goals

- **Producers (L3, ARP responder, eventually any L2-internal
  source) enqueue frames for transmission.** Lock-free, non-
  blocking — the producer does NOT wait for `TX:RECLAIMED`.
- **L2 owns** frame construction (`virtio_net_hdr` + Ethernet
  header), padding to the 60-wire-byte minimum, virtio
  submission, descriptor management, buffer-pool lifecycle.
- **Producer owns** dst MAC, EtherType, payload bytes.
- **Dispatcher stays an Astier-style bounded-transition
  engine** (D043). TX consume is budgeted per dispatch call —
  same shape as RX_FRAME_BUDGET / TX_RECLAIM_BUDGET today.

Non-goal: **synchronous send-and-wait.** Producers that want
delivery confirmation will get it via a separate completion
notification (out of scope here; will need a callback or
completion queue when first required).

## 2. Architecture

Three new pieces, two new memreq regions:

```
                       ┌──────────────────────────┐
   Producer ─────────► │  l2_tx_pending           │   (MPSC ring of TX requests)
   (L3, ARP            │  head_atomic / 16-entry  │
    responder,         │  ring; each slot carries │
    boot-baked test    │  a sequence number for   │
    request)           │  Vyukov MPSC ordering)   │
                       └──────────┬───────────────┘
                                  │  consume
                                  ▼
                       ┌──────────────────────────┐
                       │ Dispatcher TX phase      │   ── build virtio_net_hdr
                       │ (consumer; single)       │   ── prepend Eth header
                       │ TX_REQUEST_BUDGET = 16   │   ── pad to 60 wire
                       └──────────┬───────────────┘   ── submit virtio desc
                                  │
                                  ▼
                       ┌──────────────────────────┐
                       │ l2_tx_buffer_pool        │
                       │ N × 2 KiB; generation-   │
                       │ counter ring free-list   │
                       │ (alloc head / free tail) │
                       └──────────────────────────┘
```

## 3. Memreq additions

Add to `regions.yaml` for each arch:

```yaml
- name: l2_tx_pending
  size: 0x180          # 16 entries × 24 bytes + 64-byte header
  tier: hot            # producer-side cache locality matters
  alignment: 64        # one cache line for the header
- name: l2_tx_buffer_pool
  size: 0x8000         # 16 × 2 KiB buffers
  tier: warm           # only the dispatcher touches buffer
                       # bytes; producers only address via
                       # pool indices
  alignment: 4096      # page-aligned for memcpy ergonomics
```

Exact sizes are tunable. Start small (16 entries / buffers) to
match RX_FRAME_BUDGET; size up when measured backpressure
demands it.

## 4. Request entry — 24 bytes per ring slot

```
struct l2_tx_request {                  // offset
    u32 seq;                            //   0  Vyukov MPSC sequence
    u32 reserved_0;                     //   4  (alignment / future flags)
    u8  dst_mac[6];                     //   8
    u16 ethertype;                      //  14  BE wire order
    u32 payload_pool_idx;               //  16  index into l2_tx_buffer_pool
    u16 payload_len;                    //  20  bytes of payload (no Eth hdr)
    u16 reserved_1;                     //  22
};
```

24 bytes lets four entries fit per cache line (64 B). The `seq`
field at the top of each slot is the Vyukov publication signal
— see §6.

Rationale for `payload_pool_idx` (vs raw address):
- Pool index is producer-arch-agnostic (no 32-bit vs 64-bit
  address surprises for cross-arch verification)
- The dispatcher resolves index → address using
  `l2_tx_buffer_pool` base + idx * 2048
- Smaller field — 32 bits is excessive for ≤16 buffers, but
  reserve future room

## 5. Ring header — 64 bytes (one cache line)

```
struct l2_tx_pending_header {           // offset
    atomic_u32 head;                    //   0  producer claim counter
    u32        padding_0;               //   4  (separate cache line from tail)
    u32        tail;                    //   8  consumer-only
    u32        reserved;                //  12
    u8         padding_1[48];           //  16  fill to 64 bytes
};
```

`head` and `tail` are in the same cache line for our size
(64-byte cache lines, both fields in first 16 bytes). On future
architectures where false sharing between producer and consumer
hurts, split into two lines — for now the simplicity wins.

## 6. Producer protocol — Vyukov MPSC bounded queue

The classic Vyukov scheme adapted to AArch64 / x86_64. Pre-init
(at boot): for each slot `i`, `seq[i] = i`.

```
producer_enqueue(request):
    while True:
        pos  = atomic_load(head, acquire)
        slot = pos & RING_MASK
        seq  = atomic_load(ring[slot].seq, acquire)
        diff = seq - pos
        if diff == 0:
            # slot ready for THIS producer
            if CAS(head, pos, pos + 1):
                break              # claimed
            # else: another producer won; retry
        elif diff < 0:
            return FULL            # ring full; caller's problem
        # else: another producer is mid-publish; retry

    # Write request fields (NOT seq yet)
    ring[slot].dst_mac        = request.dst_mac
    ring[slot].ethertype      = request.ethertype
    ring[slot].payload_pool_idx = request.payload_pool_idx
    ring[slot].payload_len    = request.payload_len

    # Publish via seq update (release semantics)
    atomic_store(ring[slot].seq, pos + 1, release)
```

**AArch64 primitives:**
- `LDAXR` (load-acquire exclusive) / `STLXR` (store-release
  exclusive) for the CAS loop
- `LDAR` (load-acquire) for the wait-for-seq load
- `STLR` (store-release) for the publish

**x86_64 primitives:**
- `LOCK CMPXCHG` for the CAS
- TSO orders the wait-for-seq read against subsequent payload
  writes naturally; no explicit barrier needed
- The publish `MOV` is automatically release-ordered under TSO
  against prior payload writes

**ABA:** not possible here because `head` increments
monotonically (modulo 2^32 — wraps after 4 G enqueues, by
which time `tail` has long since caught up).

## 7. Consumer protocol — dispatcher TX phase

The L2 dispatcher's TX phase (`.l2_drained` on x86_64,
`.Lrx_drained` on aarch64) drains the pending queue up to
`TX_REQUEST_BUDGET` requests per call, same shape as
`RX_FRAME_BUDGET`.

```
tx_consume_phase:
    consumed = 0
    pos = tail              # consumer-only, no atomic needed
    while consumed < TX_REQUEST_BUDGET:
        slot = pos & RING_MASK
        seq  = LDAR(ring[slot].seq)
        if seq != pos + 1:
            break                          # nothing newer
        # Read request fields
        dst, etype, pool_idx, plen = ring[slot]
        # Build frame in TX virtio buffer
        tx_buf = allocate_tx_virtio_buffer()
        build_frame(tx_buf, dst, etype, pool_idx, plen)
        submit_virtio_tx(tx_buf)
        # Release pool buffer back to free list
        l2_tx_buffer_pool_free(pool_idx)
        # Release ring slot for next round
        STLR(ring[slot].seq, pos + 1 + RING_SIZE)
        pos += 1
        consumed += 1
    tail = pos              # writeback (consumer-only)
```

Frame construction (`build_frame`) does the actual L2 work:
- 12 bytes virtio_net_hdr zero-fill
- 6 bytes dst MAC from request
- 6 bytes GUEST_MAC (constant — TX src MAC is always us)
- 2 bytes ethertype from request (BE)
- `payload_len` bytes copied from `l2_tx_buffer_pool[pool_idx]`
- Zero-pad to a wire minimum of 60 bytes (= 72 virtio incl. hdr)

Per ETH-012/013: padding is zero bytes. Per ETH-015 (incoming
src MAC unicast check, already implemented) and ETH-016
(outgoing src MAC sanity — build-time assertion, see §10):
GUEST_MAC has bit 0 of byte 0 = 0.

## 8. ARP responder integration — through-queue

Today the ARP responder is a special path baked into the
dispatcher: on RX ARP match, the dispatcher sets `arp_match`,
stashes the requester's MAC/IP, and the TX phase directly
populates `tx_arp_buf` and submits.

Under the through-queue model, the ARP responder becomes a
**producer** like any other:

```
on RX ARP match:
    pool_idx = l2_tx_buffer_pool_alloc()
    build_arp_reply_payload(pool, pool_idx, request.SHA, request.SPA)
    producer_enqueue(l2_tx_request {
        dst_mac        = request.SHA
        ethertype      = 0x0806
        payload_pool_idx = pool_idx
        payload_len    = 28          # ARP payload size
    })
```

The TX phase doesn't need to know which producer enqueued —
ARP replies, L3 IP frames, and test-baked requests all flow
through the same drain code. Removes the `arp_match` flag and
the `.l2_use_canary` / `.l2_have_tx_addr` branches from the
dispatcher.

**Cost:** one extra ring entry + one extra pool buffer per ARP
reply. Tens of cycles. Acceptable for the architectural
simplification.

**Benefit:** the dispatcher's TX phase becomes spec-clean (one
path, one purpose). When L3 adds a second producer, no new
dispatcher branch is needed.

## 9. TX buffer pool — generation counter ring free-list

Free-list mechanism for the 16-buffer `l2_tx_buffer_pool`,
mirroring virtio ring shape:

```
struct l2_tx_buffer_pool_header {
    atomic_u32 next_alloc;       // producer claim counter
    u32        padding_0;
    u32        next_free;        // dispatcher-only release counter
    u32        reserved;
    u8         padding_1[48];    // fill to 64 bytes
};
```

Layout: a ring of 16 indices `{0, 1, 2, ..., 15}` that buffers
cycle through. `next_alloc - next_free` = buffers currently in
flight.

**Producer alloc:**
```
allocate():
    while True:
        pos = atomic_load(next_alloc, acquire)
        if pos - atomic_load(next_free, acquire) >= POOL_SIZE:
            return FULL                       # ring full
        if CAS(next_alloc, pos, pos + 1):
            return pos & POOL_MASK
```

**Consumer (dispatcher) free** — after virtio TX:RECLAIMED for
this buffer:
```
free(idx):
    # idx will equal next_free & POOL_MASK in FIFO order
    # (consumer is single, so no atomic needed)
    next_free += 1
```

**Symmetry:** identical generation-counter shape to the
virtio descriptor ring (Virtio 1.2 §2.7.13). One mental
model for both — pool index = "which buffer slot", virtio
desc index = "which descriptor slot", both produced by
monotonic increment, both consumed FIFO.

## 10. ETH-016 — outgoing src MAC sanity (build-time)

GUEST_MAC = `02:00:00:00:00:01`. Byte 0 = 0x02, bit 0 = 0
→ unicast. ETH-016 says we MUST NOT emit a frame with a
multicast / broadcast source MAC.

Since our TX always uses GUEST_MAC as src (the frame
construction in §7 hardcodes this — no producer-supplied
src), the check is a one-line build-time assertion next to
the GUEST_MAC constant:

```
GUEST_MAC_BYTE_0 equ 0x02

%if GUEST_MAC_BYTE_0 & 1
    %error "ETH-016: GUEST_MAC byte 0 has multicast bit set"
%endif
```

aarch64 GNU as equivalent:
```
.equ GUEST_MAC_BYTE_0, 0x02
.if GUEST_MAC_BYTE_0 & 1
    .error "ETH-016: GUEST_MAC byte 0 has multicast bit set"
.endif
```

Doable independently of the TX API (no runtime infrastructure
needed). Should land as a small standalone commit whenever
the GUEST_MAC constants are next touched.

## 11. Testing approach — boot-baked TX requests

The host-side pytest harness can't reach into guest memory
after Firecracker boots. To test the TX API, pre-populate the
ring with test requests at link time, one per test boot.

```python
# tooling/tests/integration/test_eth_tx_padding.py — sketch
def test_short_frame_padded_to_60(firecracker_guest_with_tx_request, ...):
    """ETH-012: payload < 46 bytes → frame padded to 60 wire bytes."""
    request = TxRequest(
        dst_mac="02:00:00:00:00:42",  # host MAC
        ethertype=0x88B5,
        payload=b"\xAB" * 10,         # 14 + 10 = 24 wire, must pad to 60
    )
    captured = launch_with_tx_request(request)
    assert len(captured[0]) == 60, "frame must be padded to 60 wire"
    assert captured[0][24:60] == b"\x00" * 36, "padding must be zero"
```

`launch_with_tx_request` (new fixture/helper) is the only new
machinery. Implementation sketch:
1. Build a small "tx-request init" `.S` stub that places the
   request bytes at the start of `l2_tx_pending` (slot 0,
   seq=1 = "ready for consume") and points the boot code to
   skip ARP recognition for the test build
2. Test fixture writes Python-side test data into a known
   memreq region OR rebuilds the guest with the test stub
   linked in (slower but more flexible)

Decision deferred until first implementation; both approaches
fit the harness model.

## 12. Open design questions (revisit at implementation)

- **Backpressure when pending queue full:** producer's
  `enqueue()` returns FULL. What does L3 do? Drop, retry, or
  buffer at L3 level? Decision likely L3-policy not L2-API.
- **TX completion notification:** when do producers learn that
  TX:RECLAIMED fired for their buffer? Simplest: producer
  doesn't care (fire-and-forget). For TCP retransmit logic
  it eventually matters; add a per-request completion callback
  ID then.
- **Priority:** does ARP-reply preempt L3 data when both are
  pending? Today's ARP responder runs in the same dispatch as
  the recognition; under through-queue it doesn't.
  Likely not worth solving until we see contention.
- **Multi-queue (VIRTIO_NET_F_MQ):** rejected in feature
  negotiation today. When MQ lands, do TX rings shard per
  flow, or stay unified? Probably unified at L2; L3 picks
  the queue.

## 13. References

- Virtio 1.2 §2.7 (virtq layout), §5.1.6 (virtio-net frame
  format)
- Dmitry Vyukov, *Bounded MPMC queue*
  http://www.1024cores.net/home/lock-free-algorithms/queues/bounded-mpmc-queue
  (single-consumer reduction is what we use here)
- IEEE 802.3-2018 §3.2.7 (min/max frame size), §4.1.2.1 (src
  MAC unicast)
- Jean-Yves Astier, *Finite State Automaton for I/O
  Containers*, HyperPanel Lab — the engine framing for the
  dispatcher
- D012 / D043 (this project's decision log) — FSA engine
  framing as adopted here

## 14. Status / next steps

- This document committed: design captured.
- **ETH-016 (build-time GUEST_MAC sanity):** doable
  immediately; small standalone commit. Tracked separately.
- **TX request queue + pool implementation:** wait until L3
  starts (first real consumer). Until then, every design
  choice above is unverified guesswork.
- **ETH-012 / ETH-013 tests:** wait until TX API exists.
- **ARP-responder migration to through-queue:** part of the
  TX API implementation pass, not a separate cleanup.

When L3 work opens, re-read this doc against the L3 first
draft and revise any choices that no longer match real
consumer needs. Lab-notebook discipline: design notes are a
snapshot, not contract.
