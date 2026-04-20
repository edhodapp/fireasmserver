"""Tests for audit_ontology — parser, resolver, consistency, audit,
formatter, and CLI. 100% branch + statement coverage on the new
module per the briefing's definition of done.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from audit_ontology import (
    AuditReport,
    check_constraint,
    format_json,
    format_text,
    parse_ref,
    resolve_ref,
    run_audit,
)
from audit_ontology.audit import (
    ConstraintReport,
    Summary,
    _audit_one,
)
from audit_ontology.cli import _exit_code_for, main
from audit_ontology.parser import ParsedRef
from audit_ontology.resolver import (
    ResolvedRef,
    _check_contained,
    _collect_py_names,
)
from ontology import (
    DAGNode,
    DomainConstraint,
    Entity,
    Ontology,
    OntologyDAG,
    PerformanceConstraint,
)
from ontology.dag import save_dag


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

    def test_conditional_import_resolves(
        self, tmp_path: Path,
    ) -> None:
        # Version-gated imports and definitions inside module-
        # level ``if`` blocks ARE module symbols and must resolve.
        (tmp_path / "gated.py").write_text(
            "import sys\n"
            "if sys.version_info >= (3, 11):\n"
            "    FLAG = 1\n"
            "    def new_helper():\n        pass\n"
            "else:\n"
            "    FLAG = 0\n",
            encoding="utf-8",
        )
        for sym in ("FLAG", "new_helper"):
            ref = resolve_ref(
                parse_ref(f"gated.py:{sym}"), tmp_path,
            )
            assert ref.resolution == "resolved", sym

    def test_try_except_finally_resolves(
        self, tmp_path: Path,
    ) -> None:
        # ``try`` body + except handler body + ``finally`` block
        # all contribute module-level names.
        (tmp_path / "trial.py").write_text(
            "try:\n    primary = 1\n"
            "except Exception:\n    fallback = 2\n"
            "finally:\n    always = 3\n",
            encoding="utf-8",
        )
        for sym in ("primary", "fallback", "always"):
            ref = resolve_ref(
                parse_ref(f"trial.py:{sym}"), tmp_path,
            )
            assert ref.resolution == "resolved", sym

    def test_with_and_for_blocks_resolve(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "blocks.py").write_text(
            "from contextlib import nullcontext\n"
            "with nullcontext():\n    held = 1\n"
            "for i in range(1):\n    LAST_I = i\n"
            "while False:\n    never = 0\n",
            encoding="utf-8",
        )
        for sym in ("held", "LAST_I", "never"):
            ref = resolve_ref(
                parse_ref(f"blocks.py:{sym}"), tmp_path,
            )
            assert ref.resolution == "resolved", sym

    def test_indented_assign_regex_fallback(
        self, tmp_path: Path,
    ) -> None:
        # SyntaxError forces the regex fallback; an indented
        # module-level assign (inside an if/try block) must
        # still resolve, matching the AST path's container
        # recursion semantics.
        (tmp_path / "broken_indent.py").write_text(
            "if True:\n    BROKEN = (\n    FLAG = 1\n",
            encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("broken_indent.py:FLAG"), tmp_path,
        )
        assert ref.resolution == "resolved"

    def test_async_def_regex_fallback(
        self, tmp_path: Path,
    ) -> None:
        # SyntaxError forces the regex fallback; async def must
        # still be recognized, matching the AST path's
        # AsyncFunctionDef handling.
        (tmp_path / "broken_async.py").write_text(
            "def oops(\n"
            "async def job():\n    pass\n",
            encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("broken_async.py:job"), tmp_path,
        )
        assert ref.resolution == "resolved"

    def test_function_local_not_exposed(
        self, tmp_path: Path,
    ) -> None:
        # A name defined inside a function body must NOT resolve
        # as a module symbol — otherwise common loop variables
        # (``i``, ``data``) false-positive on any ref that shares
        # the name.
        (tmp_path / "locals.py").write_text(
            "def outer():\n"
            "    data = 42\n"
            "    return data\n",
            encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("locals.py:data"), tmp_path,
        )
        assert ref.resolution == "symbol_missing"
        # ``outer`` is still resolvable (module-level def).
        ok = resolve_ref(
            parse_ref("locals.py:outer"), tmp_path,
        )
        assert ok.resolution == "resolved"

    def test_class_body_names_resolve(
        self, tmp_path: Path,
    ) -> None:
        # Methods and class-attribute assignments at class body
        # scope DO resolve — they're part of the module's public
        # surface, just not top-level.
        (tmp_path / "clazz.py").write_text(
            "class Widget:\n"
            "    color: str = 'red'\n"
            "    def paint(self):\n"
            "        pass\n",
            encoding="utf-8",
        )
        for sym in ("Widget", "paint", "color"):
            ref = resolve_ref(
                parse_ref(f"clazz.py:{sym}"), tmp_path,
            )
            assert ref.resolution == "resolved", sym

    def test_non_module_ast_returns_empty(self) -> None:
        # Direct _collect_py_names call on a non-Module AST
        # (e.g., an Expression) returns the empty set — a
        # module-only collector shouldn't invent names on
        # unexpected tree shapes.
        expr = ast.parse("1 + 2", mode="eval")
        assert _collect_py_names(expr) == set()

    def test_non_name_assign_target_ignored(
        self, tmp_path: Path,
    ) -> None:
        # Subscript / attribute assignments aren't plausible
        # symbol refs; the resolver must skip them and not
        # misclassify e.g. ``d["k"]`` as defining symbol ``d``
        # or ``k``.
        (tmp_path / "complex.py").write_text(
            "d: dict = {}\n"
            "d['k'] = 1\n"
            "class C:\n    pass\n"
            "C.attr = 2\n",
            encoding="utf-8",
        )
        # The AnnAssign target ``d`` and ClassDef ``C`` DO count.
        for good in ("d", "C"):
            ref = resolve_ref(
                parse_ref(f"complex.py:{good}"), tmp_path,
            )
            assert ref.resolution == "resolved", good
        # Subscript / attribute targets must NOT register.
        for bad in ("k", "attr"):
            ref = resolve_ref(
                parse_ref(f"complex.py:{bad}"), tmp_path,
            )
            assert ref.resolution == "symbol_missing", bad

    def test_module_level_assign(self, tmp_path: Path) -> None:
        # Real ontology refs (e.g., vm_launcher:_proc_lock) point
        # at top-level module variables, not just defs/classes.
        (tmp_path / "vars.py").write_text(
            "import threading\n"
            "_proc_registry: dict[int, int] = {}\n"
            "_proc_lock = threading.Lock()\n",
            encoding="utf-8",
        )
        for sym in ("_proc_registry", "_proc_lock"):
            ref = resolve_ref(
                parse_ref(f"vars.py:{sym}"), tmp_path,
            )
            assert ref.resolution == "resolved", sym

    def test_regex_fallback_matches_assign(
        self, tmp_path: Path,
    ) -> None:
        # A file whose syntax errors prevent AST parse but still
        # has a module-level assignment to the target symbol.
        (tmp_path / "partial.py").write_text(
            "def oops(\n"
            "SOME_CONST = 42\n",
            encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("partial.py:SOME_CONST"), tmp_path,
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

    def test_nasm_macro_definition(self, tmp_path: Path) -> None:
        # Ed flagged that the upcoming CRC/crypto work will define
        # symbols via NASM %macro; the auditor must recognize them.
        (tmp_path / "macros.S").write_text(
            "%macro crc_fold_by_4 0\n"
            "    ; body elided\n"
            "%endmacro\n",
            encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("macros.S:crc_fold_by_4"), tmp_path,
        )
        assert ref.resolution == "resolved"

    def test_nasm_define(self, tmp_path: Path) -> None:
        (tmp_path / "defs.S").write_text(
            "%define FOLD_CHUNK_SIZE 64\n", encoding="utf-8",
        )
        ref = resolve_ref(
            parse_ref("defs.S:FOLD_CHUNK_SIZE"), tmp_path,
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


class TestResolveSymlink:
    """Resolver rejects refs whose target resolves outside
    ``repo_root`` — defense-in-depth against in-repo symlinks
    that point at sensitive system files."""

    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        # Create a file OUTSIDE the fake repo and a symlink
        # INSIDE that points to it; parser-level checks accept
        # the relative path, resolver must catch the symlink
        # escape.
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        link = repo / "escape.txt"
        link.symlink_to(outside)
        ref = resolve_ref(parse_ref("escape.txt"), repo)
        assert ref.resolution == "outside_repo"
        assert "outside repo" in ref.detail

    def test_circular_symlink_reports_unsafe(
        self, tmp_path: Path,
    ) -> None:
        # A → B → A. ``target.is_file()`` returns False on a
        # broken symlink chain, so the resolver short-circuits to
        # ``file_missing`` rather than reaching the resolve()
        # path. Directly invoke ``_check_contained`` to exercise
        # the OSError branch.
        link_a = tmp_path / "a"
        link_b = tmp_path / "b"
        link_a.symlink_to(link_b)
        link_b.symlink_to(link_a)
        detail = _check_contained(link_a, tmp_path, "a")
        assert "symlink resolve failed" in detail

    def test_symlink_inside_repo_ok(self, tmp_path: Path) -> None:
        # A symlink that resolves INSIDE the repo is fine.
        (tmp_path / "real.txt").write_text("x", encoding="utf-8")
        (tmp_path / "alias.txt").symlink_to(tmp_path / "real.txt")
        ref = resolve_ref(parse_ref("alias.txt"), tmp_path)
        assert ref.resolution == "resolved"


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


# ---------- consistency ------------------------------------------


def _dc(
    name: str, *, status: str = "spec",
    impl: list[str] | None = None,
    verify: list[str] | None = None,
    rationale: str = "",
) -> DomainConstraint:
    """Test helper — build a DomainConstraint with the usual
    defaults. ``entity_ids`` stays empty so referential-integrity
    validation doesn't demand an Entity list."""
    return DomainConstraint(
        name=name, description="", expression="",
        implementation_refs=impl or [],
        verification_refs=verify or [],
        rationale=rationale, status=status,  # type: ignore[arg-type]
    )


class TestConsistencyImplemented:
    """``implemented`` demands both impl AND verify refs."""

    def test_implemented_both_present_clean(self) -> None:
        constraint = _dc(
            "ok", status="implemented",
            impl=["a.py:f"], verify=["t.py:test_f"],
        )
        assert not check_constraint(constraint)

    def test_implemented_missing_impl(self) -> None:
        gaps = check_constraint(_dc(
            "x", status="implemented", verify=["t.py:t"],
        ))
        assert any("implementation_refs empty" in g for g in gaps)

    def test_implemented_missing_verify(self) -> None:
        gaps = check_constraint(_dc(
            "x", status="implemented", impl=["a.py:f"],
        ))
        assert any("verification_refs empty" in g for g in gaps)

    def test_implemented_missing_both(self) -> None:
        gaps = check_constraint(_dc("x", status="implemented"))
        assert len(gaps) == 2


class TestConsistencyTested:
    """``tested`` demands both impl AND verify refs."""

    def test_tested_both_present(self) -> None:
        constraint = _dc(
            "ok", status="tested",
            impl=["a.py:f"], verify=["t.py:test_f"],
        )
        assert not check_constraint(constraint)

    def test_tested_missing_impl(self) -> None:
        gaps = check_constraint(_dc(
            "x", status="tested", verify=["t.py:t"],
        ))
        assert any("implementation_refs empty" in g for g in gaps)

    def test_tested_missing_verify(self) -> None:
        gaps = check_constraint(_dc(
            "x", status="tested", impl=["a.py:f"],
        ))
        assert any("verification_refs empty" in g for g in gaps)


class TestConsistencyDeviation:
    """``deviation`` demands a non-empty rationale."""

    def test_deviation_with_rationale_clean(self) -> None:
        constraint = _dc(
            "x", status="deviation", rationale="see D047",
        )
        assert not check_constraint(constraint)

    def test_deviation_empty_rationale(self) -> None:
        gaps = check_constraint(_dc("x", status="deviation"))
        assert any("rationale empty" in g for g in gaps)


class TestConsistencyStaleSpec:
    """``spec`` with impl refs warns as likely-stale."""

    def test_clean_spec(self) -> None:
        assert not check_constraint(_dc("x", status="spec"))

    def test_spec_with_impl_refs_warns(self) -> None:
        gaps = check_constraint(_dc(
            "x", status="spec", impl=["a.py:f"],
        ))
        assert any("stale status" in g for g in gaps)


class TestConsistencyNA:
    """``n_a`` imposes no ref/rationale constraints."""

    def test_na_with_nothing(self) -> None:
        assert not check_constraint(_dc("x", status="n_a"))


# ---------- audit ------------------------------------------------


def _build_dag_with_two_constraints(
    repo_root: Path, dag_path: Path,
) -> None:
    """Write a DAG whose current node has one clean domain
    constraint (refs resolve) and one performance constraint
    with status=spec (empty refs, no gaps expected)."""
    (repo_root / "src").mkdir(exist_ok=True)
    (repo_root / "src" / "m.py").write_text(
        "def hello():\n    return 1\n", encoding="utf-8",
    )
    entity = Entity(id="e", name="E")
    domain = DomainConstraint(
        name="clean-one",
        description="clean", entity_ids=["e"],
        rationale="D049",
        implementation_refs=["src/m.py:hello"],
        verification_refs=["src/m.py"],
        status="implemented",
    )
    perf = PerformanceConstraint(
        name="spec-one", description="d", entity_ids=["e"],
        metric="x", budget=1.0, unit="ns", direction="max",
        status="spec",
    )
    ontology = Ontology(
        entities=[entity],
        domain_constraints=[domain],
        performance_constraints=[perf],
    )
    dag = OntologyDAG(project_name="t")
    node = DAGNode(
        id="n1",
        ontology=ontology,
        created_at="2026-04-20T12:00:00+00:00",
    )
    dag.nodes.append(node)
    dag.current_node_id = "n1"
    save_dag(dag, str(dag_path))


class TestRunAudit:
    """End-to-end audit against a synthetic DAG + repo tree."""

    def test_clean_run(self, tmp_path: Path) -> None:
        dag_path = tmp_path / "dag.json"
        _build_dag_with_two_constraints(tmp_path, dag_path)
        report = run_audit(dag_path, tmp_path)
        assert report.ontology_node_id == "n1"
        assert report.summary.total_constraints == 2
        assert report.summary.with_impl_refs == 1
        assert report.summary.with_verify_refs == 1
        assert report.summary.gap_count == 0
        assert report.summary.resolved_ref_count == 2
        assert report.summary.broken_ref_count == 0

    def test_empty_dag_raises(self, tmp_path: Path) -> None:
        dag_path = tmp_path / "dag.json"
        dag = OntologyDAG(project_name="t")
        save_dag(dag, str(dag_path))
        with pytest.raises(ValueError, match="no current node"):
            run_audit(dag_path, tmp_path)

    def test_broken_ref_increments_counts(
        self, tmp_path: Path,
    ) -> None:
        # One resolved + one broken verify + one broken impl, so
        # the summary counters exercise both impl and verify
        # branches in _count_resolution and _count_broken.
        (tmp_path / "exists.py").write_text(
            "def ok():\n    pass\n", encoding="utf-8",
        )
        dag_path = tmp_path / "dag.json"
        entity = Entity(id="e", name="E")
        domain = DomainConstraint(
            name="broken", description="", entity_ids=["e"],
            rationale="rx",
            implementation_refs=[
                "does/not/exist.py:ghost", "exists.py:ok",
            ],
            verification_refs=["also/missing.py:x"],
            status="implemented",
        )
        ontology = Ontology(
            entities=[entity], domain_constraints=[domain],
        )
        dag = OntologyDAG(project_name="t")
        dag.nodes.append(DAGNode(
            id="n", ontology=ontology, created_at="2026-04-20T12:00:00+00:00",
        ))
        dag.current_node_id = "n"
        save_dag(dag, str(dag_path))
        report = run_audit(dag_path, tmp_path)
        # 1 resolved impl + 1 broken impl + 1 broken verify = 1
        # resolved + 2 broken.
        assert report.summary.resolved_ref_count == 1
        assert report.summary.broken_ref_count == 2
        # 2 broken refs → 2 gaps from unresolved-ref reporting.
        assert report.summary.gap_count >= 2

    def test_audit_one_fills_per_constraint_report(
        self, tmp_path: Path,
    ) -> None:
        # Direct _audit_one call — covers the performance kind
        # path independent of run_audit's dispatch.
        (tmp_path / "m.py").write_text(
            "def go():\n    pass\n", encoding="utf-8",
        )
        perf = PerformanceConstraint(
            name="p", description="", metric="x",
            budget=1.0, unit="ns", direction="max",
            implementation_refs=["m.py:go"],
            verification_refs=[],
            status="implemented", rationale="r",
        )
        report = _audit_one(perf, "performance", tmp_path)
        assert report.kind == "performance"
        assert len(report.implementation_refs) == 1
        # implemented with empty verify → one consistency gap.
        assert any(
            "verification_refs empty" in g for g in report.gaps
        )


# ---------- formatter --------------------------------------------


def _fake_report(with_gaps: bool = False) -> AuditReport:
    """Minimal AuditReport fixture for formatter tests."""
    resolved = ResolvedRef(
        parsed=parse_ref("src/m.py:hello"), resolution="resolved",
    )
    broken = ResolvedRef(
        parsed=parse_ref("missing.py:x"),
        resolution="file_missing",
        detail="no such file: missing.py",
    )
    refs_impl = [resolved]
    gaps: list[str] = []
    if with_gaps:
        refs_impl = [broken]
        gaps = ["sample gap"]
    row = ConstraintReport(
        name="fsa-budget", kind="performance",
        status="implemented", rationale="D043",
        implementation_refs=refs_impl,
        verification_refs=[],
        gaps=gaps,
    )
    summary = Summary(
        total_constraints=1, with_impl_refs=1,
        with_verify_refs=0, gap_count=len(gaps),
        resolved_ref_count=0 if with_gaps else 1,
        broken_ref_count=1 if with_gaps else 0,
    )
    return AuditReport(
        dag_path="x.json", ontology_node_id="n",
        constraints=[row], summary=summary,
    )


class TestFormatText:
    """Human text output shape checks."""

    def test_clean_report(self) -> None:
        out = format_text(_fake_report(with_gaps=False))
        assert "[✓] fsa-budget" in out
        assert "src/m.py:hello" in out
        assert "impl: src/m.py:hello" in out
        assert "verify: — (none declared)" in out
        assert "Gaps (0 total)" in out
        assert "(none)" in out

    def test_report_with_gap(self) -> None:
        out = format_text(_fake_report(with_gaps=True))
        assert "[!] fsa-budget" in out
        assert "gaps present" in out
        assert "missing.py:x!file_missing" in out
        assert "fsa-budget: sample gap" in out

    def test_percent_zero_total(self) -> None:
        # Empty-ontology edge case must not divide by zero.
        empty = AuditReport(
            dag_path="x.json", ontology_node_id="n",
            constraints=[],
            summary=Summary(
                total_constraints=0, with_impl_refs=0,
                with_verify_refs=0, gap_count=0,
                resolved_ref_count=0, broken_ref_count=0,
            ),
        )
        out = format_text(empty)
        assert "Total constraints:     0" in out
        assert "(0%)" in out


class TestFormatJSON:
    """JSON output matches the briefing appendix schema."""

    def test_schema_shape(self) -> None:
        payload = json.loads(format_json(_fake_report()))
        assert payload["dag_path"] == "x.json"
        assert payload["ontology_node_id"] == "n"
        assert len(payload["constraints"]) == 1
        row = payload["constraints"][0]
        assert set(row) >= {
            "name", "kind", "status", "rationale",
            "implementation_refs", "verification_refs", "gaps",
        }
        ref = row["implementation_refs"][0]
        assert ref["raw"] == "src/m.py:hello"
        assert ref["resolved"] is True
        assert ref["kind"] == "symbol"
        assert set(payload["summary"]) == {
            "total_constraints", "with_impl_refs",
            "with_verify_refs", "gap_count",
            "resolved_ref_count", "broken_ref_count",
        }

    def test_json_ref_unresolved(self) -> None:
        payload = json.loads(format_json(_fake_report(True)))
        ref = payload["constraints"][0]["implementation_refs"][0]
        assert ref["resolved"] is False
        assert ref["resolution"] == "file_missing"


# ---------- CLI --------------------------------------------------


class TestCLIExitCode:
    """Exit-code logic for --exit-nonzero-on-gap."""

    def test_clean_returns_zero(self) -> None:
        assert _exit_code_for(_fake_report(with_gaps=False)) == 0

    def test_gap_returns_one(self) -> None:
        assert _exit_code_for(_fake_report(with_gaps=True)) == 1


class TestCLIMain:
    """``main`` end-to-end against a synthetic DAG."""

    def test_text_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        dag_path = tmp_path / "dag.json"
        _build_dag_with_two_constraints(tmp_path, dag_path)
        code = main([
            "--dag-path", str(dag_path),
            "--repo-root", str(tmp_path),
        ])
        assert code == 0
        captured = capsys.readouterr()
        assert "Requirement → Implementation → Verification" in (
            captured.out
        )
        assert "clean-one" in captured.out

    def test_json_mode(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        dag_path = tmp_path / "dag.json"
        _build_dag_with_two_constraints(tmp_path, dag_path)
        code = main([
            "--dag-path", str(dag_path),
            "--repo-root", str(tmp_path),
            "--json",
        ])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        names = [c["name"] for c in payload["constraints"]]
        assert names == ["clean-one", "spec-one"]

    def test_nonzero_on_gap_when_clean(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        dag_path = tmp_path / "dag.json"
        _build_dag_with_two_constraints(tmp_path, dag_path)
        code = main([
            "--dag-path", str(dag_path),
            "--repo-root", str(tmp_path),
            "--exit-nonzero-on-gap",
        ])
        assert code == 0
        capsys.readouterr()

    def test_nonzero_on_gap_when_broken(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Build a DAG whose ref points at a non-existent file,
        # then confirm the CLI returns 1 with the flag.
        dag_path = tmp_path / "dag.json"
        entity = Entity(id="e", name="E")
        domain = DomainConstraint(
            name="x", description="", entity_ids=["e"],
            rationale="r",
            implementation_refs=["nope/ghost.py:func"],
            verification_refs=[],
            status="implemented",
        )
        ontology = Ontology(
            entities=[entity], domain_constraints=[domain],
        )
        dag = OntologyDAG(project_name="t")
        dag.nodes.append(DAGNode(
            id="n", ontology=ontology, created_at="2026-04-20T12:00:00+00:00",
        ))
        dag.current_node_id = "n"
        save_dag(dag, str(dag_path))
        code = main([
            "--dag-path", str(dag_path),
            "--repo-root", str(tmp_path),
            "--exit-nonzero-on-gap",
        ])
        assert code == 1
        capsys.readouterr()


class TestCLIDashM:
    """``python -m audit_ontology --help`` invokes via __main__.

    Belt-and-suspenders check: the __main__.py module is omitted
    from coverage (per project convention, see pyproject.toml
    [tool.coverage.run]), but this test confirms the entry point
    actually reaches `cli.main`.
    """

    def test_dashm_entry(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "audit_ontology", "--help"],
            capture_output=True, text=True,
            cwd=Path(__file__).resolve().parents[2],
            env={
                "PYTHONPATH": str(
                    Path(__file__).resolve().parents[1] / "src"
                ),
                "PATH": "/usr/bin:/bin",
            },
            check=False, timeout=15,
        )
        assert result.returncode == 0
        assert "audit-ontology" in result.stdout
