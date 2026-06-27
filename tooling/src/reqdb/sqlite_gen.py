"""Generate and read the SQLite projection of a reqdb.

``write_sqlite`` projects the in-memory model into a normalised SQLite
file (the generated query artefact); ``read_sqlite`` reconstructs the
model from it, enabling the round-trip fidelity test. The database is
always a derived artefact: never hand-edited, never the source of truth.

The schema is a faithful normalisation of :mod:`reqdb.model` — one
scalar table per top-level record plus a child table per multi-valued
relationship, child rows carrying an autoincrement ``id`` so their list
order survives the round-trip. ``supersedes`` / ``superseded_by`` are
plain columns, not foreign keys: a supersession pair can forward-
reference a requirement not yet inserted, so referential integrity for
those links is a model-level check, deferred.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from reqdb.model import (
    Authority,
    ImplementationRef,
    ReqDB,
    Requirement,
    SourceRef,
    VerificationRef,
)

_SCHEMA = """
CREATE TABLE authorities (
    authority_id  TEXT PRIMARY KEY,
    full_title    TEXT NOT NULL,
    publisher     TEXT NOT NULL,
    access        TEXT NOT NULL,
    canonical_url TEXT
);
CREATE TABLE requirements (
    req_id          TEXT PRIMARY KEY,
    category        TEXT NOT NULL,
    title           TEXT NOT NULL,
    statement       TEXT NOT NULL,
    verb_strength   TEXT NOT NULL,
    status          TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    notes           TEXT,
    supersedes      TEXT,
    superseded_by   TEXT
);
CREATE TABLE source_refs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id           TEXT NOT NULL REFERENCES requirements(req_id),
    authority_id     TEXT NOT NULL REFERENCES authorities(authority_id),
    kind             TEXT NOT NULL,
    section          TEXT NOT NULL,
    section_title    TEXT,
    citation         TEXT,
    content_hash     TEXT NOT NULL,
    retrieved        TEXT NOT NULL,
    retrieval_source TEXT NOT NULL
);
CREATE TABLE implementation_refs (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id TEXT NOT NULL REFERENCES requirements(req_id),
    arch   TEXT NOT NULL,
    file   TEXT NOT NULL,
    symbol TEXT,
    note   TEXT
);
CREATE TABLE verification_refs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id   TEXT NOT NULL REFERENCES requirements(req_id),
    ref_kind TEXT NOT NULL,
    file     TEXT NOT NULL,
    symbol   TEXT,
    note     TEXT
);
CREATE TABLE requirement_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id      TEXT NOT NULL REFERENCES requirements(req_id),
    decision_id TEXT NOT NULL
);
"""


def write_sqlite(db: ReqDB, out_path: Path) -> None:
    """Project ``db`` into a fresh SQLite file at ``out_path``.

    Any existing file is replaced, so the build is idempotent. Foreign
    keys are enforced; authorities are inserted before the requirements
    and child rows that reference them.
    """
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(str(out_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        _insert_authorities(conn, db.authorities)
        for req in db.requirements:
            _insert_requirement(conn, req)
        conn.commit()
    finally:
        conn.close()


def read_sqlite(db_path: Path) -> ReqDB:
    """Reconstruct a :class:`ReqDB` from the SQLite file at ``db_path``.

    Raises :class:`FileNotFoundError` if the file is absent rather than
    letting :func:`sqlite3.connect` create an empty database and return
    a silently-empty model.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"reqdb sqlite file not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        authorities = _read_authorities(conn)
        requirements = _read_requirements(conn)
    finally:
        conn.close()
    return ReqDB(authorities=authorities, requirements=requirements)


def _insert_authorities(
    conn: sqlite3.Connection,
    authorities: list[Authority],
) -> None:
    """Insert the authorities lookup rows."""
    conn.executemany(
        "INSERT INTO authorities "
        "(authority_id, full_title, publisher, access, canonical_url) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (a.authority_id, a.full_title, a.publisher, a.access,
             a.canonical_url)
            for a in authorities
        ],
    )


def _insert_requirement(
    conn: sqlite3.Connection,
    req: Requirement,
) -> None:
    """Insert one requirement's scalar row and all its child rows."""
    conn.execute(
        "INSERT INTO requirements (req_id, category, title, statement, "
        "verb_strength, status, authority_class, notes, supersedes, "
        "superseded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (req.req_id, req.category, req.title, req.statement,
         req.verb_strength, req.status, req.authority_class, req.notes,
         req.supersedes, req.superseded_by),
    )
    conn.executemany(
        "INSERT INTO requirement_decisions (req_id, decision_id) "
        "VALUES (?, ?)",
        [(req.req_id, decision) for decision in req.derived_from],
    )
    conn.executemany(
        "INSERT INTO source_refs (req_id, authority_id, kind, section, "
        "section_title, citation, content_hash, retrieved, "
        "retrieval_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (req.req_id, s.authority_id, s.kind, s.section,
             s.section_title, s.citation, s.content_hash, s.retrieved,
             s.retrieval_source)
            for s in req.source_refs
        ],
    )
    conn.executemany(
        "INSERT INTO implementation_refs (req_id, arch, file, symbol, "
        "note) VALUES (?, ?, ?, ?, ?)",
        [
            (req.req_id, i.arch, i.file, i.symbol, i.note)
            for i in req.implementation_refs
        ],
    )
    conn.executemany(
        "INSERT INTO verification_refs (req_id, ref_kind, file, symbol, "
        "note) VALUES (?, ?, ?, ?, ?)",
        [
            (req.req_id, v.ref_kind, v.file, v.symbol, v.note)
            for v in req.verification_refs
        ],
    )


def _read_authorities(conn: sqlite3.Connection) -> list[Authority]:
    """Read the authorities lookup, ordered by ``authority_id``."""
    rows = conn.execute(
        "SELECT authority_id, full_title, publisher, access, "
        "canonical_url FROM authorities ORDER BY authority_id",
    ).fetchall()
    return [Authority.model_validate(dict(row)) for row in rows]


def _read_requirements(conn: sqlite3.Connection) -> list[Requirement]:
    """Read every requirement with its child rows, ordered by ``req_id``."""
    rows = conn.execute(
        "SELECT req_id, category, title, statement, verb_strength, "
        "status, authority_class, notes, supersedes, superseded_by "
        "FROM requirements ORDER BY req_id",
    ).fetchall()
    return [_row_to_requirement(conn, row) for row in rows]


def _row_to_requirement(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> Requirement:
    """Assemble a :class:`Requirement` from its scalar row plus children."""
    req_id = row["req_id"]
    return Requirement(
        req_id=req_id,
        category=row["category"],
        title=row["title"],
        statement=row["statement"],
        verb_strength=row["verb_strength"],
        status=row["status"],
        authority_class=row["authority_class"],
        notes=row["notes"],
        derived_from=_read_decisions(conn, req_id),
        source_refs=_read_source_refs(conn, req_id),
        implementation_refs=_read_impl_refs(conn, req_id),
        verification_refs=_read_verif_refs(conn, req_id),
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
    )


def _read_decisions(conn: sqlite3.Connection, req_id: str) -> list[str]:
    """Read a requirement's derives-from decision ids, in insert order."""
    rows = conn.execute(
        "SELECT decision_id FROM requirement_decisions "
        "WHERE req_id = ? ORDER BY id",
        (req_id,),
    ).fetchall()
    return [row["decision_id"] for row in rows]


def _read_source_refs(
    conn: sqlite3.Connection,
    req_id: str,
) -> list[SourceRef]:
    """Read a requirement's source refs, in insert order."""
    rows = conn.execute(
        "SELECT authority_id, kind, section, section_title, citation, "
        "content_hash, retrieved, retrieval_source FROM source_refs "
        "WHERE req_id = ? ORDER BY id",
        (req_id,),
    ).fetchall()
    return [SourceRef.model_validate(dict(row)) for row in rows]


def _read_impl_refs(
    conn: sqlite3.Connection,
    req_id: str,
) -> list[ImplementationRef]:
    """Read a requirement's implementation refs, in insert order."""
    rows = conn.execute(
        "SELECT arch, file, symbol, note FROM implementation_refs "
        "WHERE req_id = ? ORDER BY id",
        (req_id,),
    ).fetchall()
    return [ImplementationRef.model_validate(dict(row)) for row in rows]


def _read_verif_refs(
    conn: sqlite3.Connection,
    req_id: str,
) -> list[VerificationRef]:
    """Read a requirement's verification refs, in insert order."""
    rows = conn.execute(
        "SELECT ref_kind, file, symbol, note FROM verification_refs "
        "WHERE req_id = ? ORDER BY id",
        (req_id,),
    ).fetchall()
    return [VerificationRef.model_validate(dict(row)) for row in rows]
