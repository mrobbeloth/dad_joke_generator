#!/usr/bin/env python3
"""Feature-branch name validator.

Implements Requirement 13.2: every Feature_Branch name must match the
regex ``^feature/[a-z0-9-]{3,50}$``. The short-description segment after
``feature/`` may only contain lowercase ASCII letters, digits, and
hyphens, and must be between 3 and 50 characters in length, inclusive.

Special case: ``main`` (the integration branch) is accepted by default
so this validator can run on protected branches without false-failing
in CI. ``master`` is also accepted under the same flag for repos that
still use that name. Disable with ``--no-allow-main`` to enforce the
``feature/...`` pattern strictly (useful when running on PR head refs).

CLI
---
    python scripts/check_branch_name.py [--branch <name>]
                                        [--allow-main | --no-allow-main]

When ``--branch`` is omitted, the script reads the branch name from the
``GITHUB_REF_NAME`` environment variable (set by GitHub Actions), and
falls back to ``git rev-parse --abbrev-ref HEAD`` if that variable is
not set or empty.

Exit codes
----------
    0   Branch name is valid.
    1   Branch name is invalid (does not match the rule).
    2   No branch could be resolved (e.g., ``git`` is not on PATH or
        the working directory is not a git checkout) and no explicit
        ``--branch`` was provided.

This module uses only the Python standard library so it can run inside
any CI environment that has a Python 3.12 interpreter without any
``pip install`` step.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "FEATURE_BRANCH_PATTERN",
    "FEATURE_BRANCH_REGEX",
    "INTEGRATION_BRANCHES",
    "CheckResult",
    "is_valid_feature_branch",
    "validate_branch",
    "main",
]

# Anchored on both sides so trailing characters are rejected. The {3,50}
# quantifier applies only to the segment after ``feature/``.
FEATURE_BRANCH_PATTERN: str = r"^feature/[a-z0-9-]{3,50}$"
FEATURE_BRANCH_REGEX: re.Pattern[str] = re.compile(FEATURE_BRANCH_PATTERN)

# Branches that the validator accepts under ``allow_main=True`` so this
# script can run on protected integration branches without failing.
INTEGRATION_BRANCHES: frozenset[str] = frozenset({"main", "master"})


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a branch-name validation.

    Attributes
    ----------
    branch:
        The branch name that was checked, exactly as supplied.
    ok:
        ``True`` iff the branch name was accepted under the rule and
        the supplied ``allow_main`` setting.
    reason:
        Short human-readable explanation. Empty when ``ok`` is
        ``True``; otherwise describes why the name was rejected.
    """

    branch: str
    ok: bool
    reason: str = ""


def is_valid_feature_branch(name: str) -> bool:
    """Return ``True`` iff *name* matches ``^feature/[a-z0-9-]{3,50}$``.

    This is a pure regex check. It does *not* accept ``main`` or
    ``master``; use :func:`validate_branch` for the full policy that
    includes the integration-branch escape hatch.
    """
    if not isinstance(name, str):
        return False
    return FEATURE_BRANCH_REGEX.fullmatch(name) is not None


def validate_branch(name: str, *, allow_main: bool = True) -> CheckResult:
    """Validate a branch name under Requirement 13.2.

    Parameters
    ----------
    name:
        Branch name to check.
    allow_main:
        When ``True`` (the default), ``main`` and ``master`` are also
        accepted so this validator can run on protected integration
        branches in CI without producing a false-negative.
    """
    if not isinstance(name, str) or name == "":
        return CheckResult(branch=name if isinstance(name, str) else "", ok=False,
                           reason="branch name is empty")

    if name in INTEGRATION_BRANCHES:
        if allow_main:
            return CheckResult(branch=name, ok=True, reason="")
        return CheckResult(
            branch=name,
            ok=False,
            reason=(
                f"{name!r} is the integration branch; rerun with "
                "--allow-main to permit it"
            ),
        )

    if is_valid_feature_branch(name):
        return CheckResult(branch=name, ok=True, reason="")

    return CheckResult(
        branch=name,
        ok=False,
        reason=(
            f"branch name {name!r} does not match {FEATURE_BRANCH_PATTERN}"
        ),
    )


def _resolve_branch_from_env_or_git() -> str | None:
    """Return the branch name to validate, or ``None`` if unresolvable.

    Resolution order:
    1. ``GITHUB_REF_NAME`` environment variable (set by GitHub Actions
       on push and pull_request events).
    2. ``git rev-parse --abbrev-ref HEAD`` in the current working
       directory.

    A detached-HEAD result (``HEAD``) is treated as unresolvable so
    callers fall through to exit code 2 rather than silently rejecting
    a branch named ``HEAD``.
    """
    ref_name = os.environ.get("GITHUB_REF_NAME", "").strip()
    if ref_name:
        return ref_name

    if shutil.which("git") is None:
        return None

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    branch = completed.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_branch_name.py",
        description=(
            "Validate that the current Git branch name matches the "
            "Feature_Branch rule from Requirement 13.2 "
            f"({FEATURE_BRANCH_PATTERN}). The integration branch "
            "'main' is accepted by default so this check can run on "
            "protected branches without false-failing."
        ),
    )
    parser.add_argument(
        "--branch",
        dest="branch",
        default=None,
        help=(
            "Explicit branch name to validate. When omitted, the script "
            "reads GITHUB_REF_NAME and then falls back to "
            "'git rev-parse --abbrev-ref HEAD'."
        ),
    )
    parser.add_argument(
        "--allow-main",
        dest="allow_main",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Accept the integration branches 'main' and 'master' "
            "(default: enabled). Pass --no-allow-main to enforce the "
            "feature/<short-description> pattern strictly."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = _build_argument_parser()
    args = parser.parse_args(argv)

    branch = args.branch
    if branch is None:
        branch = _resolve_branch_from_env_or_git()
        if branch is None:
            print(
                "check_branch_name: ERROR: could not resolve a branch name. "
                "Pass --branch <name>, set GITHUB_REF_NAME, or run inside a "
                "git checkout where 'git rev-parse --abbrev-ref HEAD' "
                "succeeds.",
                file=sys.stderr,
            )
            return 2

    result = validate_branch(branch, allow_main=args.allow_main)
    if result.ok:
        print(f"check_branch_name: OK: {result.branch!r} is a valid branch name")
        return 0

    print(
        f"check_branch_name: FAIL: {result.reason}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
