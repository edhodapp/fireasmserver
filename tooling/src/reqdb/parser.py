"""Parse the canonical per-requirement text into the reqdb model.

STUB — the implementation lands in a subsequent (green) commit. The
scaffold's behavioural and round-trip tests are authored RED against
this signature first (``xfail(strict=True)``) so the contract is
pinned before the logic exists.
"""

from __future__ import annotations

from pathlib import Path

from reqdb.model import ReqDB


def load_reqdb(src_dir: Path) -> ReqDB:
    """Load the authorities lookup plus every per-requirement file
    under ``src_dir`` into a :class:`ReqDB`.

    Not yet implemented — see the module docstring.
    """
    raise NotImplementedError(
        f"reqdb parser not implemented; cannot load {src_dir}",
    )
