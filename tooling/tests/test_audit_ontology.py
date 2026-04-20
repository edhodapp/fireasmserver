"""Tests for audit_ontology — parser, resolver, consistency, audit,
formatter, and CLI. Aiming for 100% branch + statement coverage on
the new module per the briefing's definition of done.

C1 lands with parser + resolver coverage; C2 extends this file with
consistency, audit, formatter, and CLI classes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audit_ontology import parse_ref, resolve_ref
from audit_ontology.parser import ParsedRef
from audit_ontology.resolver import ResolvedRef


# ---------- parser ------------------------------------------------


class TestParseRef:
    """Grammar: whole_file / line / symbol / invalid. Splitting
    on the LAST colon means a hypothetical ``path:with:symbol`` ref
    treats ``symbol`` as the tail."""

    def test_empty_is_invalid(self) -> None:
        parsed = parse_ref("")
        assert parsed.kind == "invalid"
        assert parsed.error == "empty ref"

    def test_whole_file_no_colon(self) -> None:
        parsed = parse_ref("path/to/file.py")
        assert parsed.kind == "whole_file"
        assert parsed.path == "path/to/file.py"
        assert parsed.line is None
        assert parsed.symbol is None

    def test_line_form(self) -> None:
        parsed = parse_ref("path/to/file.py:42")
        assert parsed.kind == "line"
        assert parsed.path == "path/to/file.py"
        assert parsed.line == 42

    def test_symbol_form(self) -> None:
        parsed = parse_ref("path/to/file.py:my_func")
        assert parsed.kind == "symbol"
        assert parsed.path == "path/to/file.py"
        assert parsed.symbol == "my_func"

    def test_rsplit_preserves_leading_colons(self) -> None:
        # The (hypothetical) ``a:b:c`` case — rsplit gives path=a:b,
        # tail=c; treat c as the tail for disambiguation.
        parsed = parse_ref("a:b:my_sym")
        assert parsed.kind == "symbol"
        assert parsed.path == "a:b"
        assert parsed.symbol == "my_sym"

    def test_empty_tail_is_invalid(self) -> None:
        parsed = parse_ref("path/to/file:")
        assert parsed.kind == "invalid"
        assert "empty" in parsed.error

    def test_empty_path_is_invalid(self) -> None:
        parsed = parse_ref(":symbol")
        assert parsed.kind == "invalid"
        assert "empty" in parsed.error

    def test_absolute_path_is_invalid(self) -> None:
        parsed = parse_ref("/etc/passwd:root")
        assert parsed.kind == "invalid"
        assert "absolute" in parsed.error

    def test_absolute_whole_file_is_invalid(self) -> None:
        parsed = parse_ref("/etc/passwd")
        assert parsed.kind == "invalid"
        assert "absolute" in parsed.error

    def test_parent_segment_is_invalid(self) -> None:
        parsed = parse_ref("tooling/../../etc/passwd:root")
        assert parsed.kind == "invalid"
        assert ".." in parsed.error

    def test_parent_segment_whole_file_is_invalid(self) -> None:
        parsed = parse_ref("../secrets.txt")
        assert parsed.kind == "invalid"
        assert ".." in parsed.error

    def test_double_dot_inside_filename_is_safe(self) -> None:
        # ``..`` is rejected only as a whole path segment; a file
        # literally named ``a..b`` is fine.
        parsed = parse_ref("src/a..b")
        assert parsed.kind == "whole_file"
        assert parsed.path == "src/a..b"


# ---------- resolver ----------------------------------------------


@pytest.fixture(name="scratch_repo")
def _scratch_repo(tmp_path: Path) -> Path:
    """A synthetic repo tree with one file of each interesting
    suffix plus known symbols. Keeps tests hermetic — no dependence
    on the real repo's state."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(
        "def hello():\n    return 1\n\n"
        "class Greeter:\n    def greet(self):\n        return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "asm.S").write_text(
        ".global my_label\n"
        "my_label:\n"
        "    mov eax, 0\n"
        "    ret\n"
        "  indented_label:\n"
        "    ret\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "lib.c").write_text(
        "int frob(int x) { return x + 1; }\n"
        "void nowhere(void);\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "lib.h").write_text(
        "int frob(int x);\n", encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "marker-token appears in this line\n", encoding="utf-8",
    )
    (tmp_path / "broken.py").write_text(
        "def def def:\n  syntax errror\n", encoding="utf-8",
    )
    (tmp_path / "broken2.py").write_text(
        "def (oh no:\n  class also_broken\n", encoding="utf-8",
    )
    (tmp_path / "three_lines.txt").write_text(
        "one\ntwo\nthree", encoding="utf-8",
    )
    (tmp_path / "three_lines_nl.txt").write_text(
        "one\ntwo\nthree\n", encoding="utf-8",
    )
    return tmp_path


class TestResolveWholeFile:
    """``kind=whole_file`` resolves iff the file exists under the
    repo root; no other checks."""

    def test_existing(self, scratch_repo: Path) -> None:
        ref = resolve_ref(parse_ref("README.md"), scratch_repo)
        assert ref.resolution == "resolved"

    def test_missing(self, scratch_repo: Path) -> None:
        ref = resolve_ref(parse_ref("nope.txt"), scratch_repo)
        assert ref.resolution == "file_missing"
        assert "nope.txt" in ref.detail


class TestResolveLine:
    """``kind=line`` checks file presence plus line-count bound."""

    def test_valid(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("three_lines.txt:2"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_valid_trailing_newline(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("three_lines_nl.txt:3"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_out_of_range(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("three_lines.txt:99"), scratch_repo,
        )
        assert ref.resolution == "line_out_of_range"

    def test_missing_file(self, scratch_repo: Path) -> None:
        ref = resolve_ref(parse_ref("nope.txt:1"), scratch_repo)
        assert ref.resolution == "file_missing"

    def test_empty_file_has_zero_lines(self, tmp_path: Path) -> None:
        # Gemini-flagged off-by-one: a 0-byte file previously
        # reported as 1 line, silently resolving ``:1`` refs.
        (tmp_path / "empty.txt").write_text("", encoding="utf-8")
        ref = resolve_ref(parse_ref("empty.txt:1"), tmp_path)
        assert ref.resolution == "line_out_of_range"
        assert "0 lines" in ref.detail


class TestResolvePySymbol:
    """``.py`` symbol lookup via AST, with regex fallback for
    files whose syntax AST-parsing can't handle."""

    def test_function(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/m.py:hello"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_class(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/m.py:Greeter"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_nested_method(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/m.py:greet"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_missing(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/m.py:ghost"), scratch_repo,
        )
        assert ref.resolution == "symbol_missing"

    def test_broken_py_regex_fallback_hit(
        self, scratch_repo: Path,
    ) -> None:
        # broken2.py won't AST-parse but does contain
        # ``class also_broken`` at a word boundary — the regex
        # fallback must find it.
        ref = resolve_ref(
            parse_ref("broken2.py:also_broken"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_broken_py_regex_fallback_miss(
        self, scratch_repo: Path,
    ) -> None:
        ref = resolve_ref(
            parse_ref("broken.py:nonexistent"), scratch_repo,
        )
        assert ref.resolution == "symbol_missing"


class TestResolveAsmSymbol:
    """``.S`` / ``.s`` symbol lookup — line-anchored label regex
    covers NASM (x86_64) and GAS (aarch64)."""

    def test_label(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/asm.S:my_label"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_indented_label(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/asm.S:indented_label"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_missing(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/asm.S:nowhere"), scratch_repo,
        )
        assert ref.resolution == "symbol_missing"

    def test_lowercase_s_suffix(
        self, scratch_repo: Path, tmp_path: Path,
    ) -> None:
        (tmp_path / "low.s").write_text(
            "low_label:\n    ret\n", encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("low.s:low_label"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_routing_is_label_based_not_substring(
        self, tmp_path: Path,
    ) -> None:
        # Proves ``.S`` routes to the label-regex branch, not the
        # generic substring branch. A reviewer flagged that the
        # normal label tests could coincidentally pass via
        # substring-fallback; this constructs a file where the
        # symbol appears ONLY in a comment (never as a label).
        # Substring-fallback would resolve; label-regex must not.
        (tmp_path / "comment.S").write_text(
            "; not_a_label is mentioned only in a comment\n"
            "real_label:\n    ret\n",
            encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("comment.S:not_a_label"), tmp_path,
        )
        assert ref.resolution == "symbol_missing"


class TestResolveCSymbol:
    """``.c`` / ``.h`` symbol lookup via ``<sym>(`` regex —
    matches definition and declaration alike."""

    def test_c_definition(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/lib.c:frob"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_c_declaration(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/lib.c:nowhere"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_h_declaration(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/lib.h:frob"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_missing(self, scratch_repo: Path) -> None:
        ref = resolve_ref(
            parse_ref("src/lib.c:ghost"), scratch_repo,
        )
        assert ref.resolution == "symbol_missing"


class TestResolveOtherSymbol:
    """Anything-else suffix falls back to literal substring
    match — no fuzzy matching, honest signal."""

    def test_markdown_substring_hit(
        self, scratch_repo: Path,
    ) -> None:
        ref = resolve_ref(
            parse_ref("README.md:marker-token"), scratch_repo,
        )
        assert ref.resolution == "resolved"

    def test_markdown_substring_miss(
        self, scratch_repo: Path,
    ) -> None:
        ref = resolve_ref(
            parse_ref("README.md:not-there"), scratch_repo,
        )
        assert ref.resolution == "symbol_missing"


class TestResolveInvalid:
    """Parser-invalid refs short-circuit — no filesystem access."""

    def test_invalid_parser_output(
        self, scratch_repo: Path,
    ) -> None:
        ref = resolve_ref(parse_ref(""), scratch_repo)
        assert ref.resolution == "invalid"
        assert isinstance(ref, ResolvedRef)

    def test_parsed_model_roundtrip(self) -> None:
        # Sanity: ParsedRef round-trips through pydantic.
        parsed = ParsedRef(raw="a:b", kind="symbol", path="a", symbol="b")
        dumped = parsed.model_dump()
        assert dumped["kind"] == "symbol"
