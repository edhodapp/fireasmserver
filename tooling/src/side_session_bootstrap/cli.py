"""Command-line entry point for ``side-session-bootstrap``.

Thin argparse adapter over ``Bootstrapper``: parse args,
construct the instance, run it, print the launch prompt on
success or the error on failure. Wiring lands in C6.
"""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point. Returns a process exit code.

    Invoked either directly (tests) or via the
    ``[project.scripts]`` entry that C6 adds to ``pyproject.toml``.
    """
    raise NotImplementedError(
        "cli.main() lands in C6 (CLI wiring); "
        "the C2 commit ships only the interface."
    )
