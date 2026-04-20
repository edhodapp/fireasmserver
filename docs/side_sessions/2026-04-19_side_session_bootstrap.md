# Side-session briefing: `side_session_bootstrap` — ontology-backed briefing generator + branch cutter

**For:** a fresh Claude Code session running in another terminal tab.
**Parent session owner:** main (architectural) Claude, coordinating
via git. See "Coordination with parent session" below.

**Branch:** `side/2026-04-19_side_session_bootstrap` — create this
branch at the current `main` tip before your first commit (see
"Commit + push discipline" below). **This is the first side session
running under the branch-and-merge rule**; everything you do lives on
that branch, and the main session merges it when you signal
complete.

---

## Read these before writing any code

### Project-level

1. **`~/.claude/CLAUDE.md`** (user-global rules across projects).
   Load-bearing: CD-first; build-custom-tools-leverage-AI-speed;
   rigorous path at decision points; technical writing as lab
   notebook; testability rules (behavioral then unit, track every
   deviation, repro before fix); git workflow; mandatory pre-commit
   pipeline.
2. **`CLAUDE.md`** at repo root if present.
3. **`DECISIONS.md`** — read in full:
   - **D049** — ontology schema, status lifecycle, SysE fields.
     Your new `SideSessionTask` model is a sibling entity kind
     in the same `tooling/src/ontology/` package. Match its style
     (content-hash idempotency, Pydantic v2, no mutable defaults
     without thought).
   - **D051** — `audit-ontology --exit-nonzero-on-gap` is now a
     pre-push gate. Any `implementation_refs` you add must resolve
     against the working tree; a broken ref blocks the push.
   - **D017** — two-tier quality gates (pre-commit blocking +
     pre-push integration).
   - **D046** — assembly-deferral bar — tooling quality equals
     product quality. This module meets the same bar.
4. **`tooling/src/ontology/models.py`** — read `DomainConstraint`
   and `PerformanceConstraint` as the nearest analogs for your
   new `SideSessionTask` kind. Both have `status`, traceability
   fields, and optional SysE extensions.
5. **`tooling/src/ontology/dag.py`** — understand how `load_dag` /
   `save_dag` / the snapshot mechanics work. `SideSessionTask`
   instances live in the DAG the same way other entities do.
6. **`tooling/src/ontology/types.py`** — `RequirementStatus`
   is the pattern for your new `SideSessionStatus` literal.
7. **`docs/side_sessions/2026-04-19_audit_ontology.md`** — the
   most recent hand-written briefing. The template you render
   should produce this shape (adapt the structure; don't copy
   the task-specific content). Pay particular attention to its
   Status / Deviations / Observations / Commit-SHAs closeout
   sections — those are the bits the side session fills in at
   completion.

### Memory (the main session's persistent feedback + project notes)

Located at `~/.claude/projects/-home-ed-fireasmserver/memory/`.
Read `MEMORY.md` as the index and load these files in full before
starting — they encode the rules your briefings must enforce:

**Shared-tree / parallel-session discipline:**
- `project_parallelization_strategy.md` — the branch-and-merge
  rule, directory discipline, gate expectations.
- `feedback_shared_index_coordination.md` — why we moved to
  branches: even explicit per-file `git add` can be defeated by
  the other session's `git reset` on the shared index.
- `feedback_explicit_git_add_during_parallel_sessions.md` — no
  `-a/-A/.` staging; per-file `git add` only.
- `feedback_side_session_file_scope.md` — side-session commits
  touch only files in their briefed scope; opportunistic
  out-of-scope fixes flag for main, not roll in.
- `feedback_cd_pipeline_main_owns_side_flags.md` — main owns
  `pre_push.sh`, `cd-matrix.yml`, `pyproject.toml` CD surface;
  side flags these in Deviations.

**Review discipline:**
- `feedback_no_silent_deferments.md` — when Gemini / clean-Claude
  surface findings, don't self-dispose as "low / defer"; surface
  with recommendation + tradeoff.
- `feedback_no_silent_suppressions.md` — lint suppressions need
  Ed's explicit OK.
- `feedback_review_do_not_modify.md` — review = inform, not fix.
- `feedback_pre_commit_gate_reads_working_tree.md` — `git add`
  after fixing a gate-flagged issue; the shared gate script
  reads from the working tree, not the staged blob.

**Design posture:**
- `feedback_no_future_proof_no_future_kill.md` — size for today;
  name / shape things so the scaled version fits without rewrite.
  Critical for this task: **do not build multi-agent
  orchestration plumbing**, but name things so an agent caller
  can plug in later.
- `project_agent_infra_deferred.md` — specific rules about what's
  in/out of scope for near-term tooling.
- `project_ontology_dogfooding.md` — **this task is the first
  ontology-dogfooding instance.** The ontology is the source of
  truth; the briefing markdown is a projection of the ontology
  node.
- `feedback_tooling_quality_equals_product_quality.md` — this
  module is product code, not a script.

---

## Two firm rules from Ed — violate neither

1. **Use Gemini for independent code review on every functional
   commit.** Pre-commit hook handles this automatically. Read
   the output, act on it, surface what you don't act on.
2. **No deferments without asking Ed first.** If a reviewer flags
   a concern, surface it with recommendation + tradeoff. Ed chooses.

---

## Task

Build `tooling/src/side_session_bootstrap/` — a Python module that
materializes a new side-session task as (a) a new `SideSessionTask`
entity in the project ontology (`tooling/qemu-harness.json`),
(b) a rendered markdown briefing at
`docs/side_sessions/<YYYY-MM-DD>_<slug>.md`, and (c) a git branch
`side/<YYYY-MM-DD>_<slug>` cut at the current `main` tip. A CLI
entry point `side-session-bootstrap` drives it.

### Why this tool exists

Briefings have been hand-written so far, which means boilerplate
drifts: the most recent one forgot a feedback memory, the one
before that inconsistently formatted the Status checklist, the
next one will omit the new branch-and-merge rule if we're
unlucky. Encoding the invariants into a tool makes each briefing
well-formed by construction. Equally important: the ontology is
load-bearing for requirements (D049) and for the audit gate
(D051), but it hasn't yet been the source of truth for anything
non-ontological. Side-session tasks are the first dogfood — if
the ontology can carry a task spec cleanly, it's exercised in a
new direction and any rough edges surface.

### Inputs (CLI)

```
side-session-bootstrap --slug <slug> \
    --scope <path> [--scope <path>...] \
    --required-reading <tag> [--required-reading <tag>...] \
    --deliverables <short-description-string> \
    [--rationale <text>] \
    [--date <YYYY-MM-DD>  # default: today]
```

- `<slug>`: short identifier, snake_case, matches ShortName regex
  already used elsewhere in the ontology (define limits in
  `types.py` if not present).
- `<path>`: repo-relative file or directory path. Multiple allowed.
  Paths are STORED as strings in the ontology; the tool validates
  at bootstrap time that they exist (or, for NEW paths the side
  session will create, that the parent directory exists).
- `<tag>`: identifier for a required-reading target. Accept these
  forms:
  - `CLAUDE.md` — global or project CLAUDE.md
  - `DECISIONS.md:D049` — a specific D-entry
  - `memory:feedback_shared_index_coordination` — a memory file
    (resolved against `~/.claude/projects/-home-ed-fireasmserver/memory/`)
  - `docs/l2/DESIGN.md` — an arbitrary doc path
  - Any other `path[:anchor]` form resolved verbatim
- `<deliverables>`: one-sentence summary; renders into the briefing
  header and the ontology node's `deliverables` field.
- `<rationale>`: optional longer text; renders into "Why this tool
  exists" placeholder if provided, else omitted.

### Processing

1. **Parse + validate inputs.** Reject malformed slugs, missing
   scope paths, duplicate required-reading tags. Pydantic model
   does the heavy lifting.
2. **Instantiate the ontology node.** Create a `SideSessionTask`
   Pydantic instance; compute its content hash the same way
   other entities do.
3. **Write to the DAG.** Load `tooling/qemu-harness.json`, add the
   new node, save. Use the existing `dag_transaction` /
   concurrent-safety machinery from `ontology/dag.py`. A snapshot
   row is appended.
4. **Render the briefing.** Template-render the canonical shape
   (required reading block, directory scope, task description,
   gate checklist, commit discipline, branch discipline, Status
   / Deviations / Observations / SHA placeholders). Write to
   `docs/side_sessions/<date>_<slug>.md`.
5. **Cut the branch.** `git checkout -b side/<date>_<slug>` from
   the current `main` HEAD. Fail loudly if the working tree is
   dirty (refuse to bootstrap mid-staged-work).
6. **Print the launch prompt.** A short string the main-session
   Claude (or Ed) pastes into the side session's terminal to kick
   it off. Shape:

   ```
   Read docs/side_sessions/<date>_<slug>.md and execute it.
   You are on branch side/<date>_<slug>. Do not checkout to main.
   Report your plan before writing code.
   ```

### Ontology model (SideSessionTask)

Add to `tooling/src/ontology/models.py`. Match the style of
`DomainConstraint` / `PerformanceConstraint`. Suggested shape —
adjust if you see a cleaner rendering:

```python
class SideSessionTask(BaseModel):
    """A scoped task dispatched to a side session.

    First ontology-dogfooding instance for non-requirements
    content: the task spec lives here; the markdown briefing is
    a rendering; the git branch + commits reference the task
    node by name. Lifecycle mirrors DomainConstraint's status
    pattern.
    """

    slug: ShortName                       # snake_case identifier
    date: str                             # "YYYY-MM-DD"
    branch_name: str                      # "side/<date>_<slug>"
    scope_paths: list[str]                # repo-relative
    required_reading: list[str]           # refs per spec above
    deliverables: str                     # summary sentence
    rationale: str = ""                   # optional longer text
    parent_commit_sha: str = ""           # main tip at bootstrap
    status: SideSessionStatus = "dispatched"
    commit_shas: list[str] = []           # side-branch commits
    merge_commit_sha: str = ""            # set by main at merge
```

Add `SideSessionStatus` to `tooling/src/ontology/types.py` as a
`Literal["dispatched", "in_progress", "merged", "reverted"]`.
Match `RequirementStatus`'s style.

`status` starts at `dispatched` when bootstrap runs. Subsequent
transitions (`in_progress`, `merged`, `reverted`) are done by
the main session at merge time, NOT by the bootstrap tool. The
bootstrap tool only handles the dispatch step.

### Briefing template

The rendered markdown must contain, in this order:
1. Title line with slug + date.
2. Branch name, explicitly called out.
3. **Read these before writing any code** section — required-
   reading tags resolved to human-readable paths + brief notes.
   The core set (CLAUDE.md global + project, DECISIONS D049,
   D051, D017, D046, the feedback memory files listed above,
   `project_parallelization_strategy.md`) is always included —
   treat the caller-supplied `--required-reading` tags as
   ADDITIONS to that core. The core set is the load-bearing
   discipline; caller tags are task-specific supplements.
4. **Two firm rules from Ed** block — identical prose each time.
5. **Task** section — deliverables + rationale + inputs/outputs
   (the latter two left blank for the caller's prose).
6. **Quality expectations** section — identical checklist each
   time (flake8 max-complexity=5, pylint, mypy --strict, pytest
   100% branch coverage, no hardcoded absolute paths,
   idempotent, no side effects on ontology beyond the one node
   write).
7. **Directory scope — own and do not exceed** — generated from
   `scope_paths`. Include the standard "Do NOT touch" list
   (ontology models unless explicitly in scope, pre-push hook,
   cd-matrix.yml, pyproject.toml).
8. **Coordination with parent (main) session** — identical prose
   each time, referencing the branch-and-merge rule.
9. **Gates to respect** — pre-commit + pre-push + D051 audit +
   clean-Claude review per commit. Identical prose each time.
10. **Commit + push discipline** — per-file `git add`, suggested
    commit shape placeholder.
11. **Definition of done** — checkbox list with the standard items
    (100% coverage, pre-push green, ran clean, SHAs recorded).
12. **Status** — checklist scaffold for the side session to fill.
13. **New CD surface** — scaffold for the side session to flag
    pyproject / hook / CI-file touches needed post-merge.
14. **Deviations from briefing** — empty section.
15. **Observations for the main session** — empty section.

### Outputs

- New node in `tooling/qemu-harness.json` (+ snapshot row in
  `.qemu-harness.json.history`).
- `docs/side_sessions/<YYYY-MM-DD>_<slug>.md` rendered file.
- `side/<YYYY-MM-DD>_<slug>` branch created, HEAD on it.
- Launch prompt printed to stdout.

### Idempotency + safety

- If the branch already exists: fail with a clear error. Do not
  overwrite.
- If the briefing file already exists: fail with a clear error.
- If the ontology already has a `SideSessionTask` with the same
  slug+date: fail. (Slug uniqueness WITHIN a date is the cheapest
  key.)
- If the working tree is dirty (staged or unstaged): fail.
  Bootstrap is explicitly a "clean-main → new-branch" operation.
- All three artifacts (ontology write, briefing file, branch) are
  created TRANSACTIONALLY: if branch creation fails, roll back
  the ontology write and delete the briefing file. The DAG's
  existing `dag_transaction` pattern is the right scaffolding.

### Non-goals (for this first cut)

- No automatic side-session launch / terminal control. Ed copies
  the printed prompt manually.
- No machine-agent dispatch path. `SideSessionTask` is designed
  to be liftable to that later (per
  `project_agent_infra_deferred.md`) but NOT built here.
- No pre-merge audit tool. Main session eyeballs at merge time.
- No `Side-Session-Task: <hash>` commit trailer enforcement.
  Acceptable if you add a TODO comment noting the future shape.
- No automatic status transition from `dispatched` → other
  values. Bootstrap sets `dispatched`; main-session merges are
  manual.

---

## Quality expectations

### Testing approach — behavioral tests first, unit tests second

**This is the heart of the task.** Per CLAUDE.md's Testability
rule, start with behavioral (happy-path and must-fail)
end-to-end tests that describe what the tool *does* from the
outside. Those tests drive the design — you write them first,
watch them fail, then grow the implementation until they pass.
As code artifacts emerge, add **unit tests** that pin internal
branch behavior for coverage.

**Behavioral tests (write these first):** each runs the full
`side-session-bootstrap` CLI (or the top-level `Bootstrapper`
call) against a temp git repo with a temp DAG fixture, then
asserts on the observable outcomes:

- **T-bootstrap-happy** — clean repo + all required CLI flags →
  (a) the DAG gains exactly one new `SideSessionTask` node with
  fields matching the inputs; (b) the briefing file exists at
  the expected path and contains the canonical sections in
  order; (c) the `side/<date>_<slug>` branch exists and HEAD is
  on it; (d) stdout contains a launch prompt with the slug.
- **T-bootstrap-refuses-dirty-tree** — staged or unstaged
  changes present → CLI exits non-zero with a clear message,
  NO DAG mutation, NO briefing file, NO branch.
- **T-bootstrap-refuses-existing-branch** — branch
  `side/<date>_<slug>` already exists → non-zero exit, clean
  error, NO DAG mutation, NO briefing file.
- **T-bootstrap-refuses-existing-briefing** — briefing file at
  the target path already exists → non-zero exit, NO DAG
  mutation, NO branch.
- **T-bootstrap-refuses-duplicate-task** — DAG already has a
  `SideSessionTask` with this slug+date → non-zero exit, NO
  new DAG write, NO briefing file, NO branch.
- **T-bootstrap-rollback-on-branch-failure** — simulate `git
  checkout -b` failing (e.g., by pre-creating the branch
  between DAG-write and branch-cut via a test hook) → the
  partially-written DAG change IS rolled back; the briefing
  file IS removed; exit non-zero. Tests the transactional
  guarantee.
- **T-briefing-renders-canonical-shape** — given a fixed
  `SideSessionTask`, the rendered markdown contains every
  section header listed in the Briefing Template spec, in the
  specified order, with the core required-reading set always
  included regardless of caller inputs.
- **T-launch-prompt-well-formed** — stdout after a successful
  run parses into: path to briefing, branch name, instruction
  to not checkout main.

Those tests are the spec. Everything else is implementation of
what makes them pass. Don't skip writing one because "it'll be
obvious" — the test is the evidence the behavior holds.

**Unit tests (second, for branch coverage):** pin internal
behavior — the renderer's handling of each section, the
ontology_writer's DAG-transaction integration, the git_ops
subprocess error mapping, the parser's validation of each CLI
flag. These close the 100% branch coverage target and catch
regressions narrower than the behavioral tests can.

Each test cross-references the requirement it exercises — in the
docstring, comment, or name. Behavioral test names prefixed
`test_bootstrap_*`; unit tests named after the function they
pin.

### Other quality bars

- **Tooling quality = product quality.** flake8
  `max-complexity=5`, pylint (Google style via `.pylintrc`), mypy
  `--strict`, pytest with 100% branch coverage on added code.
- **Factor for testability.** A `Bootstrapper` class or
  equivalent lets behavioral tests substitute the git / DAG /
  filesystem layers. The CLI is a thin argparse adapter that
  constructs the class and calls one method; all logic lives in
  the class so the tests can exercise it directly without
  subprocess overhead where unnecessary. (Behavioral tests
  exercising the full CLI via subprocess should also exist —
  those catch argparse / exit-code / stdout issues the unit path
  misses.)
- **No hardcoded absolute paths.** Paths resolve relative to
  `Path(__file__).resolve().parents[N]` or from CLI args.
- **Idempotent errors.** A mid-run failure must not leave
  half-written state. Use the DAG's transaction pattern +
  temp-file-then-rename for the briefing + delete-branch-on-
  failure for git. T-bootstrap-rollback-on-branch-failure is
  the acceptance test.
- **No side effects on the ontology beyond the one node write.**
  Don't rewrite other nodes, don't re-compute old content hashes,
  don't touch the audit tool's input surface.

---

## Directory scope — own and do not exceed

Own / create:

```
tooling/src/side_session_bootstrap/
    __init__.py
    __main__.py             # CLI entry
    cli.py                  # argparse + main()
    bootstrap.py            # orchestration (the Bootstrapper class)
    template.py             # briefing rendering
    ontology_writer.py      # DAG mutation
    git_ops.py              # branch creation + dirty-tree check
    py.typed
tooling/tests/
    test_side_session_bootstrap.py
```

Modify (narrow scope only):

- `tooling/src/ontology/models.py` — append `SideSessionTask`.
- `tooling/src/ontology/types.py` — append `SideSessionStatus`.
- `tooling/src/ontology/__init__.py` — export new names.

### Do NOT touch

- `tooling/src/audit_ontology/` — it's a sibling tool; import
  from it only if necessary.
- `tooling/build_qemu_harness_ontology.py` — main-session
  territory for populating the DAG.
- `tooling/qemu-harness.json` — the side session does NOT write
  a `SideSessionTask` into it by hand for testing; use test
  fixtures instead. The bootstrap tool ITSELF writes to this
  file at run time, but tests do so in temp dirs.
- `tooling/hooks/pre_push.sh`, `.github/workflows/cd-matrix.yml`,
  `pyproject.toml` — main owns the CD + console-script surface.
  If you need a `side-session-bootstrap = "..."` console-script
  entry in pyproject, flag it in the Deviations section for
  main-session integration.

### Coordination with parent (main) session

- **You are on branch `side/2026-04-19_side_session_bootstrap`,
  cut at `e0635dd` (or whatever `origin/main` is when you
  actually start — run `git pull --rebase origin main` first and
  confirm you're then on main at the current tip before cutting
  your branch).** All commits land on that branch. Push the
  branch to origin when you're ready for main to merge.
- **Main session does the merge.** When you signal complete (by
  filling in the Status section's final checkbox and pushing),
  main session fetches, reviews, and merges into `main`.
- **If main needs to concurrently modify `models.py` / `types.py`
  / `__init__.py`** — possible since those are shared ontology
  files — conflict resolution happens at merge time, not via a
  lock. Keep your edits tight so conflict surface stays small.

---

## Gates to respect

- **Pre-commit hook** (blocking): flake8, pylint, mypy, pytest
  on staged `.py`. Fires automatically.
- **Gemini advisory review**: fires automatically on staged
  `.py`. Read and act on; surface what you don't act on.
- **Clean-Claude subagent review** (REQUIRED every functional
  commit per CLAUDE.md): spawn a subagent with no project
  context, feed it the diff + review prompt at
  `~/tools/code-review/review-prompt.txt`. This gate is
  behavioral, not hook-enforced; do not skip it.
- **Pre-push hook** (blocking): full pytest suite, tracer
  bullets, CRC vectors, **D051 ontology audit**. The audit will
  resolve every `implementation_refs` in the ontology — so if
  you add a `SideSessionTask` in any test fixture that writes to
  the real `qemu-harness.json` with unresolved refs, pre-push
  will block. Write to temp paths in tests.
- **`git add` after fixing a gate-flagged issue.** The shared
  gate script reads the working tree; the commit stages the
  index. Re-stage explicitly.

---

## Commit + push discipline

Trunk-on-branch: you commit on `side/2026-04-19_side_session_bootstrap`,
push the branch to origin (`git push -u origin
side/2026-04-19_side_session_bootstrap` the first time), keep
pushing as you go.

Explicit per-file staging. Never `-a / -A / .`.

**Suggested commit shape** — note that behavioral tests are
written FIRST (C1), before any implementation. They fail. Then
implementation commits grow until they pass. This order is
load-bearing — it proves the tests describe behavior, not
whatever the code happened to do.

- **C1**: Behavioral test harness. Add `test_bootstrap_happy`,
  `test_bootstrap_refuses_*`, `test_briefing_renders_*`,
  `test_launch_prompt_*` in `test_side_session_bootstrap.py` as
  pytest functions that import a `Bootstrapper` / CLI entry
  that doesn't exist yet. Tests fail with ImportError — that's
  expected and the point. Plus the minimum skeleton needed to
  make imports resolve: `SideSessionTask` model stub,
  `Bootstrapper` class stub, `cli.main` stub, all raising
  `NotImplementedError`. Tests RED, branch cut from main.
- **C2**: `SideSessionTask` + `SideSessionStatus` fleshed out in
  `ontology/models.py` + `types.py` + `__init__.py` exports,
  plus unit tests pinning the Pydantic validation rules. Enough
  of the model to make `test_briefing_renders_*` pass when C3
  lands the renderer.
- **C3**: `template.py` renderer + `ontology_writer.py` (wraps
  the DAG transaction) + unit tests. The behavioral
  `test_briefing_renders_canonical_shape` goes GREEN in this
  commit.
- **C4**: `git_ops.py` + unit tests. `Bootstrapper` wired up in
  `bootstrap.py`. The behavioral `test_bootstrap_*` tests go
  GREEN. `test_bootstrap_rollback_on_branch_failure`
  specifically drives the transactional rollback design.
- **C5**: `cli.py` + `__main__.py` thin argparse layer + CLI-
  level behavioral tests that exercise the full subprocess
  path. `test_launch_prompt_well_formed` goes GREEN here.

Each commit goes green on its behavioral slice before moving on.
If you get to C5 and a C3-tier test went red, stop and fix —
don't paper over with a unit-test tweak.

---

## Definition of done

1. `side-session-bootstrap --slug demo --scope tooling/src/demo/
   --required-reading DECISIONS.md:D049 --deliverables "demo"`
   runs end-to-end in a clean tmp directory and produces:
   - a `SideSessionTask` node in the DAG,
   - a rendered briefing file,
   - a new branch.
2. Failure modes (existing branch, existing file, dirty tree,
   duplicate slug) each fail with clear errors and leave no
   partial state.
3. 100% branch coverage on new code.
4. Pre-push suite green (including D051 audit).
5. Commit SHAs recorded below.

## Status (updated by side session at completion)

- [ ] `SideSessionTask` model + tests
- [ ] `bootstrap.py` / `template.py` / `ontology_writer.py` + tests
- [ ] `git_ops.py` + tests
- [ ] CLI + end-to-end tests
- [ ] 100% branch coverage on the new module
- [ ] All failure modes exercised
- [ ] Pre-push integration suite green (on branch push)
- [ ] Branch pushed to origin; main-session notified for merge
- [ ] Commit SHAs recorded: <fill in at completion>

### New CD surface (flag for main session per
`feedback_cd_pipeline_main_owns_side_flags.md`)

- **New test file(s):** `<fill in>`
- **New runtime dependencies (if any):** `<fill in — likely none;
  stdlib + pydantic (already a dep)>`
- **New CLI console entry:** `side-session-bootstrap =
  "side_session_bootstrap.cli:main"` — main-session territory,
  flag for integration.
- **Any non-pytest gates introduced:** `<likely none>`

### Deviations from briefing

(Side session: fill this in at completion. Every deviation from
the plan above gets a bullet with reason, per the "track every
requirements deviation" rule.)

### Observations for the main session

(Anything the main session should know when integrating — cross-
cutting concerns, surprising findings, things to codify as
D-entries. Ontology-dogfooding friction points especially
welcome.)
