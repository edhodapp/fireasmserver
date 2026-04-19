#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Project-local pre-commit chain. Currently has no project-specific
# checks (D047's asm-syntax lint was removed when x86_64 moved from
# GAS to NASM under D048). Kept as a wrapper so a future project-local
# check can slot in ahead of the shared hook without touching the
# symlink at .git/hooks/pre-commit.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

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
