"""Unit tests for the feature-branch name validator.

Validates: Requirement 13.2 — Feature_Branch names must follow
``feature/<short-description>`` where ``<short-description>`` consists
only of lowercase letters, digits, and hyphens and is between 3 and 50
characters in length, inclusive. Documents the integration-branch
escape hatch for ``main`` so the validator can run on protected
branches without false-failing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# The validator lives in scripts/, which is intentionally not on the
# package path (it is a CI utility, not part of the joke_api package).
# Load it via importlib so the test does not depend on PYTHONPATH tweaks.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_branch_name.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_branch_name", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_branch_name = _load_module()
is_valid_feature_branch = check_branch_name.is_valid_feature_branch
validate_branch = check_branch_name.validate_branch
CheckResult = check_branch_name.CheckResult
main = check_branch_name.main


# ---------------------------------------------------------------------------
# is_valid_feature_branch — pure regex contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "feature/abc",                                        # 3 chars (lower bound)
        "feature/auth",
        "feature/joke-api",
        "feature/12-rate-limit",
        "feature/" + "a" * 50,                                 # 50 chars (upper bound)
        "feature/" + "a" + "-" * 48 + "z",                     # mixed hyphens, 50 chars
        "feature/" + "0" * 3,                                  # all digits, lower bound
        "feature/0123456789",                                  # all digits
    ],
)
def test_is_valid_feature_branch_accepts_well_formed_names(name: str) -> None:
    assert is_valid_feature_branch(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",                                                    # empty
        "feature",                                             # no slash
        "feature/",                                            # empty short-description
        "feature/ab",                                          # 2 chars (just below 3)
        "feature/" + "a" * 51,                                 # 51 chars (just over 50)
        "Feature/Auth",                                        # uppercase prefix + name
        "feature/AUTH",                                        # uppercase short-description
        "feature/Auth",                                        # mixed case
        "feature/auth_module",                                 # underscore not allowed
        "feature/auth.module",                                 # period not allowed
        "feature/auth space",                                  # space not allowed
        "feature/auth/extra",                                  # extra path segment
        "feature/auth\n",                                      # trailing newline
        " feature/auth",                                       # leading space
        "feature/auth ",                                       # trailing space
        "bugfix/foo",                                          # wrong prefix
        "release/1.0.0",                                       # wrong prefix
        "main",                                                # not a feature branch
        "master",                                              # not a feature branch
        "feature//abc",                                        # double slash
    ],
    # NOTE: R13.2 only restricts the alphabet ([a-z0-9-]) and length
    # (3..50) of the segment after ``feature/``. It does NOT forbid
    # leading or trailing hyphens, so those cases are intentionally
    # absent from this rejection list. See
    # ``test_is_valid_feature_branch_allows_leading_and_trailing_hyphens``.
)
def test_is_valid_feature_branch_rejects_malformed_names(name: str) -> None:
    assert is_valid_feature_branch(name) is False


def test_is_valid_feature_branch_allows_leading_and_trailing_hyphens() -> None:
    """R13.2 only restricts the alphabet and length, not hyphen position."""
    assert is_valid_feature_branch("feature/-abc") is True
    assert is_valid_feature_branch("feature/abc-") is True
    assert is_valid_feature_branch("feature/---") is True


def test_is_valid_feature_branch_rejects_non_strings() -> None:
    assert is_valid_feature_branch(None) is False  # type: ignore[arg-type]
    assert is_valid_feature_branch(123) is False   # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_branch — full policy including the main-branch escape hatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "feature/abc",
        "feature/auth",
        "feature/joke-api",
        "feature/12-rate-limit",
        "feature/cost-evaluation",
        "feature/" + "a" * 50,
    ],
)
def test_validate_branch_accepts_feature_branches(name: str) -> None:
    result = validate_branch(name)
    assert isinstance(result, CheckResult)
    assert result.ok is True
    assert result.branch == name
    assert result.reason == ""


@pytest.mark.parametrize("name", ["main", "master"])
def test_validate_branch_accepts_integration_branches_when_allowed(
    name: str,
) -> None:
    result = validate_branch(name, allow_main=True)
    assert result.ok is True
    assert result.branch == name


@pytest.mark.parametrize("name", ["main", "master"])
def test_validate_branch_rejects_integration_branches_when_disallowed(
    name: str,
) -> None:
    result = validate_branch(name, allow_main=False)
    assert result.ok is False
    assert result.branch == name
    assert "integration branch" in result.reason


@pytest.mark.parametrize(
    "name",
    [
        "feature/ab",                                          # too short
        "feature/" + "a" * 51,                                 # too long
        "Feature/Auth",
        "feature/AUTH",
        "feature/auth_module",
        "feature/auth.module",
        "feature/auth space",
        "bugfix/foo",
        "feature/",
        "feature",
        "",
        "feature/auth/extra",
    ],
)
def test_validate_branch_rejects_invalid_names(name: str) -> None:
    result = validate_branch(name, allow_main=True)
    assert result.ok is False
    assert result.branch == name
    assert result.reason


def test_validate_branch_default_allow_main_is_true() -> None:
    """The default policy must accept ``main`` so CI runs on protected branches don't fail."""
    assert validate_branch("main").ok is True


def test_validate_branch_returns_check_result_dataclass() -> None:
    result = validate_branch("feature/auth")
    # Frozen dataclass: assignment must fail.
    with pytest.raises(Exception):
        result.ok = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# main() — CLI exit codes
# ---------------------------------------------------------------------------


def test_main_returns_zero_for_valid_feature_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--branch", "feature/cost-evaluation"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out


def test_main_returns_one_for_invalid_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--branch", "bugfix/foo"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.err


def test_main_accepts_main_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--branch", "main"])
    assert rc == 0


def test_main_rejects_main_with_no_allow_main(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--branch", "main", "--no-allow-main"])
    assert rc == 1


def test_main_reads_branch_from_github_ref_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REF_NAME", "feature/from-env")
    rc = main([])
    assert rc == 0


def test_main_github_ref_name_invalid_yields_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REF_NAME", "Bad_Branch")
    rc = main([])
    assert rc == 1


def test_main_returns_two_when_branch_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exit code 2 when no branch can be resolved at all."""
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    # Force the git-resolution helper to return None so we don't depend
    # on whether the test runner happens to be inside a git checkout.
    monkeypatch.setattr(
        check_branch_name,
        "_resolve_branch_from_env_or_git",
        lambda: None,
    )
    rc = main([])
    assert rc == 2
