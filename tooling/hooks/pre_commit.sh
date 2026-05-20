#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Project-local pre-commit chain. P3 (task #40) wires the T4 D→REQ
# coverage gate ahead of the shared hook. The gate enforces the
# policy adopted 2026-05-01: every non-superseded D-class decision
# in DECISIONS.md must declare its Requirements, and every cited
# REQ-ID must resolve to a known entry in REQUIREMENTS.md or
# docs/l2/REQUIREMENTS.md.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# T4 — D→REQ coverage. Blocks on missing Requirements lines or
# broken REQ-ID refs. The venv-installed `req-coverage` entry point
# does the audit; if the venv is missing, fall through quietly so a
# bare clone can still commit (the shared hook below catches Python
# gates more comprehensively).
if [[ -x .venv/bin/req-coverage ]]; then
    if ! .venv/bin/req-coverage --exit-nonzero-on-error >/dev/null; then
        echo "" >&2
        echo "COMMIT BLOCKED — D→REQ coverage gate found findings." >&2
        echo "  Run \`.venv/bin/req-coverage\` for the matrix." >&2
        exit 1
    fi
fi

# Hand off to the shared hook (Python gates + Gemini review). Warn
# loudly if the shared hook is missing — silently skipping the gates
# would give false confidence that a commit was fully reviewed.
SHARED="$HOME/tools/code-review/pre-commit-hook.sh"
if [[ -x "$SHARED" ]]; then
    exec "$SHARED"
else
    echo "WARNING: shared pre-commit hook not found at $SHARED" >&2
    echo "         Python quality gates + Gemini review did NOT run." >&2
fi
