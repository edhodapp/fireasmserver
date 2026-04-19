#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Guard against the GAS .intel_syntax noprefix MOV-source quirk pinned
# in D047. In that syntax, a bare symbol as a MOV source defaults to
# a memory reference rather than an immediate — `mov ecx, ready_len`
# (where ready_len is a .equ-defined constant) silently assembles to
# `mov ecx, [ready_len_value]`, a load from the value-as-address. Caught
# once the hard way in the x86_64 virtio-net MMIO probe; we want to
# catch it at commit time next time.
#
# Rule enforced: in .S files, a `mov <reg>, <bare-identifier>` line with
# no brackets, no leading `OFFSET`, no numeric/char literal is rejected.
# Fix by writing `mov <reg>, OFFSET <symbol>` for immediate loads or
# `mov <reg>, [<symbol>]` for explicit memory loads.
#
# Scope is x86_64 intel-syntax files only. AArch64 uses a different
# mnemonic grammar where this ambiguity does not arise; we filter the
# check to paths under arch/x86_64/.
#
# Modes:
#   Default:           scan files staged in the git index (pre-commit use).
#   Args given:        scan the specified files (CI / ad-hoc use).
#
# Exit 0 on clean, 1 if any violation is found. Prints file:line:match
# for every hit so editors can jump to them.

set -euo pipefail

# Candidate pattern: `mov <reg>, <bare-identifier>` with optional comment.
# Case-insensitive matching via grep -i covers both `mov` and `MOV`.
PATTERN='^[[:space:]]*mov[[:space:]]+[a-z][a-z0-9]*[[:space:]]*,[[:space:]]*[A-Za-z_][A-Za-z_0-9]*[[:space:]]*(#.*|//.*)?$'

# x86-64 GP, index, and segment register names. A `mov reg, reg` is
# not ambiguous and must not be flagged. Extend if we ever use MMX,
# XMM, YMM, ZMM, or debug/control registers.
REG_RX='^(a[lhx]|b[lhx]|c[lhx]|d[lhxi]|si[l]?|di[l]?|bp[l]?|sp[l]?|eax|ebx|ecx|edx|esi|edi|ebp|esp|rax|rbx|rcx|rdx|rsi|rdi|rbp|rsp|r[89][bwdl]?|r1[0-5][bwdl]?|cs|ds|es|fs|gs|ss|ip|eip|rip|offset)$'

if [[ $# -gt 0 ]]; then
    FILES=("$@")
else
    mapfile -t FILES < <(git diff --cached --name-only --diff-filter=ACM | grep -E '^arch/x86_64/.*\.S$' || true)
fi

violations=0
for f in "${FILES[@]}"; do
    [[ -f "$f" ]] || continue
    [[ "$f" == arch/x86_64/* ]] || continue

    if [[ $# -eq 0 ]]; then
        content="$(git show ":$f" 2>/dev/null || true)"
    else
        content="$(cat "$f")"
    fi
    while IFS= read -r line; do
        lineno="${line%%:*}"
        text="${line#*:}"
        # Extract the source operand (everything after the comma,
        # stripped of comment and whitespace). Re-check against the
        # register exclusion list: a reg-to-reg MOV is legal.
        src="${text#*,}"
        src="${src%%#*}"
        src="$(echo -n "$src" | tr -d '[:space:]')"
        src_lc="$(echo -n "$src" | tr 'A-Z' 'a-z')"
        if [[ "$src_lc" =~ $REG_RX ]]; then
            continue
        fi
        echo "  $f:$lineno: $text"
        violations=$((violations + 1))
    done < <(printf '%s\n' "$content" | grep -niE "$PATTERN" || true)
done

if [[ $violations -gt 0 ]]; then
    echo
    echo "COMMIT BLOCKED — $violations GAS intel-syntax MOV-source ambiguity hit(s)."
    echo "Per D047: bare .equ / symbol names as MOV sources silently become"
    echo "memory loads under .intel_syntax noprefix. Rewrite as:"
    echo "    mov <reg>, OFFSET <symbol>      # immediate load"
    echo "    mov <reg>, [<symbol>]           # explicit memory load"
    exit 1
fi
exit 0
