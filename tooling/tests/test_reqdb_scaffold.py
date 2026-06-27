"""Reqdb verification scaffold — behavioural + round-trip tests.

Authored RED-first (``xfail(strict=True)``) before the reqdb parser
and generator exist, per the 2026-06 "verify database operation before
relying on it" decision. When the implementation lands, these XPASS
and strict-xfail flips them to failures, forcing the marker's
removal — that flip is the RED→GREEN signal.

Oracle discipline (see the design thread): the behavioural test
queries the generated SQLite with stdlib :mod:`sqlite3` — the engine
is an *external* oracle we did not author — and the round-trip test
asserts text→model→db→model identity, a property independent of the
generator's own logic. Neither leans on a hand-authored "expected
output" that the generator's author could get wrong in the same way as
the generator.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from reqdb import ReqDB, load_reqdb, read_sqlite, write_sqlite

_GOLDEN = Path(__file__).parent / "fixtures" / "reqdb" / "golden"

# Expected shape of the golden fixture — the behavioural contract the
# implementation must satisfy once it lands.
_EXPECT_REQUIREMENTS = 4
_EXPECT_AUTHORITIES = 1
_EXPECT_SOURCE_REFS = 3        # all three on VIO-R-NUMBUF
_EXPECT_IMPL_REFS = 6          # numbuf 2 + mr-owner 2 + demo pair 1+1

_PENDING = pytest.mark.xfail(
    strict=True,
    reason="reqdb parser/generator not implemented yet (RED scaffold)",
    raises=NotImplementedError,
)


@_PENDING
def test_build_golden_produces_expected_rows(tmp_path: Path) -> None:
    """Behavioural: building the golden fixture yields a SQLite database
    whose row counts match the fixture across every table."""
    db = load_reqdb(_GOLDEN)
    out = tmp_path / "reqdb.sqlite"
    write_sqlite(db, out)

    counts: dict[str, int] = {}
    conn = sqlite3.connect(str(out))
    try:
        for table in (
            "requirements",
            "authorities",
            "source_refs",
            "implementation_refs",
        ):
            row = conn.execute("SELECT count(*) FROM " + table).fetchone()
            counts[table] = row[0]
    finally:
        conn.close()

    assert counts["requirements"] == _EXPECT_REQUIREMENTS
    assert counts["authorities"] == _EXPECT_AUTHORITIES
    assert counts["source_refs"] == _EXPECT_SOURCE_REFS
    assert counts["implementation_refs"] == _EXPECT_IMPL_REFS


@_PENDING
def test_roundtrip_text_to_sqlite_to_model(tmp_path: Path) -> None:
    """Round-trip fidelity: text → model → SQLite → model is identity.

    The oracle is "equals the input", independent of the generator's
    own logic, so it cannot share the generator's bug."""
    original = load_reqdb(_GOLDEN)
    out = tmp_path / "reqdb.sqlite"
    write_sqlite(original, out)
    restored = read_sqlite(out)
    assert restored == original


@_PENDING
def test_write_sqlite_not_yet_implemented(tmp_path: Path) -> None:
    """The writer stub must raise, not silently no-op — a silent
    no-op would let an empty database masquerade as a built one."""
    write_sqlite(ReqDB(), tmp_path / "empty.sqlite")


@_PENDING
def test_read_sqlite_not_yet_implemented(tmp_path: Path) -> None:
    """The reader stub must raise, not silently return an empty model."""
    read_sqlite(tmp_path / "missing.sqlite")
