#!/usr/bin/env python3
"""scripts/check_cost_report.py — Cost_Report ↔ runtime consistency check.

Validates Requirement 12.6 (Property 25 in ``design.md``):

    IF the Cost_Report is missing, or if the Cost_Report's recorded model
    selection or voice selection does not match the configured runtime
    values, THEN the Production_Gate SHALL block deployment to the
    production environment.

The Cost_Report lives at ``docs/COST_REPORT.md`` and records the
recommended Bedrock model id and Polly voice id under Section 5
("Recommendation"). The runtime configuration values come from SSM
Parameter Store at deploy time (``/dadjokes/bedrock_model_id`` and
``/dadjokes/polly_voice_id``); for CI they are supplied via CLI
arguments or the ``RUNTIME_BEDROCK_MODEL_ID`` and
``RUNTIME_POLLY_VOICE_ID`` environment variables.

CLI:
    python scripts/check_cost_report.py \\
        [--cost-report docs/COST_REPORT.md] \\
        [--runtime-model-id <id>] \\
        [--runtime-voice-id <id>]

Exit codes:
    0  consistent — both ids in the report match the runtime values
    1  blocked   — report missing/unreadable, recommendation missing,
                   or runtime values mismatch the report
    2  usage     — runtime ids could not be resolved from CLI/env vars

Standard library only: ``argparse``, ``os``, ``re``, ``sys``, ``pathlib``,
``dataclasses``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_COST_REPORT_PATH = Path("docs/COST_REPORT.md")

# Recommendation-summary table rows authored in Section 5.3 of the
# Cost_Report. Permissive regex: case-insensitive, tolerant of arbitrary
# spacing and the ``Default`` qualifier.
_TABLE_MODEL_RE = re.compile(
    r"\|\s*(?:Default\s+)?Bedrock\s+model\s+id\s*\|\s*`([^`|]+)`\s*\|",
    re.IGNORECASE,
)
_TABLE_VOICE_RE = re.compile(
    r"\|\s*(?:Default\s+)?Polly\s+voice\s+id\s*\|\s*`([^`|]+)`\s*\|",
    re.IGNORECASE,
)

# Fallback: the bolded inline-code recommendation under Section 5.1 / 5.2
# headings, e.g. ``**`amazon.nova-lite-v1:0`**``. We anchor on the
# heading text so we do not pick up unrelated bold-code spans elsewhere
# in the document.
_BOLD_MODEL_RE = re.compile(
    r"Recommended\s+Bedrock\s+model[^\n]*\n.*?\*\*`([^`]+)`\*\*",
    re.IGNORECASE | re.DOTALL,
)
_BOLD_VOICE_RE = re.compile(
    r"Recommended\s+Polly\s+voice[^\n]*\n.*?\*\*`([^`]+)`\*\*",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class CheckResult:
    """Structured outcome of a Cost_Report consistency check."""

    ok: bool = False
    errors: list[str] = field(default_factory=list)
    cost_report_model_id: str | None = None
    cost_report_voice_id: str | None = None


def extract_recommended_ids(
    cost_report_text: str,
) -> tuple[str | None, str | None]:
    """Parse the Cost_Report markdown to extract recommended ids.

    Strategy:
      1. Prefer the Section 5.3 "Recommendation summary" table. Rows of
         the form ``| Default Bedrock model id | `<value>` |`` and
         ``| Default Polly voice id | `<value>` |`` are unambiguous and
         author-controlled.
      2. Fall back to the bolded inline-code recommendation immediately
         after the Section 5.1 / 5.2 headings.

    Returns ``(model_id, voice_id)`` with each element ``None`` when no
    pattern matched. Whitespace is stripped from matched values.
    """
    model_id: str | None = None
    voice_id: str | None = None

    table_model = _TABLE_MODEL_RE.search(cost_report_text)
    if table_model is not None:
        model_id = table_model.group(1).strip()

    table_voice = _TABLE_VOICE_RE.search(cost_report_text)
    if table_voice is not None:
        voice_id = table_voice.group(1).strip()

    if model_id is None:
        bold_model = _BOLD_MODEL_RE.search(cost_report_text)
        if bold_model is not None:
            model_id = bold_model.group(1).strip()

    if voice_id is None:
        bold_voice = _BOLD_VOICE_RE.search(cost_report_text)
        if bold_voice is not None:
            voice_id = bold_voice.group(1).strip()

    # Empty strings are equivalent to "not found" for downstream
    # comparison; collapsing here keeps callers simple.
    if model_id == "":
        model_id = None
    if voice_id == "":
        voice_id = None

    return model_id, voice_id


def check_consistency(
    cost_report_path: Path,
    runtime_model_id: str,
    runtime_voice_id: str,
) -> CheckResult:
    """Verify the Cost_Report's recommendation matches runtime config.

    The function never raises for I/O errors; failure modes are reported
    as ``CheckResult.errors`` so the caller can render a single uniform
    blocking message.
    """
    result = CheckResult(ok=False)

    if not cost_report_path.exists():
        result.errors.append(
            f"ERROR: cost report not found at {cost_report_path} "
            "(R12.6: Production_Gate blocks deployment)"
        )
        return result

    try:
        text = cost_report_path.read_text(encoding="utf-8")
    except OSError as exc:
        result.errors.append(
            f"ERROR: cost report at {cost_report_path} unreadable: {exc}"
        )
        return result

    model_id, voice_id = extract_recommended_ids(text)
    result.cost_report_model_id = model_id
    result.cost_report_voice_id = voice_id

    if model_id is None:
        result.errors.append(
            "ERROR: cost report missing recommended Bedrock model id "
            "(no Section 5.3 recommendation-summary row and no "
            "**`<id>`** marker under Section 5.1)"
        )
    elif model_id != runtime_model_id:
        result.errors.append(
            f"ERROR: cost_report model={model_id}, "
            f"runtime={runtime_model_id}"
        )

    if voice_id is None:
        result.errors.append(
            "ERROR: cost report missing recommended Polly voice id "
            "(no Section 5.3 recommendation-summary row and no "
            "**`<id>`** marker under Section 5.2)"
        )
    elif voice_id != runtime_voice_id:
        result.errors.append(
            f"ERROR: cost_report voice={voice_id}, "
            f"runtime={runtime_voice_id}"
        )

    result.ok = not result.errors
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_cost_report.py",
        description=(
            "Verify that the Cost_Report (docs/COST_REPORT.md) "
            "recommended Bedrock model id and Polly voice id match the "
            "configured runtime values. Implements Requirement 12.6."
        ),
    )
    parser.add_argument(
        "--cost-report",
        type=Path,
        default=DEFAULT_COST_REPORT_PATH,
        help=(
            "Path to the Cost_Report markdown file. "
            f"Defaults to {DEFAULT_COST_REPORT_PATH}."
        ),
    )
    parser.add_argument(
        "--runtime-model-id",
        default=None,
        help=(
            "Runtime Bedrock model id. Falls back to the "
            "RUNTIME_BEDROCK_MODEL_ID environment variable."
        ),
    )
    parser.add_argument(
        "--runtime-voice-id",
        default=None,
        help=(
            "Runtime Polly voice id. Falls back to the "
            "RUNTIME_POLLY_VOICE_ID environment variable."
        ),
    )
    return parser


def _resolve_runtime_ids(
    args: argparse.Namespace,
    env: dict[str, str],
) -> tuple[str | None, str | None, list[str]]:
    """Resolve runtime ids from CLI args (preferred) or env vars."""
    errors: list[str] = []

    runtime_model_id = args.runtime_model_id or env.get(
        "RUNTIME_BEDROCK_MODEL_ID"
    )
    runtime_voice_id = args.runtime_voice_id or env.get(
        "RUNTIME_POLLY_VOICE_ID"
    )

    if not runtime_model_id:
        errors.append(
            "runtime Bedrock model id not provided; pass "
            "--runtime-model-id or set RUNTIME_BEDROCK_MODEL_ID"
        )
    if not runtime_voice_id:
        errors.append(
            "runtime Polly voice id not provided; pass "
            "--runtime-voice-id or set RUNTIME_POLLY_VOICE_ID"
        )

    return runtime_model_id, runtime_voice_id, errors


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    runtime_model_id, runtime_voice_id, usage_errors = _resolve_runtime_ids(
        args, dict(os.environ)
    )
    if usage_errors:
        for err in usage_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 2

    # mypy: usage_errors guard ensures both ids are non-None below
    assert runtime_model_id is not None
    assert runtime_voice_id is not None

    result = check_consistency(
        args.cost_report, runtime_model_id, runtime_voice_id
    )

    if result.ok:
        print(
            "cost_report consistency check passed: "
            f"model={result.cost_report_model_id}, "
            f"voice={result.cost_report_voice_id}"
        )
        return 0

    for err in result.errors:
        print(err, file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
