"""Tests for branch_cov.coverage."""

from __future__ import annotations

import pytest

from pathlib import Path

from branch_cov.coverage import (
    BranchOutcome,
    CoverageGap,
    CoverageReport,
    _classify,
    _observed_outcomes,
    _required_outcomes,
    compare_to_baseline,
    compute_coverage,
    load_baseline,
)
from branch_cov.disasm import ConditionalBranch


def _make_branch(
    addr: int = 0x100,
    insn_size: int = 4,
    target: int = 0x200,
    mnemonic: str = "je",
) -> ConditionalBranch:
    return ConditionalBranch(
        addr=addr,
        insn_size=insn_size,
        target_taken=target,
        target_not_taken=addr + insn_size,
        mnemonic=mnemonic,
    )


class TestClassify:
    """_classify turns (branch, next_pc) into an outcome or None."""

    def test_taken_matches_target(self) -> None:
        b = _make_branch()
        assert _classify(b, 0x200) == BranchOutcome.TAKEN

    def test_not_taken_matches_fallthrough(self) -> None:
        b = _make_branch()
        assert _classify(b, 0x104) == BranchOutcome.NOT_TAKEN

    def test_unrelated_pc_returns_none(self) -> None:
        b = _make_branch()
        assert _classify(b, 0xDEADBEEF) is None


class TestObservedOutcomes:
    """_observed_outcomes scans adjacent trace pairs."""

    def test_empty_trace_returns_empty_set(self) -> None:
        assert _observed_outcomes([_make_branch()], []) == set()

    def test_single_pc_returns_empty_set(self) -> None:
        assert _observed_outcomes([_make_branch()], [0x100]) == set()

    def test_taken_observed(self) -> None:
        b = _make_branch()
        observed = _observed_outcomes([b], [0x100, 0x200])
        assert observed == {(0x100, BranchOutcome.TAKEN)}

    def test_not_taken_observed(self) -> None:
        b = _make_branch()
        observed = _observed_outcomes([b], [0x100, 0x104])
        assert observed == {(0x100, BranchOutcome.NOT_TAKEN)}

    def test_both_outcomes_observed(self) -> None:
        b = _make_branch()
        observed = _observed_outcomes(
            [b], [0x100, 0x200, 0x100, 0x104],
        )
        assert observed == {
            (0x100, BranchOutcome.TAKEN),
            (0x100, BranchOutcome.NOT_TAKEN),
        }

    def test_pc_not_a_branch_is_ignored(self) -> None:
        b = _make_branch()
        observed = _observed_outcomes([b], [0xDEAD, 0xBEEF])
        assert observed == set()

    def test_branch_followed_by_unrelated_pc_ignored(self) -> None:
        b = _make_branch()
        observed = _observed_outcomes([b], [0x100, 0xCAFE])
        assert observed == set()

    def test_trailing_branch_pc_has_no_successor(self) -> None:
        """A branch as the last PC in the trace contributes nothing."""
        b = _make_branch()
        observed = _observed_outcomes([b], [0x200, 0x100])
        assert observed == set()


class TestRequiredOutcomes:
    """Each branch requires both TAKEN and NOT_TAKEN."""

    def test_empty_branches_yields_empty(self) -> None:
        assert not _required_outcomes([])

    def test_one_branch_yields_two_outcomes(self) -> None:
        b = _make_branch()
        assert _required_outcomes([b]) == [
            (b, BranchOutcome.TAKEN),
            (b, BranchOutcome.NOT_TAKEN),
        ]

    def test_n_branches_yield_2n_outcomes(self) -> None:
        bs = [_make_branch(addr=a) for a in (0x100, 0x200, 0x300)]
        assert len(_required_outcomes(bs)) == 6

    def test_degenerate_branch_yields_single_outcome(self) -> None:
        """target_taken == target_not_taken → only TAKEN is required.

        Synthesises a `cbz x0, .+4` analogue: TAKEN target equals the
        fallthrough, so there's no semantically-distinct NOT_TAKEN
        path. Requiring both outcomes would create a permanent gap.
        """
        b = _make_branch(
            addr=0x100, insn_size=4, target=0x104,  # target == fallthrough
        )
        assert _required_outcomes([b]) == [(b, BranchOutcome.TAKEN)]


class TestComputeCoverage:
    """End-to-end: branches + trace → report with gaps."""

    def test_empty_branches_is_fully_covered(self) -> None:
        report = compute_coverage([], [0x100, 0x200])
        assert report.total_branches == 0
        assert report.observed_outcomes == 0
        assert report.fully_covered

    def test_no_trace_yields_all_gaps(self) -> None:
        b = _make_branch()
        report = compute_coverage([b], [])
        assert report.total_branches == 1
        assert report.observed_outcomes == 0
        assert len(report.gaps) == 2
        assert not report.fully_covered

    def test_full_trace_yields_no_gaps(self) -> None:
        b = _make_branch()
        report = compute_coverage(
            [b], [0x100, 0x200, 0x100, 0x104],
        )
        assert report.total_branches == 1
        assert report.observed_outcomes == 2
        assert report.fully_covered

    def test_partial_trace_reports_only_missing(self) -> None:
        b = _make_branch()
        report = compute_coverage([b], [0x100, 0x200])
        assert report.observed_outcomes == 1
        assert len(report.gaps) == 1
        assert report.gaps[0].missing == BranchOutcome.NOT_TAKEN


@pytest.mark.parametrize(
    "outcome_str, enum_val",
    [
        ("taken", BranchOutcome.TAKEN),
        ("not_taken", BranchOutcome.NOT_TAKEN),
    ],
)
def test_branch_outcome_string_values(
    outcome_str: str, enum_val: BranchOutcome,
) -> None:
    """Stable serialization values for the enum."""
    assert enum_val.value == outcome_str


class TestLoadBaseline:
    """Parse a baseline file into (addr, outcome) tuples."""

    def test_basic_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.txt"
        path.write_text(
            "0x48 taken\n"
            "0x64 not_taken\n",
            encoding="utf-8",
        )
        assert load_baseline(path) == {
            (0x48, BranchOutcome.TAKEN),
            (0x64, BranchOutcome.NOT_TAKEN),
        }

    def test_inline_comments_stripped(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.txt"
        path.write_text(
            "# header comment\n"
            "0x100 taken  # secondary-CPU entry\n"
            "\n"
            "0x200 not_taken # whatever\n",
            encoding="utf-8",
        )
        assert load_baseline(path) == {
            (0x100, BranchOutcome.TAKEN),
            (0x200, BranchOutcome.NOT_TAKEN),
        }

    def test_empty_file_returns_empty_set(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.txt"
        path.write_text("", encoding="utf-8")
        assert load_baseline(path) == set()

    def test_malformed_line_raises_with_context(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "baseline.txt"
        path.write_text("0x10 taken\nbogus_line\n", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            load_baseline(path)
        assert ":2:" in str(exc.value)
        assert "bogus_line" in str(exc.value)

    def test_invalid_outcome_raises_with_context(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "baseline.txt"
        path.write_text("0x48 sideways\n", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            load_baseline(path)
        assert ":1:" in str(exc.value)
        assert "sideways" in str(exc.value)

    def test_malformed_hex_raises_with_context(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "baseline.txt"
        path.write_text("xyzzy taken\n", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            load_baseline(path)
        assert ":1:" in str(exc.value)
        assert "xyzzy" in str(exc.value)


class TestCompareToBaseline:
    """Delta between a fresh report and the baseline."""

    def _report_with_gaps(
        self, gaps: list[tuple[int, BranchOutcome]],
    ) -> CoverageReport:
        # Construct a CoverageReport with the given gaps synthesized
        # from trivial ConditionalBranch stubs. Only addr + missing
        # matter for compare_to_baseline.
        cov_gaps = [
            CoverageGap(
                branch=ConditionalBranch(
                    addr=addr, insn_size=4,
                    target_taken=addr + 100,
                    target_not_taken=addr + 4,
                    mnemonic="je",
                ),
                missing=oc,
            )
            for (addr, oc) in gaps
        ]
        return CoverageReport(
            total_branches=len(gaps),
            observed_outcomes=0,
            gaps=cov_gaps,
        )

    def test_exact_match(self) -> None:
        report = self._report_with_gaps([
            (0x48, BranchOutcome.TAKEN),
            (0x64, BranchOutcome.TAKEN),
        ])
        baseline = {
            (0x48, BranchOutcome.TAKEN),
            (0x64, BranchOutcome.TAKEN),
        }
        cmp_ = compare_to_baseline(report, baseline)
        assert cmp_.matches
        assert not cmp_.new_gaps
        assert not cmp_.closed_gaps

    def test_new_gap_detected(self) -> None:
        report = self._report_with_gaps([
            (0x48, BranchOutcome.TAKEN),
            (0x64, BranchOutcome.TAKEN),
            (0x80, BranchOutcome.NOT_TAKEN),  # new
        ])
        baseline = {
            (0x48, BranchOutcome.TAKEN),
            (0x64, BranchOutcome.TAKEN),
        }
        cmp_ = compare_to_baseline(report, baseline)
        assert not cmp_.matches
        assert cmp_.new_gaps == [(0x80, BranchOutcome.NOT_TAKEN)]
        assert not cmp_.closed_gaps

    def test_closed_gap_detected(self) -> None:
        report = self._report_with_gaps([
            (0x48, BranchOutcome.TAKEN),
        ])
        baseline = {
            (0x48, BranchOutcome.TAKEN),
            (0x64, BranchOutcome.TAKEN),  # now covered — baseline stale
        }
        cmp_ = compare_to_baseline(report, baseline)
        assert not cmp_.matches
        assert not cmp_.new_gaps
        assert cmp_.closed_gaps == [(0x64, BranchOutcome.TAKEN)]

    def test_both_new_and_closed(self) -> None:
        report = self._report_with_gaps([
            (0x48, BranchOutcome.TAKEN),
            (0x80, BranchOutcome.TAKEN),  # new
        ])
        baseline = {
            (0x48, BranchOutcome.TAKEN),
            (0x64, BranchOutcome.TAKEN),  # closed
        }
        cmp_ = compare_to_baseline(report, baseline)
        assert cmp_.new_gaps == [(0x80, BranchOutcome.TAKEN)]
        assert cmp_.closed_gaps == [(0x64, BranchOutcome.TAKEN)]
