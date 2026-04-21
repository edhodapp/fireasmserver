"""Briefing renderer.

Single function: ``render_briefing(task) -> str`` produces the
canonical markdown shape the side session reads. The shape is
load-bearing — the behavioral test in
``test_side_session_bootstrap.py`` asserts every section header
appears, in order. Don't reorder or rename without updating that
test (the renaming is the entire point of having that test).

The "core required reading" set is constant — every dispatched
side session reads the same architectural baseline before
caller-supplied additions. This is the rule the bootstrap tool
exists to enforce: no briefing can ship without the load-bearing
references.
"""

from __future__ import annotations

from ontology import SideSessionTask, make_branch_name


# Required-reading anchors that every briefing carries regardless
# of caller input. Each entry is (display_label, brief_note).
# The behavioral test
# ``test_briefing_renders_canonical_sections_in_order`` asserts
# that the strings ``CLAUDE.md``, ``D049``, ``D051``, ``D052``,
# and ``project_parallelization_strategy`` all appear in the
# rendered output.
_CORE_REQUIRED_READING: list[tuple[str, str]] = [
    ("~/.claude/CLAUDE.md",
     "user-global rules across projects"),
    ("CLAUDE.md (project root, if present)",
     "project-local conventions"),
    ("DECISIONS.md — D049",
     "ontology as formal verifiable requirements (the schema "
     "your task may extend)"),
    ("DECISIONS.md — D051",
     "ontology audit as closing pre-push gate; broken refs "
     "block the push"),
    ("DECISIONS.md — D052",
     "side-session isolation via git worktree (this dispatch "
     "happens under that architecture)"),
    ("memory/project_parallelization_strategy.md",
     "scope rules + gate-discipline expectations"),
    ("memory/feedback_shared_index_coordination.md",
     "why we use worktrees — pre-D052 incidents"),
    ("memory/feedback_explicit_git_add_during_parallel_sessions.md",
     "per-file `git add` discipline"),
    ("memory/feedback_no_silent_deferments.md",
     "review findings get surfaced, not self-disposed"),
    ("memory/feedback_side_session_file_scope.md",
     "commits touch only files in this briefing's scope"),
    ("memory/feedback_cd_pipeline_main_owns_side_flags.md",
     "main owns pre_push.sh / cd-matrix.yml / pyproject; flag "
     "for integration"),
]


def render_briefing(task: SideSessionTask) -> str:
    """Render the canonical briefing markdown for ``task``.

    Output structure (load-bearing — must match
    ``CANONICAL_SECTION_HEADERS`` in the behavioral test):

    1. Title (slug + date)
    2. Branch name
    3. Read these before writing any code
    4. Two firm rules from Ed
    5. Task
    6. Quality expectations
    7. Directory scope
    8. Coordination with parent
    9. Gates to respect
    10. Commit + push discipline
    11. Definition of done
    12. Status
    13. Deviations from briefing
    14. Observations for the main session
    """
    branch = make_branch_name(task.slug, task.date)
    parts: list[str] = []
    parts.append(_render_header(task, branch))
    parts.append(_render_required_reading(task))
    parts.append(_FIRM_RULES_BLOCK)
    parts.append(_render_task(task))
    parts.append(_QUALITY_EXPECTATIONS_BLOCK)
    parts.append(_render_directory_scope(task))
    parts.append(_render_coordination(branch))
    parts.append(_GATES_BLOCK)
    parts.append(_render_commit_push(branch))
    parts.append(_DEFINITION_OF_DONE_BLOCK)
    parts.append(_STATUS_BLOCK)
    parts.append(_DEVIATIONS_BLOCK)
    parts.append(_OBSERVATIONS_BLOCK)
    return "\n\n".join(parts) + "\n"


def _render_header(task: SideSessionTask, branch: str) -> str:
    return (
        f"# Side-session briefing: `{task.slug}` ({task.date})\n\n"
        f"**Branch:** `{branch}` — you are in the peer worktree at "
        f"this branch's tip; never check out another branch.\n\n"
        f"**Deliverables:** {task.deliverables}"
    )


def _render_required_reading(task: SideSessionTask) -> str:
    """Core set + caller-supplied additions. Both appear in the
    rendered output; caller supplements never replace the core."""
    lines = ["## Read these before writing any code", ""]
    lines.append("### Core (mandatory regardless of task)")
    lines.append("")
    for label, note in _CORE_REQUIRED_READING:
        lines.append(f"- **{label}** — {note}")
    if task.required_reading:
        lines.append("")
        # Subheading deliberately avoids beginning with the
        # word "Task" — the canonical-shape test uses substring
        # matching on ``## Task`` and ``### Task...`` would
        # collide with that.
        lines.append("### Caller-supplied additions")
        lines.append("")
        for tag in task.required_reading:
            lines.append(f"- `{tag}`")
    return "\n".join(lines)


_FIRM_RULES_BLOCK = """\
## Two firm rules from Ed — violate neither

1. **Use Gemini for independent code review on every functional
   commit.** Pre-commit hook handles it automatically — read the
   output, act on it, surface what you don't act on.
2. **No deferments without asking Ed first.** If a reviewer flags
   a concern, surface it with recommendation + tradeoff. Ed
   chooses."""


def _render_task(task: SideSessionTask) -> str:
    body = f"## Task\n\n**What to build:** {task.deliverables}"
    if task.rationale:
        body += f"\n\n**Why this task exists:**\n\n{task.rationale}"
    return body


_QUALITY_EXPECTATIONS_BLOCK = """\
## Quality expectations

- **Tooling quality = product quality.** flake8
  `max-complexity=5`, pylint (Google style via `.pylintrc`),
  mypy `--strict`, pytest with 100% branch coverage on added
  code.
- **Behavioral tests first.** Write failing tests describing
  the user-observable contract before implementing. Each test
  cross-references the requirement it exercises.
- **No hardcoded absolute paths.** Resolve relative to
  `Path(__file__).resolve().parents[N]` or from CLI args.
- **No silent suppressions.** Any lint disable surfaces to Ed
  with the reason.
- **Atomic ontology writes.** Use the existing
  `dag_transaction` context for any DAG mutation."""


def _render_directory_scope(task: SideSessionTask) -> str:
    lines = [
        "## Directory scope — own and do not exceed",
        "",
        "Files / directories you MAY touch:",
        "",
    ]
    if task.scope_paths:
        for path in task.scope_paths:
            lines.append(f"- `{path}`")
    else:
        lines.append("- (none declared — confirm with main session "
                     "before touching any file)")
    lines.append("")
    lines.append("Anything outside this list goes to the main "
                 "session via the Deviations section, not into a "
                 "side-branch commit.")
    return "\n".join(lines)


def _render_coordination(branch: str) -> str:
    return (
        "## Coordination with parent (main) session\n\n"
        f"You are in the peer worktree on branch `{branch}`. "
        "Main session stays in the primary worktree on `main`. "
        "Each worktree has its own HEAD, index, and `.venv` "
        "(D052) — your `git` commands cannot affect main's "
        "working tree, and vice versa. Main does the merge "
        "when you signal complete."
    )


_GATES_BLOCK = """\
## Gates to respect

- **Pre-commit (blocking):** flake8 / pylint / mypy --strict /
  pytest with branch coverage on staged `.py` files. Fires
  automatically.
- **Gemini independent review:** fires automatically on staged
  `.py` / `.S`. Read findings, act on them, surface what you
  don't act on.
- **Clean-Claude subagent review:** spawn a subagent for each
  functional commit (per CLAUDE.md). The subagent has no
  project context — feed it the diff + the review prompt at
  `~/tools/code-review/review-prompt.txt`. This gate is
  behavioral, not hook-enforced; do not skip.
- **Pre-push (blocking):** full pytest suite + tracer bullets +
  CRC vectors + D051 ontology audit. The audit will block on
  any unresolved `implementation_refs` you add — write to
  fixtures in tests, not the real DAG."""


def _render_commit_push(branch: str) -> str:
    return (
        "## Commit + push discipline\n\n"
        f"You commit on `{branch}` and push the branch to "
        "origin (`git push -u origin <branch>` first time, "
        "plain `git push` after).\n\n"
        "**Explicit per-file `git add`** — never `-a`, `-A`, or "
        "`.`. Worktree isolation makes index contention with "
        "main impossible, but the discipline still produces "
        "smaller, easier-to-review commits.\n\n"
        "**Suggested commit shape:** behavioral tests first "
        "(RED), each subsequent commit turns one slice GREEN."
    )


_DEFINITION_OF_DONE_BLOCK = """\
## Definition of done

1. The deliverable's behavioral tests are GREEN.
2. 100% branch coverage on added code.
3. Pre-push integration suite green on the side branch's
   final push.
4. Branch pushed to origin; main session notified for merge.
5. Status checklist below filled in; commit SHAs recorded."""


_STATUS_BLOCK = """\
## Status (updated by side session at completion)

- [ ] Behavioral tests written and RED at start
- [ ] Implementation grown until each behavioral test GREEN
- [ ] 100% branch coverage on the new module
- [ ] Pre-push suite green on the side branch
- [ ] Branch pushed; main notified
- [ ] Commit SHAs recorded: <fill in at completion>"""


_DEVIATIONS_BLOCK = """\
## Deviations from briefing

(Side session: fill in at completion. Every deviation from the
plan above gets a bullet with reason, per the
"track every requirements deviation" rule.)"""


_OBSERVATIONS_BLOCK = """\
## Observations for the main session

(Anything the main session should know when integrating —
cross-cutting concerns, surprising findings, things to codify
as D-entries.)"""
