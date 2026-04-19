#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install project-local git hooks. Idempotent — safe to re-run.
# Creates (or refreshes) symlinks from .git/hooks/* to the tracked
# scripts in tooling/hooks/ so edits propagate without re-installing.
#
# pre-commit chains project-specific checks (D047 asm-syntax lint)
# ahead of the shared cross-project gates (Python + Gemini review).
# pre-push runs integration tests.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || {
    echo "ERROR: not inside a git working tree" >&2
    exit 1
})"
cd "$REPO_ROOT"

install_hook() {
    local hook=$1 script=$2
    local path=".git/hooks/$hook"
    local repo_rel="tooling/hooks/$script"    # path from repo root
    local link_target="../../$repo_rel"       # path from .git/hooks/
    # Refuse to install a dangling symlink. Check the target via its
    # repo-root-relative path (reliable from CWD) rather than the
    # link_target form (which is relative to the symlink, not CWD).
    if [[ ! -e "$repo_rel" ]]; then
        echo "ERROR: hook target '$repo_rel' does not exist" >&2
        exit 1
    fi
    # Refuse to clobber a non-symlink without confirmation — user may
    # have hand-rolled their own hook they'd rather keep.
    if [[ -e "$path" && ! -L "$path" ]]; then
        echo "ERROR: $path exists and is not a symlink." >&2
        echo "       Move it aside and re-run this installer." >&2
        exit 1
    fi
    ln -snf "$link_target" "$path"
    echo "installed: $path -> $link_target"
}

install_hook pre-commit pre_commit.sh
install_hook pre-push   pre_push.sh
