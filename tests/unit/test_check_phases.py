"""Unit tests for ``scripts/check_phases.py`` (task 13.6).

Covers:
* Assignment passes when every R appears in exactly one phase (R14.2).
* Assignment fails listing duplicates when an R appears in two phases.
* Assignment fails listing orphans when an R is declared but missing from
  every phase section.
* Scope fails when ``current_phase=2`` and an R is in Phase 3 (R14.3).
* Scope passes for every requirement when ``current_phase=3``.
* CLI exit codes for assignment-pass, assignment-fail, scope-fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the repo root is importable so ``scripts.check_phases`` resolves
# regardless of how pytest is invoked.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_phases import (  # noqa: E402  (sys.path manipulation above)
    CheckResult,
    PlanDocument,
    PlanParseError,
    check_phase_assignment,
    check_phase_scope,
    main,
    parse_plan_file,
    parse_plan_text,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


PLAN_ALL_ASSIGNED_ONCE = """\
# PLAN

## Requirements

| ID  | Title          |
| --- | -------------- |
| R1  | Joke generation |
| R2  | Voice synthesis |
| R3  | Moderation      |
| R4  | Output safety   |
| R14 | Phased delivery |

## Phase 1 Minimum Viable Product

- R1
- R3

## Phase 2 Hardening and Cost Optimization

- R2
- R4

## Phase 3 Optional Enhancements

- R14
"""

PLAN_DUPLICATE_R = """\
# PLAN

## Requirements

| ID  |
| --- |
| R1  |
| R2  |
| R3  |

## Phase 1 Minimum Viable Product

- R1
- R2

## Phase 2 Hardening and Cost Optimization

- R2

## Phase 3 Optional Enhancements

- R3
"""

PLAN_ORPHAN_R = """\
# PLAN

## Requirements

| ID  |
| --- |
| R1  |
| R2  |
| R3  |
| R7  |

## Phase 1 Minimum Viable Product

- R1

## Phase 2 Hardening and Cost Optimization

- R2

## Phase 3 Optional Enhancements

- R3
"""

PLAN_R_IN_PHASE_3 = """\
# PLAN

## Requirements

| ID |
| -- |
| R1 |
| R2 |
| R3 |

## Phase 1 Minimum Viable Product

- R1

## Phase 2 Hardening and Cost Optimization

- R2

## Phase 3 Optional Enhancements

- R3
"""


# --------------------------------------------------------------------------- #
# Parser smoke
# --------------------------------------------------------------------------- #


def test_parse_plan_text_extracts_phases_and_requirements() -> None:
    plan = parse_plan_text(PLAN_ALL_ASSIGNED_ONCE)
    assert plan.requirements == {"R1", "R2", "R3", "R4", "R14"}
    assert plan.phases[1] == {"R1", "R3"}
    assert plan.phases[2] == {"R2", "R4"}
    assert plan.phases[3] == {"R14"}
    assert plan.phase_headings_present == {1, 2, 3}


def test_parse_plan_file_missing_path_raises() -> None:
    with pytest.raises(PlanParseError):
        parse_plan_file(Path("/this/path/does/not/exist/PLAN.md"))


# --------------------------------------------------------------------------- #
# check_phase_assignment
# --------------------------------------------------------------------------- #


def test_assignment_passes_when_every_r_in_exactly_one_phase() -> None:
    plan = parse_plan_text(PLAN_ALL_ASSIGNED_ONCE)
    result = check_phase_assignment(plan)
    assert isinstance(result, CheckResult)
    assert result.passed is True
    assert result.errors == []


def test_assignment_fails_on_duplicate_listing_phases() -> None:
    plan = parse_plan_text(PLAN_DUPLICATE_R)
    result = check_phase_assignment(plan)
    assert result.passed is False
    # Exactly one duplicate (R2) and no orphans.
    duplicate_errors = [e for e in result.errors if "Duplicate" in e]
    orphan_errors = [e for e in result.errors if "Orphan" in e]
    assert len(duplicate_errors) == 1
    assert "R2" in duplicate_errors[0]
    assert "Phase 1" in duplicate_errors[0]
    assert "Phase 2" in duplicate_errors[0]
    assert orphan_errors == []


def test_assignment_fails_on_orphan_listing_id() -> None:
    plan = parse_plan_text(PLAN_ORPHAN_R)
    result = check_phase_assignment(plan)
    assert result.passed is False
    orphan_errors = [e for e in result.errors if "Orphan" in e]
    duplicate_errors = [e for e in result.errors if "Duplicate" in e]
    assert duplicate_errors == []
    assert len(orphan_errors) == 1
    assert "R7" in orphan_errors[0]


def test_assignment_reports_both_duplicates_and_orphans() -> None:
    plan = PlanDocument(
        requirements={"R1", "R2", "R3", "R4"},
        phases={1: {"R1", "R2"}, 2: {"R2"}, 3: {"R3"}},
    )
    result = check_phase_assignment(plan)
    assert result.passed is False
    joined = "\n".join(result.errors)
    assert "R2" in joined and "Duplicate" in joined
    assert "R4" in joined and "Orphan" in joined


def test_assignment_handles_empty_plan() -> None:
    plan = PlanDocument(requirements=set(), phases={1: set(), 2: set(), 3: set()})
    result = check_phase_assignment(plan)
    # Vacuously true: no R-ids to assign, so no duplicates and no orphans.
    assert result.passed is True
    assert result.errors == []


# --------------------------------------------------------------------------- #
# check_phase_scope
# --------------------------------------------------------------------------- #


def test_scope_fails_when_current_phase_2_and_r_in_phase_3() -> None:
    plan = parse_plan_text(PLAN_R_IN_PHASE_3)
    result = check_phase_scope(plan, current_phase=2)
    assert result.passed is False
    assert len(result.errors) == 1
    assert "R3" in result.errors[0]
    assert "Phase 3" in result.errors[0]
    assert "current_phase is 2" in result.errors[0]


def test_scope_passes_for_all_requirements_when_current_phase_3() -> None:
    plan = parse_plan_text(PLAN_R_IN_PHASE_3)
    result = check_phase_scope(plan, current_phase=3)
    assert result.passed is True
    assert result.errors == []


def test_scope_phase_1_rejects_phase_2_and_phase_3() -> None:
    plan = parse_plan_text(PLAN_R_IN_PHASE_3)
    result = check_phase_scope(plan, current_phase=1)
    assert result.passed is False
    joined = "\n".join(result.errors)
    assert "R2" in joined
    assert "R3" in joined


def test_scope_passes_when_only_phase_1_assignments() -> None:
    plan = PlanDocument(
        requirements={"R1", "R2"},
        phases={1: {"R1", "R2"}, 2: set(), 3: set()},
    )
    result = check_phase_scope(plan, current_phase=1)
    assert result.passed is True
    assert result.errors == []


def test_scope_rejects_invalid_current_phase() -> None:
    plan = parse_plan_text(PLAN_R_IN_PHASE_3)
    with pytest.raises(ValueError):
        check_phase_scope(plan, current_phase=0)
    with pytest.raises(ValueError):
        check_phase_scope(plan, current_phase=4)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _write_plan(tmp_path: Path, text: str) -> Path:
    plan_path = tmp_path / "PLAN.md"
    plan_path.write_text(text, encoding="utf-8")
    return plan_path


def test_cli_returns_0_when_all_checks_pass(tmp_path: Path, capsys) -> None:
    plan_path = _write_plan(tmp_path, PLAN_ALL_ASSIGNED_ONCE)
    code = main(
        ["--plan", str(plan_path), "--current-phase", "3", "--mode", "all"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "[OK] Phase assignment" in out
    assert "[OK] Phase scope" in out


def test_cli_returns_1_on_assignment_failure(tmp_path: Path, capsys) -> None:
    plan_path = _write_plan(tmp_path, PLAN_DUPLICATE_R)
    code = main(
        ["--plan", str(plan_path), "--current-phase", "3", "--mode", "assignment"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] Phase assignment" in out
    assert "R2" in out


def test_cli_returns_1_on_scope_failure(tmp_path: Path, capsys) -> None:
    plan_path = _write_plan(tmp_path, PLAN_R_IN_PHASE_3)
    code = main(
        ["--plan", str(plan_path), "--current-phase", "2", "--mode", "scope"]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] Phase scope" in out
    assert "R3" in out


def test_cli_returns_2_when_plan_missing(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "does-not-exist.md"
    code = main(["--plan", str(missing), "--mode", "assignment"])
    err = capsys.readouterr().err
    assert code == 2
    assert "ERROR" in err
