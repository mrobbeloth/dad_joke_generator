#!/usr/bin/env python3
"""scripts/production_gate.py — Production_Gate orchestrator.

Aggregates the Build_Pipeline gate scripts that gate every production
deployment and emits a single block report when any gate fails. Wires
together the standalone scripts under ``scripts/`` (each owned by an
earlier task) so the Build_Pipeline workflow has one entry point that:

* runs the gates in a deterministic order,
* captures structured results for reporting,
* meets the wall-clock SLAs called out in Requirement 12, and
* exits with a stable, parseable status code.

Implements Requirement 12.1 — 12.7:

* R12.1  Block production deployment when any unit/integration/end-to-end
         test or governance gate has not completed with a passing result.
* R12.2  Emit a self-health signal within 60 seconds of run start.
* R12.3  Block deployment when the self-health signal does not arrive in
         60 s or reports failure.
* R12.4  Block deployment when any PlantUML source under
         ``docs/architecture/`` fails to render.
* R12.5  Block deployment when any PLAN.md requirement lacks a TEST_PLAN
         reference (delegated to ``scripts/check_plan_xref.py``).
* R12.6  Block deployment on Cost_Report ↔ runtime mismatch (delegated
         to ``scripts/check_cost_report.py``).
* R12.7  When the Production_Gate blocks a deployment, produce a report
         within 30 s naming the failed gate, failing items, and run
         timestamp.

CLI
---

Usage:

    python scripts/production_gate.py --all
    python scripts/production_gate.py --gate=plan-xref
    python scripts/production_gate.py --health-check
    python scripts/production_gate.py --all --out report.txt

Exit codes
----------

    0  all gates passed
    1  at least one gate failed (deployment blocked)
    2  internal error (e.g. a gate script not found, invalid args)
    3  the 30-second report-emission SLA was breached (R12.7)

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

__all__ = [
    "Gate",
    "GateResult",
    "DEFAULT_GATES",
    "DEFAULT_REPORT_DEADLINE_SECONDS",
    "DEFAULT_SELF_HEALTH_DEADLINE_SECONDS",
    "EXIT_OK",
    "EXIT_BLOCKED",
    "EXIT_INTERNAL_ERROR",
    "EXIT_REPORT_DEADLINE",
    "build_default_gates",
    "emit_self_health",
    "format_block_report",
    "run_all_gates",
    "run_gate",
    "main",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_INTERNAL_ERROR = 2
EXIT_REPORT_DEADLINE = 3

DEFAULT_REPORT_DEADLINE_SECONDS = 30  # R12.7
DEFAULT_SELF_HEALTH_DEADLINE_SECONDS = 60  # R12.2

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """A single Production_Gate check.

    Attributes:
        name: Stable, human-readable identifier (used in CLI selection
            and in the block report). Lower-case, hyphen-separated.
        command: argv list for :func:`subprocess.run` with
            ``shell=False``. The first element is the executable.
        timeout_seconds: Per-gate wall-clock timeout. ``run_gate``
            converts a :class:`subprocess.TimeoutExpired` into a
            ``status='timeout'`` :class:`GateResult` so the orchestrator
            can keep going across multiple gates without aborting.
        description: One-line description shown in ``--help``-style
            listings.
        skip_on_platforms: Tuple of :data:`sys.platform` prefixes
            (e.g. ``("win32",)``) on which this gate is skipped with a
            warning rather than executed. Used for ``render-plantuml``
            on Windows shells where ``bash`` and Docker may not be on
            PATH.
    """

    name: str
    command: tuple[str, ...]
    timeout_seconds: float = 120.0
    description: str = ""
    skip_on_platforms: tuple[str, ...] = ()


@dataclass
class GateResult:
    """Structured outcome of running a single :class:`Gate`.

    Attributes:
        name: Mirrors :attr:`Gate.name` so the result is self-contained.
        status: One of ``"passed"``, ``"failed"``, ``"timeout"``,
            ``"error"``, or ``"skipped"``. ``"skipped"`` is treated as
            non-blocking (the platform-skip case) and never contributes
            to the block decision.
        failing_items: Per-gate list of error lines surfaced from
            stderr/stdout. Empty when ``status == "passed"`` or
            ``"skipped"``.
        duration_seconds: Wall-clock time to run the gate, measured
            with :func:`time.monotonic`.
        started_at_iso: ISO 8601 UTC timestamp captured immediately
            before the gate command was invoked.
        exit_code: The gate's process exit code, or ``None`` for
            timeout / error / skipped outcomes.
        message: Free-form note (used for skip and error reasons).
    """

    name: str
    status: str
    failing_items: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    started_at_iso: str = ""
    exit_code: int | None = None
    message: str = ""

    @property
    def passed(self) -> bool:
        """True iff this gate produced a passing or skipped outcome."""
        return self.status in ("passed", "skipped")

    @property
    def blocking(self) -> bool:
        """True iff this gate's outcome should block deployment."""
        return self.status in ("failed", "timeout", "error")


# ---------------------------------------------------------------------------
# Default gate registry
# ---------------------------------------------------------------------------


def build_default_gates(
    *,
    repo_root: Path = _REPO_ROOT,
    plan_path: Path | str = "docs/PLAN.md",
    test_plan_path: Path | str = "docs/TEST_PLAN.md",
    cost_report_path: Path | str = "docs/COST_REPORT.md",
    runtime_model_id: str | None = None,
    runtime_voice_id: str | None = None,
    current_phase: int = 1,
    branch: str | None = None,
    include_render_plantuml: bool = True,
) -> list[Gate]:
    """Build the canonical Production_Gate gate list.

    Each gate is implemented by a standalone script in ``scripts/``;
    this function only wires their argv together. Runtime values that
    depend on deploy-time configuration (model id, voice id, current
    phase) are passed as parameters so the orchestrator stays a pure
    function of its inputs.
    """
    py = sys.executable or "python3"
    scripts = repo_root / "scripts"

    gates: list[Gate] = []

    # ---- R11.6: doc freshness -------------------------------------------
    gates.append(
        Gate(
            name="doc-freshness",
            command=(py, str(scripts / "check_doc_freshness.py")),
            timeout_seconds=30,
            description=(
                "PLAN.md / TEST_PLAN.md updated within the last 90 UTC "
                "days when src/ is touched (R11.6)."
            ),
        )
    )

    # ---- R12.5: PLAN <-> TEST_PLAN cross-reference ----------------------
    gates.append(
        Gate(
            name="plan-xref",
            command=(
                py,
                str(scripts / "check_plan_xref.py"),
                "--plan",
                str(plan_path),
                "--test-plan",
                str(test_plan_path),
            ),
            timeout_seconds=30,
            description=(
                "Every requirement id in PLAN.md is referenced in "
                "TEST_PLAN.md (R12.5)."
            ),
        )
    )

    # ---- R12.6: Cost_Report consistency ---------------------------------
    cost_cmd: list[str] = [
        py,
        str(scripts / "check_cost_report.py"),
        "--cost-report",
        str(cost_report_path),
    ]
    if runtime_model_id:
        cost_cmd.extend(["--runtime-model-id", runtime_model_id])
    if runtime_voice_id:
        cost_cmd.extend(["--runtime-voice-id", runtime_voice_id])
    gates.append(
        Gate(
            name="cost-report",
            command=tuple(cost_cmd),
            timeout_seconds=30,
            description=(
                "Cost_Report's recommended model/voice match the "
                "configured runtime values (R12.6)."
            ),
        )
    )

    # ---- R13.2: feature-branch name -------------------------------------
    branch_cmd: list[str] = [py, str(scripts / "check_branch_name.py")]
    if branch is not None:
        branch_cmd.extend(["--branch", branch])
    gates.append(
        Gate(
            name="branch-name",
            command=tuple(branch_cmd),
            timeout_seconds=15,
            description=(
                "Branch name matches feature/<short-description> "
                "(R13.2)."
            ),
        )
    )

    # ---- R14.2/R14.3: phase assignment + scope --------------------------
    gates.append(
        Gate(
            name="phases",
            command=(
                py,
                str(scripts / "check_phases.py"),
                "--plan",
                str(plan_path),
                "--current-phase",
                str(current_phase),
                "--mode",
                "all",
            ),
            timeout_seconds=30,
            description=(
                "Every requirement is assigned to exactly one phase, "
                "and no requirement exceeds the current phase "
                "(R14.2, R14.3)."
            ),
        )
    )

    # ---- R15.6: AWS Manual Setup completeness ---------------------------
    gates.append(
        Gate(
            name="manual-setup",
            command=(
                py,
                str(scripts / "check_manual_setup.py"),
                "--plan",
                str(plan_path),
                "--mode",
                "build-start",
            ),
            timeout_seconds=15,
            description=(
                "Every AWS Manual Setup item is checked with a valid "
                "ISO 8601 completion date (R15.6)."
            ),
        )
    )

    # ---- R10.4 / R12.4: PlantUML rendering ------------------------------
    if include_render_plantuml:
        # Bash + Docker. Skip on Windows by default; the GitHub Actions
        # Linux runner is the system of record for this gate.
        gates.append(
            Gate(
                name="render-plantuml",
                command=(
                    "bash",
                    str(scripts / "render_plantuml.sh"),
                ),
                timeout_seconds=600,
                description=(
                    "Every .puml under docs/architecture/ renders to "
                    "PNG and SVG within 120 s (R10.4, R12.4)."
                ),
                skip_on_platforms=("win32",),
            )
        )

    return gates


DEFAULT_GATES = build_default_gates()


# ---------------------------------------------------------------------------
# Gate execution
# ---------------------------------------------------------------------------


def _now_iso_utc() -> str:
    """ISO 8601 UTC timestamp accurate to milliseconds, ending in ``Z``."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _extract_failing_items(
    *,
    stdout: str,
    stderr: str,
    fallback: str,
) -> list[str]:
    """Pull human-readable error lines out of a gate's captured output.

    Strategy: prefer stderr lines that start with ``ERROR``/``FAIL``;
    fall back to the full non-empty stderr; then to non-empty stdout;
    then to a single ``fallback`` message. The result is always
    non-empty so the block report never has an empty bullet list.
    """
    candidates: list[str] = []
    for line in (stderr or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper.startswith("ERROR") or upper.startswith("FAIL") or "ERROR:" in upper:
            candidates.append(stripped)

    if candidates:
        return candidates

    # Second pass: any non-empty stderr line at all.
    leftover_err = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if leftover_err:
        return leftover_err

    # Third pass: stdout lines (some scripts print errors to stdout via
    # ``print(...)`` rather than ``print(..., file=sys.stderr)``).
    leftover_out = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if leftover_out:
        return leftover_out

    return [fallback]


def run_gate(
    gate: Gate,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> GateResult:
    """Execute a single :class:`Gate` and return a :class:`GateResult`.

    The gate is invoked with ``shell=False`` and ``capture_output=True``
    so command-injection is impossible and stdout/stderr can be parsed
    for the block report. The function never raises for the documented
    subprocess failure modes (``TimeoutExpired``, ``FileNotFoundError``,
    ``OSError``); each is mapped onto a structured ``GateResult``.

    The ``runner`` parameter exists for testability — pass a stub that
    returns a :class:`subprocess.CompletedProcess` or raises one of the
    handled exceptions.
    """
    started_at_iso = _now_iso_utc()
    started_monotonic = time.monotonic()

    # Platform skip (R12.4 still applies on the canonical Linux runner;
    # skipping Windows local runs avoids spurious blocks while developers
    # iterate on the script itself).
    if any(sys.platform.startswith(p) for p in gate.skip_on_platforms):
        return GateResult(
            name=gate.name,
            status="skipped",
            failing_items=[],
            duration_seconds=time.monotonic() - started_monotonic,
            started_at_iso=started_at_iso,
            exit_code=None,
            message=(
                f"WARN: gate {gate.name!r} skipped on platform "
                f"{sys.platform!r} (requires bash/docker)."
            ),
        )

    try:
        completed = runner(
            list(gate.command),
            shell=False,
            capture_output=True,
            text=True,
            timeout=gate.timeout_seconds,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started_monotonic
        # ``exc.output`` / ``exc.stderr`` may be ``None`` on early kill.
        out = (exc.output or "") if isinstance(exc.output, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return GateResult(
            name=gate.name,
            status="timeout",
            failing_items=_extract_failing_items(
                stdout=out,
                stderr=err,
                fallback=(
                    f"timeout after {gate.timeout_seconds:.1f}s while "
                    f"running {' '.join(gate.command)}"
                ),
            ),
            duration_seconds=duration,
            started_at_iso=started_at_iso,
            exit_code=None,
            message=f"timeout after {gate.timeout_seconds:.1f}s",
        )
    except FileNotFoundError as exc:
        duration = time.monotonic() - started_monotonic
        return GateResult(
            name=gate.name,
            status="error",
            failing_items=[
                f"ERROR: gate command not found: {exc.filename or gate.command[0]}"
            ],
            duration_seconds=duration,
            started_at_iso=started_at_iso,
            exit_code=None,
            message=f"command not found: {exc.filename or gate.command[0]}",
        )
    except OSError as exc:
        duration = time.monotonic() - started_monotonic
        return GateResult(
            name=gate.name,
            status="error",
            failing_items=[f"ERROR: OSError invoking gate: {exc}"],
            duration_seconds=duration,
            started_at_iso=started_at_iso,
            exit_code=None,
            message=str(exc),
        )

    duration = time.monotonic() - started_monotonic
    if completed.returncode == 0:
        return GateResult(
            name=gate.name,
            status="passed",
            failing_items=[],
            duration_seconds=duration,
            started_at_iso=started_at_iso,
            exit_code=0,
            message="",
        )

    return GateResult(
        name=gate.name,
        status="failed",
        failing_items=_extract_failing_items(
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            fallback=(
                f"gate exited with status {completed.returncode} "
                "but produced no output"
            ),
        ),
        duration_seconds=duration,
        started_at_iso=started_at_iso,
        exit_code=completed.returncode,
        message=f"exit status {completed.returncode}",
    )


def run_all_gates(
    gates: Iterable[Gate],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    on_result: Callable[[GateResult], None] | None = None,
    stop_on_first_failure: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[GateResult]:
    """Run every gate sequentially and collect results.

    Sequential execution keeps log output deterministic and avoids
    interleaved subprocess writes that would make a Production_Gate
    block report harder to read. Sub-second per-gate overhead is
    negligible compared to the gates' own runtime, so parallelism is
    unnecessary.
    """
    results: list[GateResult] = []
    for gate in gates:
        result = run_gate(gate, env=env, cwd=cwd, runner=runner)
        results.append(result)
        if on_result is not None:
            try:
                on_result(result)
            except Exception:  # pragma: no cover - callbacks are best-effort
                # A buggy callback must never abort the gate run; the
                # block report is more important than the live update.
                pass
        if stop_on_first_failure and result.blocking:
            break
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_iso_run_started(run_started_at: datetime) -> str:
    if run_started_at.tzinfo is None:
        run_started_at = run_started_at.replace(tzinfo=timezone.utc)
    run_started_at = run_started_at.astimezone(timezone.utc)
    return run_started_at.strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{run_started_at.microsecond // 1000:03d}Z"
    )


def format_block_report(
    results: Sequence[GateResult],
    run_started_at: datetime,
    *,
    fmt: str = "text",
) -> str:
    """Render a Production_Gate block report (R12.7).

    The report names the failed gate, the failing items, and the run
    timestamp in ISO 8601 UTC. ``fmt`` selects between human-readable
    text (default) and a single-line JSON document suitable for
    machine consumption (e.g. attaching to a build artifact).

    The function is pure and CPU-bound; it must complete well under
    the 30 s SLA on any realistic input. The accompanying unit test
    asserts this empirically.
    """
    blocking = [r for r in results if r.blocking]
    timestamp = _format_iso_run_started(run_started_at)

    if fmt == "json":
        payload = {
            "blocked": bool(blocking),
            "run_started_at_utc": timestamp,
            "total_gates": len(results),
            "failed_count": len(blocking),
            "results": [asdict(r) for r in results],
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    if fmt != "text":
        raise ValueError(
            f"unsupported report format {fmt!r}; expected 'text' or 'json'"
        )

    lines: list[str] = []
    lines.append("=" * 70)
    if blocking:
        lines.append("PRODUCTION GATE BLOCKED")
    else:
        lines.append("PRODUCTION GATE PASSED")
    lines.append("=" * 70)
    lines.append(f"Run timestamp (UTC): {timestamp}")
    lines.append(f"Total gates:         {len(results)}")
    lines.append(f"Failed gates:        {len(blocking)}")
    lines.append("")

    if not blocking:
        for r in results:
            lines.append(
                f"  [OK]    {r.name:<20} status={r.status} "
                f"duration={r.duration_seconds:.2f}s"
            )
        return "\n".join(lines) + "\n"

    for r in blocking:
        lines.append(f"Gate:           {r.name}")
        lines.append(f"Status:         {r.status}")
        lines.append(f"Started (UTC):  {r.started_at_iso}")
        lines.append(f"Duration:       {r.duration_seconds:.2f}s")
        if r.exit_code is not None:
            lines.append(f"Exit code:      {r.exit_code}")
        if r.message:
            lines.append(f"Message:        {r.message}")
        lines.append("Failing items:")
        if r.failing_items:
            for item in r.failing_items:
                lines.append(f"  - {item}")
        else:
            lines.append("  - (no detail captured)")
        lines.append("-" * 70)

    # Trailing summary so the failed-gate name list is visible even
    # when the per-gate sections scroll off in CI logs.
    lines.append("Summary of failed gates: " + ", ".join(r.name for r in blocking))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Self-health (R12.2 / R12.3)
# ---------------------------------------------------------------------------


def emit_self_health(
    *,
    run_started_monotonic: float | None = None,
    out_path: Path | str | None = None,
    deadline_seconds: float = DEFAULT_SELF_HEALTH_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    now_utc: Callable[[], datetime] | None = None,
) -> dict:
    """Emit the Production_Gate self-health signal (R12.2).

    Returns a dict describing the heartbeat. The signal is also
    written to ``out_path`` (if supplied) as a single-line JSON
    document so an external CI step can poll for the file's
    appearance. The function asserts it ran within
    ``deadline_seconds`` and raises ``TimeoutError`` if not.

    The ``clock`` and ``now_utc`` injection points exist for tests;
    production callers leave them at the defaults.
    """
    if run_started_monotonic is None:
        run_started_monotonic = clock()
    elapsed = max(0.0, clock() - run_started_monotonic)
    if elapsed > deadline_seconds:
        raise TimeoutError(
            f"self-health emission exceeded {deadline_seconds:.1f}s budget "
            f"(actual {elapsed:.2f}s)"
        )

    timestamp = (now_utc or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    payload = {
        "status": "ok",
        "emitted_at_utc": timestamp.strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{timestamp.microsecond // 1000:03d}Z",
        "elapsed_seconds": round(elapsed, 4),
        "deadline_seconds": float(deadline_seconds),
    }

    if out_path is not None:
        out_path_p = Path(out_path)
        out_path_p.parent.mkdir(parents=True, exist_ok=True)
        out_path_p.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_runtime_id(
    cli_value: str | None, env_var: str
) -> str | None:
    """CLI value beats env var; empty strings collapse to ``None``."""
    if cli_value:
        return cli_value
    env_value = os.environ.get(env_var, "").strip()
    return env_value or None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="production_gate.py",
        description=(
            "Run the Production_Gate aggregate of governance and "
            "rendering checks. Emits a block report within 30 s "
            "(R12.7) and a self-health signal within 60 s of run "
            "start (R12.2)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--all",
        action="store_true",
        help="Run every default gate (this is also the default mode).",
    )
    mode.add_argument(
        "--gate",
        default=None,
        help="Run a single gate by name (e.g. plan-xref).",
    )
    mode.add_argument(
        "--health-check",
        action="store_true",
        help=(
            "Emit only the self-health signal and exit. No gate "
            "subprocesses are spawned."
        ),
    )
    mode.add_argument(
        "--list-gates",
        action="store_true",
        help="Print the default gate registry and exit.",
    )

    parser.add_argument(
        "--out",
        default=None,
        help="Write the block report to this path in addition to stdout.",
    )
    parser.add_argument(
        "--report-format",
        choices=("text", "json"),
        default="text",
        help="Report format (default: text).",
    )
    parser.add_argument(
        "--self-health-out",
        default=None,
        help=(
            "Write the self-health signal JSON document to this path. "
            "Useful for poll-based CI integrations."
        ),
    )
    parser.add_argument(
        "--report-deadline-seconds",
        type=float,
        default=DEFAULT_REPORT_DEADLINE_SECONDS,
        help=(
            f"Wall-clock deadline for emitting the block report after "
            f"the block decision (default: "
            f"{DEFAULT_REPORT_DEADLINE_SECONDS}s, per R12.7)."
        ),
    )
    parser.add_argument(
        "--self-health-deadline-seconds",
        type=float,
        default=DEFAULT_SELF_HEALTH_DEADLINE_SECONDS,
        help=(
            f"Wall-clock deadline for the self-health signal "
            f"(default: {DEFAULT_SELF_HEALTH_DEADLINE_SECONDS}s, per "
            "R12.2)."
        ),
    )

    parser.add_argument(
        "--plan",
        default="docs/PLAN.md",
        help="Path to PLAN.md (default: docs/PLAN.md).",
    )
    parser.add_argument(
        "--test-plan",
        default="docs/TEST_PLAN.md",
        help="Path to TEST_PLAN.md (default: docs/TEST_PLAN.md).",
    )
    parser.add_argument(
        "--cost-report",
        default="docs/COST_REPORT.md",
        help="Path to COST_REPORT.md (default: docs/COST_REPORT.md).",
    )
    parser.add_argument(
        "--runtime-model-id",
        default=None,
        help=(
            "Runtime Bedrock model id for the cost-report gate. "
            "Falls back to RUNTIME_BEDROCK_MODEL_ID."
        ),
    )
    parser.add_argument(
        "--runtime-voice-id",
        default=None,
        help=(
            "Runtime Polly voice id for the cost-report gate. "
            "Falls back to RUNTIME_POLLY_VOICE_ID."
        ),
    )
    parser.add_argument(
        "--current-phase",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help="Phase the build is currently targeting (default: 1).",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Override the branch name passed to the branch-name gate.",
    )
    parser.add_argument(
        "--no-render-plantuml",
        dest="include_render_plantuml",
        action="store_false",
        default=True,
        help="Skip the render-plantuml gate (useful for unit-test runs).",
    )
    parser.add_argument(
        "--stop-on-first-failure",
        action="store_true",
        help=(
            "Halt the gate run after the first failing gate. By "
            "default every gate is run so the block report is "
            "comprehensive."
        ),
    )

    return parser


def _print_gate_listing(gates: Sequence[Gate]) -> None:
    width = max((len(g.name) for g in gates), default=0)
    for g in gates:
        print(f"  {g.name:<{width}}  {g.description}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    run_started_monotonic = time.monotonic()
    run_started_at = datetime.now(timezone.utc)

    runtime_model_id = _resolve_runtime_id(
        args.runtime_model_id, "RUNTIME_BEDROCK_MODEL_ID"
    )
    runtime_voice_id = _resolve_runtime_id(
        args.runtime_voice_id, "RUNTIME_POLLY_VOICE_ID"
    )

    gates = build_default_gates(
        plan_path=args.plan,
        test_plan_path=args.test_plan,
        cost_report_path=args.cost_report,
        runtime_model_id=runtime_model_id,
        runtime_voice_id=runtime_voice_id,
        current_phase=args.current_phase,
        branch=args.branch,
        include_render_plantuml=args.include_render_plantuml,
    )

    if args.list_gates:
        _print_gate_listing(gates)
        return EXIT_OK

    # ---- self-health probe ------------------------------------------------
    try:
        signal = emit_self_health(
            run_started_monotonic=run_started_monotonic,
            out_path=args.self_health_out,
            deadline_seconds=args.self_health_deadline_seconds,
        )
    except TimeoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    print(
        f"production_gate self-health: {signal['status']} "
        f"@ {signal['emitted_at_utc']} "
        f"(elapsed {signal['elapsed_seconds']:.4f}s, budget "
        f"{signal['deadline_seconds']:.1f}s)"
    )

    if args.health_check:
        return EXIT_OK

    # ---- gate selection ---------------------------------------------------
    if args.gate is not None:
        selected = [g for g in gates if g.name == args.gate]
        if not selected:
            available = ", ".join(g.name for g in gates)
            print(
                f"ERROR: unknown gate {args.gate!r}; available: "
                f"{available}",
                file=sys.stderr,
            )
            return EXIT_INTERNAL_ERROR
    else:
        selected = list(gates)

    # ---- run gates --------------------------------------------------------
    def _live(result: GateResult) -> None:
        marker = {
            "passed": "PASS",
            "failed": "FAIL",
            "timeout": "TIME",
            "error": "ERR ",
            "skipped": "SKIP",
        }.get(result.status, "????")
        print(
            f"[{marker}] {result.name:<20} "
            f"({result.duration_seconds:.2f}s)"
        )

    results = run_all_gates(
        selected,
        on_result=_live,
        stop_on_first_failure=args.stop_on_first_failure,
    )

    blocking = [r for r in results if r.blocking]

    # ---- report (R12.7) ---------------------------------------------------
    report_started_monotonic = time.monotonic()
    report = format_block_report(
        results, run_started_at, fmt=args.report_format
    )
    report_elapsed = time.monotonic() - report_started_monotonic

    print(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")

    if report_elapsed > args.report_deadline_seconds:
        print(
            f"ERROR: block-report emission took {report_elapsed:.2f}s, "
            f"exceeding the {args.report_deadline_seconds:.1f}s SLA "
            "(R12.7).",
            file=sys.stderr,
        )
        return EXIT_REPORT_DEADLINE

    return EXIT_BLOCKED if blocking else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
