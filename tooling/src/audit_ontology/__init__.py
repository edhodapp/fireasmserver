"""audit_ontology — requirement → impl → verification auditor.

Reads ``tooling/qemu-harness.json``, cross-references every
``DomainConstraint`` and ``PerformanceConstraint``'s
``implementation_refs`` and ``verification_refs`` against the working
repo tree, runs status/refs consistency checks, and emits a
human-readable matrix or machine-readable JSON gap report.

The tool turns the ontology's SysE-traceability fields from
declarative aspiration into verifiable evidence: a typo in a ref
that previously went unnoticed now surfaces as ``file_missing`` or
``symbol_missing``. Earmarked as a follow-up in DECISIONS.md D049;
briefing at ``docs/side_sessions/2026-04-19_audit_ontology.md``.
"""

from audit_ontology.parser import ParsedRef, RefKind, parse_ref
from audit_ontology.resolver import ResolvedRef, Resolution, resolve_ref

__all__ = [
    "ParsedRef",
    "RefKind",
    "Resolution",
    "ResolvedRef",
    "parse_ref",
    "resolve_ref",
]
