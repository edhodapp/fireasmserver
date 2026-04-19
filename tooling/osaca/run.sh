#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run OSACA static pipeline analysis on a built guest ELF. Advisory
# per D005 — prints the report and always exits 0 so the caller's
# overall cell result isn't affected by perf findings at this stage.
#
# Usage: tooling/osaca/run.sh <arch> <platform>
#
# OSACA wants pure-instruction GAS-style input; our boot.S files have
# // line comments, directives, and .rodata sections that its parser
# rejects. We go around that by disassembling the built ELF with
# objdump and preprocessing to strip:
#   - objdump's section/header/label lines
#   - the address + raw-bytes prefix on each instruction line
#   - objdump's symbolic <target> annotations after branch operands
#   - objdump's "// #<decimal>" immediate-value annotations
#   - data pseudo-ops (.word, .short, .byte) and the 'udf' instructions
#     capstone-style skipdata would emit (for the Linux arm64 Image
#     header's non-code bytes)
#
# Result is a clean instruction stream that OSACA's AArch64 / x86
# parsers can ingest. Microarchitecture choice per cell is mid-range
# reasonable:
#   x86_64   → SKX   (Skylake-X; Firecracker hosts are typically Xeon)
#   aarch64  → N1    (Neoverse-N1; closest available match for the Pi
#                     5's Cortex-A76; OSACA does not yet model A76)

set -euo pipefail

ARCH="${1:?usage: $0 <arch> <platform>}"
PLATFORM="${2:?usage: $0 <arch> <platform>}"

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

TMPDIR="$(mktemp -d)"
cleanup() {
    if [[ -d "$TMPDIR" ]]; then
        rm -rf "$TMPDIR"
        echo "--- cleanup: removed $TMPDIR ---"
    fi
}
trap cleanup EXIT INT TERM

case "$ARCH" in
    x86_64)
        OBJDUMP=x86_64-linux-gnu-objdump
        OSACA_ARCH=SKX
        ELF="arch/x86_64/build/${PLATFORM}/guest.elf"
        ;;
    aarch64)
        OBJDUMP=aarch64-linux-gnu-objdump
        OSACA_ARCH=N1
        ELF="arch/aarch64/build/${PLATFORM}/guest.elf"
        ;;
    *)
        echo "ERROR: unknown arch '$ARCH'" >&2
        exit 1
        ;;
esac

[[ -f "$ELF" ]] || {
    echo "ERROR: missing $ELF — run make first" >&2
    exit 1
}

ASM="$TMPDIR/kernel.s"

echo "=== OSACA static pipeline analysis: $ARCH/$PLATFORM ($OSACA_ARCH) ==="
echo "--- extracting instruction stream from $ELF ---"

# The awk block keeps only instruction lines and strips the
# "<hex>:\t" leader. The sed block kills objdump annotations that
# confuse OSACA's parser.
"$OBJDUMP" -d --no-show-raw-insn "$ELF" \
    | awk '
        /^ *[0-9a-f]+:\t\./ { next }      # skip .word / .byte / .short
        /^ *[0-9a-f]+:\tudf\b/ { next }   # skip explicit undefined
        /^ *[0-9a-f]+:\t/ {
            sub(/^ *[0-9a-f]+:\t/, "\t")
            print
        }
    ' \
    | sed -E 's/\t*<[^>]+>//g; s/ *\/\/.*//' > "$ASM"

NLINES=$(wc -l < "$ASM")
if [[ "$NLINES" -eq 0 ]]; then
    echo "--- no instructions extracted; nothing to analyze ---"
    exit 0
fi
echo "--- $NLINES instructions to analyze ---"

# --ignore-unknown so the Cortex-A76/SKX DBs missing an odd opcode
# (yield, wfe, PL011 MMIO accesses, etc.) don't abort the run. Advisory.
.venv/bin/osaca \
    --arch "$OSACA_ARCH" \
    --lines "1-$NLINES" \
    --ignore-unknown \
    "$ASM" 2>&1 || {
    echo "--- OSACA exited non-zero (advisory — cell not failed) ---"
}

exit 0
