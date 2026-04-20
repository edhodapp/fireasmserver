"""Side-session bootstrap — dispatches a scoped task to a peer git worktree.

Architecture: DECISIONS.md D052.

Public API surface kept deliberately small:

    from side_session_bootstrap import (
        Bootstrapper,
        BootstrapError,
        BootstrapResult,
    )

The CLI at ``side_session_bootstrap.cli`` is a thin argparse
adapter over ``Bootstrapper``. Tests exercise either the
``Bootstrapper`` class directly (unit / most behavioral) or the
CLI via ``cli.main(argv)`` (argparse-layer + exit-code tests).
"""

from __future__ import annotations

from side_session_bootstrap.bootstrap import (
    Bootstrapper,
    BootstrapError,
    BootstrapResult,
)

__all__ = ["Bootstrapper", "BootstrapError", "BootstrapResult"]
