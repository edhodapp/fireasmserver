#!/usr/bin/env bash
# Triple-fault diagnostic for x86_64 cells running under QEMU.
#
# Per D062's "Triple-fault traps during early bring-up" watch-for: a
# bad GDT load, page-table descriptor, or far-jump target in QEMU
# silently triple-faults — the cell just resets, the tracer-bullet
# harness reports "READY not observed", and there's no signal pointing
# at the failing instruction. This tool runs the same kernel binary
# under QEMU with `-d int,cpu_reset` debug instrumentation, captures
# the CPU register state at the reset, and dumps the disassembly
# around the failing RIP/EIP so a developer can bisect quickly.
#
# Usage:
#   triple_fault_diag.sh <PLATFORM> [TIMEOUT]
#     PLATFORM: qemu | firecracker
#     TIMEOUT:  seconds to wait before killing QEMU (default 5)
#
# Limitations:
#   - QEMU-only. Today the boot path is `-machine pc -kernel <elf>`,
#     which expects a Multiboot1-compatible image — so the qemu cell
#     boots cleanly while the firecracker (PVH) cell will fail in BIOS
#     because no Multiboot magic is found. Diagnosing firecracker-only
#     triple-faults will need either (a) extending the QEMU command
#     line for PVH boot via `qemu-system-x86_64 -kernel <pvh-elf>` with
#     the right CPU/memory profile (PVH boot in QEMU is supported but
#     finicky), or (b) Firecracker-side tracing. Filed for when it
#     bites.
#   - Exit codes: 0 = no kernel-image faults observed (boot reached
#     the timeout cleanly OR hung without faulting); 1 = at least one
#     fault inside the kernel image (likely triple-fault chain) and
#     the diagnostic has output worth reading; 2 = usage error or
#     missing artifact.
#
# Output sections:
#   1. serial.log — what the guest emitted (READY etc.) before the fault.
#   2. CPU resets summary — counts of init/BIOS resets (filtered out
#      as normal boot noise) and kernel-image resets (the interesting
#      ones). Init resets list shows their EIPs for visibility.
#   3. kernel-image faults — full register state at the LAST reset in
#      the log. In a triple-fault loop every iteration faults at the
#      same address, so the last block is representative.
#   4. last fault → source — objdump excerpt around the failing RIP/EIP,
#      so the failing instruction is visible.

set -euo pipefail

PLATFORM="${1:-}"
TIMEOUT="${2:-5}"

if [[ -z "$PLATFORM" ]]; then
    echo "Usage: $(basename "$0") <PLATFORM> [TIMEOUT]" >&2
    echo "       PLATFORM: qemu | firecracker" >&2
    exit 2
fi

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

case "$PLATFORM" in
    qemu)        ELF="arch/x86_64/build/qemu/guest.elf" ;;
    firecracker) ELF="arch/x86_64/build/firecracker/guest.elf" ;;
    *)
        echo "ERROR: unknown PLATFORM '$PLATFORM' (qemu|firecracker)" >&2
        exit 2
        ;;
esac

if [[ ! -f "$ELF" ]]; then
    echo "ERROR: $ELF not found — run 'make -C arch/x86_64 PLATFORM=$PLATFORM' first" >&2
    exit 2
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT INT TERM

SERIAL="$TMPDIR/serial.log"
QEMU_LOG="$TMPDIR/qemu-debug.log"

echo "=== triple-fault diag: x86_64/$PLATFORM (timeout ${TIMEOUT}s) ==="
echo "    elf:    $ELF"
echo "    serial: $SERIAL"
echo "    debug:  $QEMU_LOG"
echo

# QEMU debug flags chosen for triple-fault diagnosis:
#   -d int,cpu_reset  log every interrupt/exception + every CPU reset
#                     with full register state. The "cpu_reset" channel
#                     dumps RAX/RBX/.../RIP/CR0/CR3/CR4 at each reset
#                     event — exactly what we need for "what was the
#                     CPU doing when the triple-fault hit".
#   -D <file>         direct debug output to a file (otherwise it goes
#                     to stderr and gets mixed with QEMU's own messages).
#
# Note: we intentionally do NOT pass -no-reboot. Under -no-reboot,
# QEMU quits the emulator on triple-fault BEFORE logging a CPU Reset
# entry, leaving the diagnostic empty. Without it, QEMU loops:
# reset → BIOS → kernel → triple-fault → reset → ... — bounded by
# the timeout — and each iteration logs its own reset events. The
# count and EIP location of those resets is exactly what we need
# to bisect: a known-good boot logs the 2 init resets (cold + BIOS
# reset vector) once; a triple-faulting boot adds one reset per
# loop iteration with EIP somewhere in the kernel image.
QEMU_ARGS=(
    -machine pc -cpu qemu64 -m 128
    -display none
    -serial "file:$SERIAL"
    -d int,cpu_reset
    -D "$QEMU_LOG"
    -kernel "$ELF"
)

timeout "${TIMEOUT}s" qemu-system-x86_64 "${QEMU_ARGS[@]}" || true

echo "--- serial.log ---"
if [[ -s "$SERIAL" ]]; then
    sed 's/^/    /' "$SERIAL"
else
    echo "    (empty — boot didn't reach any serial emit)"
fi
echo

# Defensive: if QEMU died before writing the debug log (e.g., bad
# kernel format rejected by the ELF loader), -D never created the
# file. Subsequent awk/grep on a missing file would abort the script
# under set -e with an opaque error.
if [[ ! -f "$QEMU_LOG" ]]; then
    echo "ERROR: QEMU produced no debug log at $QEMU_LOG" >&2
    echo "       (likely the ELF was rejected before any execution)" >&2
    exit 2
fi

# Distinguish kernel-image resets from BIOS/SMM init resets. QEMU's
# `-d cpu_reset` logs every reset event including the cold boot reset
# (all-zero registers, EIP=0) and the SMM-RSM reset (EIP in BIOS-area
# 0xF0000-0xFFFFF). Those are NOT triple-faults; they're normal init
# events. Filtering them out by EIP/RIP location is more robust than
# counting "first N resets are init": kernel-image addresses sit at
# >= _start (typically 0x100000), so any reset with EIP/RIP in that
# range is kernel-relevant.
#
# Get the kernel image's _start address as the lower bound.
START=$(x86_64-linux-gnu-nm "$ELF" 2>/dev/null \
    | awk '/ _start$/ { print $1; exit }')
if [[ -z "$START" ]]; then
    echo "ERROR: could not resolve _start in $ELF" >&2
    exit 2
fi
# Treat _start address as a hex literal lower bound. printf normalizes
# leading-zero variations.
START_DEC=$(printf '%d' "0x$START")

# `grep -c` on a no-match exits 1; `|| echo 0` provides a defined
# fallback. The earlier file-existence guard means missing-file is
# handled, so the only remaining failure is no-match.
reset_count=$(grep -cE "^CPU Reset" "$QEMU_LOG" 2>/dev/null || echo 0)
if [[ "$reset_count" -eq 0 ]]; then
    echo "--- no CPU resets observed ---"
    echo "    Boot either succeeded or hung past the ${TIMEOUT}s budget."
    echo "    Check serial.log above for last-emitted marker."
    exit 0
fi

# Build an array of (RIP_or_EIP, address_dec) for each reset block.
# Each reset block looks like:
#     CPU Reset (CPU N)
#     EAX=...  EBX=...  ECX=...  EDX=...
#     ESI=...  EDI=...  EBP=...  ESP=...
#     EIP=XXXXXXXX EFL=...
#     ...
# (or RIP=... in 64-bit blocks). One EIP/RIP per block.
mapfile -t RESET_ADDRS < <(awk '
    /^CPU Reset/ { in_block = 1 }
    in_block && /^E?IP=[0-9a-fA-F]+/ {
        # Extract just the hex address after = .
        n = split($1, a, "=")
        print a[2]
        in_block = 0
    }
    in_block && /^RIP=[0-9a-fA-F]+/ {
        n = split($1, a, "=")
        print a[2]
        in_block = 0
    }
' "$QEMU_LOG")

# Partition resets into "kernel-image" (EIP >= _start) and "init/BIOS"
# (EIP < _start). The kernel-image bucket is what triple-fault diag
# cares about; the init bucket is normal boot noise.
kernel_resets=()
init_resets=()
for addr_hex in "${RESET_ADDRS[@]}"; do
    addr_dec=$(printf '%d' "0x$addr_hex" 2>/dev/null || echo 0)
    if (( addr_dec >= START_DEC )); then
        kernel_resets+=("$addr_hex")
    else
        init_resets+=("$addr_hex")
    fi
done

echo "--- CPU resets ($reset_count observed: ${#init_resets[@]} init, ${#kernel_resets[@]} kernel) ---"
if [[ ${#init_resets[@]} -gt 0 ]]; then
    echo "    init/BIOS resets (filtered out, normal boot noise):"
    for h in "${init_resets[@]}"; do echo "      EIP=0x$h"; done
fi
if [[ ${#kernel_resets[@]} -eq 0 ]]; then
    echo
    echo "--- no kernel-image faults ---"
    echo "    The kernel did not triple-fault during the ${TIMEOUT}s budget."
    echo "    If serial.log above shows the expected markers, the boot"
    echo "    succeeded and was killed by timeout (normal). If markers"
    echo "    are missing, the cell hung past the budget without faulting"
    echo "    — check for an infinite loop or a wedged poll."
    exit 0
fi
echo

echo "--- kernel-image faults ($((${#kernel_resets[@]})) — likely triple-fault chain) ---"
# Dump the register state for the LAST kernel-image reset block.
# In a triple-fault loop the order is: kernel-fault → CPU resets to
# BIOS reset vector → BIOS init triggers SMM-RSM → loops back to
# kernel-fault → ... so the LAST reset in the log is usually a BIOS
# init reset, not the kernel fault we care about. Track the last
# block whose EIP/RIP matches `target` (the last-known kernel-image
# fault address); emit only that block.
#
# Block bounded to 30 lines after the "CPU Reset" header — enough
# to capture the full register dump in both 32-bit and 64-bit modes
# (~25 lines observed empirically), without trailing into subsequent
# interrupt traces emitted by `-d int`.
#
# Use ${arr[$((${#arr[@]}-1))]} rather than ${arr[-1]} for bash
# < 4.3 compatibility (negative array indices were added in 4.3).
last_addr="${kernel_resets[$((${#kernel_resets[@]}-1))]}"
awk -v target="$last_addr" '
    function flush_if_matched() {
        if (matched && current != "") last_match = current
    }
    /^CPU Reset/ {
        flush_if_matched()
        current = $0 "\n"
        lines = 1
        matched = 0
        next
    }
    lines < 30 {
        current = current $0 "\n"
        lines++
        if ($0 ~ "^E?IP=" target || $0 ~ "^RIP=" target) {
            matched = 1
        }
    }
    END {
        flush_if_matched()
        printf "%s", last_match
    }
' "$QEMU_LOG" | sed 's/^/    /'
echo

# Disassemble around the failing address.
addr="$last_addr"
echo "--- last fault → source ---"
echo "    fault address: 0x$addr"
addr_lc=$(echo "$addr" | tr 'A-F' 'a-f' | sed 's/^0*//')
[[ -z "$addr_lc" ]] && addr_lc=0
echo "    disassembly window:"
x86_64-linux-gnu-objdump -d "$ELF" 2>/dev/null \
    | grep -B 4 -A 4 -E " $addr_lc:" | sed 's/^/    /' || \
    echo "    (no objdump match — address may be outside .text)"

# Exit non-zero ONLY if kernel-image resets were observed. BIOS/SMM
# init resets are normal and should not signal a problem.
exit 1
