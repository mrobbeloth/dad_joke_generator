#!/usr/bin/env python3
"""scripts/check_plan_xref.py — PLAN.md ↔ TEST_PLAN.md cross-reference gate.

Implements Requirement 12.5 (Property 24 in ``design.md``):

    IF any requirement identifier listed in ``docs/PLAN.md`` lacks a
    corresponding test reference in ``docs/TEST_PLAN.md``, or if either
    file is missing or unreadable, THEN the Production_Gate SHALL block
    deployment to the production environment.

The check is a one-way set difference: every requirement ID declared in
``PLAN.md`` MUST appear at least once in ``TEST_PLAN.md``. The reverse
direction is intentionally unconstrained — TEST_PLAN.md may reference
identifiers that are not currently in PLAN.md (e.g. proposed tests that
anticipate a planned requirement).

CLI:
    python scripts/check_plan_xref.py \\
        [--plan docs/PLAN.md] \\
        [--test-plan docs/TEST_PLAN.md]

Exit codes:
    0  cross-reference complete
    1  one or more PLAN.md identifiers missing from TEST_PLAN.md
    2  parse failure (file missing or unreadable)

Library API:
    * :class:`CheckResult` — frozen dataclass with the structured outcome.
    * :func:`check_xref` — parse + compare given filesystem paths.
    * :func:`check_xref_documents` — pure comparison over already-parsed
      :class:`PlanDocument` / :class:`TestPlanDocument` instances.

This module relies on :mod:`scripts.plan_parser` (task 13.1) for all
markdown parsing; it never re-implements that logic.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

# Allow running the script directly as ``python scripts/check_plan_xref.py``
# in addition to ``python -m scripts.check_plan_xref``. When invoked
# directly, Python adds ``scripts/`` (not the repo root) to sys.path, so
# ``from scripts.plan_parser import ...`` would otherwise fail. Inserting
# the repo root makes the package import resolve in both cases without
# requiring a fallback parser.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.plan_parser import (  # noqa: E402  (sys.path manipulation above)
    PlanDocument,
    PlanParseError,
    TestPlanDocument,
    parse_plan,
    parse_test_plan,
)

__all__ = [
    "CheckResult",
    "check_xref",
    "check_xref_documents",
    "main",
    "DEFAULT_PLAN_PATH",
    "DEFAULT_TEST_PLAN_PATH",
    "EXIT_OK",
    "EXIT_MISSING_REFS",
    "EXIT_PARSE_ERROR",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_PLAN_PATH = Path("docs/PLAN.md")
DEFAULT_TEST_PLAN_PATH = Path("docs/TEST_PLAN.md")

EXIT_OK = 0
EXIT_MISSING_REFS = 1
EXIT_PARSE_ERROR = 2

# Used for stable numeric ordering across single- and double-digit R-ids
# (so R2 sorts before R10, not after).
_R_ID_NUMERIC_RE = re.compile(r"^R(\d+)$")


# --------------------------------------------------------------------------- #
# Public dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult:
    """Structured outcome of a cross-reference check.

    Attributes:
        ok: ``True`` iff every PLAN.md identifier is referenced by
            TEST_PLAN.md and both documents parsed successfully.
        errors: Human-readable error lines. Empty when ``ok`` is True.
            On parse failure the list contains a single message naming
            the offending file. On a missing-reference failure the list
            contains a single summary line listing every missing R-id
            in sorted order.
        missing_ids: Sorted list of R-ids declared in PLAN.md but absent
            from TEST_PLAN.md. Sort key is the numeric portion of the
            identifier so ``R2`` precedes ``R10``. Empty on parse
            failure or success.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    missing_ids: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pure comparison helpers
# --------------------------------------------------------------------------- #


def _r_id_sort_key(rid: str) -> tuple[int, str]:
    """Sort key extracting the numeric portion of an ``R<n>`` identifier.

    Falls back to a sentinel large integer for any token that does not
    match ``R\\d+`` so unrecognized values land at the end of the list
    in a deterministic order rather than raising.
    """
    match = _R_ID_NUMERIC_RE.match(rid)
    if match is None:
        return (10**9, rid)
    return (int(match.group(1)), rid)


def check_xref_documents(
    plan: PlanDocument, test_plan: TestPlanDocument,
) -> CheckResult:
    """Compare an already-parsed PLAN/TEST_PLAN pair.

    The check is a strict one-way set difference: returns ``ok=True``
    iff ``{r.id for r in plan.requirements}`` is a subset of
    ``test_plan.requirement_refs``.

    Duplicate IDs in PLAN.md (the same requirement listed under two
    phases) collapse to one set entry — the cross-ref check only
    asserts presence in TEST_PLAN.md.
    """
    plan_ids = {req.id for req in plan.requirements}
    missing = sorted(plan_ids - test_plan.requirement_refs, key=_r_id_sort_key)

    if not missing:
        return CheckResult(ok=True, errors=[], missing_ids=[])

    summary = (
        f"ERROR: {len(missing)} requirement(s) listed in PLAN.md are "
        f"missing from TEST_PLAN.md: {', '.join(missing)}"
    )
    return CheckResult(ok=False, errors=[summary], missing_ids=list(missing))


def check_xref(
    plan_path: Path | str = DEFAULT_PLAN_PATH,
    test_plan_path: Path | str = DEFAULT_TEST_PLAN_PATH,
) -> CheckResult:
    """Parse PLAN.md and TEST_PLAN.md, then compare.

    ``PlanParseError`` from either parse call is captured and surfaced
    as a structured ``CheckResult`` so callers can treat parse failure
    and missing-reference failure with a single uniform error code.
    """
    plan_p = Path(plan_path)
    test_plan_p = Path(test_plan_path)

    try:
        plan = parse_plan(plan_p)
    except PlanParseError as exc:
        return CheckResult(
            ok=False,
            errors=[f"ERROR: cannot read {plan_p}: {exc}"],
            missing_ids=[],
        )

    try:
        test_plan = parse_test_plan(test_plan_p)
    except PlanParseError as exc:
        return CheckResult(
            ok=False,
            errors=[f"ERROR: cannot read {test_plan_p}: {exc}"],
            missing_ids=[],
        )

    return check_xref_documents(plan, test_plan)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_plan_xref.py",
        description=(
            "Verify that every requirement identifier listed in "
            "docs/PLAN.md appears at least once in docs/TEST_PLAN.md. "
            "Implements Requirement 12.5 (Property 24)."
        ),
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=DEFAULT_PLAN_PATH,
        help=(
            "Path to the PLAN.md file. "
            f"Defaults to {DEFAULT_PLAN_PATH}."
        ),
    )
    parser.add_argument(
        "--test-plan",
        type=Path,
        default=DEFAULT_TEST_PLAN_PATH,
        help=(
            "Path to the TEST_PLAN.md file. "
            f"Defaults to {DEFAULT_TEST_PLAN_PATH}."
        ),
    )
    return parser


def _classify_exit_code(result: CheckResult) -> int:
    """Map a :class:`CheckResult` onto the documented exit-code scheme."""
    if result.ok:
        return EXIT_OK
    # Parse failure produces an empty ``missing_ids`` and a single error
    # line whose text begins with "ERROR: cannot read"; missing-reference
    # failure populates ``missing_ids``. Discriminate on ``missing_ids``
    # so the exit-code mapping is independent of error text wording.
    if result.missing_ids:
        return EXIT_MISSING_REFS
    return EXIT_PARSE_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = _build_arg_parser().parse_args(argv)

    result = check_xref(args.plan, args.test_plan)

    if result.ok:
        # ``check_xref_documents`` discarded the count after constructing
        # ``missing_ids``; recompute it from the parsed plan for the
        # success message so operators can confirm coverage at a glance.
        try:
            plan = parse_plan(args.plan)
            count = len({r.id for r in plan.requirements})
        except PlanParseError:
            # Should not happen — check_xref already succeeded — but
            # guard anyway so the success path can never raise.
            count = 0
        print(
            f"cross-reference check passed: {count} requirement(s) in "
            f"PLAN.md, all referenced in TEST_PLAN.md"
        )
        return EXIT_OK

    for line in result.errors:
        print(line, file=sys.stderr)
    return _classify_exit_code(result)


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
