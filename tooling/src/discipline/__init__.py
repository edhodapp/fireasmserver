"""Discipline tooling — keep authored work aligned with canonical context.

`discipline_print` reads a touched path, looks up the relevant
canonical context (schema blocks, decision entries, requirement
entries) via a declarative relevance map, and prints the assembled
context to stdout for the author to read before drafting changes.

Designed to be invoked from a PreToolUse hook so the canonical
context lands in front of Claude before any Edit/Write call against a
schema-bearing or specification-bearing file. Standalone CLI use is
also supported.
"""
