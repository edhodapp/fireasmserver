"""T4 requirements-coverage gate (task #40).

Enforces the 1-to-many D→REQ coverage policy adopted 2026-05-01
(`project_decisions_requirements_coverage_policy.md`). For every
non-superseded D-class decision in `DECISIONS.md`:

1. A `**Requirements:**` annotation must exist immediately after
   the `### D...:` heading.
2. Each REQ-ID listed in the annotation must resolve to a known
   requirement in `REQUIREMENTS.md` or
   `docs/l2/REQUIREMENTS.md`. N/A and "see block" forms are
   permitted without ID validation.

The gate runs at pre-commit time. The text formatter produces a
human-readable matrix; `--exit-nonzero-on-error` makes the CLI
suitable for hook integration.
"""

__all__ = ["audit", "cli", "parser", "formatter"]
