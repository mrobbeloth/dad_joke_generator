#!/usr/bin/env python3
"""scripts/check_doc_freshness.py

Doc freshness gate for Build_Pipeline (Requirement R11.6).

When a pull request modifies any file under ``src/``, both
``docs/PLAN.md`` and ``docs/TEST_PLAN.md`` must have been touched within
the last 90 whole UTC days. If either document is stale, the build is
terminated with a non-zero exit status and an error indicating which
document is overdue for review.

CLI:
    python scripts/check_doc_freshness.py [--repo .] [--now-utc ISO8601]
                                          [--max-days 90]

The ``--now-utc`` and ``--max-days`` flags exist for testability;
production CI runs use the defaults.

Behavior:
  1. Detect whether the PR touches ``src/``. The newline-separated env
     var ``CHANGED_FILES`` is preferred (set by an upstream CI step);
     otherwise ``git diff --name-only origin/main...HEAD`` is consulted.
  2. If ``src/`` is not touched: print a skip message and exit 0.
  3. If ``src/`` is touched: read the last-commit ISO 8601 timestamps
     for ``docs/PLAN.md`` and ``docs/TEST_PLAN.md`` via ``git log``.
  4. Compute floor((now_utc - last_mod_utc) / 1 day) for each.
  5. If either exceeds ``--max-days`` (default 90): print an error per
     stale document and exit 1.
  6. Otherwise print the per-document ages and exit 0.

Exit codes:
  0  Skipped, or both documents fresh.
  1  Freshness violation: at least one document is overdue (R11.6).
  2  Tooling error: ``git`` not on PATH, no git history for a doc, an
     invalid CLI argument, or another subprocess failure. Distinct from
     a freshness violation so CI can react accordingly.

The script is also importable as a library: ``compute_age_days``,
``src_touched``, ``parse_iso8601``, and ``check_freshness`` can be
driven directly without spawning git.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

EXIT_OK = 0
EXIT_STALE = 1
EXIT_TOOLING = 2

# Documents whose freshness we enforce. Order is preserved in error output.
TRACKED_DOCS: tuple[str, ...] = ("docs/PLAN.md", "docs/TEST_PLAN.md")
DEFAULT_MAX_DAYS = 90


# --------------------------------------------------------------------------- #
# Pure helpers (importable; no subprocess, no I/O)
# --------------------------------------------------------------------------- #

def compute_age_days(now_utc: datetime, last_mod_utc: datetime) -> int:
    """Return ``floor((now_utc - last_mod_utc) / 1 day)`` as an integer.

    Both inputs must be timezone-aware UTC datetimes. ``ValueError`` is
    raised if either input is naive or if ``now_utc`` precedes
    ``last_mod_utc`` (a negative age is non-physical for this check).
    """
    if now_utc.tzinfo is None or last_mod_utc.tzinfo is None:
        raise ValueError(
            "compute_age_days requires timezone-aware datetimes"
        )
    if now_utc < last_mod_utc:
        raise ValueError(
            f"now_utc ({now_utc.isoformat()}) precedes last_mod_utc "
            f"({last_mod_utc.isoformat()})"
        )
    delta = now_utc - last_mod_utc
    # ``timedelta.days`` is the floored whole-day count for non-negative
    # deltas, which is exactly the floor() semantics required by R11.6.
    return delta.days


def src_touched(changed_files: Iterable[str]) -> bool:
    """Return ``True`` iff any path is under ``src/`` (after normalization).

    Backslashes are normalized to forward slashes so paths emitted by
    Windows tooling are recognized. Empty/whitespace entries are ignored.
    """
    for raw in changed_files:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        if path == "src" or path.startswith("src/"):
            return True
    return False


def parse_iso8601(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC datetime.

    Accepts a trailing ``Z`` as well as numeric offsets like ``+00:00``.
    Naive timestamps (no offset) are rejected because R11.5/R11.6 both
    require UTC.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty ISO 8601 timestamp")
    # ``datetime.fromisoformat`` on Python 3.12 accepts a trailing 'Z',
    # but normalize it explicitly so the behavior is unambiguous.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone offset: {value!r}")
    return dt.astimezone(timezone.utc)


def check_freshness(
    *,
    now_utc: datetime,
    doc_timestamps: "dict[str, datetime]",
    max_days: int = DEFAULT_MAX_DAYS,
) -> "tuple[int, list[str], dict[str, int]]":
    """Evaluate freshness given pre-fetched timestamps.

    Returns ``(exit_code, error_messages, ages_by_doc)``. The exit code
    is ``EXIT_OK`` (0) when every document's age is ``<= max_days``;
    otherwise ``EXIT_STALE`` (1). Errors enumerate each stale document
    in the order given by ``doc_timestamps``.
    """
    ages: dict[str, int] = {}
    errors: list[str] = []
    for doc, last_mod in doc_timestamps.items():
        age = compute_age_days(now_utc, last_mod)
        ages[doc] = age
        if age > max_days:
            errors.append(
                f"ERROR: {doc} is {age} days old (max {max_days})"
            )
    return (EXIT_STALE if errors else EXIT_OK, errors, ages)


# --------------------------------------------------------------------------- #
# I/O layer (subprocess + env)
# --------------------------------------------------------------------------- #

def _run_git(args: Sequence[str], repo: Path) -> str:
    """Run a git command in ``repo`` and return stripped stdout.

    Raises ``RuntimeError`` if git is not on PATH, the repo is invalid,
    or git returns a non-zero exit status.
    """
    if shutil.which("git") is None:
        raise RuntimeError("git executable not found on PATH")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:  # PATH race after shutil.which
        raise RuntimeError(
            "git executable not found on PATH"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {exc.returncode}): "
            f"{stderr}"
        ) from exc
    return result.stdout.strip()


def _changed_files_from_env() -> "list[str] | None":
    """Read newline-separated changed paths from ``CHANGED_FILES``.

    Returns ``None`` when the variable is unset so the caller can fall
    back to git. An explicitly empty value yields an empty list (the PR
    touched nothing under inspection).
    """
    raw = os.environ.get("CHANGED_FILES")
    if raw is None:
        return None
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _changed_files_from_git(repo: Path) -> "list[str]":
    """Compute changed files vs. the ``origin/main`` merge-base."""
    out = _run_git(
        ["diff", "--name-only", "origin/main...HEAD"],
        repo=repo,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _last_commit_iso(repo: Path, path: str) -> datetime:
    """Return the last commit timestamp for ``path`` as a UTC datetime."""
    out = _run_git(
        ["log", "-1", "--format=%cI", "--", path],
        repo=repo,
    )
    if not out:
        raise RuntimeError(
            f"no git history for {path!r}; cannot determine "
            "last-modified date"
        )
    return parse_iso8601(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_doc_freshness",
        description=(
            "Fail when a PR touches src/ and either docs/PLAN.md or "
            "docs/TEST_PLAN.md has not been updated within the last "
            "90 whole UTC days (Requirement R11.6)."
        ),
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory).",
    )
    parser.add_argument(
        "--now-utc",
        default=None,
        help=(
            "Override current UTC time as ISO 8601 (e.g. "
            "2025-01-15T00:00:00Z). Defaults to datetime.now(UTC)."
        ),
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=DEFAULT_MAX_DAYS,
        help="Maximum allowed whole-day age (default: 90).",
    )
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()

    if args.now_utc:
        try:
            now_utc = parse_iso8601(args.now_utc)
        except ValueError as exc:
            print(f"ERROR: invalid --now-utc: {exc}", file=sys.stderr)
            return EXIT_TOOLING
    else:
        now_utc = datetime.now(timezone.utc)

    if args.max_days < 0:
        print(
            "ERROR: --max-days must be non-negative", file=sys.stderr
        )
        return EXIT_TOOLING

    # 1. Detect changed files: CHANGED_FILES env var wins; else git diff.
    changed = _changed_files_from_env()
    if changed is None:
        try:
            changed = _changed_files_from_git(repo)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_TOOLING

    # 2. Skip when src/ is untouched.
    if not src_touched(changed):
        print("freshness check skipped (no src/ changes)")
        return EXIT_OK

    # 3. Read last-modified timestamps for each tracked doc.
    doc_timestamps: dict[str, datetime] = {}
    for doc in TRACKED_DOCS:
        try:
            doc_timestamps[doc] = _last_commit_iso(repo, doc)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_TOOLING

    # 4-5. Compute ages and decide.
    try:
        exit_code, errors, ages = check_freshness(
            now_utc=now_utc,
            doc_timestamps=doc_timestamps,
            max_days=args.max_days,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_TOOLING

    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return exit_code

    # 6. Success: report per-document ages on stdout.
    pretty = ", ".join(
        f"{Path(doc).name}={ages[doc]}d" for doc in TRACKED_DOCS
    )
    print(f"freshness check passed: {pretty}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
