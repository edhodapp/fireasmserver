"""L2 dispatcher gate-order verification (PICT-style coverage).

Every existing L2 integration test isolates ONE invariant
violation per frame: ETH-010 sends a runt-size frame with
valid dst/src/ethertype; ETH-006 sends a wrong-dst frame with
valid size/src/ethertype; etc. That's the right shape for
proving each gate fires when it's the sole violation.

The gate ORDER, however, is also part of the dispatcher's
contract. From `arch/<isa>/l2/dispatcher.S`:

  1. size bounds      [72, 1530] virtio  → RX:DROP used_len=…
  2. dst MAC filter   GUEST or mc/bc     → RX:DROP mac
  3. src MAC unicast  byte 0 bit 0 = 0   → RX:DROP src
  4. PAUSE EtherType  != 0x8808          → RX:DROP pause
  5. ARP recognition  ethertype 0x0806   → ARP:REQUEST (+reply)
                      + TPA == GUEST_IP
  6. RX:FRAME emit (accept)

A future refactor that reorders these would still pass every
single-violation test in the suite. Multi-violation frames are
what catch the reordering.

This file parametrises cases where TWO OR MORE gates would
each fire on the same frame, and asserts the EARLIER gate's
marker fires while the LATER gate's marker doesn't. The PICT
name is loose (we're not generating strict pairwise coverage)
but the spirit is the same — combinatorial coverage of gate
interactions that single-axis tests miss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from l2_harness import frames
from l2_harness.capture import FrameSender, capturing
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.frames import (
    BROADCAST_MAC,
    GUEST_DEFAULT_MAC,
    GUEST_DEFAULT_IP,
    HOST_DEFAULT_MAC,
    arp_request,
    raw_eth_frame,
)
from l2_harness.serial import SerialLog
from l2_harness.tap0 import host_mtu_of


MARKER_TIMEOUT_SECONDS = 1.5
POST_MARKER_QUIESCE_SECONDS = 0.3

# Wire sizes — see existing test_eth_size_bounds.py for the
# 72/1530 virtio-side bounds these map to.
SIZE_RUNT_WIRE = 44               # < 60 → size drop, virtio used_len=0x38
SIZE_VALID_MIN_WIRE = 60          # boundary OK
SIZE_VALID_TYPICAL_WIRE = 60      # used most often below
SIZE_OVERSIZE_WIRE = 1614         # > 1518 → size drop, used_len=0x65A

# MAC choices.
WRONG_UNICAST_MAC = "02:00:00:00:00:99"
MULTICAST_SRC_MAC = "03:00:00:00:00:42"
MULTICAST_DST_MAC = "33:33:00:00:00:01"
MAC_CONTROL_DST_MAC = "01:80:c2:00:00:01"

# EtherTypes.
ETHERTYPE_PAUSE = 0x8808
ETHERTYPE_ARP = 0x0806
ETHERTYPE_OTHER = 0x88B5         # local-experimental

# IP literals (per regions / boot config).
WRONG_TARGET_IP = "192.168.42.99"

# Tests sending >1518 wire bytes need tap0 MTU bumped (default
# 1500 caps wire frames at 1514). Shared with the existing
# ETH-011 oversize test — operators run ~/bin/fireasm-tap0-up
# to set MTU 2000 at boot.
OVERSIZE_REQUIRED_MTU = 1700


@dataclass(frozen=True)
class Case:
    """One PICT case: input frame + expected/forbidden markers."""

    case_id: str
    description: str
    wire_len: int
    dst_mac: str
    src_mac: str
    ethertype: int
    # ARP-specific: if set, build an ARP request payload with
    # this target IP. Otherwise build a raw frame with a 0xAB
    # payload sized to wire_len.
    arp_target_ip: str | None
    # Substring that MUST appear in the serial log (the
    # earlier-gate marker the dispatcher is expected to emit).
    expected_marker: str
    # Substrings that MUST NOT appear (later-gate markers that
    # the gate ordering should prevent).
    forbidden_markers: tuple[str, ...]
    # If set, the test SKIPs when tap0 MTU is below this value.
    requires_mtu: int | None = None


def _virtio_used_len_hex(wire_bytes: int) -> str:
    """Format the `used_len=` marker suffix for a wire size."""
    return f"used_len={(wire_bytes + 12):08X}"


def _runt_marker() -> str:
    return f"RX:DROP {_virtio_used_len_hex(SIZE_RUNT_WIRE)}"


def _oversize_marker() -> str:
    return f"RX:DROP {_virtio_used_len_hex(SIZE_OVERSIZE_WIRE)}"


# pylint: disable=line-too-long
CASES: tuple[Case, ...] = (
    # --- size > everything else ---
    Case(
        case_id="runt-overrides-wrong-dst",
        description=(
            "Runt frame to a wrong unicast MAC: size gate fires "
            "first → RX:DROP used_len, NOT RX:DROP mac."
        ),
        wire_len=SIZE_RUNT_WIRE,
        dst_mac=WRONG_UNICAST_MAC,
        src_mac=HOST_DEFAULT_MAC,
        ethertype=ETHERTYPE_OTHER,
        arp_target_ip=None,
        expected_marker=_runt_marker(),
        forbidden_markers=("RX:DROP mac", "RX:DROP src",
                           "RX:DROP pause", "ARP:REQUEST"),
    ),
    Case(
        case_id="runt-overrides-multicast-src",
        description=(
            "Runt frame to GUEST_MAC with multicast src: size "
            "gate fires first → RX:DROP used_len, NOT RX:DROP src."
        ),
        wire_len=SIZE_RUNT_WIRE,
        dst_mac=GUEST_DEFAULT_MAC,
        src_mac=MULTICAST_SRC_MAC,
        ethertype=ETHERTYPE_OTHER,
        arp_target_ip=None,
        expected_marker=_runt_marker(),
        forbidden_markers=("RX:DROP mac", "RX:DROP src",
                           "RX:DROP pause", "ARP:REQUEST"),
    ),
    Case(
        case_id="runt-overrides-pause",
        description=(
            "Runt PAUSE frame: size gate fires before PAUSE "
            "EtherType gate → RX:DROP used_len, NOT RX:DROP pause."
        ),
        wire_len=SIZE_RUNT_WIRE,
        dst_mac=MAC_CONTROL_DST_MAC,
        src_mac=HOST_DEFAULT_MAC,
        ethertype=ETHERTYPE_PAUSE,
        arp_target_ip=None,
        expected_marker=_runt_marker(),
        forbidden_markers=("RX:DROP mac", "RX:DROP src",
                           "RX:DROP pause", "ARP:REQUEST"),
    ),
    Case(
        case_id="oversize-overrides-wrong-dst",
        description=(
            "Oversize frame to a wrong unicast MAC: size gate "
            "fires first → RX:DROP used_len, NOT RX:DROP mac."
        ),
        wire_len=SIZE_OVERSIZE_WIRE,
        dst_mac=WRONG_UNICAST_MAC,
        src_mac=HOST_DEFAULT_MAC,
        ethertype=ETHERTYPE_OTHER,
        arp_target_ip=None,
        expected_marker=_oversize_marker(),
        forbidden_markers=("RX:DROP mac", "RX:DROP src",
                           "RX:DROP pause", "ARP:REQUEST"),
        requires_mtu=OVERSIZE_REQUIRED_MTU,
    ),
    # --- dst MAC > src/PAUSE/ARP ---
    Case(
        case_id="wrong-dst-overrides-multicast-src",
        description=(
            "Wrong-dst unicast frame with multicast src: MAC "
            "filter fires before src check → RX:DROP mac, NOT "
            "RX:DROP src."
        ),
        wire_len=SIZE_VALID_TYPICAL_WIRE,
        dst_mac=WRONG_UNICAST_MAC,
        src_mac=MULTICAST_SRC_MAC,
        ethertype=ETHERTYPE_OTHER,
        arp_target_ip=None,
        expected_marker="RX:DROP mac",
        forbidden_markers=("RX:DROP src", "RX:DROP pause",
                           "ARP:REQUEST"),
    ),
    Case(
        case_id="wrong-dst-overrides-pause-etype",
        description=(
            "Wrong-dst unicast frame with PAUSE ethertype: "
            "MAC filter fires before PAUSE gate → RX:DROP mac, "
            "NOT RX:DROP pause. (Real PAUSE frames go to the "
            "multicast 01:80:C2:00:00:01 — using a unicast dst "
            "with the PAUSE etype is malformed but exercises "
            "the order.)"
        ),
        wire_len=SIZE_VALID_TYPICAL_WIRE,
        dst_mac=WRONG_UNICAST_MAC,
        src_mac=HOST_DEFAULT_MAC,
        ethertype=ETHERTYPE_PAUSE,
        arp_target_ip=None,
        expected_marker="RX:DROP mac",
        forbidden_markers=("RX:DROP src", "RX:DROP pause",
                           "ARP:REQUEST"),
    ),
    Case(
        case_id="wrong-dst-overrides-arp",
        description=(
            "Wrong-dst unicast ARP request to our IP: MAC "
            "filter fires before ARP recognition → RX:DROP mac, "
            "NOT ARP:REQUEST. Real ARP requests broadcast; "
            "sending one unicast to the wrong MAC tests the "
            "gate."
        ),
        wire_len=SIZE_VALID_TYPICAL_WIRE,
        dst_mac=WRONG_UNICAST_MAC,
        src_mac=HOST_DEFAULT_MAC,
        ethertype=ETHERTYPE_ARP,
        arp_target_ip=GUEST_DEFAULT_IP,
        expected_marker="RX:DROP mac",
        forbidden_markers=("RX:DROP src", "RX:DROP pause",
                           "ARP:REQUEST"),
    ),
    # --- src MAC > PAUSE/ARP ---
    Case(
        case_id="multicast-src-overrides-pause",
        description=(
            "PAUSE frame from a multicast src MAC: src gate "
            "fires before PAUSE gate → RX:DROP src, NOT "
            "RX:DROP pause. (PAUSE dst is multicast, so the "
            "MAC filter accepts via that branch; the src check "
            "is the next gate.)"
        ),
        wire_len=SIZE_VALID_TYPICAL_WIRE,
        dst_mac=MAC_CONTROL_DST_MAC,
        src_mac=MULTICAST_SRC_MAC,
        ethertype=ETHERTYPE_PAUSE,
        arp_target_ip=None,
        expected_marker="RX:DROP src",
        forbidden_markers=("RX:DROP mac", "RX:DROP pause",
                           "ARP:REQUEST"),
    ),
    Case(
        case_id="multicast-src-overrides-arp",
        description=(
            "ARP request from a multicast src MAC, broadcast "
            "dst: src gate fires before ARP recognition → "
            "RX:DROP src, NOT ARP:REQUEST."
        ),
        wire_len=SIZE_VALID_TYPICAL_WIRE,
        dst_mac=BROADCAST_MAC,
        src_mac=MULTICAST_SRC_MAC,
        ethertype=ETHERTYPE_ARP,
        arp_target_ip=GUEST_DEFAULT_IP,
        expected_marker="RX:DROP src",
        forbidden_markers=("RX:DROP mac", "RX:DROP pause",
                           "ARP:REQUEST"),
    ),
    # --- ARP recognition reaches accept on non-matching TPA ---
    Case(
        case_id="arp-to-wrong-tpa-reaches-rx-frame",
        description=(
            "ARP request to a wrong target IP: ARP recognition "
            "doesn't match → frame reaches RX:FRAME emit with "
            "no ARP:REQUEST marker and no drop. Distinct from "
            "the wrong-dst case above because dst is broadcast "
            "(MAC filter accepts via multicast bit)."
        ),
        wire_len=SIZE_VALID_TYPICAL_WIRE,
        dst_mac=BROADCAST_MAC,
        src_mac=HOST_DEFAULT_MAC,
        ethertype=ETHERTYPE_ARP,
        arp_target_ip=WRONG_TARGET_IP,
        # Match on the frame-specific `used_len` suffix only —
        # the leading id varies with descriptor allocation
        # (whichever virtio slot served this RX). The
        # surrounding RX:FRAME marker shape is implicit.
        expected_marker=_virtio_used_len_hex(SIZE_VALID_TYPICAL_WIRE),
        forbidden_markers=("RX:DROP", "ARP:REQUEST"),
    ),
)


@dataclass
class CaseFrameBuilder:
    """Helper: build a wire frame from a Case spec."""

    case: Case
    payload: bytes = field(init=False)
    frame: bytes = field(init=False)

    def __post_init__(self) -> None:
        if self.case.arp_target_ip is not None:
            # Use the existing ARP helper which produces a
            # 60-byte padded frame. Verify the wire length
            # matches what the case expects; if it doesn't,
            # the test setup itself has a bug (caller picked
            # an ARP case with a non-60 wire_len).
            arp = arp_request(
                target_ip=self.case.arp_target_ip,
                sender_ip="192.168.42.1",
                sender_mac=self.case.src_mac,
            )
            # arp_request always returns a frame addressed to
            # BROADCAST_MAC. For PICT cases that want a
            # different dst (unicast-to-wrong-mac with ARP
            # etype), we rewrite the dst bytes in-place.
            dst_bytes = bytes(
                int(b, 16) for b in self.case.dst_mac.split(":")
            )
            self.frame = dst_bytes + arp[6:]
            if len(self.frame) != self.case.wire_len:
                raise ValueError(
                    f"{self.case.case_id}: ARP frame is "
                    f"{len(self.frame)} wire bytes, case "
                    f"expected {self.case.wire_len}"
                )
            self.payload = b""    # n/a for ARP case
        else:
            self.payload = b"\xAB" * (self.case.wire_len - 14)
            self.frame = raw_eth_frame(
                dst_mac=self.case.dst_mac,
                src_mac=self.case.src_mac,
                ethertype=self.case.ethertype,
                payload=self.payload,
            )
            assert len(self.frame) == self.case.wire_len


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_gate_order(
    case: Case,
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """Verify dispatcher gate order — earlier-gate marker fires,
    later-gate markers don't."""
    if case.requires_mtu is not None:
        mtu = host_mtu_of("tap0")
        if mtu is None or mtu < case.requires_mtu:
            pytest.skip(
                f"{case.case_id} needs tap0 MTU >= "
                f"{case.requires_mtu}; current MTU={mtu}. "
                "Bump with: sudo ip link set tap0 mtu 2000"
            )

    builder = CaseFrameBuilder(case=case)

    captured_pcap = artifact_dir / f"captured-{case.case_id}.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=MARKER_TIMEOUT_SECONDS + POST_MARKER_QUIESCE_SECONDS,
        pcap_path=captured_pcap,
    ):
        frame_sender.send(builder.frame)
        serial_log.assert_marker_observed(
            case.expected_marker, timeout=MARKER_TIMEOUT_SECONDS,
        )
        # All forbidden markers must be absent through a small
        # quiescence window past the expected marker — late
        # firings of "shouldn't fire" gates are exactly what we
        # want to catch.
        for forbidden in case.forbidden_markers:
            serial_log.assert_marker_absent(
                forbidden, window=POST_MARKER_QUIESCE_SECONDS,
            )
    # Helpful debug: dump the case description on failure.
    # (pytest's failure trace already shows case.case_id via
    # the parametrize ids; description here is a comment-only
    # hint for the reader of the failure logs.)
    _ = frames  # silence unused-import warning (kept for parity)
