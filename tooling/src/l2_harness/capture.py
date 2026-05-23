"""Frame send + capture on tap0 for L2 integration tests.

Thin wrappers over scapy's `sendp` (write to interface) and
`sniff` (read from interface with BPF filter + timeout). Tests
use the context-manager `FrameCapturer` pattern so capture
starts BEFORE the test sends its stimulus — otherwise a fast
guest reply could land before sniffing began and be missed.

Per `docs/l2/HARNESS.md` §3.4.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager

from scapy.layers.l2 import Ether
from scapy.packet import Packet
from scapy.sendrecv import sendp, sniff
from scapy.utils import wrpcap


SNIFF_STARTUP_TIMEOUT_SECONDS = 2.0
"""How long to wait for scapy.sniff to call its `started_callback`.

If exceeded the context manager raises rather than silently
running a test with no live capture. Empirically scapy starts
in well under 50ms even on a busy laptop; 2s is generous.
"""


class FrameSender:
    """Sends a raw frame onto an interface (typically tap0).

    Wraps scapy's `sendp` with the interface bound at
    construction time. Optionally writes the sent frame to a
    pcap file for the artifact directory (diagnostic visibility
    on test failure per HARNESS.md §8).
    """

    def __init__(self, iface: str = "tap0",
                 pcap_path: Path | None = None) -> None:
        self._iface = iface
        self._pcap_path = pcap_path

    def send(self, raw: bytes) -> None:
        """Inject `raw` (full Ethernet frame) onto the iface."""
        pkt = Ether(raw)
        sendp(pkt, iface=self._iface, verbose=False)
        if self._pcap_path is not None:
            wrpcap(str(self._pcap_path), [pkt], append=True)

    def send_burst(self, raw_list: list[bytes]) -> None:
        """Inject a list of raw frames in one scapy.sendp call.

        Single-call delivery is much faster than N separate
        `send` calls because scapy amortises the AF_PACKET
        socket setup across all packets. For burst tests (e.g.,
        FSA-4 budget exhaustion), the higher density is what
        guarantees the guest's RX ring actually holds at least
        RX_FRAME_BUDGET frames at consume-loop entry — rather
        than trickling in one-at-a-time at a rate the guest can
        keep up with iteratively.
        """
        pkts = [Ether(r) for r in raw_list]
        sendp(pkts, iface=self._iface, verbose=False)
        if self._pcap_path is not None:
            wrpcap(str(self._pcap_path), pkts, append=True)


class FrameCapturer:
    """Background sniffer with a BPF filter + timeout.

    Used as a context manager: enter starts the sniff thread,
    exit stops it and exposes the captured frames as a list.
    Writes the captured frames to a pcap file when configured,
    even on the empty-capture path (so the test artifact dir
    always has a `captured.pcap` to look at on failure).
    """

    def __init__(self,
                 iface: str = "tap0",
                 bpf_filter: str = "arp",
                 timeout: float = 1.0,
                 pcap_path: Path | None = None) -> None:
        self._iface = iface
        self._bpf_filter = bpf_filter
        self._timeout = timeout
        self._pcap_path = pcap_path
        self._packets: list[Packet] = []
        self._thread: threading.Thread | None = None
        self._started_event = threading.Event()

    @property
    def packets(self) -> list[Packet]:
        """Captured packets (empty until __exit__ has run)."""
        return self._packets

    def __enter__(self) -> "FrameCapturer":
        self._thread = threading.Thread(
            target=self._sniff_blocking,
            daemon=True,
        )
        self._thread.start()
        # Block until scapy.sniff has actually bound its
        # AF_PACKET socket and is ready to receive — otherwise
        # any reply that arrives in the (small but non-zero)
        # socket-setup window is missed. The empirical 2026-05-22
        # failure mode under the harness was exactly this:
        # captures intermittently dropped the guest's ARP reply
        # because send happened before sniff was bound.
        if not self._started_event.wait(SNIFF_STARTUP_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"sniff thread did not call started_callback "
                f"within {SNIFF_STARTUP_TIMEOUT_SECONDS}s — "
                "raw-socket setup may be wedged or scapy version "
                "doesn't support started_callback"
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._thread is not None:
            # sniff has a `timeout=` so it exits on its own;
            # join with a small grace window. If it's still alive
            # after that, the sniffer is wedged — leave the
            # daemon to die with the process rather than blocking
            # the test teardown.
            self._thread.join(timeout=self._timeout + 1.0)
        if self._pcap_path is not None:
            wrpcap(str(self._pcap_path), self._packets)

    def _sniff_blocking(self) -> None:
        captured = sniff(
            iface=self._iface,
            filter=self._bpf_filter,
            timeout=self._timeout,
            store=True,
            started_callback=self._started_event.set,
        )
        self._packets = list(captured)


@contextmanager
def capturing(iface: str = "tap0",
              bpf_filter: str = "arp",
              timeout: float = 1.0,
              pcap_path: Path | None = None,
              ) -> Iterator[FrameCapturer]:
    """Sugar over FrameCapturer for `with capturing(...) as cap:`."""
    cap = FrameCapturer(iface=iface, bpf_filter=bpf_filter,
                        timeout=timeout, pcap_path=pcap_path)
    with cap:
        yield cap
