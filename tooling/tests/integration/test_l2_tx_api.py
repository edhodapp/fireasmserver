"""TX API end-to-end test — phase b.4 of D068 working order.

The production firecracker boot.S, when assembled with
`-DTXAPI_PREBAKE=1`, enqueues one TX API request before
entering the dispatch loop. The dispatcher's TX consume
phase drains the queue on the first iteration and submits
a wire frame to the host's virtio-net device, which lands
on tap0 where this test's sniffer catches it.

The pre-bake's payload is the ASCII string "TXAPI-TEST"
(10 bytes), distinct from the canary frame's 46 bytes of
0xAB, so the two streams are trivially separable in the
capture even though they share dst MAC + src MAC + ethertype.

This validates the full chain:
    producer (l2_tx_enqueue) →
    Vyukov ring (l2_tx_pending) →
    consumer (dispatcher TX phase) →
    frame builder (virtio_net_hdr + Eth header in pool buffer) →
    virtio-net device submit →
    tap0 capture.

A pass here closes phase (b) of D068's working order. A
miss localises to either the dispatcher consumer (compare
markers + a sane canary frame still arriving) or the
producer (no enqueue → consumer's queue-empty branch ran,
no extra frame at all).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from l2_harness.capture import capturing
from l2_harness.firecracker import (
    FirecrackerConfig,
    launched_guest,
)
from l2_harness.serial import SerialLog


REPO_ROOT = Path(__file__).resolve().parents[3]
TXAPI_GUEST_ELF = (
    REPO_ROOT / "arch" / "x86_64" / "build"
    / "firecracker_txapi" / "guest.elf"
)

# Pre-bake's payload: ASCII "TXAPI-TEST" (10 bytes) at +PAYLOAD_OFFSET.
EXPECTED_PAYLOAD = b"TXAPI-TEST"

# Pre-bake's ethertype: 0x88B5 (IEEE Local Experimental 1). Matches
# the canary so a single BPF filter picks up both streams.
EXPECTED_ETHERTYPE = 0x88B5

# Source MAC = GUEST_MAC = 02:00:00:00:00:01 (same as canary).
EXPECTED_SRC_MAC = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])

# Destination MAC = broadcast (same as canary).
EXPECTED_DST_MAC = bytes([0xFF] * 6)

# Capture window. The first dispatch iteration runs within a few
# hundred ms of guest boot; 3 s is generous, ample to also see the
# canary stream while not blocking the test suite.
CAPTURE_TIMEOUT_SECONDS = 3.0

# Quiesce after the marker fires before stopping the sniffer. The
# guest emits TX:RECLAIMED as soon as the virtio device acks the
# descriptor; tap0's host-side netif_rx is on a different kernel
# path and may lag by a few ms. Without this, the with-block can
# exit before the frame lands on tap0. Same pattern as the
# test_eth_src_mac POST_MARKER_QUIESCE_SECONDS.
POST_MARKER_QUIESCE_SECONDS = 0.5


@pytest.fixture(scope="session")
def _ensure_txapi_built() -> None:
    """Build firecracker_txapi via `make` — always invoke.

    Don't short-circuit on `TXAPI_GUEST_ELF.exists()`: that
    would consume any stale binary at the path regardless of
    whether it was built with `TXAPI_PREBAKE=1`. Make is fast
    on a no-op build (sub-second when nothing changed) and is
    the authoritative dependency tracker. Per Gemini MED on
    the phase (d) review.
    """
    subprocess.run(
        ["make", "-C",
         str(REPO_ROOT / "arch" / "x86_64"),
         "PLATFORM=firecracker", "TXAPI_PREBAKE=1"],
        check=True,
    )
    if not TXAPI_GUEST_ELF.exists():
        pytest.fail(
            f"TXAPI build claimed success but {TXAPI_GUEST_ELF} "
            "is missing"
        )


# pylint: disable=unused-argument,invalid-name
def test_txapi_pre_baked_frame_arrives_on_tap0(
    _ensure_txapi_built: None,
    tap_iface: str,
    artifact_dir: Path,
) -> None:
    """The pre-baked TX API request lands as a frame on tap0.

    Asserts:
      1. The dispatcher emits TX:RECLAIMED within the window
         (consumer + virtio submit + reclaim all completed).
      2. At least one frame with payload "TXAPI-TEST" arrives
         on tap0 within the capture window.

    Both assertions together prove the full producer → ring →
    consumer → virtio → tap0 chain works.

    Launch order matters here: the sniffer MUST be running
    before Firecracker is launched, because the pre-baked TX
    request lands on the wire within ~100 ms of guest boot,
    well before the typical capture-setup time. The standard
    `firecracker_guest` fixture launches before the test
    body, which would race the early TX frame. So this test
    builds its own launch sequence: enter capturing, then
    launch the guest inside the capture context.
    """
    cfg = FirecrackerConfig(
        kernel_image_path=TXAPI_GUEST_ELF,
        artifact_dir=artifact_dir,
    )
    capture_pcap = artifact_dir / "capture.pcap"

    dst_mac_str = ":".join(f"{b:02x}" for b in EXPECTED_DST_MAC)
    bpf = (
        f"ether dst {dst_mac_str} "
        f"and ether proto 0x{EXPECTED_ETHERTYPE:04x}"
    )
    with capturing(
        iface=tap_iface,
        bpf_filter=bpf,
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=capture_pcap,
    ) as cap, launched_guest(cfg) as guest:
        serial = SerialLog(guest.serial_log_path)
        # Wait for the first TX:RECLAIMED — confirms the
        # dispatcher submitted SOMETHING successfully. Doesn't
        # discriminate canary vs queue; that's what the pcap
        # check below is for.
        serial.assert_marker_observed(
            "TX:RECLAIMED",
            timeout=CAPTURE_TIMEOUT_SECONDS,
        )
        time.sleep(POST_MARKER_QUIESCE_SECONDS)

    matching = [
        p for p in cap.packets
        if EXPECTED_PAYLOAD in bytes(p)
    ]
    assert matching, (
        f"Expected at least one frame on {tap_iface} with payload "
        f"{EXPECTED_PAYLOAD!r}; got {len(cap.packets)} matching the "
        f"dst+etype filter, none with the queue-drain payload. "
        f"Pcap: {capture_pcap}"
    )

    # Belt-and-suspenders: also sanity-check the src MAC. A
    # frame with the right payload but wrong src would indicate
    # the consumer's frame builder wrote the wrong source — a
    # subtle bug that the payload check alone wouldn't catch.
    frame = bytes(matching[0])
    # virtio-net hdr is host-stripped by the time the host sees
    # the wire frame on tap0. So the captured bytes start at the
    # Eth header: dst (6) + src (6) + etype (2) + payload.
    src_in_frame = frame[6:12]
    assert src_in_frame == EXPECTED_SRC_MAC, (
        f"Frame payload matched but src MAC was "
        f"{src_in_frame.hex(':')}; expected "
        f"{EXPECTED_SRC_MAC.hex(':')}"
    )

    # --- ETH-012: TX-side padding to 60-wire minimum ---
    #
    # The pre-bake's payload is 10 bytes ("TXAPI-TEST"), well
    # below the 46-byte payload threshold that would make the
    # wire frame meet the 60-byte minimum without padding. The
    # dispatcher's TX consumer's frame builder must pad the
    # rest. Asserting the on-wire length is exactly 60 bytes
    # rejects both under-pad (frame < 60) and over-pad
    # (anything > 60 in a runt payload case implies the desc
    # length math drifted).
    assert len(frame) == 60, (
        f"ETH-012: runt frame must be padded to exactly "
        f"60 wire bytes; observed {len(frame)} bytes. Pcap: "
        f"{capture_pcap}"
    )

    # --- ETH-013: TX padding is zero-filled ---
    #
    # The pad bytes (from end-of-payload through byte 59)
    # must be all 0x00 — IEEE 802.3 §4.1.2.1 doesn't strictly
    # require zeros (any value is spec-legal) but zero pad is
    # the universal convention and the only behavior that
    # doesn't leak adjacent buffer contents on the wire. A
    # nonzero pad here would indicate the frame builder
    # accidentally wrote stale buffer bytes through.
    payload_end = 14 + len(EXPECTED_PAYLOAD)        # Eth header + payload
    pad = frame[payload_end:]
    assert pad == b"\x00" * len(pad), (
        f"ETH-013: pad bytes after payload must be zero; "
        f"observed {pad.hex()} ({len(pad)} bytes). Pcap: "
        f"{capture_pcap}"
    )
