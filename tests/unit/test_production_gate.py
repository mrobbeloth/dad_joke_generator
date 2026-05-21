"""Unit tests for ``scripts/production_gate.py``.

Validates: Requirements 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7 — the
Production_Gate orchestrator must:

* aggregate gate results without raising for the documented subprocess
  failure modes (``TimeoutExpired``, ``FileNotFoundError``);
* emit a block report within 30 s naming the failed gate, failing
  items, and run timestamp (R12.7);
* emit a self-health signal within 60 s of run start (R12.2);
* exit 0 on full pass, 1 on any block, 2 on internal error, 3 when the
  30 s report-emission SLA is breached.

The tests load the script via ``importlib`` so they don't depend on
``scripts/`` being on ``PYTHONPATH``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "production_gate.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "production_gate", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pg = _load_module()
Gate = pg.Gate
GateResult = pg.GateResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ISO_8601_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$"
)


def _make_gate(name: str = "demo", *, timeout: float = 5.0) -> Gate:
    return Gate(
        name=name,
        command=("python", "-c", "print('hi')"),
        timeout_seconds=timeout,
        description=f"Test gate {name}",
    )


def _completed(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "-c", "print('hi')"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# run_gate — success path
# ---------------------------------------------------------------------------


def test_run_gate_success_returns_passed_status() -> None:
    """A zero-exit subprocess produces a passing GateResult."""

    def fake_runner(*args, **kwargs):
        # Confirm we're invoking with shell=False (security guarantee).
        assert kwargs.get("shell") is False
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        return _completed(returncode=0, stdout="ok\n")

    result = pg.run_gate(_make_gate("xref"), runner=fake_runner)
    assert isinstance(result, GateResult)
    assert result.status == "passed"
    assert result.passed is True
    assert result.blocking is False
    assert result.failing_items == []
    assert result.exit_code == 0
    assert result.name == "xref"
    assert _ISO_8601_UTC_RE.match(result.started_at_iso) is not None


# ---------------------------------------------------------------------------
# run_gate — failure path (non-zero exit)
# ---------------------------------------------------------------------------


def test_run_gate_failure_captures_failing_items() -> None:
    """A non-zero exit produces ``status='failed'`` with stderr lines."""

    def fake_runner(*args, **kwargs):
        return _completed(
            returncode=1,
            stdout="some progress\n",
            stderr=(
                "ERROR: docs/PLAN.md missing R3\n"
                "ERROR: docs/PLAN.md missing R7\n"
            ),
        )

    result = pg.run_gate(_make_gate("plan-xref"), runner=fake_runner)
    assert result.status == "failed"
    assert result.blocking is True
    assert result.passed is False
    assert result.exit_code == 1
    # Both ERROR lines from stderr must be preserved.
    assert any("R3" in item for item in result.failing_items)
    assert any("R7" in item for item in result.failing_items)


# ---------------------------------------------------------------------------
# run_gate — timeout path
# ---------------------------------------------------------------------------


def test_run_gate_timeout_returns_timeout_status() -> None:
    """``subprocess.TimeoutExpired`` becomes ``status='timeout'``."""

    def fake_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0], timeout=2.0, output="partial-out", stderr="partial-err"
        )

    gate = _make_gate("slow-gate", timeout=2.0)
    result = pg.run_gate(gate, runner=fake_runner)
    assert result.status == "timeout"
    assert result.blocking is True
    assert result.exit_code is None
    assert "2.0" in result.message
    assert result.failing_items, "timeout must produce at least one failing item"


# ---------------------------------------------------------------------------
# run_gate — error path (script missing)
# ---------------------------------------------------------------------------


def test_run_gate_file_not_found_returns_error_status() -> None:
    """``FileNotFoundError`` becomes ``status='error'`` with a clear message."""

    def fake_runner(*args, **kwargs):
        raise FileNotFoundError(2, "No such file", "/nonexistent/script.py")

    result = pg.run_gate(_make_gate("ghost"), runner=fake_runner)
    assert result.status == "error"
    assert result.blocking is True
    assert result.exit_code is None
    assert any("not found" in item.lower() for item in result.failing_items)


# ---------------------------------------------------------------------------
# run_all_gates — aggregation: all-pass returns no blocking results
# ---------------------------------------------------------------------------


def test_run_all_gates_all_pass_yields_no_blocking_results() -> None:
    """Aggregation: every passing gate ⇒ no blocking results."""

    def fake_runner(*args, **kwargs):
        return _completed(returncode=0)

    gates = [_make_gate(f"gate-{i}") for i in range(3)]
    results = pg.run_all_gates(gates, runner=fake_runner)
    assert len(results) == 3
    assert all(r.status == "passed" for r in results)
    assert not any(r.blocking for r in results)


# ---------------------------------------------------------------------------
# run_all_gates — aggregation: any-fail surfaces a blocking result
# ---------------------------------------------------------------------------


def test_run_all_gates_any_fail_yields_blocking_result() -> None:
    """Aggregation: a single failure makes the run blocking."""

    failing_name = "phases"

    def fake_runner(args, **kwargs):
        # ``args`` is the argv list. The Gate name is encoded in the
        # script path's last segment, but our test gates use ``-c``;
        # discriminate on the marker we encode in the command instead.
        if "FAIL_HERE" in args:
            return _completed(
                returncode=1,
                stderr="ERROR: phase scope violation: R20 in Phase 3\n",
            )
        return _completed(returncode=0)

    gates = [
        Gate(
            name="doc-freshness",
            command=("python", "-c", "print('ok')"),
            timeout_seconds=5,
        ),
        Gate(
            name=failing_name,
            command=("python", "-c", "FAIL_HERE"),
            timeout_seconds=5,
        ),
        Gate(
            name="cost-report",
            command=("python", "-c", "print('ok')"),
            timeout_seconds=5,
        ),
    ]
    results = pg.run_all_gates(gates, runner=fake_runner)
    assert [r.status for r in results] == ["passed", "failed", "passed"]
    blocking = [r for r in results if r.blocking]
    assert len(blocking) == 1
    assert blocking[0].name == failing_name


# ---------------------------------------------------------------------------
# format_block_report — content and formatting (R12.7)
# ---------------------------------------------------------------------------


def test_format_block_report_names_failed_gate_and_items() -> None:
    """R12.7: the report names the failed gate, failing items, and run timestamp."""
    run_started_at = datetime(2025, 1, 15, 12, 30, 45, 123000, tzinfo=timezone.utc)
    results = [
        GateResult(
            name="doc-freshness",
            status="passed",
            failing_items=[],
            duration_seconds=0.4,
            started_at_iso="2025-01-15T12:30:45.123Z",
            exit_code=0,
        ),
        GateResult(
            name="plan-xref",
            status="failed",
            failing_items=[
                "ERROR: 2 requirement(s) listed in PLAN.md are missing from "
                "TEST_PLAN.md: R7, R12",
            ],
            duration_seconds=1.2,
            started_at_iso="2025-01-15T12:30:46.001Z",
            exit_code=1,
            message="exit status 1",
        ),
    ]

    report = pg.format_block_report(results, run_started_at, fmt="text")

    assert "PRODUCTION GATE BLOCKED" in report
    assert "plan-xref" in report
    assert "R7" in report and "R12" in report
    # ISO 8601 UTC timestamp present.
    assert "2025-01-15T12:30:45.123Z" in report
    # Passed gates are not enumerated in the blocked-detail body, but the
    # summary footer should still mention the blocking gate by name.
    assert "Summary of failed gates: plan-xref" in report


def test_format_block_report_supports_json_output() -> None:
    run_started_at = datetime(2025, 1, 15, 12, 30, 45, tzinfo=timezone.utc)
    results = [
        GateResult(
            name="phases",
            status="failed",
            failing_items=["ERROR: orphan requirement R42"],
            duration_seconds=0.5,
            started_at_iso="2025-01-15T12:30:45.000Z",
            exit_code=1,
        ),
    ]
    report_json = pg.format_block_report(
        results, run_started_at, fmt="json"
    )
    payload = json.loads(report_json)
    assert payload["blocked"] is True
    assert payload["failed_count"] == 1
    assert payload["total_gates"] == 1
    assert payload["run_started_at_utc"].endswith("Z")
    assert payload["results"][0]["name"] == "phases"
    assert payload["results"][0]["failing_items"] == [
        "ERROR: orphan requirement R42"
    ]


# ---------------------------------------------------------------------------
# format_block_report — wall-clock budget (R12.7: 30 s)
# ---------------------------------------------------------------------------


def test_format_block_report_within_30s_budget_on_realistic_input() -> None:
    """R12.7: the report must be emittable within 30 s.

    Synthesize a realistic Production_Gate result list (one entry per
    default gate, each with several failing items) and verify the
    formatter completes well under budget.
    """
    run_started_at = datetime.now(timezone.utc)
    results: list[GateResult] = []
    # Mix of pass/fail; mimic a worst-case "everything failed" run so
    # the formatter must serialize every failing-items list.
    for i, name in enumerate(
        [
            "doc-freshness",
            "plan-xref",
            "cost-report",
            "branch-name",
            "phases",
            "manual-setup",
            "render-plantuml",
        ]
    ):
        results.append(
            GateResult(
                name=name,
                status="failed",
                failing_items=[
                    f"ERROR: {name} item {j} failed" for j in range(50)
                ],
                duration_seconds=float(i) + 0.123,
                started_at_iso="2025-01-15T12:30:45.123Z",
                exit_code=1,
                message="exit status 1",
            )
        )

    started = time.monotonic()
    text = pg.format_block_report(results, run_started_at, fmt="text")
    elapsed = time.monotonic() - started
    assert elapsed < 30.0, (
        f"format_block_report took {elapsed:.2f}s, exceeding the "
        f"30 s SLA from R12.7"
    )
    # Sanity: the report is non-empty and mentions every failed gate.
    for name in (
        "doc-freshness",
        "plan-xref",
        "cost-report",
        "branch-name",
        "phases",
        "manual-setup",
        "render-plantuml",
    ):
        assert name in text


# ---------------------------------------------------------------------------
# emit_self_health — within 60 s budget (R12.2)
# ---------------------------------------------------------------------------


def test_emit_self_health_within_60s_budget() -> None:
    """R12.2: self-health signal must be emitted within 60 s of run start."""
    # Synthetic clock: start at t=0, "now" at t=12 (well under 60 s).
    sequence = iter([0.0, 12.0])

    def fake_clock() -> float:
        return next(sequence)

    started = fake_clock()  # t=0
    signal = pg.emit_self_health(
        run_started_monotonic=started,
        clock=fake_clock,
        deadline_seconds=60.0,
    )
    assert signal["status"] == "ok"
    assert signal["elapsed_seconds"] == 12.0
    assert signal["deadline_seconds"] == 60.0
    # ISO 8601 UTC, ending in Z.
    assert _ISO_8601_UTC_RE.match(signal["emitted_at_utc"]) is not None


def test_emit_self_health_writes_signal_file_when_path_supplied(
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "health" / "signal.json"
    sequence = iter([0.0, 5.0])

    def fake_clock() -> float:
        return next(sequence)

    started = fake_clock()
    signal = pg.emit_self_health(
        run_started_monotonic=started,
        out_path=out_path,
        clock=fake_clock,
        deadline_seconds=60.0,
    )
    assert out_path.exists()
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk == signal
    assert on_disk["status"] == "ok"


def test_emit_self_health_raises_when_deadline_exceeded() -> None:
    """A clock that already overshoots the budget triggers TimeoutError."""
    sequence = iter([0.0, 75.0])

    def fake_clock() -> float:
        return next(sequence)

    started = fake_clock()
    with pytest.raises(TimeoutError):
        pg.emit_self_health(
            run_started_monotonic=started,
            clock=fake_clock,
            deadline_seconds=60.0,
        )


# ---------------------------------------------------------------------------
# main() exit-code wiring
# ---------------------------------------------------------------------------


def test_main_health_check_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = pg.main(
        [
            "--health-check",
            "--no-render-plantuml",  # avoid building a non-applicable gate
        ]
    )
    assert rc == pg.EXIT_OK
    captured = capsys.readouterr()
    assert "self-health" in captured.out


def test_main_unknown_gate_returns_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = pg.main(
        [
            "--gate",
            "does-not-exist",
            "--no-render-plantuml",
        ]
    )
    assert rc == pg.EXIT_INTERNAL_ERROR
    err = capsys.readouterr().err
    assert "unknown gate" in err
