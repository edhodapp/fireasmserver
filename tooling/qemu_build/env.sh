# SPDX-License-Identifier: AGPL-3.0-or-later
# Prepend the fork-qemu sandbox to PATH for the current shell.
# Source this file — do not execute it.
#
#   source tooling/qemu_build/env.sh
#
# After sourcing, `qemu-x86_64`, `qemu-system-x86_64`, etc.
# resolve to the fork build at $QEMU_PREFIX/bin rather than
# any system-installed qemu.

QEMU_PREFIX="${QEMU_PREFIX:-$HOME/opt/qemu-fork}"

if [[ ! -d "$QEMU_PREFIX/bin" ]]; then
    echo "tooling/qemu_build/env.sh: $QEMU_PREFIX/bin not found;"
    echo "  run tooling/qemu_build/build_qemu_fork.sh first." >&2
    return 1 2>/dev/null || exit 1
fi

case ":$PATH:" in
    *":$QEMU_PREFIX/bin:"*) ;;
    *) export PATH="$QEMU_PREFIX/bin:$PATH" ;;
esac
