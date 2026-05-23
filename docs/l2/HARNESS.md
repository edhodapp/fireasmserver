# L2 Integration Test Harness — Design

**Purpose:** specify the Tier B test infrastructure described in
[`TEST_PLAN.md`](TEST_PLAN.md) §0. The harness boots a fireasmserver
guest under Firecracker with a tap-backed virtio-net device,
injects frames onto the tap from the host side, captures frames
egressing the tap from the guest side, reads the guest's serial
output, and asserts against the L2 protocol spec.

**Drives:** the transition of every "planned" row in TEST_PLAN.md
§9 with tier `B` to `passing`. Until each such row passes via
this harness, the corresponding requirement is not verified at
the production bar adopted 2026-05-22.

**Scope of this document:** the Python harness itself. The tests
that use it live in `tooling/tests/integration/` and are
catalogued in TEST_PLAN.md sections 1-7; this document describes
the fixtures, the lifecycle, and the protocol-frame plumbing the
tests build on. It is NOT a re-spec of the test cases.

**Status:** design only. Implementation begins after Ed signs off
on the open questions in §9.

---

## 1. Goals

1. **Real protocol verification, not marker-chain inference.**
   Tests assert against the bytes that ingress and egress the tap
   device. A test passes only if the wire-observable behavior
   matches the spec — guest-emitted serial markers are evidence,
   not proof.
2. **Reproducibility.** A test that passes on the developer's
   laptop must pass in CD (modulo the documented cell-skip rules
   for unavailable hardware). No timing-dependent assertions
   without explicit budgets and retries.
3. **Isolation.** A test must not contaminate the next test's
   state. tap0 returns to a known idle state; Firecracker
   processes terminate; sockets and temp files are cleaned up
   even on test failure.
4. **Same quality bar as production code.** flake8
   `--max-complexity=5`, pylint with the Google rc, mypy
   `--strict`, pytest `--cov --cov-branch` with 100% target.
   pydantic `BaseModel` for any structured data.
5. **Diagnosable failures.** When a test fails, the harness
   captures the inbound frames the test sent, the outbound
   frames the guest emitted (or didn't), the guest's full serial
   log, and the Firecracker side-channel log. All four are
   accessible from the pytest failure output without re-running.

## 2. Non-goals

- **Replacement of the tracer-bullet.** The tracer-bullet stays
  as a coarse "did the boot path execute the full marker chain"
  smoke test. It is not the layer-completion gate (that's this
  harness); it is the build-sanity gate.
- **Unit-testing assembly internals.** Tier A tests (host-side
  unit tests on assembly modules) are out of scope here. They
  use the existing `tooling/qemu_harness/` and dedicated drivers.
- **Performance assertion.** This harness verifies correctness.
  Performance verification (Tier B perf tests, D040 ratchet) is
  a separate addition, layered on top of this one.
- **L3+.** This harness is L2-only. When TCP/HTTP land, they get
  their own integration layer (likely above scapy's L3 stack,
  not below it).

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│ pytest test function (e.g., test_arp_request_reply)     │
│                                                          │
│   1. Construct ARP request frame (scapy)                │
│   2. send_frame(req)                                    │
│   3. captured = capture_frames(filter=arp, timeout=1s)  │
│   4. assert captured[0].op == ARP_REPLY                 │
│   5. assert captured[0].psrc == GUEST_IP                │
│   6. assert captured[0].hwsrc == GUEST_MAC              │
└────────────┬────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────┐
│ Harness public API (`tooling/src/l2_harness/`)          │
│                                                          │
│  Fixtures: firecracker_guest, tap0_iface,               │
│            frame_sender, frame_capturer, serial_log     │
│                                                          │
│  Frame helpers: arp_request(), eth_frame(),             │
│                 with_padding()                          │
└────────────┬────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────┐
│ External tools                                           │
│                                                          │
│  - firecracker (subprocess, --no-api --config-file)     │
│  - tap0 (pre-persistent or ephemeral-via-sudo)          │
│  - scapy.sendp / scapy.sniff for tap frame I/O          │
│  - tail of Firecracker's serial-output file             │
└─────────────────────────────────────────────────────────┘
```

### 3.1 Components

| Component | Responsibility | Module |
|-----------|---------------|--------|
| `Firecracker` | Subprocess lifecycle, config-file generation, /var/run/firecracker-* cleanup. Yields when the guest's `READY` marker is observed (timeout-bounded). | `l2_harness/firecracker.py` |
| `Tap0` | Verify tap0 exists at expected IP/netmask; ephemeral creation under sudo if missing; clear ARP cache between tests. | `l2_harness/tap0.py` |
| `FrameSender` | scapy `sendp` on `tap0`. Knows the test's GUEST_MAC so it can construct sane Ethernet headers. | `l2_harness/frames.py` |
| `FrameCapturer` | scapy `sniff` on tap0 with filter expression + timeout. Returns a list of captured frames. Context-manager so capture starts before send. | `l2_harness/capture.py` |
| `SerialLog` | Tail Firecracker's serial-output file; expose blocking `wait_for_marker()` and snapshot `text()` methods. | `l2_harness/serial.py` |

### 3.2 Test lifecycle

```
session_setup:
    verify tap0 (or create ephemeral)

per-test:
    build firecracker config
    launch firecracker, wait for READY marker (5s budget)
    yield (test runs)
    SIGTERM firecracker, wait for exit (2s budget; SIGKILL after)
    delete firecracker side-channel files
    snapshot serial log into test artifact dir on failure
    flush host ARP cache for guest IP
    flush host neighbor table entry for guest IP

session_teardown:
    if ephemeral tap0: delete it
```

Firecracker is launched **per test** in the MVP. A future
optimization (§10) is session-scoped Firecracker with a guest
"reset" command, but that requires guest cooperation we haven't
designed.

### 3.3 Privilege model

Tap raw-frame I/O requires `CAP_NET_RAW` or root. Three
candidate approaches:

| Approach | Pros | Cons |
|----------|------|------|
| Run `pytest` under `sudo` | Simple; matches existing `arping` behavior. | Pollutes the user's environment; CD-friendly with passwordless sudo, which we already require. |
| `setcap CAP_NET_RAW+ep $(which python3.X)` | No sudo at test time. | Global side effect on the Python interpreter; breaks if Python is upgraded. |
| Setuid-root helper binary | Minimal sudo surface; helper does only the raw I/O. | More code to maintain; extra IPC layer. |

**Recommendation:** start with `sudo pytest` for the MVP. The
existing CD pre-push pipeline already requires passwordless sudo
for tap0 ephemeral creation, so this isn't new ground. Revisit
if developer ergonomics suffer.

**Status (2026-05-22+):** adopted the `setcap` path. One-time
setup on the developer's venv:

    sudo setcap cap_net_raw+eip $(readlink -f .venv/bin/python3)

The `setcap` is applied to the actual interpreter binary (the
symlink target of `.venv/bin/python3`, typically a per-user
custom Python build under `~/python/...`), not to a per-venv
copy. File capabilities don't propagate across hard / symbolic
links, so the kernel checks caps on the resolved binary.

#### Known limitation: AT_SECURE strips environment hints

Linux marks a process as `AT_SECURE=1` when it executes a
binary with file capabilities. CPython detects this via
`PyConfig.use_environment = 0` (driven indirectly by the
init logic that respects `secure_getenv`) and IGNORES every
`PYTHON*` environment variable on startup. glibc's loader
similarly strips `LD_LIBRARY_PATH` (outside default trusted
dirs), `LD_PRELOAD`, and `LD_AUDIT` from the environment.

Practical consequences for the L2 harness:

- `.venv/bin/activate`-based site-packages discovery still
  works, because the venv's `pyvenv.cfg` is read from the
  filesystem (not env) and the venv's `bin/python3` symlink
  chain establishes the site-packages search path via
  Python's compiled-in defaults.
- `PYTHONPATH` (used by some pytest plugins for discovering
  test fixtures via env, instead of via `pyproject.toml`) is
  silently ignored. Plugins that need this won't load.
- `PYTHONHOME` (alternative site-packages root) is silently
  ignored. Custom Python distributions that rely on this
  break.
- `PYTHONUSERBASE` (per-user site-packages override) is
  silently ignored. `pip install --user` paths don't get
  picked up.
- `PYTHONDEVMODE`, `PYTHONFAULTHANDLER`, `PYTHONIOENCODING`,
  and other CPython runtime knobs are all ignored.

For the project's own local development this is fine — the
venv is the source of truth and we don't rely on env-driven
plugin discovery. **For hosted CI** (when we eventually run
these tests on GHA), a different approach is needed:

1. **Run pytest under `sudo`** instead of granting caps. Loses
   the developer-ergonomic no-sudo run path but keeps env
   intact. Requires passwordless sudo on the runner.
2. **Use a setuid-root helper binary** that does only the raw
   I/O on pytest's behalf. Keeps the test process unprivileged
   and env-intact; adds an IPC layer.
3. **Use ambient capabilities** (`prctl(PR_CAP_AMBIENT_RAISE)`)
   to propagate CAP_NET_RAW from a privileged wrapper to the
   pytest child. Less common; needs the runner to support it.

No CI integration is in scope yet — when it lands we choose
based on runner constraints.

#### Verifying the cap is still in place

A `pip install --upgrade` of the venv's Python interpreter
(e.g., a python3.11 → python3.12 rebuild) silently loses the
capability. The pre-push gate's `run_l2_integration_tests`
detects this via the conftest's `has_root_or_capability`
probe and SKIPS with a visible warning rather than failing
the push opaquely. To restore:

    sudo setcap cap_net_raw+eip \
        $(readlink -f .venv/bin/python3)

Verify:

    getcap $(readlink -f .venv/bin/python3)
    # should print: ... cap_net_raw=eip

### 3.3a Other one-time operator prereqs

In addition to the privilege grant, the harness needs:

1. **`tap0` configured at `192.168.42.1/24`.**
   ```
   sudo ip tuntap add dev tap0 mode tap user $USER
   sudo ip link set tap0 up
   sudo ip addr add 192.168.42.1/24 dev tap0
   ```

2. **`tap0` MTU bumped to ≥ 1700.** The kernel's AF_PACKET raw
   send refuses frames larger than the device MTU (errno
   EMSGSIZE). The default tap0 MTU is 1500, which caps wire
   frames at 1514 bytes — below the ETH-003 oversize threshold
   of 1518. Tests that need to send oversize stimuli (e.g.,
   `test_oversize_frame_dropped`) skip cleanly if MTU is too
   low; bump once with:
   ```
   sudo ip link set tap0 mtu 2000
   ```

   The increased MTU is purely a host-side TX permission ceiling
   — it does NOT change Firecracker's virtio-net negotiated MTU
   nor the guest's view of the frame size. The L2 wire-size
   bounds in the dispatcher (60..1518 wire) are unaffected.

### 3.4 Frame send/capture mechanism

scapy. Rationale:

- High-level frame construction (`Ether()/ARP()/...`) avoids
  hand-built byte arrays for the test code, keeping tests
  focused on behavior not byte twiddling.
- `sniff()` with filter expressions is well-trodden territory.
- Already widely available in Python integration test suites.

Negative-test edge cases that need byte-level control (corrupt
headers, oversize frames) drop down to scapy's `Raw()` layer
plus explicit byte construction. The harness provides helpers
for the common patterns (`oversize_frame()`,
`with_truncated_ethertype()`, etc.).

scapy is installed via the venv. Adding it to `pyproject.toml`'s
test dependencies.

### 3.5 Serial log access

Firecracker writes guest serial output to a file path set in the
config (`/tmp/<id>/serial.log` by convention). The harness opens
this file at launch and tails it for marker assertions.

API:

- `serial.wait_for(marker: str, timeout: float = 1.0) -> bool` —
  block until the substring appears or timeout.
- `serial.text() -> str` — snapshot the full log so far.
- `serial.assert_marker_chain(markers: list[str], ordered: bool = True)`
  — convenience for full-chain validation.

## 4. Test types covered (per TEST_PLAN.md §3 ARP, §4 virtio-net)

The harness must support each of:

- **Single-shot request/response.** Send a frame, expect a frame.
  ARP-001..ARP-004, ARP-011.
- **Send-and-expect-silence.** Send a frame, assert no matching
  reply within timeout. ARP-004 (wrong-IP), ARP-011 (non-local).
- **Stream behavior.** Send N frames, assert N counter increments
  or all-replied. VIO-R-001..VIO-R-007 multi-frame consume.
- **Negative bytes.** Send malformed/oversize/short frames,
  assert guest doesn't crash and emits a specific drop marker.
  ETH-003 (oversize), ETH-013 (runt), fuzz seeds (Tier C, not
  scope here but the harness must serve them).
- **Persistent state.** Send sequence A, observe state X; send
  sequence B, observe X has updated. Persistent shadow correctness
  across reentrant dispatch.
- **Identity matching.** Variable GUEST_MAC and GUEST_IP across
  tests would be ideal; for MVP we use the hardcoded values and
  document configurability as a follow-up (§9.5).

## 5. Quality bar

Mirror the existing tooling/ package conventions:

```
tooling/src/l2_harness/
├── __init__.py
├── firecracker.py
├── tap0.py
├── frames.py
├── capture.py
└── serial.py

tooling/tests/integration/
├── conftest.py
├── test_arp_request_reply.py
├── test_validation_paths.py
├── test_multi_frame_rx.py
├── test_persistent_shadows.py
└── ...
```

Gates per CLAUDE.md:

- `flake8 --max-complexity=5`
- `pylint --rcfile=~/.claude/pylintrc` (Google style)
- `mypy --strict`
- `pytest --cov --cov-branch` (100% target on `l2_harness/`)

The tests themselves don't need 100% branch coverage in the same
sense (they're the thing being measured against the guest); the
harness library does.

## 6. CD integration

- **Pre-commit:** lint/type-check the harness package alongside
  other Python.
- **Pre-push:** the L2 integration cell runs the full integration
  test suite for x86_64/firecracker. Slow (~30s per test budgeted
  initially); expected total budget ~5 min for full coverage.
  Replaces the tracer-bullet's "layer-completion" claim with real
  evidence.
- **GitHub Actions matrix:** the x86_64/firecracker cell adds an
  `integration` step after the build step. aarch64/firecracker
  runs via the existing Pi-side delegation pattern with the same
  harness shipped over SSH.
- The tracer-bullet stays in both cells as a smoke check that
  precedes integration tests — if the boot is broken, fail
  fast before running the integration suite.

## 7. First milestone (MVP)

**Goal:** the test `test_arp_request_reply` either passes or
gives a diagnostic trace that pinpoints why FSA-4(A)'s ARP
responder doesn't actually respond.

**Scope:**

1. `Firecracker` + `Tap0` fixtures.
2. `FrameSender` with one helper: `arp_request(target_ip)`.
3. `FrameCapturer` with one filter: `arp`.
4. `SerialLog` reader.
5. One test file: `test_arp_request_reply.py` with three cases:
   - `ARP-001`: well-formed request to GUEST_IP → expect reply
     with correct OPER, HW, IP fields.
   - `ARP-004`: well-formed request to wrong IP → expect no
     reply.
   - `ARP-011`: well-formed request to non-local IP → expect no
     reply.
6. Wire into pre-push pipeline (after the tracer-bullet, gated
   on the same x86_64/firecracker cell).
7. Document the harness usage in `README.md` alongside the test
   plan.

**Out of MVP:** virtio-net negative tests, persistent shadow
tests, fuzz harness, identity configurability, aarch64 cell.
Each is its own follow-up. The order is governed by §3 of
TEST_PLAN.md.

## 8. Diagnosability features

Required from day one:

- Per-test artifact directory (kept on failure): serial log,
  Firecracker stderr, captured frames (pcap), sent frames (pcap),
  full test parameters.
- `--keep-artifacts` pytest flag to keep artifacts on pass too,
  for debugging.
- Frame captures include both the sent frame and any received
  frame, even if the test asserts on absence — so a failing
  "expect no reply" test can show what reply (if any) actually
  came back.

## 9. Open questions / decisions needed

These need Ed's input before implementation starts.

### 9.1 Test-process privilege

Confirmed default: **sudo pytest**. (Pre-push hook already runs
under sudo for tap0 setup.) If we instead want CAP_NET_RAW on
python or a setuid helper, decide now.

### 9.2 scapy vs hand-built frames

Recommended: **scapy for both send and capture**. Hand-built only
for explicit-bytes negative tests where scapy's layer logic would
"fix" malformed inputs we want to send literally.

### 9.3 Per-test vs session-scoped Firecracker

Recommended: **per-test in MVP** (clean isolation, slower).
Session-scoped is a future optimization once a "reset" mechanism
is designed (probably involves a guest-side "soft reboot" or
state-reset command).

### 9.4 Package location

Recommended: **`tooling/src/l2_harness/`** for the library,
**`tooling/tests/integration/`** for the integration tests
themselves. Matches the existing `tooling/src/<pkg>/` and
`tooling/tests/test_<pkg>.py` pattern.

### 9.5 Identity configurability

Currently `GUEST_MAC` and `GUEST_IP` are hardcoded in the
dispatcher. MVP tests use the hardcoded values. **Question for
later:** is configurability a real requirement (e.g., for
multi-instance testing) or YAGNI? Defer decision until we hit a
case that actually needs it.

### 9.6 aarch64 cell integration

The Pi-side aarch64/firecracker tracer is currently a separate
SSH-driven script. Either:
- Ship the harness package to the Pi and run pytest there over
  SSH (matches the tracer-bullet's pattern; harness needs
  scapy on the Pi).
- Use a host-driven scheme where the laptop's harness sends
  frames to the Pi's tap0 over the network (requires routable
  topology, harder to set up).

Recommended: **ship + SSH** for symmetry with the existing
aarch64 tracer.

### 9.7 Existing tracer-bullet relationship

Recommended: **keep the tracer-bullet, rescope it.** It becomes
the "did boot work, did READY appear" smoke check that precedes
the integration suite. The boot-failure case still fails fast
under the tracer-bullet without running the slower integration
tests.

## 10. Future extensions (post-MVP)

- Session-scoped Firecracker with a guest-side reset mechanism
  for faster test runs.
- Fuzz harness (Tier C) — feeds scapy-generated frames into the
  same `FrameSender`, runs N iterations against a single guest.
- Performance tests (D040 ratchet) — same harness measures
  per-frame cycles via guest-side counters exposed through the
  serial log.
- Interop tier (Tier D, D042) — containerlab topology built on
  top of the harness's primitives.

## 11. Implementation order

Subject to Ed's sign-off on §9. Initial sequence:

1. Create `tooling/src/l2_harness/` package skeleton + pyproject
   entry. Add scapy to dev dependencies.
2. `Firecracker` fixture — launch + READY-wait + cleanup. Use
   the existing `tooling/tracer_bullet/run_local.sh`'s
   Firecracker invocation as the reference.
3. `Tap0` fixture — verify existence, flush ARP cache, optional
   ephemeral creation.
4. `SerialLog` reader.
5. `FrameSender` + `FrameCapturer` thin wrappers over scapy.
6. `test_arp_request_reply.py` — the diagnostic test for the
   FSA-4(A) failure.
7. Pre-push hook integration.
8. Iterate: every subsequent TEST_PLAN.md row that transitions
   from "planned" to "passing" is its own commit, with the
   harness extension (if any) preceding it.
