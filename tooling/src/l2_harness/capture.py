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
from scapy.sendrecv import AsyncSniffer, sendp
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
    exit stops it (asynchronously, no wait-for-timeout) and
    exposes the captured frames as a list. Writes the captured
    frames to a pcap file when configured.

    Backed by `scapy.AsyncSniffer` (task #34 refactor on bd7aa1f's
    successor) — replaces the prior `threading.Thread` +
    `scapy.sniff` pattern that blocked __exit__ for the FULL
    timeout window. The async sniffer responds to .stop() within
    one scapy-internal poll tick (~10 ms), letting tests exit as
    soon as their body finishes rather than waiting out the
    capture window.

    Captured packets are appended via a prn callback (kept
    from the prior implementation, NOT AsyncSniffer's default
    store=True), so `self.packets` is live during the with
    block — useful for tests that want to inspect the capture
    in-progress.
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
        self._sniffer: AsyncSniffer | None = None
        self._started_event = threading.Event()

    @property
    def packets(self) -> list[Packet]:
        """Captured packets — populated live via prn callback."""
        return self._packets

    def __enter__(self) -> "FrameCapturer":
        # AsyncSniffer handles its own background thread; we
        # just pass the same prn / store / started_callback /
        # timeout we used to pass to sniff(). store=False keeps
        # AsyncSniffer.results empty (we use prn for everything
        # so we have a live view rather than a post-hoc one).
        self._sniffer = AsyncSniffer(
            iface=self._iface,
            filter=self._bpf_filter,
            timeout=self._timeout,
            prn=self._packets.append,
            store=False,
            started_callback=self._started_event.set,
        )
        self._sniffer.start()
        # Block until scapy has bound its AF_PACKET socket —
        # otherwise any reply that arrives in the (small but
        # non-zero) socket-setup window is missed. The 2026-05-22
        # ARP-reply intermittent-drop failure mode that motivated
        # adding this gate is unchanged by the AsyncSniffer
        # refactor; we still need it.
        signalled = self._started_event.wait(
            SNIFF_STARTUP_TIMEOUT_SECONDS,
        )
        # AsyncSniffer exposes a captured-exception attribute
        # (.exception) populated by its thread on failure. If
        # the sniffer crashed before firing started_callback,
        # _started_event stays unset; if it crashed AFTER, we
        # might see signalled=True but .exception non-None.
        # Check both paths.
        if self._sniffer.exception is not None:
            raise RuntimeError(
                "AsyncSniffer failed during startup"
            ) from self._sniffer.exception
        if not signalled:
            raise RuntimeError(
                f"AsyncSniffer did not call started_callback "
                f"within {SNIFF_STARTUP_TIMEOUT_SECONDS}s — "
                "raw-socket setup may be wedged"
            )
        return self

    def _stop_sniffer(self) -> None:
        """Stop the AsyncSniffer if it's still running.

        .stop() signals the sniff loop to exit at its next poll
        tick (~10 ms) AND joins the thread. Replaces the prior
        `thread.join(timeout=sniff_timeout+1)` that waited the
        FULL capture window. Treat a stop-after-natural-timeout
        race as benign — we have whatever the prn callback
        collected.
        """
        if self._sniffer is None or not self._sniffer.running:
            return
        try:
            self._sniffer.stop(join=True)
        except Exception:  # pylint: disable=broad-except
            pass

    def _write_pcap(self) -> None:
        """Flush captured packets to the configured pcap path.

        append=True so a test pointing BOTH a FrameSender AND
        this FrameCapturer at the same path doesn't overwrite
        the sender's already-written frames.
        """
        if self._pcap_path is not None:
            wrpcap(str(self._pcap_path), self._packets,
                   append=True)

    def _maybe_raise_sniffer_error(self, test_body_exc: object) -> None:
        """Surface a sniffer exception iff the test body didn't
        already raise. Masking the test's own failure with an
        infrastructure error confuses the failure signal more
        than it helps.
        """
        if (self._sniffer is not None
                and self._sniffer.exception is not None
                and test_body_exc is None):
            raise RuntimeError(
                "AsyncSniffer raised during capture"
            ) from self._sniffer.exception

    def __exit__(self, *exc_info: object) -> None:
        self._stop_sniffer()
        self._write_pcap()
        self._maybe_raise_sniffer_error(exc_info[1])


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
