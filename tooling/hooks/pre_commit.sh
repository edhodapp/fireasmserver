#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Project-local pre-commit chain. Runs project-specific blocking
# checks first, then hands off to the shared cross-project hook for
# Python gates + Gemini review.
#
# Order matters: the asm syntax lint is cheap (~ms) and blocks on a
# correctness issue (D047); run it ahead of the more expensive
# Python-gate + Gemini review pipeline so a failing commit stops early.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# D047 guard — GAS .intel_syntax noprefix MOV-source ambiguity.
./tooling/hooks/asm_syntax_lint.sh

# Hand off to the shared hook (quality gates + Gemini review). Warn
# loudly if the shared hook is missing — silently skipping the Python
# gates would give false confidence that a commit was fully reviewed.
SHARED="$HOME/tools/code-review/pre-commit-hook.sh"
if [[ -x "$SHARED" ]]; then
    exec "$SHARED"
else
    echo "WARNING: shared pre-commit hook not found at $SHARED" >&2
    echo "         Python quality gates + Gemini review did NOT run." >&2
    echo "         Commit proceeds because only the project-local D047 lint is in scope here." >&2
fi
