"""Parser for ``docs/PLAN.md`` and ``docs/TEST_PLAN.md``.

This module is the foundation for the governance scripts authored under task
group 13 (freshness, cross-reference, cost-report, phases, manual-setup) and
the Production_Gate orchestrator (task 17.2).  It transforms the two
markdown documents into immutable, typed dataclasses that downstream scripts
can validate without re-implementing markdown parsing.

The parser is intentionally **permissive** about layout because PLAN.md and
TEST_PLAN.md are authored later (tasks 15.1 and 15.2) and the exact table
versus list format has not yet been frozen.  Rather than enforcing a single
syntax here, the parser supports several common shapes so that the documents
can be edited freely without breaking the build.

The validators (tasks 13.3, 13.6, 13.7) are responsible for asserting
*normative* properties such as "exactly three phases", "every requirement
appears in exactly one phase", and "every checked manual-setup item carries
an ISO 8601 completion date".  This module merely surfaces the structure.

Supported PLAN.md shapes
------------------------
Phase sections:

    ## Phase 1 Minimum Viable Product
    ## Phase 2 Hardening and Cost Optimization
    ## Phase 3 Optional Enhancements

Requirement entries within (or outside) a phase section, in any of:

* Markdown table::

      | ID | Title                  | Phase                              | Status      |
      | -- | ---------------------- | ---------------------------------- | ----------- |
      | R5 | IP-Based Rate Limiting | Phase 1 Minimum Viable Product     | In-Progress |

* List item with bracketed annotations::

      - R5: IP-Based Rate Limiting [phase=Phase 1 Minimum Viable Product] [status=In-Progress]

* List item with trailing status (phase inferred from surrounding section)::

      - R5: IP-Based Rate Limiting — In-Progress

* Bare line::

      R5: IP-Based Rate Limiting [status=Completed]

"AWS Manual Setup" section, recognised as any header whose text contains the
phrase "AWS Manual Setup" (case-insensitive).  Items::

    - [ ] Request Bedrock model access
    - [x] Create deployment IAM role (2024-05-15)
    - [x] Configure billing alert — 2024-06-01

Bedrock and Polly selections, anywhere in the document::

    Bedrock model: amazon.nova-lite-v1:0
    bedrock_model_id = amazon.nova-lite-v1:0
    | Bedrock Model ID | amazon.nova-lite-v1:0 |
    Polly voice: Joanna
    polly_voice_id: Joanna

Training_Corpus rights flag (R17.6)::

    training_corpus_rights_confirmed: true
    rights_confirmed = false

Supported TEST_PLAN.md shapes
-----------------------------
Test-type rows in either a markdown table or a list, where the first column /
token names one of {unit, integration, end-to-end, accessibility,
performance}.  The numeric coverage target is the first ``N%`` token if any,
otherwise the first integer in ``[0, 100]`` found in the row.

Requirement references are extracted globally via ``\\bR\\d+\\b`` over the
whole file.

Public API
----------
* :class:`RequirementEntry`, :class:`ManualSetupItem`, :class:`PlanDocument`
* :class:`TestTypeEntry`, :class:`TestPlanDocument`
* :class:`PlanParseError`
* :func:`parse_plan`
* :func:`parse_test_plan`

Both ``parse_plan`` and ``parse_test_plan`` accept either a ``str`` path or a
:class:`pathlib.Path`.  A :class:`PlanParseError` is raised when the file
cannot be read.

CLI
---
Run ``python -m scripts.plan_parser <path> [--json]`` to dump the parsed
structure as JSON.  Useful as a smoke check while iterating on the documents.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

__all__ = [
    "RequirementEntry",
    "ManualSetupItem",
    "PlanDocument",
    "TestTypeEntry",
    "TestPlanDocument",
    "PlanParseError",
    "parse_plan",
    "parse_test_plan",
    "KNOWN_STATUSES",
    "KNOWN_TEST_TYPES",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Status vocabulary defined by R11.1.
KNOWN_STATUSES: tuple[str, ...] = ("Planned", "In-Progress", "Completed", "Deferred")

#: Test-type vocabulary defined by R11.2.
KNOWN_TEST_TYPES: tuple[str, ...] = (
    "unit",
    "integration",
    "end-to-end",
    "accessibility",
    "performance",
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class PlanParseError(Exception):
    """Raised when PLAN.md or TEST_PLAN.md cannot be read.

    The parser is permissive about *shape*: malformed individual entries are
    captured as best-effort or simply skipped.  ``PlanParseError`` is
    reserved for IO-level failures such as a missing file or an unreadable
    path so that downstream gate scripts can distinguish "document missing"
    from "document present but malformed".
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequirementEntry:
    """A single requirement row extracted from PLAN.md.

    Attributes:
        id: Requirement identifier matching ``R[0-9]+`` (e.g. ``"R5"``).
        title: Human-readable requirement title; may be empty if not stated.
        phase: Phase header text the entry was found under, or supplied via
            an explicit ``[phase=...]`` annotation.  May be empty if neither
            source is available.
        status: One of :data:`KNOWN_STATUSES` when recognised, otherwise the
            raw text as authored (or empty).  Validation of status vocabulary
            is left to downstream gate scripts.
    """

    id: str
    title: str
    phase: str
    status: str


@dataclass(frozen=True, slots=True)
class ManualSetupItem:
    """A checkbox row inside the "AWS Manual Setup" section.

    Attributes:
        label: The item description with any trailing date/punctuation
            stripped.
        checked: ``True`` when the markdown checkbox is marked ``[x]`` or
            ``[X]``; ``False`` for ``[ ]``.
        completion_date: ISO 8601 date parsed from the line when present and
            valid; ``None`` otherwise.  A checked item without a valid date
            yields ``None`` so that the manual-setup validator (task 13.7)
            can flag it per R15.4.
    """

    label: str
    checked: bool
    completion_date: date | None


@dataclass(frozen=True, slots=True)
class PlanDocument:
    """Parsed view of ``docs/PLAN.md``.

    Attributes:
        requirements: All :class:`RequirementEntry` rows discovered, in the
            order they appear in the document.  Duplicate IDs are preserved
            so that the phase-uniqueness validator (task 13.6) can detect
            them.
        phases: Ordered, deduplicated list of phase header texts, in the
            order they were first encountered.
        manual_setup: All :class:`ManualSetupItem` entries from the "AWS
            Manual Setup" section, in document order.
        bedrock_model_id: First Bedrock model identifier discovered, or
            ``None`` if the document does not name one.
        polly_voice_id: First Polly voice identifier discovered, or ``None``.
        rights_confirmed: ``True`` when a ``rights_confirmed: true`` (or
            equivalent) flag is present.  Defaults to ``False`` when absent
            so that R17.7 fail-closed behaviour is the default.
    """

    requirements: tuple[RequirementEntry, ...]
    phases: tuple[str, ...]
    manual_setup: tuple[ManualSetupItem, ...]
    bedrock_model_id: str | None
    polly_voice_id: str | None
    rights_confirmed: bool


@dataclass(frozen=True, slots=True)
class TestTypeEntry:
    """One row in the TEST_PLAN.md coverage table.

    Attributes:
        test_type: One of :data:`KNOWN_TEST_TYPES` (always lower-case).
        coverage_target_pct: Numeric target in ``[0, 100]``.  Defaulted to
            ``0`` when the row is present but no number is parseable.
        pass_criterion: Free-text observable pass condition.
    """

    # Tell pytest not to collect this class even though its name starts
    # with ``Test`` — it is a dataclass, not a test case.
    __test__ = False

    test_type: str
    coverage_target_pct: int
    pass_criterion: str


@dataclass(frozen=True, slots=True)
class TestPlanDocument:
    """Parsed view of ``docs/TEST_PLAN.md``.

    Attributes:
        test_types: One :class:`TestTypeEntry` per recognised test type
            present in the document, in the order they appear.
        requirement_refs: Set of every requirement identifier referenced
            anywhere in the document.  Cross-reference completeness against
            PLAN.md is the cross-ref checker's job (task 13.3).
    """

    __test__ = False

    test_types: tuple[TestTypeEntry, ...]
    requirement_refs: frozenset[str]


# ---------------------------------------------------------------------------
# Compiled regular expressions
# ---------------------------------------------------------------------------

# Header patterns ----------------------------------------------------------

# Matches "## Phase 1 Minimum Viable Product" (and any ## level).
_PHASE_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(Phase\s+\d+\b[^\n]*?)\s*$")

# Matches a header whose text contains "AWS Manual Setup" (case-insensitive).
_MANUAL_SETUP_HEADER_RE = re.compile(
    r"^\s*#{1,6}\s+.*aws\s+manual\s+setup.*$",
    re.IGNORECASE,
)

# Generic header used to detect that we have left the phase / manual-setup
# context.  Matches any markdown header.
_GENERIC_HEADER_RE = re.compile(r"^\s*#{1,6}\s+\S")


# Requirement / row patterns -----------------------------------------------

# Single R-id token, used both for cell parsing and for ref extraction.
_R_ID_TOKEN_RE = re.compile(r"^\s*\*{0,2}R(\d+)\*{0,2}\s*$")
_R_ID_FIND_RE = re.compile(r"\bR(\d+)\b")

# Bracketed annotations on a requirement line.
_PHASE_ANNOT_RE = re.compile(r"\[\s*phase\s*=\s*([^\]]+?)\s*\]", re.IGNORECASE)
_STATUS_ANNOT_RE = re.compile(r"\[\s*status\s*=\s*([^\]]+?)\s*\]", re.IGNORECASE)
_TRAILING_STATUS_RE = re.compile(
    r"\s*[\u2014\-:]\s*(Planned|In-Progress|Completed|Deferred)\s*$",
)

# List or bare requirement line.  Supports "- R5: Title", "* R5 - Title",
# "R5: Title", and bold variants like "**R5**: Title".
_REQ_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}R(\d+)\*{0,2}\s*[:.\-\u2014]\s*(.+?)\s*$",
)


# Manual setup item --------------------------------------------------------

_CHECKBOX_RE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<rest>.+?)\s*$")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


# Bedrock / Polly / rights selectors.  Anchored to the start of a line
# (after at most one of `>|-*` and optional bold markers) so that mid-line
# prose mentions in narrative paragraphs do not produce false positives.

_BEDROCK_RE = re.compile(
    r"(?im)^\s*[>|\-*]?\s*\*{0,2}\s*"
    r"bedrock[\s_\-]*model(?:[\s_\-]*id)?"
    r"\s*\*{0,2}\s*[:=|]\s*[`\"']?"
    r"(?P<value>[A-Za-z0-9](?:[A-Za-z0-9._:/\-]*[A-Za-z0-9])?)"
    r"[`\"']?",
)
_POLLY_RE = re.compile(
    r"(?im)^\s*[>|\-*]?\s*\*{0,2}\s*"
    r"polly[\s_\-]*voice(?:[\s_\-]*id)?"
    r"\s*\*{0,2}\s*[:=|]\s*[`\"']?"
    r"(?P<value>[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?)"
    r"[`\"']?",
)
_RIGHTS_RE = re.compile(
    r"(?i)(?:training[_ ]corpus[_ ])?rights[_ ]confirmed"
    r"\s*[:=|]\s*[`\"']?(?P<value>true|false|yes|no)[`\"']?",
)


# Test-plan row ------------------------------------------------------------

_TEST_TYPE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}"
    r"(?P<ttype>unit|integration|end-to-end|accessibility|performance)"
    r"\*{0,2}\s*[:\-\u2014]\s*(?P<rest>.+?)\s*$",
    re.IGNORECASE,
)
_PERCENT_TOKEN_RE = re.compile(r"\b(\d{1,3})\s*%")
_INT_TOKEN_RE = re.compile(r"\b(\d{1,3})\b")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_plan(path: str | Path) -> PlanDocument:
    """Parse ``docs/PLAN.md`` (or any compatible markdown file).

    Args:
        path: Filesystem path to the PLAN.md file.

    Returns:
        A :class:`PlanDocument` capturing all entries discovered.

    Raises:
        PlanParseError: If the file does not exist or cannot be read.
    """

    text = _read_text(path)
    return _parse_plan_text(text)


def parse_test_plan(path: str | Path) -> TestPlanDocument:
    """Parse ``docs/TEST_PLAN.md`` (or any compatible markdown file).

    Args:
        path: Filesystem path to the TEST_PLAN.md file.

    Returns:
        A :class:`TestPlanDocument` listing every recognised test-type row
        and every requirement identifier referenced in the document.

    Raises:
        PlanParseError: If the file does not exist or cannot be read.
    """

    text = _read_text(path)
    return _parse_test_plan_text(text)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_text(path: str | Path) -> str:
    """Read ``path`` as UTF-8, raising :class:`PlanParseError` on IO error."""

    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PlanParseError(f"file not found: {p}") from exc
    except OSError as exc:  # pragma: no cover - rare on test fixtures
        raise PlanParseError(f"cannot read {p}: {exc}") from exc


def _parse_plan_text(text: str) -> PlanDocument:
    """Implementation of :func:`parse_plan` operating on a string."""

    requirements: list[RequirementEntry] = []
    manual_setup: list[ManualSetupItem] = []
    phases: list[str] = []
    seen_phases: set[str] = set()

    current_phase: str | None = None
    in_manual_setup = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # Phase section header.
        m_phase = _PHASE_HEADER_RE.match(line)
        if m_phase:
            current_phase = m_phase.group(1).strip()
            if current_phase not in seen_phases:
                seen_phases.add(current_phase)
                phases.append(current_phase)
            in_manual_setup = False
            continue

        # AWS Manual Setup header.
        if _MANUAL_SETUP_HEADER_RE.match(line):
            current_phase = None
            in_manual_setup = True
            continue

        # Any other header exits both contexts.
        if _GENERIC_HEADER_RE.match(line):
            current_phase = None
            in_manual_setup = False
            continue

        # Inside the manual-setup section, attempt checkbox extraction first.
        if in_manual_setup:
            item = _try_parse_manual_setup(line)
            if item is not None:
                manual_setup.append(item)
                continue
            # Fall through: a non-checkbox line inside the section is ignored.
            continue

        # Otherwise, attempt to parse as a requirement entry.
        req = _try_parse_requirement_line(line, current_phase)
        if req is not None:
            requirements.append(req)

    bedrock_id = _first_match(_BEDROCK_RE, text)
    polly_id = _first_match(_POLLY_RE, text)

    rights_match = _RIGHTS_RE.search(text)
    rights_confirmed = (
        rights_match is not None
        and rights_match.group("value").strip().lower() in {"true", "yes"}
    )

    return PlanDocument(
        requirements=tuple(requirements),
        phases=tuple(phases),
        manual_setup=tuple(manual_setup),
        bedrock_model_id=bedrock_id,
        polly_voice_id=polly_id,
        rights_confirmed=rights_confirmed,
    )


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    """Return the first ``"value"`` named group from ``pattern`` in ``text``."""

    m = pattern.search(text)
    if m is None:
        return None
    return m.group("value").strip()


def _try_parse_manual_setup(line: str) -> ManualSetupItem | None:
    """Parse a checkbox line into a :class:`ManualSetupItem`.

    A checked item with no parseable date yields ``completion_date=None``;
    the manual-setup validator (task 13.7) treats that as the R15.4
    violation case.
    """

    m = _CHECKBOX_RE.match(line)
    if m is None:
        return None

    checked = m.group("mark").lower() == "x"
    rest = m.group("rest")

    completion_date: date | None = None
    label_text = rest

    date_match = _DATE_RE.search(rest)
    if date_match is not None:
        try:
            completion_date = date(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
            )
        except ValueError:
            completion_date = None
        # Strip the date and any surrounding separators / parentheses from
        # the visible label so that downstream consumers do not see e.g.
        # "Configure billing alert (2024-06-01)".
        label_text = (rest[: date_match.start()] + rest[date_match.end() :])
        label_text = label_text.strip(" \t\u2014-:()[],")

    return ManualSetupItem(
        label=label_text.strip(),
        checked=checked,
        completion_date=completion_date,
    )


def _try_parse_requirement_line(
    line: str, current_phase: str | None,
) -> RequirementEntry | None:
    """Parse a single line as a requirement entry, in any supported shape."""

    stripped = line.lstrip()

    # Markdown table row.  The table separator row (e.g. "| --- | --- |") is
    # ignored because its first cell is empty / dashes.
    if stripped.startswith("|"):
        cells = _split_table_row(stripped)
        if cells:
            id_match = _R_ID_TOKEN_RE.match(cells[0])
            if id_match is not None:
                return _build_table_requirement(cells, id_match, current_phase)

    # List / bare line with explicit R-id prefix.
    m_line = _REQ_LINE_RE.match(line)
    if m_line is not None:
        rid = f"R{m_line.group(1)}"
        rest = m_line.group(2)
        return _build_inline_requirement(rid, rest, current_phase)

    return None


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row into trimmed cell texts.

    Returns an empty list when the row is in fact the table separator
    (``| --- | --- |``) so that callers can distinguish it from a real row.
    """

    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = [c.strip() for c in inner.split("|")]
    if not cells:
        return []
    # Detect a separator row: every cell consists only of dashes / colons /
    # whitespace and there is at least one dash among them.
    separator_chars = set(" -:")
    if all(set(c) <= separator_chars for c in cells) and any(
        "-" in c for c in cells
    ):
        return []
    return cells


def _build_table_requirement(
    cells: list[str],
    id_match: re.Match[str],
    current_phase: str | None,
) -> RequirementEntry:
    """Build a :class:`RequirementEntry` from a table row.

    The order of columns is taken to be (id, title, phase, status), but the
    parser is robust to status appearing in any cell after the title.
    """

    rid = f"R{id_match.group(1)}"
    title = cells[1] if len(cells) > 1 else ""
    phase = cells[2] if len(cells) > 2 else ""
    status = cells[3] if len(cells) > 3 else ""

    # If the recognised status appears in a different cell, prefer it.
    if status not in KNOWN_STATUSES:
        for c in cells[2:]:
            if c in KNOWN_STATUSES:
                status = c
                break

    # If the phase cell is itself a known status (i.e. column order swapped),
    # fall back to the surrounding section header for the phase value.
    if phase in KNOWN_STATUSES and status == phase:
        phase = current_phase or ""
    elif not phase:
        phase = current_phase or ""

    return RequirementEntry(
        id=rid,
        title=title.strip(),
        phase=phase.strip(),
        status=status.strip(),
    )


def _build_inline_requirement(
    rid: str, rest: str, current_phase: str | None,
) -> RequirementEntry:
    """Build a :class:`RequirementEntry` from a list / bare line.

    Recognises ``[phase=...]``, ``[status=...]`` annotations and a trailing
    ``— Status`` suffix.  The phase falls back to the surrounding section
    header when no explicit phase annotation is present.
    """

    phase = current_phase or ""
    status = ""
    remainder = rest

    m_phase = _PHASE_ANNOT_RE.search(remainder)
    if m_phase is not None:
        phase = m_phase.group(1).strip()
        remainder = remainder[: m_phase.start()] + remainder[m_phase.end() :]

    m_status = _STATUS_ANNOT_RE.search(remainder)
    if m_status is not None:
        status = m_status.group(1).strip()
        remainder = remainder[: m_status.start()] + remainder[m_status.end() :]

    if not status:
        m_trail = _TRAILING_STATUS_RE.search(remainder)
        if m_trail is not None:
            status = m_trail.group(1)
            remainder = remainder[: m_trail.start()].rstrip()

    title = remainder.strip().rstrip(",;.")
    return RequirementEntry(id=rid, title=title, phase=phase, status=status)


def _parse_test_plan_text(text: str) -> TestPlanDocument:
    """Implementation of :func:`parse_test_plan` operating on a string."""

    test_types: list[TestTypeEntry] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        entry = _try_parse_test_type_line(line)
        if entry is None:
            continue
        if entry.test_type in seen:
            continue
        seen.add(entry.test_type)
        test_types.append(entry)

    requirement_refs = frozenset(f"R{m}" for m in _R_ID_FIND_RE.findall(text))

    return TestPlanDocument(
        test_types=tuple(test_types),
        requirement_refs=requirement_refs,
    )


def _try_parse_test_type_line(line: str) -> TestTypeEntry | None:
    """Parse a single TEST_PLAN.md line into a :class:`TestTypeEntry`."""

    stripped = line.strip()

    # Markdown table row.
    if stripped.startswith("|"):
        cells = _split_table_row(stripped)
        if cells:
            first = cells[0].strip().strip("*`").lower()
            if first in KNOWN_TEST_TYPES:
                target_cell = cells[1] if len(cells) > 1 else ""
                criterion_cell = " | ".join(cells[2:]).strip() if len(cells) > 2 else ""
                return TestTypeEntry(
                    test_type=first,
                    coverage_target_pct=_extract_percent(target_cell),
                    pass_criterion=criterion_cell,
                )
        return None

    # List or bare entry: "- unit: target=80, criterion=..." etc.
    m = _TEST_TYPE_LINE_RE.match(line)
    if m is None:
        return None
    ttype = m.group("ttype").lower()
    rest = m.group("rest")
    target = _extract_percent(rest)

    # Prefer an explicit "criterion=" or "criterion:" tail.
    m_crit = re.search(
        r"(?:criterion|pass(?:[\s_\-]*criterion)?)\s*[:=]\s*(.+)$",
        rest,
        re.IGNORECASE,
    )
    criterion = m_crit.group(1).strip() if m_crit else rest.strip()

    return TestTypeEntry(
        test_type=ttype,
        coverage_target_pct=target,
        pass_criterion=criterion,
    )


def _extract_percent(s: str) -> int:
    """Extract a coverage target in ``[0, 100]`` from a free-form cell.

    Prefers the first ``N%`` token, falling back to the first integer in
    range.  Returns ``0`` if no in-range integer is present.
    """

    m = _PERCENT_TOKEN_RE.search(s)
    if m is not None:
        v = int(m.group(1))
        if 0 <= v <= 100:
            return v
    for m in _INT_TOKEN_RE.finditer(s):
        v = int(m.group(1))
        if 0 <= v <= 100:
            return v
    return 0


# ---------------------------------------------------------------------------
# CLI smoke runner
# ---------------------------------------------------------------------------


def _to_jsonable(obj: object) -> object:
    """Recursively convert dataclasses, dates, frozensets, tuples to JSON."""

    if isinstance(obj, (PlanDocument, TestPlanDocument, RequirementEntry,
                        TestTypeEntry, ManualSetupItem)):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or "-h" in argv or "--help" in argv:
        print(
            "usage: python -m scripts.plan_parser <PLAN-or-TEST_PLAN.md> [--json] "
            "[--test-plan]",
            file=sys.stderr,
        )
        return 2

    path = argv[1]
    flags = set(argv[2:])
    as_json = "--json" in flags
    is_test_plan = "--test-plan" in flags or path.lower().endswith("test_plan.md")

    try:
        if is_test_plan:
            doc: object = parse_test_plan(path)
        else:
            doc = parse_plan(path)
    except PlanParseError as exc:
        print(f"plan_parser: {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(_to_jsonable(doc), indent=2, sort_keys=True))
    else:
        print(repr(doc))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess test
    sys.exit(_main(sys.argv))
