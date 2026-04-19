# Layer-2 Requirements Tracker

Living document, arch-neutral (same requirements apply to x86_64 and
AArch64 L2 implementations). Updated in every commit that touches L2
code or tests. **If an L2 feature is implemented without a row here,
that is a process bug** — audit against this file periodically.

Conceptually parallel to `DECISIONS.md`, but where `DECISIONS.md`
records *why we chose* one architectural path over another, this file
records *what external standards we must conform to* and our status
against each.

## Status legend

| Status | Meaning |
|--------|---------|
| `spec` | Captured from upstream standard; no project code or test exists yet. |
| `tested` | A test case asserts the behavior. Test may be passing or failing — see Notes. |
| `implemented` | Assembly implements the behavior AND the test passes. |
| `deviation` | Deliberately NOT implemented. Notes MUST cite a `DECISIONS.md` entry that explains why. |
| `N/A` | Requirement in the standard, but does not apply to our scope (e.g., PHY-layer autonegotiation — we ride virtio-net). Notes MUST state why. |

## ID scheme

`<AREA>-<NNN>`. AREA:

| AREA | Standard family |
|------|-----------------|
| `ETH` | IEEE 802.3 Ethernet MAC framing |
| `VLAN` | IEEE 802.1Q / 802.1ad VLAN tagging |
| `ARP` | IETF RFC 826 / 5227 Address Resolution Protocol |
| `VIO` | OASIS Virtio v1.2 virtio-net device |
| `MAC` | Ethernet-address semantics (OUI, LSb locally-administered bit, etc.) |

Numbers are stable — once assigned, a requirement ID does not change.
Superseded rows are marked `deviation` with a pointer to the newer row
(analogous to how `DECISIONS.md` handles supersession).

---

## Scope for v0 of fireasmserver L2

Scope-defining decisions landed and cross-referenced from here:

- **Deployment target is virtio-net inside a Firecracker microVM.** No
  physical PHY, no autonegotiation, no cabling. Physical-layer clauses
  of 802.3 are `N/A`.
- **Role is endpoint (host), not switch or bridge.** Spanning Tree
  (802.1D), MSTP (802.1s), LACP-as-switch (802.1AX) are out of scope.
- **VLAN scope** — deliberately open. To be resolved in the L2 design
  doc per D039 item 4; rows seeded below as `spec` so the question is
  tracked.

---

## 1. Ethernet II framing — IEEE 802.3

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `ETH-001` | Frame layout: 6-byte DA, 6-byte SA, 2-byte EtherType/Length, payload, 4-byte FCS. | 802.3-2018 §3.1.1 | spec | |
| `ETH-002` | EtherType ≥ `0x0600` identifies Ethernet II; smaller values are 802.3 Length + LLC header. We support Ethernet II only. | 802.3-2018 §3.2.6, RFC 894 | spec | LLC/SNAP (RFC 1042) out of scope. |
| `ETH-003` | Minimum frame size: 64 bytes including FCS (512 bit-times). | 802.3-2018 §3.2.7 | spec | Relaxed to 60 if driver strips FCS. |
| `ETH-004` | Maximum frame size: 1518 bytes untagged, 1522 with one VLAN tag. | 802.3-2018 §3.2.7 | spec | Jumbo-frame rules tracked separately. |
| `ETH-005` | FCS = CRC-32 over DA..payload, polynomial `0xedb88320` reflected. | 802.3-2018 §3.2.9 | spec | Virtio-net typically offloads FCS. |
| `ETH-006` | MUST accept broadcast DA `FF:FF:FF:FF:FF:FF`. | 802.3-2018 §4.2.2 | spec | |
| `ETH-007` | MUST accept multicast DA (group-bit-set) matching configured filter set. | 802.3-2018 §4.2.2 | spec | Virtio-net multicast MAC filter list. |
| `ETH-008` | MUST accept unicast DA equal to the device's assigned MAC. | 802.3-2018 §4.2.2 | spec | |
| `ETH-009` | MUST discard frames with incorrect FCS. | 802.3-2018 §4.2.4 | spec | Typically signaled by virtio host. |
| `ETH-010` | MUST discard runt frames (<64 bytes incl. FCS). | 802.3-2018 §3.2.7 | spec | Guard against runt-length L2 attacks. |
| `ETH-011` | MUST discard oversized frames (>1518 untagged, >1522 tagged, >jumbo cap if negotiated). | 802.3-2018 §3.2.7 | spec | |
| `ETH-012` | MUST pad short outgoing frames to the 64-byte minimum. | 802.3-2018 §4.2.3 | spec | Pad byte value unspecified; convention is zero. |
| `ETH-013` | SHOULD zero-fill padding bytes. | convention | spec | Conservative choice; avoids info leaks. |
| `ETH-014` | Inter-frame gap: 96 bit-times at the negotiated link speed. | 802.3-2018 §4.2.3.3 | `N/A` | Virtio abstracts PHY; host handles. |
| `ETH-015` | Source MAC MUST have unicast bit clear (LSb of first byte = 0). | 802.3-2018 §4.1.2.1 | spec | |
| `ETH-016` | MUST NOT emit a frame with source MAC = broadcast or multicast. | 802.3-2018 §4.1.2.1 | spec | Sanity check at TX. |
| `ETH-017` | MAC address locally-administered bit (2nd LSb of first byte) is informational only; we don't treat L/A MACs differently. | IEEE 802c | spec | |
| `ETH-018` | MUST silently discard Ethernet PAUSE frames (EtherType `0x8808`, MAC control opcode `0x0001`) and increment a `rx_pause_dropped` counter. | 802.3x-1997 §31B.1 | spec | Acting on received pause is a separate flow-control module, explicitly deferred per D045's "stays deferred" list. |

## 2. VLAN tagging — IEEE 802.1Q

Per D045 (superseding D044), VLAN parsing is designed in at MVP and
runtime-inert by default. The RX parser unconditionally handles
tagged frames by skipping the tag and extracting VID into per-frame
metadata; TX insertion is opt-in via `tx_request_t.vid`. See
`docs/l2/DESIGN.md` §5 for the parser layout.

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `VLAN-001` | 4-byte tag inserted after SA: TPID=`0x8100`, TCI = 3-bit PCP + 1-bit DEI + 12-bit VID. | 802.1Q-2022 §9.6 | spec | RX parses unconditionally; TX inserts when `tx_request_t.vid != 0`. |
| `VLAN-002` | Tagged-frame EtherType field is at byte offset 16 (not 12). | 802.1Q-2022 §9.5 | spec | RX re-reads EtherType at offset 16 after tag detection. |
| `VLAN-003` | VID `0x000` = priority-tagged (no VLAN membership); `0xFFF` reserved. | 802.1Q-2022 §9.6.1 | spec | VID extracted into per-frame metadata regardless. |
| `VLAN-004` | PCP field maps to 802.1p priority classes 0–7. | 802.1Q-2022 §6.9 | spec | Propagated into per-frame metadata; QoS routing is upper-layer. |
| `VLAN-005` | MUST silently discard tagged frames on a port that is not VLAN-capable. | 802.1Q-2022 §8 | deviation | D045 reverses this behavior — we parse tagged frames rather than discard. Kept for historical reference; obsoleted by the D045 design. |
| `VLAN-006` | 802.1ad Q-in-Q (outer TPID `0x88A8`). | 802.1ad-2005 | spec | RX recognizes outer + inner tag; extracts both VIDs to metadata. TX can emit Q-in-Q when two `vid` fields are non-zero (MVP default: one-level tagging). |
| `VLAN-007` | 802.1Qbb Priority Flow Control (PFC). | 802.1Qbb | deviation | D045 keeps PFC deferred — additive feature, doesn't reshape parser. |
| `VLAN-008` | VLAN filter management via control queue (`VIRTIO_NET_CTRL_VLAN_ADD` / `_DEL`). | Virtio 1.2 §5.1.6.5.3 | deviation | D045 keeps filter management deferred — additive on already-designed control-queue interface. MVP accepts all VIDs. |

## 3. ARP — IETF RFC 826 / 5227

ARP operates at L2.5 but shares frame handling with the L2 driver, so
it's tracked here rather than in an L3 doc.

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `ARP-001` | ARP frame EtherType = `0x0806`. | RFC 826 | spec | |
| `ARP-002` | ARP packet: HTYPE=1 (Ethernet), PTYPE=`0x0800` (IPv4), HLEN=6, PLEN=4. | RFC 826 | spec | |
| `ARP-003` | OP=1 is REQUEST, OP=2 is REPLY. | RFC 826 | spec | |
| `ARP-004` | MUST answer an ARP REQUEST whose Target Protocol Address equals a local IP. | RFC 826 §3 | spec | |
| `ARP-005` | MUST update cache entry on REPLY from an address we have a pending REQUEST for. | RFC 826 §3 | spec | |
| `ARP-006` | SHOULD also update cache on any ARP packet whose Sender HW/Protocol Addresses are in the cache (opportunistic). | RFC 826 §3 | spec | |
| `ARP-007` | MUST NOT add a cache entry solely on observed traffic — only on ARP packets. | RFC 826 §3 | spec | |
| `ARP-008` | Cache entries MUST age out after an implementation-defined TTL. | RFC 826 §3 (implicit) | spec | Convention: 20 min for complete, 3 min for incomplete. |
| `ARP-009` | SHOULD emit gratuitous ARP on interface-up / IP-assignment (ACD per RFC 5227). | RFC 5227 §2.1.1 | spec | |
| `ARP-010` | SHOULD defend local IP against conflicting claim (RFC 5227 §2.4). | RFC 5227 §2.4 | spec | |
| `ARP-011` | MUST NOT respond to REQUEST whose Target Protocol Address is not a local IP. | RFC 826 §3 | spec | |

## 4. virtio-net — OASIS Virtio v1.2

This is the concrete device we implement against. The densest section
of this file by design.

### 4.1 Device initialization (§2.1.2, §5.1.5)

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `VIO-001` | On init, reset the device (write 0 to Device Status register). | Virtio 1.2 §2.1.2 step 1 | spec | |
| `VIO-002` | Set `ACKNOWLEDGE` (bit 0) in status. | Virtio 1.2 §2.1.2 step 2 | spec | |
| `VIO-003` | Set `DRIVER` (bit 1) in status. | Virtio 1.2 §2.1.2 step 3 | spec | |
| `VIO-004` | Read device feature bits; select a subset; write driver feature bits. | Virtio 1.2 §2.1.2 step 4 | spec | |
| `VIO-005` | Set `FEATURES_OK` (bit 3) in status. | Virtio 1.2 §2.1.2 step 5 | spec | |
| `VIO-006` | Re-read status; MUST abort init if `FEATURES_OK` is not still set. | Virtio 1.2 §2.1.2 step 6 | spec | Host rejected our feature set. |
| `VIO-007` | Read device-specific config, discover + initialize virtqueues. | Virtio 1.2 §2.1.2 step 7 | spec | |
| `VIO-008` | Set `DRIVER_OK` (bit 2) in status; device is now live. | Virtio 1.2 §2.1.2 step 8 | spec | |
| `VIO-009` | On any fatal driver error, set `FAILED` (bit 7) in status. | Virtio 1.2 §2.1.2 | spec | |

### 4.2 Feature negotiation (§5.1.3)

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `VIO-F-001` | MUST negotiate `VIRTIO_F_VERSION_1` (bit 32) — we are a modern driver. | Virtio 1.2 §6 | spec | Legacy/transitional not supported. |
| `VIO-F-002` | MUST negotiate `VIRTIO_NET_F_MAC` (bit 5) if device offers it, and read MAC from config space. | Virtio 1.2 §5.1.3 | spec | Otherwise random local MAC. |
| `VIO-F-003` | MAY negotiate `VIRTIO_NET_F_STATUS` (bit 16) to observe link state. | Virtio 1.2 §5.1.3 | spec | |
| `VIO-F-004` | MAY negotiate `VIRTIO_NET_F_MQ` (bit 22) for multi-queue. | Virtio 1.2 §5.1.3 | deviation-candidate | MVP single-queue; design-doc decision. |
| `VIO-F-005` | MAY negotiate `VIRTIO_NET_F_CTRL_VQ` (bit 17) to expose the control queue. | Virtio 1.2 §5.1.3 | spec | Needed for MAC-filter / MQ / VLAN config. |
| `VIO-F-006` | SHOULD NOT negotiate `VIRTIO_NET_F_CSUM` / `F_GUEST_CSUM` unless our TCP/UDP code handles partial checksums. | Virtio 1.2 §5.1.3 | deviation | Defer to L4 layer decisions. |
| `VIO-F-007` | MUST NOT negotiate `VIRTIO_F_EVENT_IDX` (bit 29) unless we implement the event-suppression protocol correctly. | Virtio 1.2 §2.6.7 | spec | Optimization; may skip for MVP. |

### 4.3 Virtqueue layout (§2.7, split virtqueue)

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `VIO-Q-001` | Split virtqueue has three areas: Descriptor Table, Available Ring, Used Ring. | Virtio 1.2 §2.7 | spec | Packed queues out of MVP scope. |
| `VIO-Q-002` | Descriptor Table: array of 16-byte descriptors (addr, len, flags, next). | Virtio 1.2 §2.7.5 | spec | |
| `VIO-Q-003` | Descriptor flags: `NEXT=1`, `WRITE=2`, `INDIRECT=4`. | Virtio 1.2 §2.7.5.1 | spec | |
| `VIO-Q-004` | Available Ring: idx + ring[] of descriptor-table indices; updated by driver. | Virtio 1.2 §2.7.6 | spec | |
| `VIO-Q-005` | Used Ring: idx + ring[] of used-descriptor + length; updated by device. | Virtio 1.2 §2.7.8 | spec | |
| `VIO-Q-006` | Queue size MUST be power-of-2, ≤ queue_size (from device), queried from Common config. | Virtio 1.2 §4.1.4.3 | spec | |
| `VIO-Q-007` | Descriptor Table alignment: 16 bytes; Available Ring: 2 bytes; Used Ring: 4 bytes. | Virtio 1.2 §2.7 | spec | Modern device uses `queue_desc` / `queue_driver` / `queue_device` registers. |
| `VIO-Q-008` | Proper memory barriers around ring-index updates (see §2.7.11). | Virtio 1.2 §2.7.11 | spec | Critical: cross-references D039 §2 (DMA/cache coherence). |

### 4.4 Receive path (§5.1.6.3)

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `VIO-R-001` | Receive queue index = 0 (single-queue) or 0,2,4,... (MQ). | Virtio 1.2 §5.1.2 | spec | |
| `VIO-R-002` | Pre-populate RX queue with buffers marked `WRITE`. | Virtio 1.2 §5.1.6.3 | spec | |
| `VIO-R-003` | Each incoming packet is prefixed with a `virtio_net_hdr` (length depends on negotiated features). | Virtio 1.2 §5.1.6 | spec | |
| `VIO-R-004` | `virtio_net_hdr.flags`, `.gso_type`, `.hdr_len`, `.gso_size`, `.csum_start`, `.csum_offset`, `.num_buffers` — our handler MUST at minimum read `num_buffers`. | Virtio 1.2 §5.1.6.1 | spec | |
| `VIO-R-005` | MUST handle multi-descriptor RX when `num_buffers > 1` (VIRTIO_NET_F_MRG_RXBUF). | Virtio 1.2 §5.1.6.3.1 | spec | |
| `VIO-R-006` | After consuming a descriptor, return it to the Available Ring. | Virtio 1.2 §2.7.13 | spec | |
| `VIO-R-007` | Notify the device of returned buffers via Queue Notify register (unless `EVENT_IDX` suppresses it). | Virtio 1.2 §2.7.12 | spec | |

### 4.5 Transmit path (§5.1.6.2)

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `VIO-T-001` | Transmit queue index = 1 (single-queue) or 1,3,5,... (MQ). | Virtio 1.2 §5.1.2 | spec | |
| `VIO-T-002` | Each outgoing packet MUST be preceded by a `virtio_net_hdr`. | Virtio 1.2 §5.1.6.2 | spec | |
| `VIO-T-003` | If `VIRTIO_NET_F_CSUM` not negotiated, `virtio_net_hdr.flags.NEEDS_CSUM` MUST be 0. | Virtio 1.2 §5.1.6.2 | spec | Consistent with `VIO-F-006` deviation. |
| `VIO-T-004` | If GSO features not negotiated, `virtio_net_hdr.gso_type` MUST be `GSO_NONE`. | Virtio 1.2 §5.1.6.2 | spec | MVP: no GSO. |
| `VIO-T-005` | Submit descriptor chain: (hdr, payload_0, payload_1, …) with only the first writable flag clear (RX flag doesn't apply at TX). | Virtio 1.2 §2.7.5 | spec | |
| `VIO-T-006` | Wait for Used Ring index advancement, then reclaim buffers. | Virtio 1.2 §2.7.8 | spec | |

### 4.6 Control path — `VIRTIO_NET_F_CTRL_VQ` (§5.1.6.5)

Only in scope if `VIO-F-005` is negotiated. Used for MAC filter, VLAN
filter, MQ configuration, announce.

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `VIO-C-001` | Control queue index = `MAX_QUEUE_PAIRS * 2` (or 2 for single-queue). | Virtio 1.2 §5.1.2 | spec | |
| `VIO-C-002` | Command format: class + cmd + data + ack; device writes `ack` byte at end. | Virtio 1.2 §5.1.6.5 | spec | |
| `VIO-C-003` | SHOULD support `VIRTIO_NET_CTRL_MAC_TABLE_SET` to populate the multicast filter (paired with `VIO-F-005` + `VIRTIO_NET_F_CTRL_RX`). | Virtio 1.2 §5.1.6.5.2 | spec | |
| `VIO-C-004` | If VLAN in scope (§2 above), SHOULD support `VIRTIO_NET_CTRL_VLAN_ADD` / `_DEL`. | Virtio 1.2 §5.1.6.5.3 | spec | |

---

## 5. Cross-cutting

| ID | Requirement | Source | Status | Notes |
|----|-------------|--------|--------|-------|
| `MAC-001` | MAC address format: 6 bytes, big-endian on the wire. | IEEE 802-2014 | spec | |
| `MAC-002` | OUI (first 3 bytes) assigned by IEEE. Locally administered MACs have bit 1 of first byte set. | IEEE 802-2014 | spec | |
| `MAC-003` | Multicast MACs: first byte LSb = 1. Broadcast = `FF:FF:FF:FF:FF:FF`. | IEEE 802-2014 | spec | |
| `MAC-004` | Multicast IPv4 mapping: `01:00:5E:<lower-23-of-IP>`. | RFC 1112 | spec | For L3-side, but L2 filter must accept. |
| `MAC-005` | Multicast IPv6 mapping: `33:33:<lower-32-of-IPv6>`. | RFC 2464 | spec | |

---

## Process notes

- **Every L2 pull request** must update this file: new tests flip a row
  from `spec` → `tested`; code that passes that test flips to
  `implemented`. A `deviation` row must cite a `DECISIONS.md` ID.
- **Coverage audit**: before declaring L2 done, every row is either
  `implemented`, `deviation`, or `N/A`. No `spec` or `tested` rows left.
- **Per-arch column (later):** when x86_64 is `implemented` but
  AArch64 is still `tested`, add columns `status-x86_64` / `status-aarch64`
  to the relevant tables. Until then, `Status` means "either arch" and
  per-arch deltas live in git history.
- **Living doc:** requirements get added as we discover them during
  implementation. Absence from this file doesn't license absence from
  the code — it's a request to add the row too.

## Out of scope for fireasmserver L2

- PHY layer (cabling, autonegotiation, MII/RMII) — virtio abstracts.
- Wake-on-LAN, EEE, PoE — not applicable to virtio endpoints.
- Bridging (802.1D/s), LAG-as-switch — we are an endpoint.
- EAPOL / 802.1X port authentication — environment-specific.
- IEEE 802.1AS time synchronization — deferred.
