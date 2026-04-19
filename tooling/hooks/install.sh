#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install project-local git hooks.
# Idempotent — safe to re-run. Creates (or refreshes) a symlink
# from .git/hooks/pre-push to tooling/hooks/pre_push.sh so edits to
# the tracked script propagate without re-running this installer.
#
# The pre-commit hook is installed separately (Ed's global
# ~/tools/code-review/pre-commit-hook.sh symlink) and is not
# managed here — quality gates are cross-project, integration
# tests are this-project.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || {
    echo "ERROR: not inside a git working tree" >&2
    exit 1
})"
cd "$REPO_ROOT"

HOOK=".git/hooks/pre-push"
TARGET="../../tooling/hooks/pre_push.sh"

# Refuse to clobber a non-symlink without confirmation — the user
# may have hand-rolled their own hook they'd rather keep.
if [[ -e "$HOOK" && ! -L "$HOOK" ]]; then
    echo "ERROR: $HOOK exists and is not a symlink." >&2
    echo "       Move it aside and re-run this installer." >&2
    exit 1
fi

ln -snf "$TARGET" "$HOOK"
echo "installed: $HOOK -> $TARGET"
