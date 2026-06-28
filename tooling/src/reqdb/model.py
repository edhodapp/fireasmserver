"""Reqdb in-memory data model — the relational schema as Pydantic records.

This module is the *contract* the reqdb test scaffold asserts against:
the per-requirement canonical text parses into these records, the
SQLite generator projects them into tables, and the round-trip tests
read them back. It carries the schema *structure* only — field types,
enums, and the relational shape (one ``Requirement`` with child lists
for the multi-valued types). Cross-field business rules (an
authority-derived requirement must carry ``source_refs``,
``content_hash`` well-formedness, bidirectional supersession) are
validation *behaviour* and land with the validator implementation and
its negative tests, not here.

Canonical source of truth is the git-tracked per-requirement text; the
SQLite database is a generated projection. These records are the
in-memory bridge between the two.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccessClass = Literal["open", "closed"]
SourceKind = Literal[
    "statute", "standard", "specification", "datasheet", "erratum",
]
VerbStrength = Literal[
    "shall", "shall_not", "should", "should_not", "may",
]
ReqStatus = Literal[
    "implemented", "partial", "gap", "spec_only", "deviation", "na",
]
AuthorityClass = Literal["authority_derived", "internally_originated"]
Arch = Literal["x86_64", "aarch64", "common"]
VerificationKind = Literal[
    "behavioural", "property", "integration", "proof",
]


class Authority(BaseModel):
    """A citable external authority — one ``authorities`` lookup row.

    ``access`` carries the open/closed policy once per authority: open
    authorities (IETF RFC, OASIS Virtio) admit a verbatim citation
    body; closed ones (paywalled IEEE) do not.
    """

    model_config = ConfigDict(extra="forbid")

    authority_id: str
    full_title: str
    publisher: str
    access: AccessClass
    canonical_url: str | None = None


class SourceRef(BaseModel):
    """One verbatim citation of an external authority.

    ``citation`` is the quoted text (``None`` when the parent
    authority's access is ``closed`` and policy forbids reproduction).
    ``content_hash`` is the SHA-256 of the quoted text and is present
    even when ``citation`` is ``None``, so drift stays detectable
    against a licensed copy.
    """

    model_config = ConfigDict(extra="forbid")

    authority_id: str
    kind: SourceKind
    section: str
    section_title: str | None = None
    citation: str | None = None
    content_hash: str
    retrieved: str
    retrieval_source: str


class ImplementationRef(BaseModel):
    """A code site realising a requirement, tagged by ``arch`` so the
    two-arch / one-design split is first-class — one requirement, one
    implementation row per architecture."""

    model_config = ConfigDict(extra="forbid")

    arch: Arch
    file: str
    symbol: str | None = None
    note: str | None = None


class VerificationRef(BaseModel):
    """A test or proof that verifies a requirement."""

    model_config = ConfigDict(extra="forbid")

    ref_kind: VerificationKind
    file: str
    symbol: str | None = None
    note: str | None = None


class Requirement(BaseModel):
    """One requirement — the scalar fields plus child lists for the
    multi-valued relationships (source / implementation / verification
    refs and the derives-from-decisions junction)."""

    model_config = ConfigDict(extra="forbid")

    req_id: str
    category: str
    title: str
    statement: str
    verb_strength: VerbStrength
    status: ReqStatus
    authority_class: AuthorityClass
    notes: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    implementation_refs: list[ImplementationRef] = Field(
        default_factory=list,
    )
    verification_refs: list[VerificationRef] = Field(
        default_factory=list,
    )
    supersedes: str | None = None
    superseded_by: str | None = None


class ReqDB(BaseModel):
    """The full in-memory requirements set — the authorities lookup
    plus every requirement. This is what the parser produces and what
    the SQLite / ReqIF generators consume."""

    model_config = ConfigDict(extra="forbid")

    authorities: list[Authority] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
