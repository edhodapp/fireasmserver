# Observability design — proposal

**Status:** draft for Ed's review, pre-D051 (D049 took ontology,
D050 took fold-by-N pclmul constants; observability's D-number
assigns when Ed resolves the 6 open questions at the bottom).
**Scope:** cross-layer. Lands first in L2 per Ed's 2026-04-19 direction
("starting with L2 work, we need dynamically configurable observability
baked in"). L1 stays as-is (adequate via existing tracer-bullet markers
and CF-signalling UART path).

---

## The problem

You can't measure what you can't see, and can't fix what you can't
measure. Ethernet-frame-rate work (10 Gbps target per `docs/l2/DESIGN.md`
§2) gives us ~67 ns per frame of budget — production CRC / checksum /
FSA-dispatch sits inside that window. When something regresses, a
post-hoc log or a VMM-side trace is too coarse; we need guest-side
events that carry enough state to bisect the failure to a specific FSA
transition or buffer operation. When nothing's wrong, observability
must get out of the way — sub-10-cycle cost on the disabled path, 0
cost when compiled out.

## Design requirements (from Ed)

1. **Dynamically configurable.** The host can change verbosity / enable
   categories / reset counters at runtime without a rebuild or reboot
   of the guest.
2. **Baked in.** Every new L2+ component ships with its observability
   hooks in the first commit, not retrofitted. D046 (assembly-deferral
   bar) applies: instrumentation reshapes hot-path data layout, and
   retrofit is expensive.
3. **Performance-aware.** Must not impact production throughput in the
   common (disabled) case. Quantified target: ≤ 5 cycles per event
   site on the disabled path, ≤ 15 cycles when enabled but category
   filtered off. No target set for emitted events — that cost is
   intrinsic and users pay it by asking for the event.

## What's NOT in scope here

- **L1 instrumentation** beyond what already exists. `READY`,
  `VIRTIO:OK`, `VIRTIO:FAIL magic=XXXXXXXX` cover the bootstrap
  observability need; adding more at L1 would instrument either
  invariants or fatal-failure paths that Firecracker's VMM log
  already captures.
- **Host-side log ingestion / dashboards.** We emit structured lines
  on COM1; parsing those into a tool is downstream work, not L2 scope.
- **Per-event sampling or rate-limiting** at the emit path itself —
  deferred as a follow-on once baseline measurements exist.

---

## Proposal

### Control word

A single 64-bit word at a fixed guest-RAM physical address, initialized
in the ELF, writable from the host at any time. Layout:

```
bit  63 ........................................................ 0
    | reserved (32 bits)  | flags (16)   | categories (12) | lvl (4) |
```

- **`lvl` [3:0]** — verbosity level: 0 off, 1 error, 2 warn, 3 info,
  4 debug, 5 trace. Monotonic: a level-N event emits iff
  `verbosity ≥ N`.
- **`categories` [15:4]** — per-category enable bitmask (12 cats).
  Initial set: `core`, `uart`, `virtio`, `fsa`, `eth` (L2 rx/tx),
  `arp`, `ip`, `tcp`, `http`, `timer`, `perf_sample`, plus one
  reserved.
- **`flags` [31:16]** — reserved for toggles: timestamp-on,
  structured-vs-human format, etc.
- **`reserved` [63:32]** — future use.

**Address:** proposed `obs_ctrl` symbol in a dedicated `.obs` section
at a fixed linker-script address (e.g., `0x101000`, one page above the
current `.text` end at ~`0x100200`). Exact address pinned when the
linker script is extended.

**Host write surface:** for Firecracker, the simplest mechanism today
is kernel command-line (parsed at `_start`) for the initial value; for
runtime writes, a future virtio-console or dedicated MMIO hook. The
guest doesn't need to know how the host writes — it just reads a word.

**Initial default** (compile-time, overridable by linker-arg or host):
level=1 (error), categories=0xFFF (all on), flags=0. Errors always
emit; nothing noisy until a human asks for it.

### Emission macro

```nasm
; OBS_EMIT cat_bit, min_level, msg_label
; Fast path (disabled / filtered out): master gate load, compare,
; branch. Cost ~5 cycles on modern x86 (1 load, 1 compare, 1
; branch-not-taken, memory read from L1).
%macro OBS_EMIT 3
    cmp byte [rel obs_verbosity], %2
    jb  %%skip                       ; level too low
    test word [rel obs_categories], 1 << %1
    jz  %%skip                       ; category off
    lea rsi, [rel %%msg]
    mov ecx, %%msg_end - %%msg
    call obs_emit_line
%%msg:
    db '[OBS][cat=', CAT_NAME(%1), '][lvl=', LVL_NAME(%2), '] '
    db ..., 10
%%msg_end:
%%skip:
%endmacro
```

**Disabled-path cost** (3-5 cycles):
- `cmp byte [rel X], imm` → 1 load + 1 compare fused-op on modern x86
- `jb` → correctly predicted as not-taken in the steady state
- Falls through to next event site cleanly

**Category-off cost** (6-8 cycles):
- Above + 1 additional load (`obs_categories`) + test + branch.

**Enabled + emitted cost** (dominated by UART + string build):
- `lea` address-load + `call` overhead: ~5 cycles
- `obs_emit_line` body: LSR-polled serial writes, ~300 cycles for a
  30-byte line at 1 cycle per byte + the LSR poll. This is the
  intrinsic cost of "I want to see this event."

**Why split into two words (verbosity byte + categories word)?**
The common path is "check if minimum verbosity is met" — a single byte
load is cheaper than a bit-test against a larger mask. Categories
only check when verbosity gates pass, so they're on the rare-ish path.

### Emission helper (outline)

```nasm
; obs_emit_line: write the NUL-or-length-terminated line at rsi/ecx to
; the current observability channel. Today the channel is COM1 via
; the LSR-polled emit_bytes; future host-readable ring buffer would
; plug in here.
;
; Preserves all caller registers except rax/rcx/rsi/rdi/flags.
obs_emit_line:
    ...
    jmp emit_bytes        ; tail-call the existing LSR-polled path
```

Re-using `emit_bytes` keeps the channel singular for now. A later
upgrade can add a second channel (ring buffer, virtio-console) without
changing the macro.

### Structured format

One line per event:

```
[OBS][cat=eth][lvl=info] rx frame accepted da=aa:bb:cc:dd:ee:ff len=512
```

Choices:
- Prefix `[OBS]` lets a log parser distinguish observability lines
  from boot markers (`READY`, `VIRTIO:OK`) and from any future raw
  diagnostic output.
- Category and level tags let the host filter by either dimension
  without re-parsing the message body.
- Message body is ISA-idiomatic lower-case key=value pairs — see
  `feedback_unfriendly_assembly.md`: ISA-idiomatic formatting beats
  "pretty" for an assembly codebase where every byte emitted is
  explicitly written.
- Terminator is `\n`. No CR.

### Integration with Astier FSA transitions

Every FSA transition becomes a natural OBS_EMIT site:

```nasm
fsa_transition_rx_irq_to_dispatch:
    OBS_EMIT CAT_FSA, LVL_TRACE, "fsa:rx_irq->dispatch"
    ...
```

Under D043 (FSA runtime model), transitions are statically-generated
table entries. The OBS_EMIT can live in the per-transition handler
body or — nicer — be generated into the table-walk shim so every
transition emits uniformly without each handler remembering.

### Counters

Deferred as a follow-on — separate from the event-emit path. Proposed
shape: a fixed array of 64-bit counters at a known offset from
`obs_ctrl`, one per (category × event-type) slot. Inlined inc:

```nasm
inc qword [rel obs_counter_eth_rx_frames]
```

Cost: one RMW-to-L1 per increment. Reads by the host happen via the
same shared-page mechanism as the control word.

### Runtime reconfiguration

Host writes a new value into the 64-bit control word. Guest reads
fresh on every check. On x86 an aligned 8-byte store is atomic — no
torn reads — so no interlock is needed on either side. A write by the
host is immediately visible to subsequent guest event checks.

If the host needs confirmation that a write took effect, the guest
can ring a bell (write to a guest->host MMIO port) after the next
event check observes the change. Out of scope for the first cut.

### Host-side control app

**Requirement (Ed, 2026-04-19):** a dedicated app for controlling
the observability system. Ships alongside the guest; runs on the
host; used both interactively and from scripts.

Lands at `tooling/obs_ctl/` (new). Minimum features:

- `obs-ctl set-level <level>` — change verbosity
- `obs-ctl enable <cat> [cat ...]` / `disable <cat> ...` — toggle
  per-category emit
- `obs-ctl dump` — show current control word + counter snapshot
- `obs-ctl tail` — stream events live from the guest's COM1 serial,
  colored per category / level
- `obs-ctl reset-counters` — zero the counter region

**Language:** Python under `tooling/src/obs_ctl/`, same quality bar
as other Python in the repo (flake8 / pylint / mypy --strict / 100%
branch cov).

**Transport to the guest:** depends on open-question #4 below. The
app must not care about the low-level mechanism — we add a
`Transport` abstraction that speaks one of:

1. Firecracker **MMDS** (metadata service): guest reads JSON from
   a reserved MMIO region that the host can update via the
   Firecracker REST API. Cleanest fit — no new driver in the guest,
   Firecracker already wires the plumbing, and `obs-ctl` talks
   HTTP to the Firecracker socket.
2. **Shared serial line** (COM2 or virtio-console): host writes
   ASCII commands on one channel, guest parses them, guest emits
   events on another. Heavier guest-side parser; separates event
   egress from command ingress (a plus).
3. **Direct poke** via Firecracker's future VMOS / GDB-stub: read /
   write guest memory as the debugger would. Lowest guest cost
   (no command parser) but couples the app to a debugger-like host
   surface.

Lean: **MMDS** for the first cut. Zero new guest code beyond the
existing MMIO read (we're already doing that for virtio-net magic);
host side is a thin HTTP client. Promote to a dedicated channel if
MMDS polling frequency becomes a bottleneck.

**Interactive vs scripted:** `obs-ctl` must be usable both as a
one-shot CLI (`obs-ctl set-level debug`) and as a long-running TUI
that tails events and re-renders on control-word changes. The CLI
is the first cut; TUI grows from it when event volume justifies.

**Tests:** the usual — unit tests with a fake Transport, integration
tests that spin up a Firecracker guest with the probe stub and
verify `obs-ctl tail` sees the markers. Integration tests run
pre-push, matching the existing tracer-bullet cell.

---

## Implementation phases

**P1 — scaffolding (one commit):**
1. Linker-script `.obs` section at a fixed address.
2. `arch/x86_64/obs.inc` (NASM include) defining: constants, the
   `OBS_EMIT` macro, the `obs_emit_line` helper that tail-calls
   `emit_bytes`, initial control-word default.
3. Corresponding aarch64 include (equivalent macro using AArch64
   registers; UART side reuses that arch's tracer-bullet emit).
4. Unit test: a tiny driver that toggles the control word across
   level/category combinations and asserts the right events emit.

**P2 — wire into the first L2 component (next commit):**
- Add `OBS_EMIT` calls at every meaningful boundary in the virtio-net
  init sequence (VIO-002..VIO-009 per D038 methodology).
- Perf baseline recorded per D040: disabled-path cost per event site,
  measured via OSACA and/or a tight-loop microbenchmark.

**P3 — host-side `obs-ctl` app** (`tooling/obs_ctl/`):
- CLI commands: `set-level`, `enable`, `disable`, `dump`, `tail`,
  `reset-counters`.
- Transport: MMDS first (pending open-question #4 resolution).
- Same Python quality bar as existing tooling; integration tests
  that spin up the probe guest and round-trip a control word change.

**P4 — counters, per-category rate limiting, ring-buffer channel**
(deferred to their own D-entries when we reach them).

---

## Open questions for Ed

1. **Control-word address.** Pick a canonical physical address now so
   everyone agrees. Proposed: `0x101000` (first page above `.text`;
   symbolically `OBS_CTRL_BASE`). Any reason to prefer elsewhere?
2. **Category set.** Initial list above (12 categories including a
   reserved slot) — okay, or is the partition wrong?
3. **Verbosity levels.** 6 levels (off/error/warn/info/debug/trace) —
   fine, or fewer to keep the macro tighter?
4. **Runtime reconfig channel.** Which transport does `obs-ctl`
   speak to the guest? My lean is Firecracker **MMDS** for the
   first cut (see "Host-side control app" above): guest reads a
   reserved MMIO region, host updates it via Firecracker's REST
   API, `obs-ctl` is a thin HTTP client. Alternatives: dedicated
   virtio-console (heavier guest parser, cleaner separation) or a
   debugger-style direct memory poke (lowest guest cost, couples
   us to a debug surface we don't have yet). Confirm MMDS, or
   pick one of the alternatives.
5. **Counter storage.** Inline per-site counters (spreads counters
   across hot paths) vs. a central counters region (one cache-line
   hot among all writers — false sharing risk under future
   multi-queue)? Central for now; promote to per-core once we have
   multiple queues.
6. **Structured vs human format.** Current proposal is human-readable
   key=value. A binary TLV format would be cheaper to emit and parse
   but harder to grep. Lean is human; revisit when we have a log
   parser that could consume either.

---

## Costs vs. benefits summary

| Scenario            | Cost per site  | Benefit                              |
|---------------------|----------------|--------------------------------------|
| Compiled out        | 0 (macro nop)  | Zero obs — only for perf-critical    |
|                     |                | profile builds under D034            |
| Runtime disabled    | 3–5 cycles     | All gates off; production default    |
| Category gated off  | 6–8 cycles     | One category quiet; others on        |
| Event emits         | ~300 cycles    | Line goes out COM1; human-readable   |

The 3–5-cycle disabled-path cost is the main ongoing tax. At 10 Gbps
with ~1 event site per RX packet, that's roughly 0.5–1 % of the
per-packet budget. Acceptable for the level of insight this buys;
D040's perf ratchet will catch any regression beyond this figure.

## References

- D046 — assembly-deferral bar (why obs must ship first-iteration)
- D040 — perf-regression ratchet (will gate disabled-path cost)
- D043 — FSA runtime model (transition boundaries as natural event
  sites)
- D034 — platform-profile flags (a future compile-out flag lives here)
- `~/.claude/Finite_State_Automaton_for_Input_Output_Containers_1746783516.pdf`
  — Astier FSA engine; observability fits naturally at transition
  boundaries
- `feedback_tooling_quality_equals_product_quality.md` — the event
  parser (when we write one) is product code
