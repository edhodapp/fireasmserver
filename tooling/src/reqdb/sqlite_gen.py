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

import os
import sqlite3
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from reqdb.model import (
    Authority,
    ImplementationRef,
    ReqDB,
    Requirement,
    SourceRef,
    VerificationRef,
)

_RefT = TypeVar("_RefT", bound=BaseModel)


class UnknownAuthorityError(ValueError):
    """A requirement's ``source_ref`` cites an ``authority_id`` absent
    from the authorities lookup.

    Raised by :func:`write_sqlite` before any rows are written, naming
    the offending requirement and authority, so an authoring typo
    surfaces with context instead of as an opaque SQLite foreign-key
    violation mid-insert.
    """


class DuplicateIdError(ValueError):
    """Two records share a primary-key id (``authority_id`` or
    ``req_id``).

    Raised by :func:`write_sqlite` before any rows are written, naming
    the duplicates, so the conflict surfaces with context instead of as
    an opaque SQLite UNIQUE/IntegrityError mid-insert (which would also
    leave a partial file behind).
    """


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

    The database is built in a sibling temporary file and atomically
    renamed into place (``os.replace``), so a failed or interrupted
    build never leaves a partial/corrupt file at ``out_path`` — an
    existing one stays intact. Foreign keys are enforced; authorities
    are inserted before the requirements and child rows that reference
    them.

    Raises (before any write) :class:`UnknownAuthorityError` if a
    source_ref cites an authority absent from the lookup, or
    :class:`DuplicateIdError` if two records share a primary-key id.
    """
    _check_authority_refs(db)
    _check_unique_ids(db)
    tmp_path = out_path.with_name(f"{out_path.name}.tmp")
    try:
        _build_db(tmp_path, db)
        os.replace(tmp_path, out_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _build_db(tmp_path: Path, db: ReqDB) -> None:
    """Construct the SQLite database at ``tmp_path`` (a temporary file).

    Clears any stale partial first, then builds with foreign keys on.
    """
    tmp_path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(tmp_path))
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


def _check_authority_refs(db: ReqDB) -> None:
    """Fail loud, with context, if a source_ref cites an unknown authority.

    The SQLite foreign key would also catch this, but only as an opaque
    mid-insert ``IntegrityError``; this names the requirement and the
    authority so an authoring typo is diagnosable.
    """
    known = {authority.authority_id for authority in db.authorities}
    for req in db.requirements:
        for ref in req.source_refs:
            if ref.authority_id not in known:
                raise UnknownAuthorityError(
                    f"requirement {req.req_id!r} cites unknown "
                    f"authority_id {ref.authority_id!r}; "
                    f"known authorities: {sorted(known)}",
                )


def _check_unique_ids(db: ReqDB) -> None:
    """Fail loud, with context, on duplicate primary-key ids.

    The SQLite PRIMARY KEY would also catch this, but only as an opaque
    mid-insert ``IntegrityError`` that leaves a partial file; this names
    the duplicates up front.
    """
    _check_unique([a.authority_id for a in db.authorities], "authority_id")
    _check_unique([r.req_id for r in db.requirements], "req_id")


def _check_unique(ids: list[str], label: str) -> None:
    """Raise :class:`DuplicateIdError` if ``ids`` holds any duplicate."""
    seen: set[str] = set()
    dups: set[str] = set()
    for value in ids:
        if value in seen:
            dups.add(value)
        seen.add(value)
    if dups:
        raise DuplicateIdError(f"duplicate {label}(s): {sorted(dups)}")


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
    """Read every requirement with its child rows, ordered by ``req_id``.

    Child rows are fetched one query per table (not one per requirement)
    and grouped by ``req_id`` in memory — avoiding an N+1 query pattern
    while preserving authored child-list order via ``ORDER BY req_id, id``.
    """
    rows = conn.execute(
        "SELECT req_id, category, title, statement, verb_strength, "
        "status, authority_class, notes, supersedes, superseded_by "
        "FROM requirements ORDER BY req_id",
    ).fetchall()
    decisions = _group_decisions(conn)
    source_refs = _group_refs(
        conn,
        "SELECT req_id, authority_id, kind, section, section_title, "
        "citation, content_hash, retrieved, retrieval_source "
        "FROM source_refs ORDER BY req_id, id",
        SourceRef,
    )
    impl_refs = _group_refs(
        conn,
        "SELECT req_id, arch, file, symbol, note "
        "FROM implementation_refs ORDER BY req_id, id",
        ImplementationRef,
    )
    verif_refs = _group_refs(
        conn,
        "SELECT req_id, ref_kind, file, symbol, note "
        "FROM verification_refs ORDER BY req_id, id",
        VerificationRef,
    )
    return [
        _build_requirement(
            row, decisions, source_refs, impl_refs, verif_refs,
        )
        for row in rows
    ]


def _group_decisions(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Group derives-from decision ids by ``req_id``, in insert order."""
    grouped: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT req_id, decision_id FROM requirement_decisions "
        "ORDER BY req_id, id",
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["req_id"], []).append(row["decision_id"])
    return grouped


def _group_refs(
    conn: sqlite3.Connection,
    query: str,
    model: type[_RefT],
) -> dict[str, list[_RefT]]:
    """Group a child-ref table by ``req_id``, in insert order, validating
    each row into ``model``. One query for the whole table (no N+1)."""
    grouped: dict[str, list[_RefT]] = {}
    for row in conn.execute(query).fetchall():
        data = dict(row)
        req_id = data.pop("req_id")
        grouped.setdefault(req_id, []).append(model.model_validate(data))
    return grouped


def _build_requirement(
    row: sqlite3.Row,
    decisions: dict[str, list[str]],
    source_refs: dict[str, list[SourceRef]],
    impl_refs: dict[str, list[ImplementationRef]],
    verif_refs: dict[str, list[VerificationRef]],
) -> Requirement:
    """Assemble a :class:`Requirement` from its scalar row plus the
    pre-grouped child collections (empty list when a req has none)."""
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
        derived_from=decisions.get(req_id, []),
        source_refs=source_refs.get(req_id, []),
        implementation_refs=impl_refs.get(req_id, []),
        verification_refs=verif_refs.get(req_id, []),
        supersedes=row["supersedes"],
        superseded_by=row["superseded_by"],
    )
