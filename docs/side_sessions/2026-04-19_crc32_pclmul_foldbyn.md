# Side-session briefing: CRC-32 IEEE 802.3 PCLMULQDQ fold-by-N

**For:** a fresh Claude Code session running in another terminal tab.
**Parent session owner:** main (architectural) Claude, coordinating
via git on `origin/main`. See "Coordination with parent session"
below for area partitioning.

---

## Read these before writing any code

The goal is not to follow a cookbook but to internalize how this
project thinks about correctness, tooling, and style. Start here.

### Project-level

1. **`~/.claude/CLAUDE.md`** (user-global rules across projects).
   Load-bearing: assembly philosophy ("don't write C in assembly";
   principle-level transfer only, not process copy); CD-first for
   deliverables; build custom tools (AI-speed advantage); hardware
   acceleration checks; rigorous-path-at-decision-points default;
   technical writing as lab notebook not pitch deck; testability
   rules (behavioral then unit, track every deviation, repro before
   fix, isolate what you test, integration tests are the ultimate
   authority); git workflow; the mandatory pre-commit pipeline
   (quality gates + Gemini + clean-Claude review); Python quality
   standards; GNU-as gotchas; QEMU gotchas.
2. **`CLAUDE.md`** (project-local at repo root, if present). Overrides
   and additions specific to fireasmserver.
3. **`DECISIONS.md`** — full decision log, immutable with
   bidirectional supersession markers. Most-relevant entries for this
   task:
   - **D003** — 100% assembly (no C, even for hot-path helpers)
   - **D006** — GNU-as was the default; superseded for x86_64 only by D048
   - **D034** — hardware platform profiles, including `HAS_PCLMULQDQ`
     flag per profile; fold-by-N is the concrete payoff
   - **D038** / **D042** — L2 methodology and interop matrix; CRC-32
     is an L2 prerequisite (ETH-005)
   - **D040** — perf regression ratchet; fold-by-N adds perf-relevant
     code paths that will need baselines eventually
   - **D046** — assembly-deferral bar: features that reshape hot-path
     data layout must land at MVP
   - **D047** (**DEPRECATED** → D048) — former GAS intel-syntax
     OFFSET convention; obsolete under NASM but read it for context
   - **D048** — **x86_64 is NASM now.** Read this decision fully.
     The conventions it encodes shape every line you write in
     `arch/x86_64/crypto/crc32_ieee.S`.
4. **`docs/l2/REQUIREMENTS.md`** — row `ETH-005` cites CRC-32 IEEE
   802.3 polynomial and references the existing `arch/x86_64/crypto/
   crc32_ieee.S` implementation.

### Memory (the parent session's persistent feedback + project notes)

Located at `~/.claude/projects/-home-ed-fireasmserver/memory/`. Read
`MEMORY.md` as the index and load the feedback files in their
entirety before starting work. Especially load-bearing here:

- `feedback_unfriendly_assembly.md` — ISA-idiomatic, not softened
- `feedback_no_silent_suppressions.md` — disclose every suppression
- `feedback_no_silent_deferments.md` — **newest rule.** When any
  reviewer (Gemini, subagent, your own analysis) raises a concern,
  do not unilaterally dispose it as "deferred." Surface it with
  recommendation + tradeoff and let Ed choose.
- `feedback_immutable_decision_log.md` — DECISIONS.md entries never
  edited or deleted; use DEPRECATED markers + forward-refs
- `feedback_complete_pipeline_before_shipping.md` — no false-confidence
  gates. Your CRC tests must actually run in CI, not skip silently.
- `feedback_tooling_quality_equals_product_quality.md` — a constants-
  derivation script is as much product as the asm it feeds.

### Previous side-session reference

`docs/side_sessions/2026-04-19_crc32_ieee.md` — the original CRC-32
bring-up. Shows the handoff pattern, deviation tracking, and status
update at completion.

---

## Quality expectations — read this before starting

Ed was not completely happy with the quality of the prior CRC side
session's output. The NASM port (later main-session work) surfaced
issues that had been latent: stale comments referencing retired
rules, LEA-as-reg-copy workarounds that were never removed when
their reason went away, CPUID on every call, fold-by-1 leaving
obvious throughput on the floor, stack save/restore in the reduce
path, and pytest with hardcoded paths that skipped silently in CI.
None of those were wrong at the moment they landed, but several
were visible as suboptimal at review time and were disposed as
"good enough for now" without surfacing the tradeoff for a decision.

**Your bar is higher than "tests pass":**

1. **No silent deferments.** If a reviewer (Gemini, a subagent, or
   your own analysis) flags a concern you'd reflexively call "low
   priority / defer," surface it with recommendation + tradeoff
   and let Ed choose. The feedback file
   `~/.claude/projects/-home-ed-fireasmserver/memory/feedback_no_silent_deferments.md`
   codifies this — read it before first commit.
2. **Comments refer to what the code does, not to obsolete rules.**
   The current file has `; reg-copy via LEA` comments blaming a
   lint that no longer exists. Under NASM with D048 in force, an
   explanatory comment that justifies a superseded workaround is a
   code smell to remove, not preserve. If you find yourself
   writing a comment that would be wrong if D047 had never existed,
   rewrite the code instead.
3. **Verification is the work, not an afterthought.** The previous
   session's 1024-byte all-zero and all-0xFF expected values had
   to be corrected against zlib; the corrections were called out
   as a "deviation from briefing" but would have been avoided
   entirely if the driver had started from zlib and worked backward
   to named vectors. Derive first, cross-check second, pin as
   vectors third.
4. **Verify across a sweep, not just named vectors.** 7 named
   vectors + a 0..256 sweep found the polynomial correctness but
   not, e.g., fold-chunk boundary effects. For fold-by-4 the
   boundaries at 64, 128, 256, 512, 1024, 4096, 8192 bytes all
   matter; exercise all of them in the derivation script's
   self-test (which is independent of the C test driver).
5. **No hardcoded absolute paths.** This is a project-local rule
   (see `tooling/tests/test_branch_cov_disasm.py` — the prior
   form resolved `/home/ed/fireasmserver/...` which silently
   skipped in CI. The repo has a `Path(__file__).resolve()` idiom;
   use it).
6. **Clean-Claude pre-commit review is mandatory per CLAUDE.md**
   (both global and project). For EVERY commit that changes
   functional code or test code, spawn a subagent with no project
   context and feed it the diff + `~/tools/code-review/
   review-prompt.txt`. Act on the findings. This is not optional
   and did not happen reliably in the prior session.
7. **Tooling quality = product quality.** The derivation script is
   product code — it decides whether the assembly is correct.
   Apply the same flake8 / pylint / mypy --strict / 100 % branch
   coverage bar as any other Python in the repo.

## Task

Replace the current fold-by-1 PCLMULQDQ path in
`arch/x86_64/crypto/crc32_ieee.S` with fold-by-4 (or fold-by-2 as a
fallback — see "Scope of ambition" below), processing 64 bytes per
loop iteration instead of 16. Intel's "Fast CRC Computation Using
PCLMULQDQ Instruction" white paper (2009) is the canonical reference;
treat it as a design source, **not** as a constants source —
constants must be derived and verified in this repository, not
pasted from a third-party document we cannot audit.

### Why now

Gemini's review of the current fold-by-1 path called out the serial
dependency chain: each PCLMULQDQ has 5–7 cycles of latency, and the
fold-by-1 loop waits on the previous iteration's result before
starting the next. Parallel accumulators (fold-by-N, N ∈ {2,3,4})
hide that latency by processing N independent 16-byte chunks per
iteration. For Ethernet-frame-sized payloads (64–1518 bytes), the
throughput improvement matters — this primitive will eventually sit
on the L2 RX/TX hot path.

### Scope of ambition

Primary target: **fold-by-4** (64 bytes per iteration). Fallback:
**fold-by-2** if fold-by-4 introduces constants or reduction paths
you cannot verify to satisfaction. In either case, the small-input
path (len < one fold chunk) must still fall through to
`crc32_ieee_802_3_slice8`, which this briefing does NOT touch.

---

## Function contract (unchanged from original)

Host-callable leaf per SysV AMD64 ABI:

```c
uint32_t crc32_ieee_802_3_pclmulqdq(const void *data, size_t len);
```

- **Polynomial:** `0xEDB88320` (reflected form of `0x04C11DB7`).
- **Initial value:** `0xFFFFFFFF`.
- **Final XOR:** `0xFFFFFFFF`.
- **Zero-length input:** returns `0x00000000`.
- **Byte order:** input is a byte stream; output is the polynomial-
  correct value per IEEE 802.3 Annex F.3 reference.

### Must-pass test vectors

The existing host C driver in `tooling/crypto_tests/crc32_test.c`
already drives this function and compares against a dual reference:
zlib's `crc32()` and a slicing-by-8 equivalence cross-check. Your
fold-by-N path must match zlib for **every length** the driver
sweeps (currently 0..256 named + sweep) AND produce identical output
to `crc32_ieee_802_3_slice8` for any input you feed both. The
existing tests are the safety net; don't remove them.

**Important edge cases to bench-test manually before committing:**

- Lengths exactly on fold-chunk boundaries: 16, 32, 48, 64, 128, 256
  (especially 64 for fold-by-4 — one full iteration then tail)
- Lengths just under and over a boundary: 63, 65, 127, 129
- Zero-length (must return `0x00000000`, no fold iterations run)
- Inputs smaller than the fold chunk (must fall to slice8)

---

## Approach — recommended sequence (deviate if you see reason)

**Step 1 — Constants derivation script (COMMIT FIRST, before any
asm).** Put it at `tooling/crypto_tests/derive_fold_constants.py`.
It must:

- Compute rev32(x^k mod P) for all k the chosen fold factor needs.
  For fold-by-4 that's k ∈ {512, 448, 384, 320, 192, 128} (verify
  against Intel paper §4 for the exact set).
- Use Python's built-in polynomial arithmetic or a small
  polynomial-multiply+mod helper — whichever is more readable.
  **Do not use any external library that itself claims to implement
  CRC-32 constants**; the derivation must be independent.
- For each derived constant, verify against a ground-truth CRC-32
  computed by `zlib.crc32` on a test payload that exercises that
  constant's role (e.g., a 64-byte block for the x^512 constant in
  fold-by-4).
- Emit the constants as NASM-ready `dq` literals with comments
  identifying each constant's exponent.
- Run `python3 tooling/crypto_tests/derive_fold_constants.py`
  verifies self-consistency; exit 0 means "constants are
  reproducible and verified against zlib across all tested shapes."

**Quality-gate bar for this script:** same as any Python in the
repo — `flake8 --max-complexity=5`, `mypy --strict`, `pylint`,
100% branch coverage with pytest. See
`~/tools/code-review/run-python-gates.sh`.

**Step 2 — Paste the derived constants into the asm constants
section.** Then rewrite the `crc32_ieee_802_3_pclmulqdq` body to
fold-by-N. Use **NASM macros** (`%macro fold_step N`) so the N=1
fallback (for `rsi` in [16, fold_chunk_size)) and the N=main path
share one implementation. See "Optimization notes" below.

**Step 3 — Build + run the CRC tests** (`make -C tooling/crypto_tests
test`). If any vector mismatches, STOP and re-derive — do not
micro-adjust constants by hand. The derivation script is the source
of truth.

**Step 4 — Push a single logical group per commit.** Likely
sequence:

- C1: `derive_fold_constants.py` + its pytest wrapper
- C2: fold-by-N rewrite of `crc32_ieee_802_3_pclmulqdq`

---

## Optimization notes — macros, cache, and beyond

Ed specifically asked the briefing to capture the design thinking
for computationally heavy code and cache behavior, since those
patterns carry across future hot-path primitives (TCP checksum,
virtio-ring fences, potential AES-NI paths).

### Macros for parameterized code generation

NASM's `%macro` + `%rep` are strong enough to let you write the
fold-by-N body once and expand it for any N. Pattern:

```nasm
%macro fold_chunk 2  ; args: register_index, constant_label
    movdqa  xmm_scratch, xmm%1
    pclmulqdq xmm%1, [constant_%2], 0x00
    pclmulqdq xmm_scratch, [constant_%2], 0x11
    pxor    xmm%1, xmm_scratch
%endmacro

.fold_loop:
    %assign i 0
    %rep N
        fold_chunk i, fold_by_%[N]_k%[i]
        %assign i i+1
    %endrep
    ; XOR in N × 16 bytes of new input ...
```

Two things to prefer when macros get involved:

- **Keep macros small and comment-heavy.** The x86_64 SDM operand
  rules bite hard when a macro expansion generates a form the
  assembler can't encode. A short, well-documented macro that
  expands to 6 lines reads fine; a 50-line macro with conditional
  `%if`s reads like a nightmare.
- **Name constants by their mathematical role,** not by register
  offset. `fold_by_4_k192` (= rev32(x^192 mod P), the multiplier for
  the oldest accumulator in a fold-by-4) is a name a future
  maintainer can reason about. `k0`/`k1` is not.

### Cache usage

- **Slicing-by-8 table** is 8 KiB, 64-byte aligned, fits comfortably
  in a 32 KiB L1d cache. It's already warm for any PCLMULQDQ tail
  reduction that reuses `_crc32_update_slice8`.
- **PCLMULQDQ fold constants:** 16 bytes per xmm constant, so
  fold-by-4 needs ~6 × 16 B = 96 B of constants, one cache line.
  Pin the constants section `.balign 64` so the whole set lives on
  one 64-byte cache line — eliminates a second cache fill in the
  fold loop's warm-up.
- **Input pointer alignment:** `movdqu` tolerates misalignment but
  pays a penalty on hosts with split cache lines. An optional
  alignment pre-amble (byte-at-a-time until `rdi & 0xF == 0`) can
  upgrade the main loop to `movdqa`. Benchmark on
  boundary-straddling inputs before committing to the pre-amble —
  the branch cost may outweigh the alignment gain on short frames.
- **Prefetching:** for large inputs (MTU-sized frames up for
  batched checksumming), a `PREFETCHNTA [rdi + CACHE_DISTANCE]` in
  the fold loop can keep the demand-fetch latency hidden. The
  `NTA` variant avoids polluting L2/L3 with bytes we won't look at
  twice. This is almost certainly out of scope for this commit
  series — document it in the header comment as a future
  optimization guarded by perf measurement.

### Other computation-heavy patterns worth noting as you work

- **Reduction from 128-bit state to 32-bit CRC:** the current code
  reuses `_crc32_update_slice8` on 16 state bytes, which is
  readable but spends ~16 cache-hit loads and ~64 XORs for a job
  that Intel's paper does in ~3 PCLMULQDQ operations (Barrett's
  folding reduction). If you have energy after fold-by-N is
  verified, this is the biggest remaining win in the function —
  but it requires more constants (`P(x)` reciprocal for Barrett)
  and careful verification. Surface it as a follow-up if you think
  fold-by-N is "one thing at a time" enough for this commit.
- **Horner's method** does not apply for parallel folds (the whole
  point of fold-by-N is to break the Horner dependency chain). Do
  not be tempted.
- **Unrolling beyond fold-by-4:** fold-by-8 exists in some
  implementations (Intel white paper, Linux kernel). Unless you
  see a measured throughput ceiling at fold-by-4, don't go there —
  it doubles the constants table and makes the critical warm-up
  path heavier on small frames.

---

## Directory scope — own and do not exceed

Create or modify only:

```
arch/x86_64/crypto/crc32_ieee.S         # fold-by-N rewrite (PCLMULQDQ region only)
tooling/crypto_tests/derive_fold_constants.py   # NEW — derivation + verification
tooling/tests/test_derive_fold_constants.py      # NEW — pytest coverage
docs/side_sessions/2026-04-19_crc32_pclmul_foldbyn.md  # this file; update status at the end
```

### Do NOT touch

- `arch/aarch64/crypto/crc32_ieee.S` — AArch64 uses FEAT_CRC32
  natively, fold-by-N doesn't apply.
- `arch/*/platform/` — boot stubs; main session's territory.
- `DECISIONS.md` — read only. If you discover a design principle
  that deserves codifying, note it in this briefing's "Deviations"
  section at completion for main session to formalize.
- `docs/l2/REQUIREMENTS.md` — read only.
- `tooling/crypto_tests/crc32_test.c` — the test driver already
  drives `crc32_ieee_802_3_pclmulqdq`; if you need to extend the
  driver (new test entry point, extended vector sweep), discuss in
  this file's deviations section rather than editing unilaterally.

### Coordination with parent (main) session

The main session is concurrently doing a narrower set of CRC follow-ups:

- **#6 — cached dispatcher** (replaces per-call CPUID). Touches the
  top-level `crc32_ieee_802_3` entry and adds a static function
  pointer to a new `.data` / `.bss` section.
- **#10 — LEA-as-reg-copy cleanup** (D047 lint-era workaround, no
  longer needed under NASM). Touches every reg-copy site EXCEPT the
  ones inside `crc32_ieee_802_3_pclmulqdq` — you own those. If the
  fold-by-N rewrite naturally replaces the LEA sites in your region
  anyway, this coordination is self-resolving.

**Not in main's scope** (explicitly deferred until you're done):

- **#9 — helper ABI change** so `_crc32_update_slice8` doesn't
  clobber `rdi`/`rsi`. Originally planned for main but deferred
  because it affects the PCLMUL reduce path (your territory). If
  you want the saved/restored `rdi`/`rsi` gone as part of the
  fold-by-N rewrite, make the contract change yourself and update
  the helper and the slice8 entry caller together — you have the
  broadest view of whether the cleaner ABI is worth the coupling.
  Otherwise main session picks it up after you push.

**Area partition:**

- YOU own: the PCLMUL constants section and everything between
  `crc32_ieee_802_3_pclmulqdq:` and its implicit end (up to but not
  including `crc32_ieee_802_3_has_pclmulqdq:`) — that function's
  body. You MAY touch `_crc32_update_slice8`'s signature IF you
  take on #9 as described above; in that case also update
  `crc32_ieee_802_3_slice8` which is the other caller.
- MAIN owns: the dispatcher `crc32_ieee_802_3`, the feature probe
  `crc32_ieee_802_3_has_pclmulqdq`, and LEA→MOV cleanup everywhere
  except inside your function.

Merge by rebasing on `origin/main` before each push
(`git pull --rebase origin main`). Conflicts should be contained to
the file's constants section and the PCLMULQDQ function body;
resolve by preferring your extended constants / rewritten function.
Notify main session via a commit comment or a note in this file if
anything else diverges.

---

## Gates to respect

Same as the previous CRC briefing — nothing is relaxed for this work.

- **Pre-commit hook** (`.git/hooks/pre-commit` → project-local
  `tooling/hooks/pre_commit.sh` → shared `~/tools/code-review/
  pre-commit-hook.sh`). Runs the Python quality gates on staged
  `.py` files, then Gemini review on `.py` + `.S`. Python gates
  BLOCK the commit if they fail; Gemini is advisory.
- **Pre-push hook** runs the full integration suite:
  `tooling/hooks/pre_push.sh`. Now includes the CRC-32 IEEE vector
  tests (added this session — `make -C tooling/crypto_tests test`).
  **Your CRC path change must keep this green.** Do not
  `--no-verify` under any circumstance.
- **Python quality gates** (same as last briefing): flake8, pylint,
  mypy --strict, pytest with 100% branch coverage on added code.
- **No silent lint suppressions** — every `# type: ignore`,
  `# pylint: disable`, etc., must be explained in the commit
  message AND surfaced to Ed for decision.
- **Clean-Claude pre-commit review** (CLAUDE.md requirement): spawn
  a subagent with NO project context, give it only the changed code
  + `~/tools/code-review/review-prompt.txt`. Act on findings before
  committing.

## Commit + push discipline

- Trunk-based: commit directly to `main` locally, push after each
  logical group. `git pull --rebase` before `git push`.
- **Suggested commit shape:**
  - **C1** — `derive_fold_constants.py` + test. Small, reviewable,
    establishes the ground truth.
  - **C2** — NASM fold-by-N rewrite. References C1's output for
    the constants. Cites "verified by
    tooling/crypto_tests/derive_fold_constants.py on commit <sha>"
    in the commit message body.

## Definition of done

1. `derive_fold_constants.py` emits constants that match
   zlib.crc32 across a sweep (≥ 257 lengths, extending up to 8192
   bytes) and can be rerun to regenerate identically.
2. The pytest wrapper for that script (`test_derive_fold_constants
   .py`) passes under the full quality gate stack.
3. NASM-built `crc32_ieee.o` under the new fold-by-N path passes
   every vector in `crc32_test.c` — both x86_64 and aarch64 (the
   latter is unchanged but must still be green in the pre-push run).
4. The commit SHAs for C1 and C2 are recorded in this file's
   Status section below, so main session can cross-reference.
5. The fold-by-N constants are named by mathematical role (e.g.
   `fold_by_4_k512`), not register offset; their derivation is
   stated in a comment block above the constants section.

## Status (updated by side session at completion)

- [ ] Constants derivation script + pytest coverage
- [ ] NASM fold-by-N rewrite
- [ ] CRC-32 host-side tests green after switch
- [ ] Pre-push integration suite green
- [ ] Commit SHAs: <fill in: C1, C2>

### Deviations from briefing

(Side session: fill this in at completion. Every deviation — fold-by-4
downgraded to fold-by-2, Barrett reduction deferred, alignment
pre-amble not attempted, etc. — gets a bullet with the reason, per
the "track every requirements deviation" rule.)
