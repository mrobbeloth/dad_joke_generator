"""Manual-setup completion validator (Requirement 15).

Implements two checks against the "AWS Manual Setup" section of PLAN.md:

1.  ``edit`` mode (R15.4, R15.5): every checked item must record a
    completion date in strict ISO 8601 ``YYYY-MM-DD`` form representing a
    real calendar date. This is the gate for an edit that flips a
    checkbox from ``- [ ]`` to ``- [x]``.
2.  ``build-start`` mode (R15.6): every manual-setup item must be
    checked. The Build_Pipeline calls this on startup and refuses to
    proceed if any item is still incomplete.

Property 29 (design.md) ties this module together: the validator
accepts a "checked" edit *iff* the same edit records a date matching
``^\\d{4}-\\d{2}-\\d{2}$`` and representing a valid calendar date.

The module is intentionally small and stdlib-only. It prefers the
shared parser from task 13.1 (``scripts.plan_parser``) when available
and falls back to a minimal in-file parser otherwise so this script
can be developed and tested independently.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Parser interop
# ---------------------------------------------------------------------------
#
# Task 13.1 owns the canonical PLAN.md parser and exports
# ``ManualSetupItem`` and ``parse_plan``. Until that lands, we provide a
# compatible local definition and a minimal parser so this validator is
# usable on its own. The shape is the contract we rely on:
#
#     ManualSetupItem(identifier: str, label: str, checked: bool,
#                     completion_date: str | None)
#
try:  # pragma: no cover - exercised once task 13.1 lands.
    from scripts.plan_parser import (  # type: ignore[no-redef]
        ManualSetupItem,
        parse_plan,
    )
    # Task 13.1's ``ManualSetupItem`` may not carry an ``identifier`` field
    # (its contract is ``label/checked/completion_date`` only).  This
    # module needs ``identifier`` for error reporting, so when the shape
    # doesn't match we fall back to the local stand-in.
    import dataclasses as _dataclasses

    if not (
        _dataclasses.is_dataclass(ManualSetupItem)
        and {"identifier", "label", "checked", "completion_date"}.issubset(
            {f.name for f in _dataclasses.fields(ManualSetupItem)}
        )
    ):
        raise ImportError(
            "scripts.plan_parser.ManualSetupItem shape mismatch; "
            "using local stand-in."
        )
except Exception:  # ImportError or AttributeError if shape differs.

    @dataclass(frozen=True)
    class ManualSetupItem:  # type: ignore[no-redef]
        """Minimal stand-in matching the planned 13.1 contract."""

        identifier: str
        label: str
        checked: bool
        completion_date: str | None = None

    _HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
    _ITEM_RE = re.compile(
        r"^\s*-\s*\[(?P<box>[ xX])\]\s+(?P<rest>.+?)\s*$"
    )
    _DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
    _ID_RE = re.compile(r"^(?P<id>[A-Za-z][\w-]*)\s*[:\-]\s*(?P<label>.+)$")

    def parse_plan(plan_path: str | Path) -> list[ManualSetupItem]:
        """Parse PLAN.md and return manual-setup items.

        This minimal parser scans the section whose heading text is
        exactly ``AWS Manual Setup`` (case-insensitive). It collects
        every ``- [ ]`` / ``- [x]`` bullet beneath that heading until
        the next heading is reached. Identifier and completion date are
        extracted heuristically; a missing identifier falls back to the
        first whitespace-delimited token of the bullet text.
        """

        text = Path(plan_path).read_text(encoding="utf-8")
        in_section = False
        items: list[ManualSetupItem] = []
        for raw_line in text.splitlines():
            heading = _HEADING_RE.match(raw_line)
            if heading is not None:
                in_section = (
                    heading.group(1).strip().lower() == "aws manual setup"
                )
                continue
            if not in_section:
                continue
            item_match = _ITEM_RE.match(raw_line)
            if item_match is None:
                continue
            checked = item_match.group("box") in ("x", "X")
            rest = item_match.group("rest")
            date_match = _DATE_RE.search(rest)
            completion_date = date_match.group(1) if date_match else None
            label_text = rest
            id_match = _ID_RE.match(rest)
            if id_match is not None:
                identifier = id_match.group("id")
                label_text = id_match.group("label")
            else:
                identifier = rest.split()[0] if rest.split() else ""
            items.append(
                ManualSetupItem(
                    identifier=identifier,
                    label=label_text.strip(),
                    checked=checked,
                    completion_date=completion_date,
                )
            )
        return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a manual-setup validation pass.

    ``ok`` is ``True`` iff ``errors`` is empty. ``errors`` lists one
    human-readable message per failing item so callers can surface every
    problem at once instead of stopping at the first failure.
    """

    ok: bool
    errors: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def passed(cls) -> "CheckResult":
        return cls(ok=True, errors=())

    @classmethod
    def failed(cls, errors: Iterable[str]) -> "CheckResult":
        message_tuple = tuple(errors)
        return cls(ok=not message_tuple, errors=message_tuple)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_iso_date(value: str) -> bool:
    """Return ``True`` iff ``value`` is a strict ``YYYY-MM-DD`` calendar date.

    The check is deliberately stricter than ``datetime.date.fromisoformat``
    on its own. We require:

    * ``value`` is a ``str`` of length exactly 10;
    * matches ``^\\d{4}-\\d{2}-\\d{2}$`` literally; and
    * parses to a real calendar date (rejects e.g. ``2025-13-01``,
      ``2025-02-30``).

    This matches the regex called out in Property 29.
    """

    if not isinstance(value, str):
        return False
    if len(value) != 10:
        return False
    if _ISO_DATE_RE.match(value) is None:
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _describe(item: ManualSetupItem) -> str:
    """Render an item as ``identifier: label`` for error messages."""

    identifier = (item.identifier or "").strip()
    label = (item.label or "").strip()
    if identifier and label:
        return f"{identifier}: {label}"
    return identifier or label or "<unnamed item>"


def validate_manual_setup_edits(
    items: Sequence[ManualSetupItem],
) -> CheckResult:
    """Validate edit-time manual-setup contract (R15.4, R15.5).

    Passes iff *every* checked item carries a ``completion_date`` that
    is a strict ISO 8601 calendar date. Unchecked items are ignored
    here; ``validate_manual_setup_complete`` is the corresponding
    build-start gate.
    """

    errors: list[str] = []
    for item in items:
        if not item.checked:
            continue
        date_value = item.completion_date
        if date_value is None or not is_valid_iso_date(date_value):
            shown = "<missing>" if date_value is None else date_value
            errors.append(
                f"{_describe(item)} is checked but has no valid ISO 8601 "
                f"completion date (got {shown!r}); expected YYYY-MM-DD."
            )
    return CheckResult.failed(errors) if errors else CheckResult.passed()


def validate_manual_setup_complete(
    items: Sequence[ManualSetupItem],
) -> CheckResult:
    """Validate build-start completeness (R15.6).

    Passes iff *every* manual-setup item is checked. The Build_Pipeline
    invokes this and aborts on any failure within 5 seconds of
    startup.
    """

    errors: list[str] = []
    for item in items:
        if not item.checked:
            errors.append(
                f"{_describe(item)} is not marked complete in the "
                "AWS Manual Setup section."
            )
    return CheckResult.failed(errors) if errors else CheckResult.passed()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_manual_setup",
        description=(
            "Validate the AWS Manual Setup section of PLAN.md against "
            "Requirements 15.4-15.6."
        ),
    )
    parser.add_argument(
        "--plan",
        default="docs/PLAN.md",
        help="Path to PLAN.md (default: docs/PLAN.md).",
    )
    parser.add_argument(
        "--mode",
        choices=("build-start", "edit"),
        default="build-start",
        help=(
            "build-start: every item must be checked (R15.6). "
            "edit: every checked item must carry a valid ISO 8601 date "
            "(R15.4, R15.5)."
        ),
    )
    return parser


def _emit(result: CheckResult, *, mode: str) -> int:
    if result.ok:
        print(f"check_manual_setup ({mode}): OK")
        return 0
    print(f"check_manual_setup ({mode}): FAIL", file=sys.stderr)
    for error in result.errors:
        print(f"  - {error}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    items = parse_plan(args.plan)
    if args.mode == "edit":
        result = validate_manual_setup_edits(items)
    else:
        result = validate_manual_setup_complete(items)
    return _emit(result, mode=args.mode)


if __name__ == "__main__":  # pragma: no cover - exercised via CLI
    raise SystemExit(main())
