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

from audit_ontology.audit import (
    AuditReport,
    ConstraintReport,
    Summary,
    run_audit,
)
from audit_ontology.consistency import check_constraint
from audit_ontology.formatter import format_json, format_text
from audit_ontology.parser import ParsedRef, RefKind, parse_ref
from audit_ontology.resolver import ResolvedRef, Resolution, resolve_ref

__all__ = [
    "AuditReport",
    "ConstraintReport",
    "ParsedRef",
    "RefKind",
    "Resolution",
    "ResolvedRef",
    "Summary",
    "check_constraint",
    "format_json",
    "format_text",
    "parse_ref",
    "resolve_ref",
    "run_audit",
]
