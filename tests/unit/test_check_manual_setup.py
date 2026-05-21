"""Unit tests for ``scripts.check_manual_setup``.

Validates: Requirements 15.4, 15.5, 15.6 — manual-setup completion edits
must record an ISO 8601 date, and Build_Pipeline startup must fail when
any AWS Manual Setup item is incomplete.
"""

from __future__ import annotations

import pytest

from scripts.check_manual_setup import (
    CheckResult,
    ManualSetupItem,
    is_valid_iso_date,
    validate_manual_setup_complete,
    validate_manual_setup_edits,
)


def _item(
    *,
    identifier: str = "MS-01",
    label: str = "AWS account creation",
    checked: bool = False,
    completion_date: str | None = None,
) -> ManualSetupItem:
    """Build a ``ManualSetupItem`` with sensible defaults for tests."""

    return ManualSetupItem(
        identifier=identifier,
        label=label,
        checked=checked,
        completion_date=completion_date,
    )


# ---------------------------------------------------------------------------
# is_valid_iso_date
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-03-15", True),
        ("25-03-15", False),
        ("2025/03/15", False),
        ("2025-13-01", False),
        ("2025-02-30", False),
        ("", False),
        ("Tomorrow", False),
    ],
)
def test_is_valid_iso_date(value: str, expected: bool) -> None:
    """R15.4: only strict ``YYYY-MM-DD`` calendar dates are accepted."""
    assert is_valid_iso_date(value) is expected


def test_is_valid_iso_date_rejects_non_strings() -> None:
    """Non-string inputs are rejected outright."""
    assert is_valid_iso_date(None) is False  # type: ignore[arg-type]
    assert is_valid_iso_date(20250315) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_manual_setup_edits (R15.4, R15.5)
# ---------------------------------------------------------------------------


def test_edit_mode_accepts_checked_item_with_valid_date() -> None:
    """A checked item with a real ISO 8601 date is accepted."""
    result = validate_manual_setup_edits(
        [_item(checked=True, completion_date="2025-03-15")]
    )
    assert result.ok is True
    assert result.errors == ()


def test_edit_mode_rejects_checked_item_with_invalid_month() -> None:
    """``2025-13-01`` is the right shape but not a real date."""
    result = validate_manual_setup_edits(
        [_item(checked=True, completion_date="2025-13-01")]
    )
    assert result.ok is False
    assert len(result.errors) == 1
    assert "2025-13-01" in result.errors[0]


def test_edit_mode_rejects_checked_item_with_invalid_day() -> None:
    """February has no 30th day; reject the edit."""
    result = validate_manual_setup_edits(
        [_item(checked=True, completion_date="2025-02-30")]
    )
    assert result.ok is False
    assert len(result.errors) == 1
    assert "2025-02-30" in result.errors[0]


def test_edit_mode_rejects_checked_item_without_date() -> None:
    """R15.4: a checked item without any date must be rejected."""
    result = validate_manual_setup_edits(
        [_item(checked=True, completion_date=None)]
    )
    assert result.ok is False
    assert len(result.errors) == 1
    assert "missing" in result.errors[0].lower()


def test_edit_mode_accepts_unchecked_item() -> None:
    """Unchecked items are out of scope for edit-mode validation."""
    result = validate_manual_setup_edits(
        [_item(checked=False, completion_date=None)]
    )
    assert result.ok is True
    assert result.errors == ()


def test_edit_mode_reports_all_failing_items() -> None:
    """Errors accumulate so engineers see every problem in one pass."""
    items = [
        _item(identifier="MS-01", checked=True, completion_date="bogus"),
        _item(identifier="MS-02", checked=True, completion_date=None),
        _item(identifier="MS-03", checked=True, completion_date="2025-03-15"),
    ]
    result = validate_manual_setup_edits(items)
    assert result.ok is False
    assert len(result.errors) == 2
    assert any("MS-01" in error for error in result.errors)
    assert any("MS-02" in error for error in result.errors)


# ---------------------------------------------------------------------------
# validate_manual_setup_complete (R15.6)
# ---------------------------------------------------------------------------


def test_build_start_mode_passes_when_all_items_checked() -> None:
    """R15.6: Build_Pipeline only proceeds if every item is checked."""
    items = [
        _item(identifier="MS-01", checked=True, completion_date="2025-03-15"),
        _item(identifier="MS-02", checked=True, completion_date="2025-03-16"),
    ]
    result = validate_manual_setup_complete(items)
    assert result.ok is True
    assert result.errors == ()


def test_build_start_mode_fails_when_any_item_unchecked() -> None:
    """The error message must identify the unchecked item by label."""
    items = [
        _item(identifier="MS-01", label="AWS account creation", checked=True,
              completion_date="2025-03-15"),
        _item(identifier="MS-02", label="Billing alert configuration",
              checked=False, completion_date=None),
    ]
    result = validate_manual_setup_complete(items)
    assert result.ok is False
    assert len(result.errors) == 1
    assert "MS-02" in result.errors[0]
    assert "Billing alert configuration" in result.errors[0]


def test_build_start_mode_fails_for_every_unchecked_item() -> None:
    """All unchecked items appear in the error list, not just the first."""
    items = [
        _item(identifier="MS-01", checked=False),
        _item(identifier="MS-02", checked=True, completion_date="2025-03-15"),
        _item(identifier="MS-03", checked=False),
    ]
    result = validate_manual_setup_complete(items)
    assert result.ok is False
    assert len(result.errors) == 2
    assert any("MS-01" in error for error in result.errors)
    assert any("MS-03" in error for error in result.errors)


def test_check_result_failed_with_no_errors_is_ok() -> None:
    """An empty error iterable still represents a passing result."""
    result = CheckResult.failed([])
    assert result.ok is True
    assert result.errors == ()
