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

import hashlib
import sqlite3
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from reqdb import (
    Authority,
    DuplicateIdError,
    ImplementationRef,
    ReqDB,
    Requirement,
    SourceRef,
    UnknownAuthorityError,
    VerificationRef,
    load_reqdb,
    read_sqlite,
    sqlite_gen,
    write_sqlite,
)

_GOLDEN = Path(__file__).parent / "fixtures" / "reqdb" / "golden"

# Expected shape of the golden fixture — the behavioural contract.
_EXPECT_REQUIREMENTS = 4
_EXPECT_AUTHORITIES = 1
_EXPECT_SOURCE_REFS = 3        # all three on VIO-R-NUMBUF
_EXPECT_CITED_SOURCE_REFS = 3  # of those, all 3 carry a citation today
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


# (model, its table, fields that project to child tables not columns)
_FIELD_COVERAGE: list[tuple[type[BaseModel], str, set[str]]] = [
    (Authority, "authorities", set()),
    (SourceRef, "source_refs", set()),
    (ImplementationRef, "implementation_refs", set()),
    (VerificationRef, "verification_refs", set()),
    (
        Requirement,
        "requirements",
        {
            "derived_from",
            "source_refs",
            "implementation_refs",
            "verification_refs",
        },
    ),
]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of ``table``, via the sqlite3 schema as oracle."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


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


def test_unknown_yaml_key_rejected() -> None:
    """Unknown top-level keys in canonical requirement text must be
    rejected, not silently dropped — a typo like `implementation_ref`
    (missing the plural s) would otherwise lose data from the source of
    truth without a trace."""
    with pytest.raises(ValidationError):
        Requirement.model_validate(
            {
                "req_id": "X-1",
                "category": "X",
                "title": "t",
                "statement": "s",
                "verb_strength": "shall",
                "status": "implemented",
                "authority_class": "internally_originated",
                "implementation_ref": [{"arch": "common", "file": "f"}],
            },
        )


def test_load_missing_requirements_dir_raises(tmp_path: Path) -> None:
    """A source tree lacking a requirements/ directory must fail loud,
    not silently produce an empty ReqDB — a wrong path or partial
    checkout would otherwise generate an empty projection that looks
    like a successful build."""
    (tmp_path / "authorities.yaml").write_text("[]\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_reqdb(tmp_path)


def test_write_duplicate_req_id_raises(tmp_path: Path) -> None:
    """A duplicate req_id must fail with a contextful error before any
    write, not as an opaque mid-insert IntegrityError leaving a partial
    file."""
    req = Requirement(
        req_id="DUP",
        category="X",
        title="t",
        statement="s",
        verb_strength="shall",
        status="implemented",
        authority_class="internally_originated",
    )
    db = ReqDB(authorities=[], requirements=[req, req])
    out = tmp_path / "dup.sqlite"
    with pytest.raises(DuplicateIdError) as excinfo:
        write_sqlite(db, out)
    assert "DUP" in str(excinfo.value)
    assert not out.exists()


def test_write_failure_preserves_existing_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the build fails mid-way, an existing database at out_path is
    left intact (atomic temp-then-replace) and no .tmp residue remains."""
    db = load_reqdb(_GOLDEN)
    out = tmp_path / "reqdb.sqlite"
    write_sqlite(db, out)
    before = out.read_bytes()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated build failure")

    monkeypatch.setattr(sqlite_gen, "_build_db", _boom)
    with pytest.raises(RuntimeError):
        write_sqlite(db, out)

    assert out.read_bytes() == before
    assert not (tmp_path / "reqdb.sqlite.tmp").exists()


def test_build_is_reproducible(tmp_path: Path) -> None:
    """Building the same model twice yields byte-identical databases — a
    reproducible projection. Silent nondeterminism in a generated
    artefact is the most corrosive failure to miss: it defeats diffing,
    caching, and content-hash identity downstream.

    The byte-identity holds because the SQLite file format embeds no
    timestamp and the build uses a fixed insert order and a single
    commit; it is, however, sensitive to the SQLite library version
    (page format / defaults). If this ever flakes across environments,
    that is the first place to look — not a reqdb regression."""
    db = load_reqdb(_GOLDEN)
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"
    write_sqlite(db, first)
    write_sqlite(db, second)
    assert first.read_bytes() == second.read_bytes()


def test_schema_covers_every_model_field(tmp_path: Path) -> None:
    """Projection completeness, independent of the round-trip: every
    scalar field of every model must have a column in its table, so a
    field added to the model but forgotten in the schema is caught even
    when no fixture exercises it. Multi-valued fields project to child
    tables (the child models are themselves checked here; derived_from
    via the junction table)."""
    out = tmp_path / "reqdb.sqlite"
    write_sqlite(load_reqdb(_GOLDEN), out)
    conn = sqlite3.connect(str(out))
    try:
        for model, table, child_fields in _FIELD_COVERAGE:
            fields = set(model.model_fields)
            # self-check the exclusion list: a typo'd child-field name
            # would otherwise silently weaken the coverage assertion.
            assert child_fields <= fields, f"{table} stale child_fields"
            missing = (fields - child_fields) - _columns(conn, table)
            assert not missing, f"{table} missing columns {missing}"
        assert "decision_id" in _columns(conn, "requirement_decisions")
    finally:
        conn.close()


def test_source_ref_content_hashes_recompute() -> None:
    """D069 validation as a test: every source_ref that carries a
    citation must have content_hash == sha256 of that citation, so the
    stored hash genuinely pins the quoted authority text. sha256 is the
    external oracle. The count assertion guards against the loop
    vacuously checking nothing — and is tied to the *cited* population,
    not the total, so a closed-authority ref (citation=None, hash still
    present) added later doesn't fail this test for the wrong reason."""
    db = load_reqdb(_GOLDEN)
    checked = 0
    for req in db.requirements:
        for ref in req.source_refs:
            if ref.citation is None:
                continue
            digest = hashlib.sha256(ref.citation.encode("utf-8")).hexdigest()
            assert ref.content_hash == f"sha256:{digest}", (
                f"{req.req_id} {ref.section}"
            )
            checked += 1
    assert checked == _EXPECT_CITED_SOURCE_REFS
