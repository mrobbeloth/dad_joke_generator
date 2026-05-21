"""Unit tests for ``scripts/check_doc_freshness.py``.

Validates: Requirement 11.6 — when a PR touches ``src/``, both
``docs/PLAN.md`` and ``docs/TEST_PLAN.md`` must have been updated
within the last 90 whole UTC days; otherwise the build fails with a
non-zero exit status and a clear per-document error.

The tests drive the importable functions directly with synthesized
timestamps, plus a few ``main()`` runs that swap the changed-files
source and per-doc timestamps via env vars and a monkeypatched
``_last_commit_iso`` so no real git invocations occur.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Load scripts/check_doc_freshness.py as a module without polluting sys.path
# permanently. The file lives outside the package layout so we import it via
# importlib.
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_doc_freshness.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_doc_freshness", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None, (
        "could not build import spec for check_doc_freshness.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cdf = _load_module()


# --------------------------------------------------------------------------- #
# compute_age_days: floor((now - last) / 1 day)
# --------------------------------------------------------------------------- #

UTC = timezone.utc
T0 = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


def test_compute_age_days_zero_when_now_equals_last() -> None:
    assert cdf.compute_age_days(T0, T0) == 0


def test_compute_age_days_floors_partial_day() -> None:
    # 89 days + 23h59m59s should still floor to 89.
    last = T0 - timedelta(days=89, hours=23, minutes=59, seconds=59)
    assert cdf.compute_age_days(T0, last) == 89


def test_compute_age_days_exact_boundary() -> None:
    last = T0 - timedelta(days=90)
    assert cdf.compute_age_days(T0, last) == 90


def test_compute_age_days_91_days() -> None:
    last = T0 - timedelta(days=91)
    assert cdf.compute_age_days(T0, last) == 91


def test_compute_age_days_rejects_naive_now() -> None:
    naive_now = datetime(2025, 1, 15, 12, 0, 0)
    with pytest.raises(ValueError):
        cdf.compute_age_days(naive_now, T0)


def test_compute_age_days_rejects_naive_last() -> None:
    naive_last = datetime(2025, 1, 1, 0, 0, 0)
    with pytest.raises(ValueError):
        cdf.compute_age_days(T0, naive_last)


def test_compute_age_days_rejects_now_before_last() -> None:
    future = T0 + timedelta(days=1)
    with pytest.raises(ValueError):
        cdf.compute_age_days(T0, future)


# --------------------------------------------------------------------------- #
# src_touched
# --------------------------------------------------------------------------- #

def test_src_touched_detects_forward_slash() -> None:
    assert cdf.src_touched(["src/joke_api/handler.py"]) is True


def test_src_touched_detects_windows_path() -> None:
    assert cdf.src_touched(["src\\joke_api\\handler.py"]) is True


def test_src_touched_ignores_other_paths() -> None:
    assert (
        cdf.src_touched(
            ["docs/PLAN.md", "tests/unit/test_x.py", "README.md"]
        )
        is False
    )


def test_src_touched_does_not_match_prefix_only() -> None:
    # "source/foo.py" must not be confused with "src/...".
    assert cdf.src_touched(["source/foo.py", "src_helper.py"]) is False


def test_src_touched_handles_empty_and_whitespace() -> None:
    assert cdf.src_touched(["", "   ", "\t"]) is False


def test_src_touched_empty_iterable() -> None:
    assert cdf.src_touched([]) is False


# --------------------------------------------------------------------------- #
# parse_iso8601
# --------------------------------------------------------------------------- #

def test_parse_iso8601_accepts_z_suffix() -> None:
    dt = cdf.parse_iso8601("2025-01-15T12:00:00Z")
    assert dt == datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


def test_parse_iso8601_accepts_offset() -> None:
    dt = cdf.parse_iso8601("2025-01-15T07:00:00-05:00")
    assert dt == datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)


def test_parse_iso8601_rejects_naive() -> None:
    with pytest.raises(ValueError):
        cdf.parse_iso8601("2025-01-15T12:00:00")


# --------------------------------------------------------------------------- #
# check_freshness (pure)
# --------------------------------------------------------------------------- #

def _ts(days_old: int) -> datetime:
    return T0 - timedelta(days=days_old)


def test_check_freshness_both_fresh_returns_ok() -> None:
    code, errors, ages = cdf.check_freshness(
        now_utc=T0,
        doc_timestamps={
            "docs/PLAN.md": _ts(10),
            "docs/TEST_PLAN.md": _ts(20),
        },
    )
    assert code == cdf.EXIT_OK
    assert errors == []
    assert ages == {"docs/PLAN.md": 10, "docs/TEST_PLAN.md": 20}


def test_check_freshness_exact_90_days_is_ok() -> None:
    # R11.6: "exceeds 90" => 90 itself is NOT a violation.
    code, errors, _ages = cdf.check_freshness(
        now_utc=T0,
        doc_timestamps={
            "docs/PLAN.md": _ts(90),
            "docs/TEST_PLAN.md": _ts(90),
        },
    )
    assert code == cdf.EXIT_OK
    assert errors == []


def test_check_freshness_89_days_is_ok() -> None:
    code, errors, _ages = cdf.check_freshness(
        now_utc=T0,
        doc_timestamps={
            "docs/PLAN.md": _ts(89),
            "docs/TEST_PLAN.md": _ts(0),
        },
    )
    assert code == cdf.EXIT_OK
    assert errors == []


def test_check_freshness_91_days_plan_only_fails() -> None:
    code, errors, ages = cdf.check_freshness(
        now_utc=T0,
        doc_timestamps={
            "docs/PLAN.md": _ts(91),
            "docs/TEST_PLAN.md": _ts(5),
        },
    )
    assert code == cdf.EXIT_STALE
    assert ages["docs/PLAN.md"] == 91
    assert len(errors) == 1
    assert "docs/PLAN.md" in errors[0]
    assert "91" in errors[0]
    assert "max 90" in errors[0]


def test_check_freshness_both_stale_reports_both() -> None:
    code, errors, _ages = cdf.check_freshness(
        now_utc=T0,
        doc_timestamps={
            "docs/PLAN.md": _ts(120),
            "docs/TEST_PLAN.md": _ts(95),
        },
    )
    assert code == cdf.EXIT_STALE
    assert len(errors) == 2
    joined = "\n".join(errors)
    assert "docs/PLAN.md" in joined
    assert "docs/TEST_PLAN.md" in joined


def test_check_freshness_custom_max_days() -> None:
    # max_days=30: 31 is stale, 30 is fresh.
    code_stale, errors_stale, _ = cdf.check_freshness(
        now_utc=T0,
        doc_timestamps={
            "docs/PLAN.md": _ts(31),
            "docs/TEST_PLAN.md": _ts(0),
        },
        max_days=30,
    )
    assert code_stale == cdf.EXIT_STALE
    assert errors_stale and "max 30" in errors_stale[0]

    code_ok, _, _ = cdf.check_freshness(
        now_utc=T0,
        doc_timestamps={
            "docs/PLAN.md": _ts(30),
            "docs/TEST_PLAN.md": _ts(0),
        },
        max_days=30,
    )
    assert code_ok == cdf.EXIT_OK


# --------------------------------------------------------------------------- #
# main(): exit codes via env-driven CHANGED_FILES + monkeypatched git lookup
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate_changed_files_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure CHANGED_FILES doesn't leak between tests."""
    monkeypatch.delenv("CHANGED_FILES", raising=False)


def _patch_doc_timestamps(
    monkeypatch: pytest.MonkeyPatch,
    timestamps: "dict[str, datetime]",
) -> None:
    def fake_last(repo: Path, path: str) -> datetime:
        if path not in timestamps:
            raise RuntimeError(f"unexpected path {path!r}")
        return timestamps[path]

    monkeypatch.setattr(cdf, "_last_commit_iso", fake_last)


def test_main_exits_zero_when_src_untouched(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "CHANGED_FILES", "docs/PLAN.md\nREADME.md\n"
    )
    rc = cdf.main(["--repo", ".", "--now-utc", "2025-01-15T12:00:00Z"])
    assert rc == cdf.EXIT_OK
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "no src/" in out


def test_main_exits_zero_at_exact_90_days(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHANGED_FILES", "src/joke_api/handler.py\n")
    _patch_doc_timestamps(
        monkeypatch,
        {
            "docs/PLAN.md": _ts(90),
            "docs/TEST_PLAN.md": _ts(90),
        },
    )
    rc = cdf.main(["--repo", ".", "--now-utc", T0.isoformat()])
    assert rc == cdf.EXIT_OK
    out = capsys.readouterr().out
    assert "PLAN.md=90d" in out
    assert "TEST_PLAN.md=90d" in out


def test_main_exits_zero_at_89_days(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHANGED_FILES", "src/joke_api/handler.py\n")
    _patch_doc_timestamps(
        monkeypatch,
        {
            "docs/PLAN.md": _ts(89),
            "docs/TEST_PLAN.md": _ts(1),
        },
    )
    rc = cdf.main(["--repo", ".", "--now-utc", T0.isoformat()])
    assert rc == cdf.EXIT_OK
    out = capsys.readouterr().out
    assert "PLAN.md=89d" in out


def test_main_exits_one_at_91_days(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHANGED_FILES", "src/joke_api/handler.py\n")
    _patch_doc_timestamps(
        monkeypatch,
        {
            "docs/PLAN.md": _ts(91),
            "docs/TEST_PLAN.md": _ts(1),
        },
    )
    rc = cdf.main(["--repo", ".", "--now-utc", T0.isoformat()])
    assert rc == cdf.EXIT_STALE
    err = capsys.readouterr().err
    assert "docs/PLAN.md" in err
    assert "91" in err
    assert "max 90" in err


def test_main_both_stale_mentions_both(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHANGED_FILES", "src/joke_api/handler.py\n")
    _patch_doc_timestamps(
        monkeypatch,
        {
            "docs/PLAN.md": _ts(120),
            "docs/TEST_PLAN.md": _ts(95),
        },
    )
    rc = cdf.main(["--repo", ".", "--now-utc", T0.isoformat()])
    assert rc == cdf.EXIT_STALE
    err = capsys.readouterr().err
    assert "docs/PLAN.md" in err
    assert "docs/TEST_PLAN.md" in err


def test_main_tooling_failure_when_git_history_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHANGED_FILES", "src/joke_api/handler.py\n")

    def fake_last(repo: Path, path: str) -> datetime:
        raise RuntimeError(f"no git history for {path!r}")

    monkeypatch.setattr(cdf, "_last_commit_iso", fake_last)
    rc = cdf.main(["--repo", ".", "--now-utc", T0.isoformat()])
    assert rc == cdf.EXIT_TOOLING
    err = capsys.readouterr().err
    assert "no git history" in err


def test_main_rejects_invalid_now_utc(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cdf.main(["--now-utc", "not-a-timestamp"])
    assert rc == cdf.EXIT_TOOLING
    assert "invalid --now-utc" in capsys.readouterr().err


def test_main_rejects_negative_max_days(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cdf.main(["--max-days", "-1"])
    assert rc == cdf.EXIT_TOOLING
    assert "non-negative" in capsys.readouterr().err
