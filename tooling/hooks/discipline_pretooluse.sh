#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# PreToolUse hook wrapper for fireasmserver: inject canonical
# schema/decisions/requirements context BEFORE every Edit/Write
# tool call.
#
# Reads the hook input JSON from stdin, extracts tool_input.file_path,
# runs `discipline-print` on it, and emits the stdout to Claude as
# `hookSpecificOutput.additionalContext` so the canonical context
# lands in front of the model before the edit executes.
#
# Silent and harmless on:
#   - hooks without a file_path in tool_input (Edit/Write always have
#     one; other tool matchers may not)
#   - paths the relevance map doesn't claim (discipline-print emits a
#     one-line "no canonical context for X" note; we filter that case
#     so the model isn't spammed with empty diagnostics on every Edit
#     to a non-domain file)
#   - any hook-side error (no JSON output → no additional context;
#     the edit proceeds because we never write decision/continue)

set -uo pipefail

VENV_BIN="/home/ed/fireasmserver/.venv/bin/discipline-print"
[ -x "$VENV_BIN" ] || exit 0

FILE_PATH=$(jq -r '.tool_input.file_path // empty' 2>/dev/null || true)
[ -n "$FILE_PATH" ] || exit 0

CONTEXT=$("$VENV_BIN" "$FILE_PATH" 2>/dev/null || true)
[ -n "$CONTEXT" ] || exit 0

# discipline-print emits exactly one line for paths outside the
# relevance map ("# discipline-print: no canonical context for X").
# Skip injection in that case so non-domain edits aren't spammed.
case "$CONTEXT" in
    "# discipline-print: no canonical context for"*) exit 0 ;;
esac

jq -n --arg ctx "$CONTEXT" \
    '{hookSpecificOutput: {hookEventName: "PreToolUse", additionalContext: $ctx}}' \
    2>/dev/null || true
