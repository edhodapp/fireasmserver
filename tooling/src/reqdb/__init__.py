"""reqdb — git-text-canonical requirements with a generated SQLite
projection (experiment; verification scaffold first).

Canonical source of truth is the per-requirement text under the
requirements tree; :mod:`reqdb.sqlite_gen` produces a queryable
database as a *derived* artefact. This package is an experiment per the
2026-06 design thread: the durable deliverable is the verification
scaffold (``tooling/tests/test_reqdb_*``), which proves correct
database generation before any real database is relied upon.
"""

from __future__ import annotations

from reqdb.model import (
    Authority,
    ImplementationRef,
    ReqDB,
    Requirement,
    SourceRef,
    VerificationRef,
)
from reqdb.parser import load_reqdb
from reqdb.sqlite_gen import UnknownAuthorityError, read_sqlite, write_sqlite

__all__ = [
    "Authority",
    "ImplementationRef",
    "ReqDB",
    "Requirement",
    "SourceRef",
    "UnknownAuthorityError",
    "VerificationRef",
    "load_reqdb",
    "read_sqlite",
    "write_sqlite",
]
