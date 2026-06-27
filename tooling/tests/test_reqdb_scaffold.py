"""Reqdb verification scaffold — behavioural + round-trip tests.

Oracle discipline (see the design thread): the behavioural test queries
the generated SQLite with stdlib :mod:`sqlite3` — the engine is an
*external* oracle we did not author — and the round-trip test asserts
text→model→db→model identity, a property independent of the generator's
own logic. Neither leans on a hand-authored "expected output" that the
generator's author could get wrong in the same way as the generator.

Authored RED-first (``xfail(strict=True, raises=NotImplementedError)``)
before the parser/generator existed; now that the implementation has
landed the markers are gone and the two stub-contract placeholders have
become real behavioural tests (empty build → zero rows; missing file →
raises).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reqdb import (
    ReqDB,
    Requirement,
    SourceRef,
    UnknownAuthorityError,
    load_reqdb,
    read_sqlite,
    write_sqlite,
)

_GOLDEN = Path(__file__).parent / "fixtures" / "reqdb" / "golden"

# Expected shape of the golden fixture — the behavioural contract.
_EXPECT_REQUIREMENTS = 4
_EXPECT_AUTHORITIES = 1
_EXPECT_SOURCE_REFS = 3        # all three on VIO-R-NUMBUF
_EXPECT_IMPL_REFS = 6          # numbuf 2 + mr-owner 2 + demo pair 1+1

_TABLES = ("requirements", "authorities", "source_refs", "implementation_refs")


def _row_counts(db_path: Path) -> dict[str, int]:
    """Row count per table, read with stdlib sqlite3 as external oracle."""
    counts: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path))
    try:
        for table in _TABLES:
            row = conn.execute("SELECT count(*) FROM " + table).fetchone()
            counts[table] = row[0]
    finally:
        conn.close()
    return counts


def test_build_golden_produces_expected_rows(tmp_path: Path) -> None:
    """Behavioural: building the golden fixture yields a SQLite database
    whose row counts match the fixture across every table."""
    db = load_reqdb(_GOLDEN)
    out = tmp_path / "reqdb.sqlite"
    write_sqlite(db, out)

    counts = _row_counts(out)
    assert counts["requirements"] == _EXPECT_REQUIREMENTS
    assert counts["authorities"] == _EXPECT_AUTHORITIES
    assert counts["source_refs"] == _EXPECT_SOURCE_REFS
    assert counts["implementation_refs"] == _EXPECT_IMPL_REFS


def test_roundtrip_text_to_sqlite_to_model(tmp_path: Path) -> None:
    """Round-trip fidelity: text → model → SQLite → model is identity.

    The oracle is "equals the input", independent of the generator's
    own logic, so it cannot share the generator's bug."""
    original = load_reqdb(_GOLDEN)
    out = tmp_path / "reqdb.sqlite"
    write_sqlite(original, out)
    restored = read_sqlite(out)
    assert restored == original


def test_write_empty_db_produces_zero_rows(tmp_path: Path) -> None:
    """An empty model must produce a valid, queryable database with the
    schema present and every table empty — not a missing or malformed
    file, and not a silent no-op."""
    out = tmp_path / "empty.sqlite"
    write_sqlite(ReqDB(), out)

    assert out.exists()
    counts = _row_counts(out)
    assert all(count == 0 for count in counts.values())
    # Reading it back exercises the batched readers against empty child
    # tables and round-trips to the empty model.
    assert read_sqlite(out) == ReqDB()


def test_read_missing_file_raises(tmp_path: Path) -> None:
    """Reading an absent database must raise, not let sqlite3 create an
    empty file and return a silently-empty model."""
    with pytest.raises(FileNotFoundError):
        read_sqlite(tmp_path / "missing.sqlite")


def test_write_unknown_authority_raises_with_context(tmp_path: Path) -> None:
    """A source_ref citing an authority absent from the lookup must fail
    with a contextful error naming the requirement and the authority —
    not an opaque SQLite foreign-key violation — and must leave no file
    behind (validation precedes any write)."""
    db = ReqDB(
        authorities=[],
        requirements=[
            Requirement(
                req_id="X-1",
                category="X",
                title="cites a missing authority",
                statement="The system shall cite an unknown authority.",
                verb_strength="shall",
                status="implemented",
                authority_class="authority_derived",
                source_refs=[
                    SourceRef(
                        authority_id="no-such-authority",
                        kind="specification",
                        section="§1",
                        content_hash="sha256:00",
                        retrieved="2026-06-27",
                        retrieval_source="test",
                    ),
                ],
            ),
        ],
    )
    out = tmp_path / "bad.sqlite"
    with pytest.raises(UnknownAuthorityError) as excinfo:
        write_sqlite(db, out)
    message = str(excinfo.value)
    assert "X-1" in message
    assert "no-such-authority" in message
    assert not out.exists()


def test_rebuild_overwrites_existing_db(tmp_path: Path) -> None:
    """Building over an existing file replaces it cleanly (idempotent
    rebuild): a second write to the same path reconstructs the same
    model, with no stale rows left over from the first."""
    db = load_reqdb(_GOLDEN)
    out = tmp_path / "reqdb.sqlite"
    write_sqlite(db, out)
    write_sqlite(db, out)

    assert read_sqlite(out) == db
    counts = _row_counts(out)
    assert counts["requirements"] == _EXPECT_REQUIREMENTS
