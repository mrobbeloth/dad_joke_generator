"""Unit tests for ``scripts/check_plan_xref.py`` (task 13.3).

Validates: Requirement 12.5 — every requirement identifier listed in
``docs/PLAN.md`` SHALL appear at least once in ``docs/TEST_PLAN.md``;
otherwise the Production_Gate blocks deployment.

The fixtures are inlined synthetic markdown rather than the real
``docs/PLAN.md`` / ``docs/TEST_PLAN.md`` because at the time this task is
executed those documents are still empty stubs (tasks 15.1 and 15.2
populate them).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

# Ensure the repo root is importable so ``scripts.check_plan_xref``
# resolves regardless of how pytest is invoked.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_plan_xref import (  # noqa: E402  (sys.path manipulation above)
    EXIT_MISSING_REFS,
    EXIT_OK,
    EXIT_PARSE_ERROR,
    CheckResult,
    check_xref,
    check_xref_documents,
    main,
)
from scripts.plan_parser import (  # noqa: E402
    PlanDocument,
    RequirementEntry,
    TestPlanDocument,
)


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(body).lstrip("\n"), encoding="utf-8")
    return p


def _plan_md(*requirement_ids: str) -> str:
    """Build a minimal PLAN.md naming the given R-ids in Phase 1."""
    lines = ["# PLAN", "", "## Phase 1 Minimum Viable Product", ""]
    for rid in requirement_ids:
        lines.append(f"- {rid}: Title for {rid}")
    lines.append("")
    return "\n".join(lines)


def _test_plan_md(*requirement_ids: str) -> str:
    """Build a minimal TEST_PLAN.md mentioning the given R-ids in prose."""
    lines = ["# TEST PLAN", ""]
    for rid in requirement_ids:
        lines.append(f"- unit: covers {rid} with 80% target")
    lines.append("")
    return "\n".join(lines)


def _make_plan_doc(*requirement_ids: str) -> PlanDocument:
    """Build a synthetic :class:`PlanDocument` with the given R-ids.

    Useful for the duplicate-id test and for tests of
    :func:`check_xref_documents` that bypass markdown parsing entirely.
    """
    reqs = tuple(
        RequirementEntry(id=rid, title="", phase="Phase 1", status="")
        for rid in requirement_ids
    )
    return PlanDocument(
        requirements=reqs,
        phases=("Phase 1",),
        manual_setup=(),
        bedrock_model_id=None,
        polly_voice_id=None,
        rights_confirmed=False,
    )


def _make_test_plan_doc(*requirement_ids: str) -> TestPlanDocument:
    return TestPlanDocument(
        test_types=(),
        requirement_refs=frozenset(requirement_ids),
    )


# --------------------------------------------------------------------------- #
# check_xref / check_xref_documents
# --------------------------------------------------------------------------- #


def test_all_r_ids_present_returns_ok(tmp_path: Path) -> None:
    """PLAN lists R1, R5, R12; TEST_PLAN mentions all three → ok=True."""
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1", "R5", "R12"))
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R1", "R5", "R12"))

    result = check_xref(plan, test_plan)

    assert isinstance(result, CheckResult)
    assert result.ok is True
    assert result.errors == []
    assert result.missing_ids == []


def test_one_missing_id_reports_that_id(tmp_path: Path) -> None:
    """PLAN lists R1, R5, R12; TEST_PLAN mentions only R1, R5 → R12 missing."""
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1", "R5", "R12"))
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R1", "R5"))

    result = check_xref(plan, test_plan)

    assert result.ok is False
    assert result.missing_ids == ["R12"]
    assert len(result.errors) == 1
    assert "R12" in result.errors[0]
    assert "1 requirement" in result.errors[0]


def test_multiple_missing_ids_sorted_by_numeric_r_id(tmp_path: Path) -> None:
    """PLAN R1, R5, R12, R17; TEST_PLAN R5 → missing [R1, R12, R17]."""
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1", "R5", "R12", "R17"))
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R5"))

    result = check_xref(plan, test_plan)

    assert result.ok is False
    assert result.missing_ids == ["R1", "R12", "R17"]
    # Error summary preserves the same numeric ordering.
    assert "R1, R12, R17" in result.errors[0]


def test_numeric_sort_order_not_lexicographic(tmp_path: Path) -> None:
    """PLAN R2, R10, R3; TEST_PLAN R2 → missing must be [R3, R10] (numeric)."""
    plan = _write(tmp_path, "PLAN.md", _plan_md("R2", "R10", "R3"))
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R2"))

    result = check_xref(plan, test_plan)

    assert result.ok is False
    # Lexicographic order would put "R10" before "R3"; numeric ordering
    # places "R3" first because 3 < 10.
    assert result.missing_ids == ["R3", "R10"]


def test_plan_duplicate_id_collapses_to_single_check() -> None:
    """Same R-id listed under two phases counts as one cross-ref entry."""
    plan = _make_plan_doc("R1", "R5", "R5")  # R5 duplicated
    test_plan = _make_test_plan_doc("R1", "R5")

    result = check_xref_documents(plan, test_plan)

    assert result.ok is True
    assert result.missing_ids == []


def test_test_plan_extras_are_allowed(tmp_path: Path) -> None:
    """TEST_PLAN may reference IDs not in PLAN — only PLAN→TEST_PLAN matters."""
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1", "R2"))
    test_plan = _write(
        tmp_path, "TEST_PLAN.md", _test_plan_md("R1", "R2", "R99")
    )

    result = check_xref(plan, test_plan)

    assert result.ok is True
    assert result.missing_ids == []


def test_plan_md_missing_returns_parse_failure(tmp_path: Path) -> None:
    """Missing PLAN.md → ok=False, error names the file."""
    missing_plan = tmp_path / "PLAN.md"
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R1"))

    result = check_xref(missing_plan, test_plan)

    assert result.ok is False
    assert result.missing_ids == []
    assert len(result.errors) == 1
    assert "PLAN.md" in result.errors[0]
    assert "cannot read" in result.errors[0]


def test_test_plan_md_missing_returns_parse_failure(tmp_path: Path) -> None:
    """Missing TEST_PLAN.md → ok=False, error names the file."""
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1"))
    missing_test_plan = tmp_path / "TEST_PLAN.md"

    result = check_xref(plan, missing_test_plan)

    assert result.ok is False
    assert result.missing_ids == []
    assert len(result.errors) == 1
    assert "TEST_PLAN.md" in result.errors[0]
    assert "cannot read" in result.errors[0]


def test_empty_plan_is_vacuously_satisfied(tmp_path: Path) -> None:
    """PLAN with zero requirements → ok=True, missing_ids=[]."""
    plan = _write(tmp_path, "PLAN.md", "# PLAN\n\nNo requirements yet.\n")
    test_plan = _write(tmp_path, "TEST_PLAN.md", "# TEST PLAN\n\n")

    result = check_xref(plan, test_plan)

    assert result.ok is True
    assert result.missing_ids == []
    assert result.errors == []


# --------------------------------------------------------------------------- #
# CLI exit codes
# --------------------------------------------------------------------------- #


def test_cli_returns_0_on_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1", "R5"))
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R1", "R5"))

    code = main(["--plan", str(plan), "--test-plan", str(test_plan)])
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert "cross-reference check passed" in captured.out
    assert "2 requirement" in captured.out


def test_cli_returns_1_on_missing_refs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1", "R5", "R12"))
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R1"))

    code = main(["--plan", str(plan), "--test-plan", str(test_plan)])
    captured = capsys.readouterr()

    assert code == EXIT_MISSING_REFS
    # Missing-ref errors go to stderr.
    assert "R5" in captured.err
    assert "R12" in captured.err


def test_cli_returns_2_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_plan = tmp_path / "PLAN.md"  # never created
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R1"))

    code = main(["--plan", str(missing_plan), "--test-plan", str(test_plan)])
    captured = capsys.readouterr()

    assert code == EXIT_PARSE_ERROR
    assert "PLAN.md" in captured.err


def test_cli_subprocess_smoke(tmp_path: Path) -> None:
    """End-to-end: invoke the script as ``python scripts/check_plan_xref.py``."""
    plan = _write(tmp_path, "PLAN.md", _plan_md("R1"))
    test_plan = _write(tmp_path, "TEST_PLAN.md", _test_plan_md("R1"))

    completed = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "scripts" / "check_plan_xref.py"),
            "--plan",
            str(plan),
            "--test-plan",
            str(test_plan),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == EXIT_OK, completed.stderr
    assert "cross-reference check passed" in completed.stdout
