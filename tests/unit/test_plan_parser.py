"""Unit tests for ``scripts.plan_parser``.

These tests exercise the parser against synthetic markdown fixtures that
mimic the multiple shapes :func:`scripts.plan_parser.parse_plan` and
:func:`scripts.plan_parser.parse_test_plan` are required to support.

The fixtures are inlined here rather than committed to ``docs/`` because at
the time this task is executed ``docs/PLAN.md`` and ``docs/TEST_PLAN.md``
are still empty stubs (tasks 15.1 and 15.2 populate them).

Validates: Requirements 11.1, 11.2, 14.2, 15.1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from scripts import plan_parser as pp
from scripts.plan_parser import (
    KNOWN_STATUSES,
    KNOWN_TEST_TYPES,
    ManualSetupItem,
    PlanDocument,
    PlanParseError,
    RequirementEntry,
    TestPlanDocument,
    TestTypeEntry,
    parse_plan,
    parse_test_plan,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(body).lstrip("\n"), encoding="utf-8")
    return p


PLAN_TABLE = """
# PLAN

Bedrock model: amazon.nova-lite-v1:0
Polly voice: Joanna
training_corpus_rights_confirmed: true

## Phase 1 Minimum Viable Product

| ID | Title                  | Phase                              | Status      |
| -- | ---------------------- | ---------------------------------- | ----------- |
| R1 | Joke generation        | Phase 1 Minimum Viable Product     | Completed   |
| R5 | IP-Based Rate Limiting | Phase 1 Minimum Viable Product     | In-Progress |

## Phase 2 Hardening and Cost Optimization

| ID | Title           | Phase                                  | Status   |
| -- | --------------- | -------------------------------------- | -------- |
| R12 | Production Gate | Phase 2 Hardening and Cost Optimization | Planned  |

## Phase 3 Optional Enhancements

- R17: Training Corpus Handling [phase=Phase 3 Optional Enhancements] [status=Deferred]

## AWS Manual Setup

- [x] Create AWS account (2024-05-01)
- [x] Configure billing alert — 2024-06-01
- [ ] Request Bedrock model access
- [ ] Create deployment IAM role
"""


PLAN_LIST_FORMAT = """
# PLAN

Bedrock Model ID = anthropic.claude-3-haiku-20240307-v1:0
polly_voice_id: Matthew
rights_confirmed: false

## Phase 1 Minimum Viable Product

- R1: Joke generation [status=Completed]
- R5: IP-Based Rate Limiting — In-Progress

## Phase 2 Hardening and Cost Optimization

- **R12**: Production Gate [status=Planned]

## AWS Manual Setup

- [x] Domain delegated to Route 53 (2024-04-15)
- [ ] Request Bedrock model access
"""


PLAN_NO_BEDROCK_NO_POLLY = """
# PLAN

Some narrative text mentioning the word polly in passing but not as a field.

## Phase 1 Minimum Viable Product

- R1: Joke generation [status=Completed]

## AWS Manual Setup

- [ ] Create AWS account
"""


PLAN_MALFORMED_DATE = """
# PLAN

## AWS Manual Setup

- [x] Configure billing alert (2024-13-40)
- [x] Set up SNS topics
- [x] Open ticket for Polly access (2024-07-04)
"""


PLAN_MISSING_PHASE_3 = """
# PLAN

## Phase 1 Minimum Viable Product

- R1: Joke generation [status=Completed]

## Phase 2 Hardening and Cost Optimization

- R5: IP-Based Rate Limiting [status=Planned]

## AWS Manual Setup

- [ ] Create AWS account
"""


# ---------------------------------------------------------------------------
# parse_plan tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [PLAN_TABLE, PLAN_LIST_FORMAT],
    ids=["table-format", "list-format"],
)
def test_parse_plan_extracts_requirement_ids_across_supported_formats(
    tmp_path: Path, fixture: str,
) -> None:
    """R11.1: every requirement entry must be discovered regardless of layout."""
    plan_path = _write(tmp_path, "PLAN.md", fixture)

    doc = parse_plan(plan_path)

    ids = [r.id for r in doc.requirements]
    assert "R1" in ids
    assert "R5" in ids
    assert "R12" in ids
    # Every id matches the R[0-9]+ shape required by R11.1.
    for r in doc.requirements:
        assert r.id.startswith("R") and r.id[1:].isdigit()
        # Status, when present, comes from the documented vocabulary.
        if r.status:
            assert r.status in KNOWN_STATUSES


def test_parse_plan_table_format_assigns_phase_and_status(tmp_path: Path) -> None:
    """Table cells must populate phase and status fields directly (R11.1, R14.2)."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)

    doc = parse_plan(plan_path)
    by_id = {r.id: r for r in doc.requirements}

    assert by_id["R1"].title == "Joke generation"
    assert by_id["R1"].phase == "Phase 1 Minimum Viable Product"
    assert by_id["R1"].status == "Completed"

    assert by_id["R5"].phase == "Phase 1 Minimum Viable Product"
    assert by_id["R5"].status == "In-Progress"

    assert by_id["R12"].phase == "Phase 2 Hardening and Cost Optimization"
    assert by_id["R12"].status == "Planned"

    assert by_id["R17"].phase == "Phase 3 Optional Enhancements"
    assert by_id["R17"].status == "Deferred"


def test_parse_plan_list_format_uses_section_for_phase(tmp_path: Path) -> None:
    """List-format entries inherit phase from the surrounding ``##`` header."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_LIST_FORMAT)

    doc = parse_plan(plan_path)
    by_id = {r.id: r for r in doc.requirements}

    assert by_id["R1"].phase == "Phase 1 Minimum Viable Product"
    assert by_id["R1"].status == "Completed"

    # Trailing "— In-Progress" must be parsed.
    assert by_id["R5"].status == "In-Progress"
    assert by_id["R5"].title == "IP-Based Rate Limiting"

    assert by_id["R12"].phase == "Phase 2 Hardening and Cost Optimization"
    assert by_id["R12"].status == "Planned"


def test_parse_plan_extracts_phases_in_document_order(tmp_path: Path) -> None:
    """R14.2-supporting: ``phases`` should be ordered and deduplicated."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)

    doc = parse_plan(plan_path)

    assert doc.phases == (
        "Phase 1 Minimum Viable Product",
        "Phase 2 Hardening and Cost Optimization",
        "Phase 3 Optional Enhancements",
    )


def test_parse_plan_missing_phase_section_yields_only_present_phases(
    tmp_path: Path,
) -> None:
    """Missing Phase 3 section must not crash the parser; it just isn't listed."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_MISSING_PHASE_3)

    doc = parse_plan(plan_path)

    assert "Phase 3 Optional Enhancements" not in doc.phases
    assert doc.phases == (
        "Phase 1 Minimum Viable Product",
        "Phase 2 Hardening and Cost Optimization",
    )
    # Validation that exactly three phases must exist is the job of the
    # phase-checker (task 13.6); the parser exposes the raw structure.


def test_parse_plan_extracts_manual_setup_with_dates(tmp_path: Path) -> None:
    """R15.1: AWS Manual Setup section yields one item per checkbox row."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)

    doc = parse_plan(plan_path)

    assert len(doc.manual_setup) == 4

    by_label = {m.label: m for m in doc.manual_setup}

    item_account = by_label["Create AWS account"]
    assert item_account.checked is True
    assert item_account.completion_date == date(2024, 5, 1)

    item_billing = by_label["Configure billing alert"]
    assert item_billing.checked is True
    assert item_billing.completion_date == date(2024, 6, 1)

    item_bedrock = by_label["Request Bedrock model access"]
    assert item_bedrock.checked is False
    assert item_bedrock.completion_date is None

    item_iam = by_label["Create deployment IAM role"]
    assert item_iam.checked is False
    assert item_iam.completion_date is None


def test_parse_plan_malformed_manual_setup_date_is_none(tmp_path: Path) -> None:
    """A checked item with a non-existent calendar date returns ``None``.

    The R15.4 validator (task 13.7) is responsible for treating that as a
    completion-without-date violation; the parser only reports the fact.
    """
    plan_path = _write(tmp_path, "PLAN.md", PLAN_MALFORMED_DATE)

    doc = parse_plan(plan_path)

    by_label = {m.label: m for m in doc.manual_setup}

    bad = by_label["Configure billing alert"]
    assert bad.checked is True
    assert bad.completion_date is None  # 2024-13-40 is not a valid date

    no_date = by_label["Set up SNS topics"]
    assert no_date.checked is True
    assert no_date.completion_date is None

    good = by_label["Open ticket for Polly access"]
    assert good.checked is True
    assert good.completion_date == date(2024, 7, 4)


def test_parse_plan_picks_up_bedrock_and_polly_selections(tmp_path: Path) -> None:
    """R11.1: selected Bedrock and Polly options must be surfaced."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)

    doc = parse_plan(plan_path)

    assert doc.bedrock_model_id == "amazon.nova-lite-v1:0"
    assert doc.polly_voice_id == "Joanna"


def test_parse_plan_picks_up_alternative_field_styles(tmp_path: Path) -> None:
    """The parser should accept ``=`` and snake_case_id field names."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_LIST_FORMAT)

    doc = parse_plan(plan_path)

    assert doc.bedrock_model_id == "anthropic.claude-3-haiku-20240307-v1:0"
    assert doc.polly_voice_id == "Matthew"


def test_parse_plan_no_bedrock_or_polly_yields_none(tmp_path: Path) -> None:
    """Documents missing the model/voice fields return ``None`` (R11.1 default)."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_NO_BEDROCK_NO_POLLY)

    doc = parse_plan(plan_path)

    assert doc.bedrock_model_id is None
    assert doc.polly_voice_id is None


def test_parse_plan_rights_flag_true(tmp_path: Path) -> None:
    """R17.6: explicit ``rights_confirmed: true`` is reflected in the model."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)

    doc = parse_plan(plan_path)

    assert doc.rights_confirmed is True


def test_parse_plan_rights_flag_false(tmp_path: Path) -> None:
    """R17.6/R17.7 fail-closed: an explicit false flag stays false."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_LIST_FORMAT)

    doc = parse_plan(plan_path)

    assert doc.rights_confirmed is False


def test_parse_plan_rights_flag_default_false_when_absent(
    tmp_path: Path,
) -> None:
    """Absent rights flag must default to ``False`` (R17.7 fail-closed)."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_NO_BEDROCK_NO_POLLY)

    doc = parse_plan(plan_path)

    assert doc.rights_confirmed is False


def test_parse_plan_returns_immutable_document(tmp_path: Path) -> None:
    """Frozen dataclasses guarantee downstream scripts cannot mutate state."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)

    doc = parse_plan(plan_path)

    assert isinstance(doc, PlanDocument)
    assert isinstance(doc.requirements, tuple)
    assert isinstance(doc.phases, tuple)
    assert isinstance(doc.manual_setup, tuple)
    with pytest.raises((AttributeError, TypeError)):
        doc.requirements = ()  # type: ignore[misc]


def test_parse_plan_missing_file_raises_plan_parse_error(tmp_path: Path) -> None:
    """IO failures bubble up as :class:`PlanParseError`, not ``FileNotFoundError``."""
    missing = tmp_path / "does-not-exist.md"

    with pytest.raises(PlanParseError):
        parse_plan(missing)


def test_parse_plan_accepts_pathlib_and_str(tmp_path: Path) -> None:
    """Callers may pass either a ``str`` path or a :class:`pathlib.Path`."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)

    from_str = parse_plan(str(plan_path))
    from_path = parse_plan(plan_path)

    assert from_str == from_path


# ---------------------------------------------------------------------------
# parse_test_plan tests
# ---------------------------------------------------------------------------


TEST_PLAN_TABLE = """
# Test Plan

| Test type     | Coverage target | Pass criterion                            |
| ------------- | --------------- | ----------------------------------------- |
| unit          | 80%             | All unit tests green                      |
| integration   | 70%             | All integration tests green               |
| end-to-end    | 50%             | Happy-path against deployed stack passes  |
| accessibility | 100%            | Zero serious or critical violations       |
| performance   | 90%             | p95 < 10s at 50 RPS for 5 minutes         |

Cross references: R1, R2, R5, R5, R12, R17, R18.
Mentions of R3 in the prose for completeness.
"""


TEST_PLAN_LIST = """
# Test Plan

- unit: target=80, criterion=All unit tests green
- integration: target=70, criterion=All integration tests green
- end-to-end: target=50, criterion=Happy-path passes
- accessibility: target=100, criterion=Zero violations
- performance: target=90, criterion=p95 < 10s

Refs: R1 R5 R5 R12.
"""


TEST_PLAN_MISSING_PERFORMANCE = """
# Test Plan

| Test type     | Coverage target | Pass criterion              |
| ------------- | --------------- | --------------------------- |
| unit          | 80%             | All unit tests green        |
| integration   | 70%             | All integration tests green |
| end-to-end    | 50%             | Happy path passes           |
| accessibility | 100%            | Zero violations             |

Mentions: R1 R2.
"""


@pytest.mark.parametrize(
    "fixture",
    [TEST_PLAN_TABLE, TEST_PLAN_LIST],
    ids=["table-format", "list-format"],
)
def test_parse_test_plan_extracts_all_five_test_types(
    tmp_path: Path, fixture: str,
) -> None:
    """R11.2: each of the five required test types is parsed."""
    test_plan_path = _write(tmp_path, "TEST_PLAN.md", fixture)

    doc = parse_test_plan(test_plan_path)

    types = {entry.test_type for entry in doc.test_types}
    assert types == set(KNOWN_TEST_TYPES)


def test_parse_test_plan_table_extracts_targets_and_criteria(
    tmp_path: Path,
) -> None:
    """Coverage targets stay in [0, 100] and criteria text is preserved."""
    test_plan_path = _write(tmp_path, "TEST_PLAN.md", TEST_PLAN_TABLE)

    doc = parse_test_plan(test_plan_path)
    by_type = {e.test_type: e for e in doc.test_types}

    assert by_type["unit"].coverage_target_pct == 80
    assert by_type["unit"].pass_criterion.startswith("All unit tests green")

    assert by_type["integration"].coverage_target_pct == 70
    assert by_type["end-to-end"].coverage_target_pct == 50
    assert by_type["accessibility"].coverage_target_pct == 100
    assert by_type["performance"].coverage_target_pct == 90

    for entry in doc.test_types:
        assert 0 <= entry.coverage_target_pct <= 100
        assert entry.pass_criterion  # non-empty


def test_parse_test_plan_missing_test_type_is_simply_absent(
    tmp_path: Path,
) -> None:
    """Missing rows do not crash the parser; the validator catches them."""
    test_plan_path = _write(tmp_path, "TEST_PLAN.md", TEST_PLAN_MISSING_PERFORMANCE)

    doc = parse_test_plan(test_plan_path)
    types = {entry.test_type for entry in doc.test_types}

    assert "performance" not in types
    assert types == {"unit", "integration", "end-to-end", "accessibility"}


def test_parse_test_plan_collects_all_requirement_refs(tmp_path: Path) -> None:
    """Every ``Rn`` token anywhere in the document appears in the ref set."""
    test_plan_path = _write(tmp_path, "TEST_PLAN.md", TEST_PLAN_TABLE)

    doc = parse_test_plan(test_plan_path)

    assert doc.requirement_refs == frozenset(
        {"R1", "R2", "R3", "R5", "R12", "R17", "R18"}
    )
    # Set semantics dedupe repeated mentions of "R5".
    assert len(doc.requirement_refs) == 7


def test_parse_test_plan_returns_immutable_document(tmp_path: Path) -> None:
    """Frozen dataclasses; refs is a ``frozenset``."""
    test_plan_path = _write(tmp_path, "TEST_PLAN.md", TEST_PLAN_LIST)

    doc = parse_test_plan(test_plan_path)

    assert isinstance(doc, TestPlanDocument)
    assert isinstance(doc.test_types, tuple)
    assert isinstance(doc.requirement_refs, frozenset)
    with pytest.raises((AttributeError, TypeError)):
        doc.test_types = ()  # type: ignore[misc]


def test_parse_test_plan_missing_file_raises_plan_parse_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-test-plan.md"

    with pytest.raises(PlanParseError):
        parse_test_plan(missing)


# ---------------------------------------------------------------------------
# Importability and CLI smoke
# ---------------------------------------------------------------------------


def test_module_importable_via_dotted_path() -> None:
    """``import scripts.plan_parser`` must work from the project root."""
    import importlib

    module = importlib.import_module("scripts.plan_parser")

    assert module is pp
    assert hasattr(module, "parse_plan")
    assert hasattr(module, "parse_test_plan")
    assert hasattr(module, "PlanParseError")


def test_module_importable_via_from_import() -> None:
    """``from scripts.plan_parser import parse_plan`` must also work."""
    from scripts.plan_parser import parse_plan as imported_parse_plan
    from scripts.plan_parser import parse_test_plan as imported_parse_test_plan

    assert imported_parse_plan is parse_plan
    assert imported_parse_test_plan is parse_test_plan


def test_cli_smoke_runner_emits_json(tmp_path: Path) -> None:
    """``python -m scripts.plan_parser <path> --json`` prints valid JSON."""
    plan_path = _write(tmp_path, "PLAN.md", PLAN_TABLE)
    project_root = Path(__file__).resolve().parents[2]

    proc = subprocess.run(
        [sys.executable, "-m", "scripts.plan_parser", str(plan_path), "--json"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert isinstance(data, dict)
    assert "requirements" in data
    assert any(r["id"] == "R5" for r in data["requirements"])
    assert data["bedrock_model_id"] == "amazon.nova-lite-v1:0"
    assert data["polly_voice_id"] == "Joanna"


# ---------------------------------------------------------------------------
# Sanity checks on dataclass shapes
# ---------------------------------------------------------------------------


def test_dataclasses_are_frozen_and_typed() -> None:
    """Smoke test that the public dataclasses behave as documented."""
    req = RequirementEntry(id="R1", title="t", phase="p", status="Planned")
    item = ManualSetupItem(label="x", checked=False, completion_date=None)
    tt = TestTypeEntry(test_type="unit", coverage_target_pct=80, pass_criterion="ok")

    with pytest.raises((AttributeError, TypeError)):
        req.id = "R2"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        item.checked = True  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        tt.coverage_target_pct = 50  # type: ignore[misc]
