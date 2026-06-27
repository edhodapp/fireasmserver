"""Generate and read the SQLite projection of a reqdb.

STUBS — implementations land green later. ``write_sqlite`` projects the
in-memory model into a normalised SQLite file (the generated query
artefact); ``read_sqlite`` reconstructs the model from it, enabling the
round-trip fidelity test. The database is always a derived artefact:
never hand-edited, never the source of truth.
"""

from __future__ import annotations

from pathlib import Path

from reqdb.model import ReqDB


def write_sqlite(db: ReqDB, out_path: Path) -> None:
    """Project ``db`` into a fresh SQLite file at ``out_path``.

    Not yet implemented — see the module docstring.
    """
    raise NotImplementedError(
        f"reqdb sqlite writer not implemented; cannot write "
        f"{len(db.requirements)} requirements to {out_path}",
    )


def read_sqlite(db_path: Path) -> ReqDB:
    """Reconstruct a :class:`ReqDB` from the SQLite file at ``db_path``.

    Not yet implemented — see the module docstring.
    """
    raise NotImplementedError(
        f"reqdb sqlite reader not implemented; cannot read {db_path}",
    )
