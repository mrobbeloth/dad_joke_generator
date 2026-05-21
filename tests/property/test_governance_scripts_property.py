"""Property tests for the governance scripts (task 13.8).

Implements the seven correctness properties from ``design.md`` covering
the gate scripts authored under tasks 13.2 - 13.7:

* **Property 23: Plan/Test-Plan freshness check on src/ changes.**
  *For any* ``(now_utc, plan_last_modified_utc, test_plan_last_modified_utc)``
  triple where a PR modifies any file under ``src/``, the freshness
  check SHALL fail iff ``floor((now - plan_last_modified) / 1 day) > 90``
  OR ``floor((now - test_plan_last_modified) / 1 day) > 90``.
* **Property 24: Plan/Test-Plan cross-reference completeness.** *For
  any* parsed ``(PLAN.md, TEST_PLAN.md)`` pair, the cross-reference
  check SHALL pass iff every requirement identifier listed in
  ``PLAN.md`` appears at least once in ``TEST_PLAN.md``.
* **Property 25: Cost_Report ↔ runtime configuration consistency.**
  *For any* ``(cost_report_model_id, runtime_model_id,
  cost_report_voice_id, runtime_voice_id)`` tuple, the check SHALL
  block deployment iff either id mismatches.
* **Property 26: Feature branch name validator.** *For any* candidate
  branch name, the validator SHALL accept iff the name matches
  ``^feature/[a-z0-9-]{3,50}$`` (with ``main``/``master`` accepted as
  integration-branch escape hatches).
* **Property 27: Each requirement assigned to exactly one phase.**
  *For any* parsed ``PLAN.md`` document, the assignment check SHALL
  pass iff every requirement identifier appears in exactly one of
  the three phase sections.
* **Property 28: Phase scope rule for deployments.** *For any*
  ``(current_phase, requirement_phase)`` pair where phases are ordered
  Phase 1 < Phase 2 < Phase 3, deployment SHALL be allowed iff every
  ``requirement_phase <= current_phase``.
* **Property 29: Manual-setup completion requires an ISO 8601 date.**
  *For any* edit setting a manual-setup item to checked, the validator
  SHALL accept iff the same edit records a completion date matching
  ``^\\d{4}-\\d{2}-\\d{2}$`` and representing a valid calendar date.

**Validates: Requirements 11.6, 12.5, 12.6, 13.2, 14.2, 14.3, 15.4, 15.5**

Test strategy
-------------
Each property exercises only its leaf-level pure helper -- the markdown
parsers themselves are well-tested elsewhere, so this file builds
synthetic ``PlanDocument`` / ``TestPlanDocument`` / ``RequirementEntry``
/ ``ManualSetupItem`` instances directly and feeds them to the gate
functions under test. The Cost_Report check is the only one that
requires a real file on disk; we use the ``tmp_path`` pytest fixture
plus ``@example`` decorations so the per-example file write does not
push the property tests into the slow lane. ``ManualSetupItem`` is
imported from ``scripts.check_manual_setup`` so we pick up the
local-fallback shape (``identifier``, ``label``, ``checked``,
``completion_date``) that the 13.7 validators duck-type against.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from scripts.check_branch_name import (
    INTEGRATION_BRANCHES,
    is_valid_feature_branch,
    validate_branch,
)
from scripts.check_cost_report import (
    check_consistency,
    extract_recommended_ids,
)
from scripts.check_doc_freshness import (
    EXIT_OK,
    EXIT_STALE,
    TRACKED_DOCS,
    check_freshness,
    compute_age_days,
)
from scripts.check_manual_setup import (
    ManualSetupItem,
    is_valid_iso_date,
    validate_manual_setup_complete,
    validate_manual_setup_edits,
)
from scripts.check_phases import (
    PlanDocument as PhasesPlanDocument,
    VALID_PHASES,
    check_phase_assignment,
    check_phase_scope,
)
from scripts.check_plan_xref import check_xref_documents
from scripts.plan_parser import (
    PlanDocument,
    RequirementEntry,
    TestPlanDocument,
)


# ---------------------------------------------------------------------------
# Hypothesis settings
# ---------------------------------------------------------------------------

PBT_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.large_base_example,
    ],
)

# A version of the settings profile that allows function-scoped fixtures
# (``tmp_path`` is function-scoped and is reused across hypothesis
# examples in the Cost_Report tests; that reuse is intentional).
PBT_SETTINGS_TMP = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.large_base_example,
        HealthCheck.function_scoped_fixture,
    ],
)


# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# Requirement identifiers like ``R1``, ``R10``, ``R299``. Bounded so the
# resulting set sizes stay small and shrinking is fast.
_R_ID_STRATEGY = st.from_regex(r"^R[1-9]\d{0,2}$", fullmatch=True)

# Aware UTC ``datetime``s with second granularity. Microseconds are
# zeroed so the ``timedelta.days`` floor in ``compute_age_days`` lines up
# with what the test arithmetic expects.
_UTC_DATETIME_STRATEGY = st.datetimes(
    min_value=datetime(2024, 1, 1),
    max_value=datetime(2030, 12, 31),
).map(lambda dt: dt.replace(tzinfo=timezone.utc, microsecond=0))


# ===========================================================================
# Property 23: Plan/Test-Plan freshness check on src/ changes
# Validates: Requirements 11.6
# ===========================================================================


@PBT_SETTINGS
@given(
    now_utc=_UTC_DATETIME_STRATEGY,
    delta_days=st.integers(min_value=0, max_value=200),
    extra_seconds=st.integers(min_value=0, max_value=86_399),
)
def test_property_23_compute_age_days_floors_to_whole_days(
    now_utc: datetime, delta_days: int, extra_seconds: int
) -> None:
    """``compute_age_days`` returns ``floor(delta / 1 day)``.

    Construct ``last_mod = now - timedelta(days=delta_days,
    seconds=extra_seconds)`` so the actual elapsed time is strictly
    greater than ``delta_days`` whole days but strictly less than
    ``delta_days + 1`` whole days. ``timedelta.days`` floors towards
    ``-inf``, which for non-negative deltas is equivalent to the
    floor we want.
    """
    last_mod = now_utc - timedelta(days=delta_days, seconds=extra_seconds)
    assert compute_age_days(now_utc, last_mod) == delta_days


@PBT_SETTINGS
@given(
    now_utc=_UTC_DATETIME_STRATEGY,
    future_seconds=st.integers(min_value=1, max_value=10 * 86_400),
)
def test_property_23_compute_age_days_rejects_future_last_mod(
    now_utc: datetime, future_seconds: int
) -> None:
    """``now < last_mod`` is non-physical; the helper raises."""
    last_mod = now_utc + timedelta(seconds=future_seconds)
    with pytest.raises(ValueError):
        compute_age_days(now_utc, last_mod)


@PBT_SETTINGS
@given(
    now_utc=_UTC_DATETIME_STRATEGY,
    plan_age_days=st.integers(min_value=0, max_value=200),
    test_plan_age_days=st.integers(min_value=0, max_value=200),
    max_days=st.integers(min_value=0, max_value=180),
)
def test_property_23_check_freshness_iff_both_within_max_days(
    now_utc: datetime,
    plan_age_days: int,
    test_plan_age_days: int,
    max_days: int,
) -> None:
    """``check_freshness`` returns OK iff every doc's age is ``<= max_days``.

    Stale docs produce ``EXIT_STALE`` and an error per stale doc that
    names that doc.
    """
    doc_timestamps = {
        TRACKED_DOCS[0]: now_utc - timedelta(days=plan_age_days),
        TRACKED_DOCS[1]: now_utc - timedelta(days=test_plan_age_days),
    }

    exit_code, errors, ages = check_freshness(
        now_utc=now_utc,
        doc_timestamps=doc_timestamps,
        max_days=max_days,
    )

    expected_ok = (
        plan_age_days <= max_days and test_plan_age_days <= max_days
    )
    assert (exit_code == EXIT_OK) is expected_ok
    if expected_ok:
        assert errors == []
    else:
        assert exit_code == EXIT_STALE
        # Every stale doc must contribute an error line that names it.
        if plan_age_days > max_days:
            assert any(TRACKED_DOCS[0] in line for line in errors)
        if test_plan_age_days > max_days:
            assert any(TRACKED_DOCS[1] in line for line in errors)
    # Ages map matches the constructed inputs regardless of pass/fail.
    assert ages == {
        TRACKED_DOCS[0]: plan_age_days,
        TRACKED_DOCS[1]: test_plan_age_days,
    }


def test_property_23_boundary_exactly_90_days_is_ok() -> None:
    """``delta_days == max_days`` is OK; ``> max_days`` is stale.

    The requirement is "older than 90 whole UTC days", so a doc updated
    exactly 90 days ago is within the freshness window.
    """
    now = datetime(2025, 6, 1, tzinfo=timezone.utc)
    on_boundary = {
        TRACKED_DOCS[0]: now - timedelta(days=90),
        TRACKED_DOCS[1]: now - timedelta(days=90),
    }
    exit_code, errors, _ = check_freshness(
        now_utc=now, doc_timestamps=on_boundary, max_days=90,
    )
    assert exit_code == EXIT_OK
    assert errors == []

    just_stale = {
        TRACKED_DOCS[0]: now - timedelta(days=91),
        TRACKED_DOCS[1]: now - timedelta(days=90),
    }
    exit_code, errors, _ = check_freshness(
        now_utc=now, doc_timestamps=just_stale, max_days=90,
    )
    assert exit_code == EXIT_STALE
    assert any(TRACKED_DOCS[0] in line for line in errors)
    # The fresh doc does NOT produce a line.
    assert not any(TRACKED_DOCS[1] in line for line in errors)


# ===========================================================================
# Property 24: Plan/Test-Plan cross-reference completeness
# Validates: Requirements 12.5
# ===========================================================================


def _make_plan(plan_ids: frozenset[str]) -> PlanDocument:
    """Build a :class:`PlanDocument` populated only with R-ids.

    All other fields are filled with neutral values so the parser-level
    invariants are satisfied without affecting the cross-ref check.
    """
    return PlanDocument(
        requirements=tuple(
            RequirementEntry(id=rid, title="", phase="", status="")
            for rid in sorted(plan_ids)
        ),
        phases=(),
        manual_setup=(),
        bedrock_model_id=None,
        polly_voice_id=None,
        rights_confirmed=False,
    )


def _make_test_plan(test_plan_ids: frozenset[str]) -> TestPlanDocument:
    return TestPlanDocument(
        test_types=(),
        requirement_refs=test_plan_ids,
    )


@PBT_SETTINGS
@given(
    plan_ids=st.frozensets(_R_ID_STRATEGY, min_size=0, max_size=20),
    test_plan_ids=st.frozensets(_R_ID_STRATEGY, min_size=0, max_size=20),
)
def test_property_24_xref_ok_iff_plan_subset_of_test_plan(
    plan_ids: frozenset[str], test_plan_ids: frozenset[str]
) -> None:
    """Cross-ref passes iff ``plan_ids - test_plan_ids == set()``."""
    plan = _make_plan(plan_ids)
    test_plan = _make_test_plan(test_plan_ids)

    result = check_xref_documents(plan, test_plan)

    expected_missing = plan_ids - test_plan_ids
    assert result.ok is (len(expected_missing) == 0)
    assert set(result.missing_ids) == expected_missing
    if not result.ok:
        # The summary line names every missing R-id.
        assert len(result.errors) == 1
        for rid in expected_missing:
            assert rid in result.errors[0]


@PBT_SETTINGS
@given(plan_ids=st.frozensets(_R_ID_STRATEGY, min_size=0, max_size=20))
def test_property_24_extras_in_test_plan_are_allowed(
    plan_ids: frozenset[str],
) -> None:
    """Extra ids in TEST_PLAN.md never cause a failure."""
    extras = frozenset({"R900", "R901", "R902"})
    plan = _make_plan(plan_ids)
    test_plan = _make_test_plan(plan_ids | extras)

    result = check_xref_documents(plan, test_plan)

    assert result.ok is True
    assert result.missing_ids == []
    assert result.errors == []


def test_property_24_missing_ids_sorted_numerically() -> None:
    """``R3`` comes before ``R10`` in the missing-ids list."""
    plan_ids = frozenset({"R10", "R3", "R2", "R100"})
    plan = _make_plan(plan_ids)
    test_plan = _make_test_plan(frozenset())  # nothing referenced.

    result = check_xref_documents(plan, test_plan)

    assert result.ok is False
    assert result.missing_ids == ["R2", "R3", "R10", "R100"]


def test_property_24_empty_plan_passes_vacuously() -> None:
    plan = _make_plan(frozenset())
    test_plan = _make_test_plan(frozenset())

    result = check_xref_documents(plan, test_plan)

    assert result.ok is True
    assert result.missing_ids == []


# ===========================================================================
# Property 25: Cost_Report ↔ runtime configuration consistency
# Validates: Requirements 12.6
# ===========================================================================


# Plausible Bedrock / Polly identifiers. Hypothesis samples four ids
# independently per example so the (match, mismatch) cross product is
# fully covered without inventing meaningful combinations by hand.
_MODEL_IDS = (
    "amazon.nova-lite-v1:0",
    "amazon.nova-pro-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
    "meta.llama3-8b-instruct-v1:0",
    "mistral.mistral-7b-instruct-v0:2",
)
_VOICE_IDS = (
    "Joanna",
    "Matthew",
    "Ivy",
    "Salli",
    "Kendra",
    "Amy",
)


def _write_cost_report(
    path: Path, model_id: str, voice_id: str
) -> None:
    """Write a Cost_Report whose Section 5.3 table names ``model_id`` and
    ``voice_id`` in the exact format ``extract_recommended_ids`` expects.
    """
    body = (
        "# Cost Report\n"
        "\n"
        "## 5. Recommendation\n"
        "\n"
        "### 5.3 Recommendation summary\n"
        "\n"
        "| Setting | Value |\n"
        "| --- | --- |\n"
        f"| Default Bedrock model id | `{model_id}` |\n"
        f"| Default Polly voice id | `{voice_id}` |\n"
    )
    path.write_text(body, encoding="utf-8")


@PBT_SETTINGS_TMP
@given(
    report_model=st.sampled_from(_MODEL_IDS),
    runtime_model=st.sampled_from(_MODEL_IDS),
    report_voice=st.sampled_from(_VOICE_IDS),
    runtime_voice=st.sampled_from(_VOICE_IDS),
)
def test_property_25_consistency_iff_both_ids_match(
    report_model: str,
    runtime_model: str,
    report_voice: str,
    runtime_voice: str,
    tmp_path: Path,
) -> None:
    """Cost_Report check passes iff *both* recommended ids match runtime."""
    report_path = tmp_path / "COST_REPORT.md"
    _write_cost_report(report_path, report_model, report_voice)

    result = check_consistency(report_path, runtime_model, runtime_voice)

    expected_ok = (
        report_model == runtime_model and report_voice == runtime_voice
    )
    assert result.ok is expected_ok
    assert result.cost_report_model_id == report_model
    assert result.cost_report_voice_id == report_voice
    if not expected_ok:
        # At least one of the mismatch error lines is present.
        joined = "\n".join(result.errors)
        if report_model != runtime_model:
            assert report_model in joined and runtime_model in joined
        if report_voice != runtime_voice:
            assert report_voice in joined and runtime_voice in joined


def test_property_25_extract_round_trips_recommendation_table() -> None:
    """``extract_recommended_ids`` recovers the values written into the
    Section 5.3 recommendation table verbatim."""
    text = (
        "| Default Bedrock model id | `amazon.nova-lite-v1:0` |\n"
        "| Default Polly voice id | `Joanna` |\n"
    )
    model, voice = extract_recommended_ids(text)
    assert model == "amazon.nova-lite-v1:0"
    assert voice == "Joanna"


def test_property_25_missing_report_blocks_with_path_in_error(
    tmp_path: Path,
) -> None:
    """A non-existent Cost_Report path yields ``ok=False`` and the path
    is named in the error line so operators can locate the missing
    document quickly."""
    missing = tmp_path / "no_such_report.md"

    result = check_consistency(
        missing, "amazon.nova-lite-v1:0", "Joanna",
    )

    assert result.ok is False
    assert result.errors
    assert str(missing) in result.errors[0]


# ===========================================================================
# Property 26: Feature branch name validator
# Validates: Requirements 13.2
# ===========================================================================


_BRANCH_SEGMENT_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"


@PBT_SETTINGS
@given(
    segment=st.text(
        alphabet=_BRANCH_SEGMENT_ALPHABET, min_size=3, max_size=50,
    ),
)
def test_property_26_valid_feature_branch_names_accepted(
    segment: str,
) -> None:
    """Any ``feature/<segment>`` with the documented charset and length
    matches the regex."""
    name = f"feature/{segment}"
    assert is_valid_feature_branch(name) is True
    result = validate_branch(name, allow_main=True)
    assert result.ok is True
    assert result.reason == ""


@PBT_SETTINGS
@given(
    segment=st.one_of(
        st.text(alphabet=_BRANCH_SEGMENT_ALPHABET, min_size=0, max_size=2),
        st.text(alphabet=_BRANCH_SEGMENT_ALPHABET, min_size=51, max_size=100),
    ),
)
def test_property_26_invalid_length_segments_rejected(segment: str) -> None:
    """``feature/<segment>`` with length outside ``[3, 50]`` is rejected."""
    name = f"feature/{segment}"
    assert is_valid_feature_branch(name) is False


@PBT_SETTINGS
@given(
    bad_char=st.sampled_from(
        [" ", "_", ".", "/", "A", "Z", "*", "@", "+", "!", "#"]
    ),
    prefix=st.text(
        alphabet=_BRANCH_SEGMENT_ALPHABET, min_size=1, max_size=24,
    ),
    suffix=st.text(
        alphabet=_BRANCH_SEGMENT_ALPHABET, min_size=1, max_size=24,
    ),
)
def test_property_26_invalid_charset_rejected(
    bad_char: str, prefix: str, suffix: str,
) -> None:
    """Any character outside ``[a-z0-9-]`` after ``feature/`` is rejected."""
    segment = prefix + bad_char + suffix
    # Length still in [3, 50] so the only failure mode is the charset.
    assume(3 <= len(segment) <= 50)
    name = f"feature/{segment}"
    assert is_valid_feature_branch(name) is False


@PBT_SETTINGS
@given(
    prefix=st.sampled_from(
        ["", "fix/", "bugfix/", "release/", "feat/", "feature-", "FEATURE/"]
    ),
    segment=st.text(
        alphabet=_BRANCH_SEGMENT_ALPHABET, min_size=3, max_size=50,
    ),
)
def test_property_26_non_feature_prefix_rejected(
    prefix: str, segment: str,
) -> None:
    """Non-``feature/`` prefixes are rejected by the bare regex check."""
    name = prefix + segment
    # Skip the case where ``prefix`` happens to be exactly ``feature/``;
    # that is the accept case covered by another test.
    assume(not name.startswith("feature/"))
    # And exclude the integration-branch escape hatch -- those go
    # through ``validate_branch`` rather than the bare regex.
    assume(name not in INTEGRATION_BRANCHES)
    assert is_valid_feature_branch(name) is False


def test_property_26_integration_branches_respect_allow_main_flag() -> None:
    """``main``/``master`` are accepted iff ``allow_main=True``."""
    for branch in INTEGRATION_BRANCHES:
        # Bare regex never accepts these.
        assert is_valid_feature_branch(branch) is False

        accept = validate_branch(branch, allow_main=True)
        assert accept.ok is True

        reject = validate_branch(branch, allow_main=False)
        assert reject.ok is False
        assert branch in reject.reason


# ===========================================================================
# Property 27: Each requirement assigned to exactly one phase
# Validates: Requirements 14.2
# ===========================================================================


@st.composite
def _phase_assignment_strategy(
    draw: st.DrawFn,
) -> tuple[set[str], dict[int, set[str]]]:
    """Generate a synthetic phase assignment.

    Each requirement id may be assigned to zero, one, or several phases
    so the property test exercises orphan, ok, and duplicate cases
    uniformly.
    """
    ids = draw(st.sets(_R_ID_STRATEGY, min_size=0, max_size=15))
    phases: dict[int, set[str]] = {1: set(), 2: set(), 3: set()}
    for rid in ids:
        chosen = draw(
            st.lists(
                st.sampled_from(VALID_PHASES),
                min_size=0,
                max_size=3,
                unique=True,
            )
        )
        for p in chosen:
            phases[p].add(rid)
    return ids, phases


@PBT_SETTINGS
@given(assignment=_phase_assignment_strategy())
def test_property_27_assignment_passes_iff_exactly_one_phase_per_req(
    assignment: tuple[set[str], dict[int, set[str]]],
) -> None:
    """Phase-assignment check passes iff every requirement appears in
    exactly one of the three phase sections."""
    requirements, phases = assignment
    plan = PhasesPlanDocument(
        requirements=set(requirements),
        phases=phases,
        phase_headings_present={1, 2, 3},
    )

    result = check_phase_assignment(plan)

    # Reference computation.
    counts: dict[str, list[int]] = {}
    for phase_idx, ids in phases.items():
        for rid in ids:
            counts.setdefault(rid, []).append(phase_idx)
    duplicates = {rid for rid, ps in counts.items() if len(ps) > 1}
    orphans = {rid for rid in requirements if rid not in counts}

    expected_pass = not duplicates and not orphans
    assert result.passed is expected_pass

    if not expected_pass:
        joined = "\n".join(result.errors)
        for rid in duplicates:
            assert rid in joined
        for rid in orphans:
            assert rid in joined


# ===========================================================================
# Property 28: Phase scope rule for deployments
# Validates: Requirements 14.3
# ===========================================================================


@st.composite
def _single_phase_assignment_strategy(
    draw: st.DrawFn,
) -> tuple[set[str], dict[str, int]]:
    """Generate an assignment with each id mapped to exactly one phase."""
    ids = draw(st.sets(_R_ID_STRATEGY, min_size=0, max_size=15))
    mapping: dict[str, int] = {}
    for rid in ids:
        mapping[rid] = draw(st.sampled_from(VALID_PHASES))
    return ids, mapping


@PBT_SETTINGS
@given(
    assignment=_single_phase_assignment_strategy(),
    current_phase=st.sampled_from(VALID_PHASES),
)
def test_property_28_scope_passes_iff_no_req_past_current_phase(
    assignment: tuple[set[str], dict[str, int]],
    current_phase: int,
) -> None:
    """Phase-scope check passes iff every requirement's phase is
    ``<= current_phase``."""
    requirements, mapping = assignment
    phases: dict[int, set[str]] = {1: set(), 2: set(), 3: set()}
    for rid, phase_idx in mapping.items():
        phases[phase_idx].add(rid)
    plan = PhasesPlanDocument(
        requirements=set(requirements),
        phases=phases,
        phase_headings_present={1, 2, 3},
    )

    result = check_phase_scope(plan, current_phase)

    expected_violations = {
        rid for rid, ph in mapping.items() if ph > current_phase
    }
    assert result.passed is (len(expected_violations) == 0)

    if expected_violations:
        joined = "\n".join(result.errors)
        for rid in expected_violations:
            assert rid in joined


def test_property_28_current_phase_3_always_passes() -> None:
    """At ``current_phase=3`` no requirement can be in a later phase."""
    plan = PhasesPlanDocument(
        requirements={"R1", "R2", "R3"},
        phases={1: {"R1"}, 2: {"R2"}, 3: {"R3"}},
        phase_headings_present={1, 2, 3},
    )

    result = check_phase_scope(plan, current_phase=3)

    assert result.passed is True
    assert result.errors == []


# ===========================================================================
# Property 29: Manual-setup completion requires an ISO 8601 date
# Validates: Requirements 15.4, 15.5
# ===========================================================================


# Strict ``YYYY-MM-DD`` strings drawn from a date strategy. The
# ``strftime`` format guarantees zero-padding so 2025-03-05 (not
# 2025-3-5) reaches the validator.
_VALID_ISO_DATE_STRATEGY = st.dates(
    min_value=date(1900, 1, 1), max_value=date(2100, 12, 31),
).map(lambda d: d.strftime("%Y-%m-%d"))

_INVALID_ISO_DATE_STRINGS: tuple[str, ...] = (
    "",
    "2025/03/15",
    "25-03-15",
    "2025-3-15",
    "2025-03-15T00:00:00Z",
    "2025-03-15 00:00:00",
    "Tomorrow",
    "yesterday",
    "2025-13-01",
    "2025-02-30",
    "2024-04-31",
    "2025-00-15",
    "2025-12-32",
)


@PBT_SETTINGS
@given(value=_VALID_ISO_DATE_STRATEGY)
def test_property_29_valid_iso_dates_accepted(value: str) -> None:
    """Every ``YYYY-MM-DD`` calendar date is accepted."""
    assert is_valid_iso_date(value) is True


@pytest.mark.parametrize("value", _INVALID_ISO_DATE_STRINGS)
def test_property_29_invalid_iso_date_strings_rejected(value: str) -> None:
    """Format and calendar violations are rejected."""
    assert is_valid_iso_date(value) is False


@st.composite
def _manual_setup_items_strategy(
    draw: st.DrawFn,
) -> list[ManualSetupItem]:
    """Generate a list of manual-setup items with mixed completion data.

    Each item is independently checked-or-not, and each carries a
    completion date drawn from a mix of valid ISO strings, invalid
    strings, and ``None``.
    """
    size = draw(st.integers(min_value=0, max_value=8))
    items: list[ManualSetupItem] = []
    date_pool = st.one_of(
        st.none(),
        _VALID_ISO_DATE_STRATEGY,
        st.sampled_from(_INVALID_ISO_DATE_STRINGS),
    )
    for index in range(size):
        identifier = draw(
            st.text(
                alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
                min_size=1,
                max_size=10,
            )
        )
        items.append(
            ManualSetupItem(
                identifier=f"setup-{index}-{identifier}",
                label=f"Item {index}",
                checked=draw(st.booleans()),
                completion_date=draw(date_pool),
            )
        )
    return items


@PBT_SETTINGS
@given(items=_manual_setup_items_strategy())
def test_property_29_edits_pass_iff_every_checked_item_has_valid_date(
    items: list[ManualSetupItem],
) -> None:
    """Edit-time validator passes iff every checked item carries an
    ISO 8601 calendar date."""
    result = validate_manual_setup_edits(items)

    expected_ok = all(
        item.completion_date is not None
        and is_valid_iso_date(item.completion_date)
        for item in items
        if item.checked
    )
    assert result.ok is expected_ok

    if not expected_ok:
        joined = "\n".join(result.errors)
        # Every offending checked item is named in the error output.
        for item in items:
            if not item.checked:
                continue
            cd = item.completion_date
            if cd is not None and is_valid_iso_date(cd):
                continue
            assert item.identifier in joined or item.label in joined


@PBT_SETTINGS
@given(items=_manual_setup_items_strategy())
def test_property_29_complete_passes_iff_every_item_checked(
    items: list[ManualSetupItem],
) -> None:
    """Build-start validator passes iff every item is checked."""
    result = validate_manual_setup_complete(items)

    expected_ok = all(item.checked for item in items)
    assert result.ok is expected_ok

    if not expected_ok:
        joined = "\n".join(result.errors)
        for item in items:
            if item.checked:
                continue
            assert item.identifier in joined or item.label in joined


def test_property_29_unchecked_items_ignored_by_edit_validator() -> None:
    """Edit validator only inspects items where ``checked is True``."""
    items = [
        ManualSetupItem(
            identifier="a", label="A", checked=False, completion_date=None,
        ),
        ManualSetupItem(
            identifier="b",
            label="B",
            checked=False,
            completion_date="not-a-date",
        ),
    ]
    result = validate_manual_setup_edits(items)
    assert result.ok is True
    assert result.errors == ()
