# Side-session briefing: `audit_ontology.py` — req → impl → verification matrix tool

**For:** a fresh Claude Code session running in another terminal tab.
**Parent session owner:** main (architectural) Claude, coordinating
via git on `origin/main`. See "Coordination with parent session"
below for area partitioning.

---

## Read these before writing any code

### Project-level

1. **`~/.claude/CLAUDE.md`** (user-global rules across projects).
   Load-bearing: CD-first for deliverables; build custom tools
   (AI-speed advantage); technical writing as lab notebook not pitch
   deck; testability rules (behavioral then unit, track every
   deviation, repro before fix); git workflow; mandatory pre-commit
   pipeline (quality gates + Gemini + clean-Claude review); Python
   quality standards.
2. **`CLAUDE.md`** (project-local at repo root, if present).
3. **`DECISIONS.md`** — most-relevant entries for this task:
   - **D049** — Ontology as formal verifiable requirements. **Read
     this in full.** The traceability fields (`rationale`,
     `implementation_refs`, `verification_refs`, `status`) this
     tool audits came from here, as did the `PerformanceConstraint`
     shape. D049 explicitly earmarks "the audit tool that reads
     the ontology + the repo + the perf-ratchet artifacts and
     emits a requirement → impl → verification matrix with gap
     flags" as a follow-up — this is that follow-up.
   - **D040** — perf regression ratchet. Once this tool lands, it
     becomes the bridge between `PerformanceConstraint` rows and
     D040's baseline artifacts.
   - **D046** — assembly-deferral bar. Tooling quality = product
     quality; build this to the same standard as shipped code.
   - **D050** — fold-by-N pclmul constants. Adjacent example of a
     design-note decision that the tool should treat as a valid
     `rationale` target.
4. **`tooling/src/ontology/`** — the forked schema. Read
   `models.py` to understand the data shapes you'll be loading,
   particularly `DomainConstraint` and `PerformanceConstraint`
   with their traceability fields.
5. **`tooling/qemu-harness.json`** — the DAG artifact you'll audit.
6. **`tooling/build_qemu_harness_ontology.py`** — see how the
   ontology is populated today; this is your input shape.

### Memory (the parent session's persistent feedback + project notes)

Located at `~/.claude/projects/-home-ed-fireasmserver/memory/`. Read
`MEMORY.md` as the index and load the feedback files in their
entirety before starting work. Especially load-bearing here:

- `feedback_no_silent_suppressions.md` — disclose every suppression
- `feedback_no_silent_deferments.md` — no silent "low priority /
  defer" dispositions
- `feedback_complete_pipeline_before_shipping.md` — no
  false-confidence gates
- `feedback_tooling_quality_equals_product_quality.md` — this tool
  is product code
- `feedback_side_session_file_scope.md` — commits touch only the
  files in this briefing's scope
- `feedback_cd_pipeline_main_owns_side_flags.md` — if you introduce
  new test files or dependencies, flag them in Deviations (main
  session owns the CD surface wiring)
- `feedback_explicit_git_add_during_parallel_sessions.md` —
  explicit `git add <file>` per file, never `-a`/`-A`

---

## Two firm rules from Ed — violate neither

1. **Use Gemini for independent code review on every functional
   commit.** Pre-commit hook handles this automatically. Read the
   output, act on it, surface what you don't act on.
2. **No deferments without asking Ed first.** If a reviewer flags
   a concern, surface it with recommendation + tradeoff — do not
   self-dispose as "low priority / defer."

---

## Task

Build `tooling/src/audit_ontology/` — a Python module that reads
`tooling/qemu-harness.json`, cross-references every constraint's
`implementation_refs` and `verification_refs` against the working
repo, and emits a human-readable requirement → implementation →
verification matrix plus a machine-readable gap report.

### Why this tool exists

Right now the ontology carries traceability fields but nothing
verifies they point at real things. A typo in an `implementation_refs`
entry (`arch/x86_64/crypto/crc32_ieee.S:crc_ieee_802_3` vs the
actual `crc32_ieee_802_3` — note the missing `32`) would go
unnoticed. An SysE reviewer reading the post-release audit
(`project_sysengineering_expert_review.md`) would ask "how do you
know this traceability isn't fiction?" — the audit tool is the
answer. It turns the ontology from declarative aspiration into
verifiable evidence.

### Inputs

1. `tooling/qemu-harness.json` — the DAG artifact. Load via
   `ontology.dag.load_dag(path, project_name)` and extract the
   current node's `Ontology` snapshot.
2. The repo working tree at `Path(__file__).resolve().parents[N]`
   (figure out the right N from where this tool lives).

### Processing

For each `DomainConstraint` and `PerformanceConstraint`:

1. **Parse `implementation_refs`** — each is a string; the
   conventional form is `path/to/file:symbol` but callers may
   also use `path/to/file:line_number` or a bare `path/to/file`
   for whole-file references. Accept all three forms.
2. **Resolve each impl ref** against the working tree:
   - The file must exist.
   - If a `:symbol` is given, the symbol must be findable in the
     file. For `.S` (NASM per D048 on x86_64; GAS on aarch64),
     grep for the symbol name followed by `:` at line start (NASM
     label syntax). For `.py`, ideally use AST parse; grep for
     `def <symbol>` / `class <symbol>` is acceptable fallback.
     For `.c`/`.h`, grep for `<symbol>\s*\(`. For `.md` or other
     text, literal substring match.
   - If a `:line_number` is given, file must have at least that
     many lines.
3. **Parse `verification_refs`** the same way.
4. **Record resolution status** per ref: `resolved` / `file_missing`
   / `symbol_missing` / `line_out_of_range`.
5. **Check internal consistency:**
   - `status == "implemented"` but `implementation_refs == []` →
     inconsistency.
   - `status == "tested"` but `verification_refs == []` →
     inconsistency.
   - `status == "deviation"` but `rationale == ""` → missing
     required justification.
   - `status == "spec"` with non-empty `implementation_refs` →
     mild warning (likely stale status).

### Outputs

The tool should support two invocation modes:

1. **`audit-ontology` (no flags)** — human-readable stdout:
   ```
   === Requirement → Implementation → Verification matrix ===
   [✓] fsa-transition-budget             (spec)
       impl: — (none declared)
       verify: — (none declared)
   [!] clean-vm-kill                     (implemented) ← status/refs mismatch
       impl: — (none declared; status=implemented)
       verify: — (none declared; status=implemented)
   ...
   === Gaps (3 total) ===
     - clean-vm-kill: status=implemented but implementation_refs empty
     - l2-frame-rate-floor: status=spec with empty refs (expected)
     - ...
   === Summary ===
     Total constraints:     12
     With impl refs:         0 (0%)
     With verify refs:       0 (0%)
     Gaps / inconsistencies: 3
   ```
2. **`audit-ontology --json`** — machine-readable JSON on stdout
   with the same information, schema pinned in this briefing's
   appendix (see bottom).
3. **`audit-ontology --exit-nonzero-on-gap`** — return code
   reflects gap count (0 if clean, 1 if any gaps). For future
   pre-push / CI integration.

### Unresolved-ref reporting

When a ref doesn't resolve, the output should state WHICH ref,
WHICH constraint it belongs to, and WHAT kind of failure (file
vs symbol vs line). No guessing or "fuzzy match" — the auditor
needs honest signal.

### Non-goals (for this first cut)

- Don't correlate `PerformanceConstraint.metric` against
  D040 perf-ratchet baseline files. That's a follow-up once the
  perf ratchet actually has baselines (currently only the
  framework exists).
- Don't flag orphan implementations (code files that no
  constraint references). That's a separate, larger audit —
  different tool.
- Don't try to parse rationale strings for decision-pointer
  validity. Keep it to structural checks this pass.

---

## Quality expectations

- **Tooling quality = product quality.** flake8 max-complexity=5,
  pylint (Google style at `~/.claude/pylintrc`), mypy --strict,
  pytest with 100 % branch coverage on added code.
- **Testable units.** The ref-parser, the resolver, the
  consistency checker, and the formatter should all be
  independently unit-testable.
- **No hardcoded absolute paths.** Resolve paths relative to the
  repo root via `Path(__file__)` or a CLI arg. The earlier
  `/home/ed/fireasmserver/...` silent-skip pattern is a known
  anti-pattern (see
  `feedback_pre_commit_gate_reads_working_tree.md` context).
- **Idempotent.** Running the tool twice on the same ontology
  must produce identical output.
- **No side effects on the ontology.** This tool is read-only;
  it never writes to `qemu-harness.json` or its sidecar.

---

## Directory scope — own and do not exceed

Create / own:

```
tooling/src/audit_ontology/
    __init__.py
    __main__.py             # CLI entry point
    parser.py               # ref string parsing
    resolver.py             # file + symbol lookup
    consistency.py          # status/refs cross-checks
    formatter.py            # human + JSON output
    py.typed                # PEP 561 marker
tooling/tests/
    test_audit_ontology.py  # pytest coverage
```

Optional:

```
pyproject.toml              # add audit-ontology script entry
```

### Do NOT touch

- `tooling/src/ontology/` — main session's territory. You import
  from it but don't modify it.
- `tooling/build_qemu_harness_ontology.py` — main session back-
  fills the existing constraints' traceability here in parallel
  with your work. Touching it would merge-conflict.
- `tooling/qemu-harness.json` — read-only input to your tool.
- `tooling/hooks/pre_push.sh`, `.github/workflows/cd-matrix.yml` —
  main owns the CD pipeline per
  `feedback_cd_pipeline_main_owns_side_flags.md`. Your pytest
  file will be auto-discovered; if you need new deps, flag them
  in Deviations.

### Coordination with parent (main) session

- **Your tool READS the ontology.** Main session is WRITING
  traceability fields into the existing DomainConstraints
  (back-fill pass) in parallel. Until main finishes, the
  ontology's existing 7 DomainConstraints mostly have empty
  traceability fields — that's expected; your tool should
  report them as "empty refs (warning)" rather than failing.
  After main's back-fill lands, most of those warnings should
  clear to proper resolved refs.
- Trunk-based: `git pull --rebase` before every push. Explicit
  `git add <file>` per file — no `-a`/`-A`/`.` — per the hard
  lesson from 2026-04-19 b655543
  (`feedback_explicit_git_add_during_parallel_sessions.md`).

---

## Gates to respect

Standard:

- Pre-commit hook: quality gates on staged `.py` (flake8, pylint,
  mypy, pytest). Gemini review on `.py` / `.S`. Clean-Claude
  review via subagent per CLAUDE.md.
- Pre-push hook: full pytest suite now runs (added 2026-04-19);
  your tests get exercised automatically.
- GitHub Actions `cd-matrix.yml`: now has a dedicated `pytest`
  job that will pick up your test file via auto-discovery.
- **`git add` after fixing a gate-flagged issue!** Don't rely on
  the gate to re-check; re-stage explicitly per
  `feedback_pre_commit_gate_reads_working_tree.md`.

## Commit + push discipline

- Trunk-based.
- Explicit per-file staging (no wide-net).
- **Suggested commit shape:**
  - **C1 (parser + resolver)**: `tooling/src/audit_ontology/
    {__init__,parser,resolver}.py` + tests. The data-layer
    primitives, without formatting or CLI.
  - **C2 (consistency + formatter + CLI)**: adds the
    status/refs consistency checks, the human-readable
    formatter, the JSON formatter, and `__main__.py` CLI entry.
  - **C3 (optional)**: `pyproject.toml` script entry for
    `audit-ontology` console command.

---

## Definition of done

1. `audit-ontology` CLI runs cleanly against the current
   committed `tooling/qemu-harness.json`. Output is accurate:
   gaps correspond to real empty-refs; resolved refs correspond
   to real code sites.
2. `audit-ontology --json` emits valid JSON conforming to the
   schema below.
3. 100 % branch + statement coverage on the new module via
   `pytest tooling/tests/test_audit_ontology.py --cov`.
4. Pre-push gate green after your commits land.
5. Commit SHAs recorded in this file's Status section below.

## Output JSON schema (pin this; main session will consume)

```json
{
  "dag_path": "tooling/qemu-harness.json",
  "ontology_node_id": "<uuid of current node>",
  "constraints": [
    {
      "name": "fsa-transition-budget",
      "kind": "performance",
      "status": "spec",
      "rationale": "D043 FSA runtime model, §FSA_TRANSITION_BUDGET_NS.",
      "implementation_refs": [
        {"raw": "arch/...", "resolved": true, "kind": "file_symbol"}
      ],
      "verification_refs": [],
      "gaps": [
        "status=implemented but implementation_refs empty"
      ]
    }
  ],
  "summary": {
    "total_constraints": 12,
    "with_impl_refs": 0,
    "with_verify_refs": 0,
    "gap_count": 3,
    "resolved_ref_count": 0,
    "broken_ref_count": 0
  }
}
```

## Status (updated by side session at completion)

- [x] Parser module + tests
- [x] Resolver module + tests
- [x] Consistency checker + tests
- [x] Formatter (human + JSON) + tests
- [x] CLI entry point + tests
- [x] 100 % branch coverage on the new module
- [x] Ran clean against current ontology; output reviewed for
      accuracy against known state
- [x] Pre-push integration suite green
- [x] Commit SHAs recorded:
      - `f4936f5` — C1: ref parser + repo resolver
      - `46a60db` — C2: consistency + audit + formatter + CLI
      - `5423b02` — C2a: symlink guard + AST-scope tightening
      - `eb0691e` — C2b: container-block recursion + async-def
        fallback + root cache
      - `8fa291f` — C2c: drop CWD-sensitive cache + indent-tolerate
        regex fallback

### New CD surface (flag for main session per
`feedback_cd_pipeline_main_owns_side_flags.md`)

- **New test file(s):**
  `tooling/tests/test_audit_ontology.py` — picked up
  automatically by `pytest -q` collection in both the pre-push
  hook and CI `cd-matrix.yml`; zero-wiring addition.
- **New runtime dependencies:** none. Stdlib (`ast`, `re`,
  `json`, `argparse`, `functools`, `pathlib`, `collections.abc`,
  `contextlib`) plus pydantic (already a dep) plus the in-repo
  `ontology` package (forked 2026-04-19 into
  `tooling/src/ontology/`).
- **New CLI console entry (NOT added — flagged for main):** the
  `[project.scripts]` table in `pyproject.toml` would benefit
  from
  `audit-ontology = "audit_ontology.cli:main"` so Ed can run
  `audit-ontology` instead of
  `PYTHONPATH=tooling/src python -m audit_ontology`.
  Side session did NOT add this per
  `feedback_cd_pipeline_main_owns_side_flags.md` (pyproject is
  main-session territory).
- **Non-pytest gates introduced:** none. The tool itself could
  become a non-pytest gate in a future pass (
  `audit-ontology --exit-nonzero-on-gap` as a pre-push step once
  Ed wants broken-ref CI enforcement), but that's a
  main-session call to wire into `tooling/hooks/pre_push.sh`.

### Deviations from briefing

- **Parser path-traversal guard.** Briefing didn't specify one;
  clean-Claude review flagged `Path(root) / "/etc/passwd"`
  silently drops `root`, so the parser now rejects absolute
  paths and `..` segments at parse time. Mirrors
  `vm_launcher.py:_reject_traversal` posture. Ed's call
  (approved).
- **Resolver outside-repo symlink guard.** Briefing didn't
  specify one; Gemini round-2 flagged in-repo symlinks to
  sensitive system files as a CI risk. Resolver now rejects
  refs whose `Path.resolve(strict=True)` lands outside
  `repo_root` with a new `outside_repo` resolution. Ed's call
  (approved).
- **`.py` symbol lookup expanded to module-level assigns AND
  class-body names AND container-block bodies (if / try /
  with / for / while).** Briefing specified "AST parse,
  FunctionDef / AsyncFunctionDef / ClassDef"; live-ontology
  run surfaced that refs like `vm_launcher.py:_proc_lock`
  point at module-level variables and previously
  false-positive-resolved via substring coincidence. Expanded
  to module + class body (but NOT function bodies — local
  variables don't leak as module symbols). Container-block
  recursion added post-C2a so version-gated imports resolve.
- **`.S` symbol lookup expanded from label-only to label +
  NASM `%macro` + NASM `%define`.** Ed flagged mid-task that
  the upcoming CRC / crypto work will define fold-by-N
  helpers as NASM macros — added the regex alternatives so
  refs to those macros resolve when they land.
- **Two consistency-check extensions.** Briefing listed four
  gap rules; Ed approved two extensions: `status=tested` with
  empty impl_refs is a gap (verifying what isn't implemented
  is incoherent), and `status=implemented` with empty
  verify_refs is a gap ("implemented but unverified" is a
  claim, not a fact).
- **No `pyproject.toml` edit.** Briefing's Commit C3 proposed
  a console-script entry; side session skipped per
  `feedback_cd_pipeline_main_owns_side_flags.md`. Flagged
  above as a main-session integration task.

### Observations for the main session

- **Live audit is currently clean** (12 constraints, 24
  resolved refs, 0 gaps) against the committed ontology at
  node `a8e4d129-2055-4869-8526-1d08fffb998d`. This was
  NOT true before C1 landed — the O4 back-fill commit
  (`6fe19c7`) technically shipped two latent
  symbol-resolution bugs (`_proc_registry` / `_proc_lock`
  referenced as module-level vars but the audit tool's first
  version only knew defs/classes); they'd have surfaced as
  `symbol_missing` if the tool had landed first. Worth
  adding a D-entry noting the tool is now the closing gate
  on ontology edits: "run `audit-ontology
  --exit-nonzero-on-gap` before committing ontology
  changes."
- **Pre-push gate wiring.** Consider adding
  `audit-ontology --exit-nonzero-on-gap` to
  `tooling/hooks/pre_push.sh` once CI is ready to enforce.
  Currently optional, but enforcing prevents broken refs
  from landing on `origin/main`. Main session's call.
- **Deferred Gemini findings (same "add on first real need"
  line as C-macro loosening).** Current ontology doesn't
  exercise any of these; noting for future triage:
  - `ast.Match` (Python 3.10+) blocks — add to container
    list + handle `MatchCase` body when match shows up in
    tooling code.
  - `ast.Import` / `ast.ImportFrom` target capture — when
    the ontology starts referencing re-exports via
    `__init__.py:Name`.
  - Tuple/list unpacking in `Assign` targets (`X, Y = ...`)
    — trivial extension of `_names_from_assign_targets`
    when needed.
  - `_py_regex_fallback`'s `def_or_class` pattern is not
    line-anchored (unlike the assign branch); could
    false-positive on `# def foo()` in a comment. Only
    matters on SyntaxError-recovery path; tighten if it
    ever bites.
  - `_symbol_in_c` matches function calls as well as
    definitions; Ed explicitly chose to keep briefing's
    `<sym>(` pattern for now. Revisit when C code appears
    in the ontology or when the call-vs-definition
    distinction starts mattering.
  - `_resolve_line` / `_resolve_symbol` read the whole file;
    OK at current scale (~24 refs × ≤ few-KB files). Revisit
    if the ontology grows thousands of refs or starts
    pointing into generated data.
- **Commit mis-attribution incident.** First C1 attempt hit
  a parallel-session index collision (main session's commit
  landed between my `git add` and `git commit`, lost my
  staged state; pre-commit hook reported "No Python files
  staged" and my `_proc_registry` / `_proc_lock`-fixing
  resolver work briefly looked committed when it wasn't). No
  content lost — files existed in working tree, re-staged
  and landed as `f4936f5`. Relevant for
  `feedback_explicit_git_add_during_parallel_sessions.md`:
  the rule "explicit per-file add" held; the failure mode
  was index concurrency, not wide-net staging.
- **Resolver module now embeds some fireasmserver-specific
  knowledge** (NASM macro syntax, GAS label syntax,
  `.py`/`.S`/`.c`/`.h`/`.md` suffix routing). If this tool
  ever gets extracted to be reusable across projects, the
  suffix-to-handler map should become a constructor-injected
  registry. Not a today-problem.
