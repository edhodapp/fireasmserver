"""Hypothesis round-trip property for the reqdb SQLite projection.

Generates random *valid* ReqDB instances — unique primary-key ids,
source_refs that reference declared authorities, top-level records
pre-sorted by id — and asserts text→model→sqlite→model is the identity.

The oracle is "equals the input", independent of the generator's own
logic, so it cannot share the generator's bug. Where the golden fixture
pins a handful of shapes, this exercises the writer/reader across inputs
it never covers: a field the writer drops or the reader mis-orders fails
the property. Enum value sets are derived from the model's ``Literal``
aliases via ``get_args`` so they cannot drift from the model.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import get_args

from hypothesis import given, settings, strategies as st

from reqdb import (
    Authority,
    ImplementationRef,
    ReqDB,
    Requirement,
    SourceRef,
    VerificationRef,
    read_sqlite,
    write_sqlite,
)
from reqdb.model import (
    AccessClass,
    Arch,
    AuthorityClass,
    ReqStatus,
    SourceKind,
    VerbStrength,
    VerificationKind,
)

# Identifiers are ASCII only, so Python's str sort matches SQLite's
# BINARY collation — the round-trip's ORDER BY then agrees with the
# parser's sort, keeping the identity exact.
_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1,
    max_size=8,
)
# Explicit alphabet (mypy-clean, no NUL/surrogates) covering the shapes
# that actually matter for round-trip fidelity: newlines and tabs (real
# citations are multi-line), punctuation, and some non-ASCII. Unicode
# torture (NUL, surrogates) is sqlite/Python behaviour, not reqdb's, so
# it is deliberately out of scope here.
_TEXT = st.text(
    alphabet=(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789 .,;:-_/()[]§\n\t"
        "é—✓"
    ),
    max_size=24,
)
_OPT_TEXT = st.none() | _TEXT
_OPT_IDENT = st.none() | _IDENT

# Enum domains taken straight from the model so they cannot drift.
_ACCESS = list(get_args(AccessClass))
_ARCHES = list(get_args(Arch))
_KINDS = list(get_args(SourceKind))
_VERBS = list(get_args(VerbStrength))
_STATUSES = list(get_args(ReqStatus))
_ACLASSES = list(get_args(AuthorityClass))
_VKINDS = list(get_args(VerificationKind))

_IMPL = st.builds(
    ImplementationRef,
    arch=st.sampled_from(_ARCHES),
    file=_TEXT,
    symbol=_OPT_TEXT,
    note=_OPT_TEXT,
)
_VERIF = st.builds(
    VerificationRef,
    ref_kind=st.sampled_from(_VKINDS),
    file=_TEXT,
    symbol=_OPT_TEXT,
    note=_OPT_TEXT,
)


def _authority(aid: str) -> st.SearchStrategy[Authority]:
    return st.builds(
        Authority,
        authority_id=st.just(aid),
        full_title=_TEXT,
        publisher=_TEXT,
        access=st.sampled_from(_ACCESS),
        canonical_url=_OPT_TEXT,
    )


def _source_refs(auth_ids: list[str]) -> st.SearchStrategy[list[SourceRef]]:
    if not auth_ids:
        empty: list[SourceRef] = []
        return st.just(empty)
    return st.lists(
        st.builds(
            SourceRef,
            authority_id=st.sampled_from(auth_ids),
            kind=st.sampled_from(_KINDS),
            section=_TEXT,
            section_title=_OPT_TEXT,
            citation=_OPT_TEXT,
            content_hash=_TEXT,
            retrieved=_TEXT,
            retrieval_source=_TEXT,
        ),
        max_size=3,
    )


def _requirement(
    req_id: str,
    auth_ids: list[str],
) -> st.SearchStrategy[Requirement]:
    return st.builds(
        Requirement,
        req_id=st.just(req_id),
        category=_TEXT,
        title=_TEXT,
        statement=_TEXT,
        verb_strength=st.sampled_from(_VERBS),
        status=st.sampled_from(_STATUSES),
        authority_class=st.sampled_from(_ACLASSES),
        notes=_OPT_TEXT,
        derived_from=st.lists(_IDENT, max_size=3),
        source_refs=_source_refs(auth_ids),
        implementation_refs=st.lists(_IMPL, max_size=3),
        verification_refs=st.lists(_VERIF, max_size=3),
        supersedes=_OPT_IDENT,
        superseded_by=_OPT_IDENT,
    )


@st.composite
def _reqdbs(draw: st.DrawFn) -> ReqDB:
    """A valid ReqDB: unique ids, source_refs referencing declared
    authorities, top-level records pre-sorted by id."""
    auth_ids = sorted(draw(st.lists(_IDENT, max_size=4, unique=True)))
    authorities = [draw(_authority(aid)) for aid in auth_ids]
    req_ids = sorted(draw(st.lists(_IDENT, max_size=4, unique=True)))
    requirements = [draw(_requirement(rid, auth_ids)) for rid in req_ids]
    return ReqDB(authorities=authorities, requirements=requirements)


@given(_reqdbs())
@settings(max_examples=100)
def test_roundtrip_identity_over_random_reqdbs(db: ReqDB) -> None:
    """text→model→sqlite→model is the identity for any valid ReqDB."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "reqdb.sqlite"
        write_sqlite(db, out)
        assert read_sqlite(out) == db
