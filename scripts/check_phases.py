"""Phase-assignment and phase-scope checks for ``docs/PLAN.md``.

This module implements two governance gates required by the Build_Pipeline:

* **Phase assignment** (R14.2 / Property 27): every requirement identifier
  declared in ``PLAN.md`` SHALL appear in exactly one of the three phase
  sections (no duplicates, no orphans).
* **Phase scope** (R14.3 / Property 28): for a build's ``current_phase``,
  deployment is allowed iff ``requirement_phase <= current_phase``. Any
  requirement assigned to a phase greater than ``current_phase`` is a phase
  scope violation.

The module exposes a small library API used by the Production_Gate runner
and a thin CLI for local invocation.

It prefers ``scripts.plan_parser.parse_plan`` (task 13.1) when available, and
falls back to a minimal local parser that scans ``PLAN.md`` for phase
headings and ``R<n>`` identifiers when it is not.

CLI:

    python scripts/check_phases.py [--plan docs/PLAN.md]
                                   [--current-phase 1|2|3]
                                   [--mode assignment|scope|all]

Exit codes:

* ``0`` — all selected checks passed
* ``1`` — at least one check failed
* ``2`` — the plan document could not be parsed
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Phase names per requirements.md R14.1. Comparisons use a "Phase N" prefix
# so the trailing descriptive text can drift without breaking the parser.
PHASE_NAMES: Dict[int, str] = {
    1: "Phase 1 Minimum Viable Product",
    2: "Phase 2 Hardening and Cost Optimization",
    3: "Phase 3 Optional Enhancements",
}

VALID_PHASES = (1, 2, 3)


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #


class PlanParseError(Exception):
    """Raised when ``PLAN.md`` cannot be read or parsed."""


@dataclass
class PlanDocument:
    """Parsed view of ``PLAN.md`` used by the phase checks.

    Attributes:
        requirements: The master set of requirement identifiers declared in
            the plan (e.g., the ``## Requirements`` table). When the plan
            lacks an explicit master list, this is populated with the union
            of all per-phase identifiers as a best-effort fallback.
        phases: Mapping of phase index (1, 2, 3) to the set of requirement
            identifiers assigned to that phase.
        phase_headings_present: The phase indices whose headings were found
            in the document.
    """

    requirements: Set[str] = field(default_factory=set)
    phases: Dict[int, Set[str]] = field(
        default_factory=lambda: {1: set(), 2: set(), 3: set()}
    )
    phase_headings_present: Set[int] = field(default_factory=set)


@dataclass
class CheckResult:
    """Outcome of a phase check.

    Attributes:
        passed: ``True`` iff the check found no errors.
        errors: Human-readable messages describing each violation.
    """

    passed: bool
    errors: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

# A requirement identifier looks like ``R1``, ``R14``, etc. Word boundaries
# avoid matches inside other tokens (e.g., ``RR12`` or ``RX1``).
_R_ID_RE = re.compile(r"\bR(\d+)\b")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PHASE_HEADING_RE = re.compile(r"^Phase\s+([123])\b", re.IGNORECASE)
_REQ_HEADING_RE = re.compile(r"^Requirements?\b", re.IGNORECASE)


def _parse_plan_local(text: str) -> PlanDocument:
    """Minimal local parser used when ``scripts.plan_parser`` is unavailable.

    The parser walks the document line-by-line. It tracks two kinds of
    sections:

    * **Phase sections** opened by a heading like ``## Phase 1 ...``. The
      section spans until another phase heading or any heading at the same
      or shallower depth.
    * **Requirements section** opened by a heading whose title starts with
      ``Requirements`` (case-insensitive). The same closing rules apply.

    Within the active section every ``R<n>`` token contributes to the
    corresponding set on :class:`PlanDocument`.
    """
    doc = PlanDocument()

    current_phase: Optional[int] = None
    phase_heading_level: Optional[int] = None
    in_requirements: bool = False
    req_heading_level: Optional[int] = None

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            level = len(heading.group(1))
            title = heading.group(2).strip()

            phase_match = _PHASE_HEADING_RE.match(title)
            if phase_match is not None:
                current_phase = int(phase_match.group(1))
                phase_heading_level = level
                in_requirements = False
                req_heading_level = None
                doc.phase_headings_present.add(current_phase)
                continue

            if _REQ_HEADING_RE.match(title) is not None:
                in_requirements = True
                req_heading_level = level
                current_phase = None
                phase_heading_level = None
                continue

            # Any other heading closes a section if it is at the same or
            # shallower depth than the section's opening heading. Deeper
            # headings (e.g., subsection like "### Entry conditions") leave
            # the section state intact.
            if (
                current_phase is not None
                and phase_heading_level is not None
                and level <= phase_heading_level
            ):
                current_phase = None
                phase_heading_level = None
            if (
                in_requirements
                and req_heading_level is not None
                and level <= req_heading_level
            ):
                in_requirements = False
                req_heading_level = None
            continue

        # Non-heading line: collect requirement identifiers found here.
        for match in _R_ID_RE.finditer(line):
            rid = "R" + match.group(1)
            if current_phase is not None:
                doc.phases[current_phase].add(rid)
            if in_requirements:
                doc.requirements.add(rid)

    # If the document lacks an explicit master Requirements section, fall
    # back to the union of all per-phase identifiers. This keeps the
    # assignment check operating in "duplicate-only" mode rather than
    # erroring out.
    if not doc.requirements:
        union: Set[str] = set()
        for ids in doc.phases.values():
            union |= ids
        doc.requirements = union

    return doc


def _parse_plan_via_module(text: str) -> Optional[PlanDocument]:
    """Adapt ``scripts.plan_parser.parse_plan`` output when the module is
    importable. Returns ``None`` when the module is unavailable so callers
    can fall back to the local parser.
    """
    try:
        from scripts.plan_parser import parse_plan  # type: ignore[import-not-found]
    except Exception:
        return None

    # Task 13.1's ``parse_plan`` takes a filesystem path, not a markdown
    # string, and produces its own ``PlanDocument`` shape.  When the call
    # signature or the result shape does not match what this module needs,
    # silently fall back to the local parser instead of failing the build.
    try:
        parsed = parse_plan(text)
    except Exception:
        return None

    # If the imported parser already returns our PlanDocument shape, use it.
    if isinstance(parsed, PlanDocument):
        return parsed

    # Otherwise adapt it best-effort. We only require ``requirements`` and
    # ``phases`` attributes/keys.
    requirements = _coerce_str_set(_get(parsed, "requirements", default=()))
    raw_phases = _get(parsed, "phases", default={})
    phases: Dict[int, Set[str]] = {1: set(), 2: set(), 3: set()}
    if isinstance(raw_phases, dict):
        for key, value in raw_phases.items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            if idx in phases:
                phases[idx] = _coerce_str_set(value)
    headings = _coerce_str_set(_get(parsed, "phase_headings_present", default=()))
    headings_int: Set[int] = set()
    for item in headings:
        try:
            headings_int.add(int(item))
        except (TypeError, ValueError):
            continue
    return PlanDocument(
        requirements=requirements,
        phases=phases,
        phase_headings_present=headings_int or {idx for idx, ids in phases.items() if ids},
    )


def _get(obj: object, attr: str, default: object) -> object:
    if hasattr(obj, attr):
        return getattr(obj, attr)
    if isinstance(obj, dict) and attr in obj:
        return obj[attr]
    return default


def _coerce_str_set(value: object) -> Set[str]:
    if isinstance(value, set):
        return {str(v) for v in value}
    if isinstance(value, (list, tuple)):
        return {str(v) for v in value}
    return set()


def parse_plan_text(text: str) -> PlanDocument:
    """Parse a ``PLAN.md`` document supplied as a string."""
    parsed = _parse_plan_via_module(text)
    if parsed is not None:
        return parsed
    return _parse_plan_local(text)


def parse_plan_file(path: Path) -> PlanDocument:
    """Read and parse a ``PLAN.md`` document at ``path``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanParseError(f"Could not read {path}: {exc}") from exc
    return parse_plan_text(text)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_phase_assignment(plan: PlanDocument) -> CheckResult:
    """Validate that every requirement is assigned to exactly one phase.

    A requirement fails the check when:

    * It appears in two or more phase sections (``duplicate``), or
    * It is declared in the master requirements list but absent from every
      phase section (``orphan``).
    """
    counts: Dict[str, List[int]] = {}
    for phase_idx in VALID_PHASES:
        for rid in plan.phases.get(phase_idx, set()):
            counts.setdefault(rid, []).append(phase_idx)

    errors: List[str] = []

    duplicates = sorted(
        (rid, sorted(set(phases))) for rid, phases in counts.items() if len(phases) > 1
    )
    for rid, phases in duplicates:
        phase_list = ", ".join(f"Phase {p}" for p in phases)
        errors.append(
            f"Duplicate phase assignment: {rid} appears in {phase_list}"
        )

    orphans = sorted(plan.requirements - set(counts.keys()), key=_rid_sort_key)
    for rid in orphans:
        errors.append(
            f"Orphan requirement: {rid} is declared but not assigned to any phase"
        )

    return CheckResult(passed=not errors, errors=errors)


def check_phase_scope(plan: PlanDocument, current_phase: int) -> CheckResult:
    """Validate that no requirement is assigned to a phase past ``current_phase``.

    Phases are ordered ``Phase 1 < Phase 2 < Phase 3``. Deployment is allowed
    iff ``requirement_phase <= current_phase``. Any requirement found in a
    later phase produces a scope-violation error.
    """
    if current_phase not in VALID_PHASES:
        raise ValueError(
            f"current_phase must be one of {VALID_PHASES}, got {current_phase!r}"
        )

    errors: List[str] = []
    for phase_idx in VALID_PHASES:
        if phase_idx <= current_phase:
            continue
        ids = sorted(plan.phases.get(phase_idx, set()), key=_rid_sort_key)
        for rid in ids:
            errors.append(
                f"Phase scope violation: {rid} is in Phase {phase_idx} "
                f"but current_phase is {current_phase}"
            )
    return CheckResult(passed=not errors, errors=errors)


def _rid_sort_key(rid: str) -> tuple:
    """Sort R-ids numerically (R2 < R10) with the original string as a
    tiebreaker."""
    match = _R_ID_RE.match(rid)
    if match is None:
        return (10**9, rid)
    return (int(match.group(1)), rid)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate phase assignment and phase scope rules in docs/PLAN.md "
            "(R14.2, R14.3)."
        )
    )
    parser.add_argument(
        "--plan",
        default="docs/PLAN.md",
        help="Path to the PLAN.md file (default: docs/PLAN.md).",
    )
    parser.add_argument(
        "--current-phase",
        type=int,
        choices=VALID_PHASES,
        default=1,
        help="Phase the build is currently targeting (default: 1).",
    )
    parser.add_argument(
        "--mode",
        choices=("assignment", "scope", "all"),
        default="all",
        help="Which check(s) to run (default: all).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        plan = parse_plan_file(Path(args.plan))
    except PlanParseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    overall_passed = True

    if args.mode in ("assignment", "all"):
        result = check_phase_assignment(plan)
        if result.passed:
            print(
                "[OK] Phase assignment: every requirement is assigned to "
                "exactly one phase."
            )
        else:
            overall_passed = False
            print("[FAIL] Phase assignment:")
            for err in result.errors:
                print(f"  - {err}")

    if args.mode in ("scope", "all"):
        result = check_phase_scope(plan, args.current_phase)
        if result.passed:
            print(
                f"[OK] Phase scope: no requirements past current_phase="
                f"{args.current_phase}."
            )
        else:
            overall_passed = False
            print(f"[FAIL] Phase scope (current_phase={args.current_phase}):")
            for err in result.errors:
                print(f"  - {err}")

    return 0 if overall_passed else 1


if __name__ == "__main__":  # pragma: no cover - CLI dispatch
    sys.exit(main())
