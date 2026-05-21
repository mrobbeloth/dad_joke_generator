"""Smoke test for ``scripts/render_plantuml.sh``.

This test exercises the real PlantUML rendering toolchain end-to-end:
it shells out to the bash script, lets it pull/run the official
``plantuml/plantuml`` Docker image, and verifies that all three required
architecture diagrams (component, deployment, sequence) are emitted as
both PNG and SVG artifacts under ``docs/architecture/``.

Validates Requirements 10.2 and 10.5:
- 10.2: The Architecture_Document includes exactly three required
  diagrams (component, deployment, sequence).
- 10.5: When all three required diagrams are present and render
  successfully, the Build_Pipeline publishes the rendered diagrams.
  This smoke test is the local equivalent of that publish gate: it
  proves the renderer produces the artifacts the pipeline would
  publish.

The test is intentionally NOT mocked. Its purpose is to catch real
breakage of the rendering pipeline (Docker image tag drift, script
regressions, syntax errors in the .puml sources). It skips cleanly in
environments that lack the required toolchain (no ``bash``, no
``docker`` on PATH, or unreachable Docker daemon) so it does not
produce false negatives in sandboxes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Repo root resolved relative to this test file, so the test runs the
# same way regardless of pytest's invocation directory.
REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_SCRIPT = REPO_ROOT / "scripts" / "render_plantuml.sh"
ARCH_DIR = REPO_ROOT / "docs" / "architecture"

# The three required diagrams from Requirement 10.2.
REQUIRED_DIAGRAMS = ("component", "deployment", "sequence")

# Generous overall budget: 3 diagrams * 2 formats * up to 120 s each
# (script-enforced per-file timeout) plus image-pull overhead on a cold
# cache. 600 s leaves headroom without letting a true hang block CI
# indefinitely.
RENDER_TIMEOUT_SECONDS = 600


def _docker_available() -> bool:
    """Return True iff a working Docker daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _find_usable_bash() -> str | None:
    """Locate a bash interpreter that can execute a script at a native path.

    On Windows, ``shutil.which("bash")`` often resolves to the WSL launcher
    (``C:\\Windows\\System32\\bash.exe``), which translates POSIX paths but
    cannot directly invoke a script referenced by a native Windows path.
    We probe candidates by asking each bash to ``test -f`` the render
    script's actual filesystem path; only an interpreter that can see the
    script is considered usable.
    """
    candidates: list[str] = []

    if sys.platform == "win32":
        # Prefer Git Bash, which understands native Windows paths.
        for env_var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var)
            if not base:
                continue
            for rel in (r"Git\bin\bash.exe", r"Git\usr\bin\bash.exe"):
                candidates.append(str(Path(base) / rel))

    path_bash = shutil.which("bash")
    if path_bash:
        candidates.append(path_bash)

    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.lower() if sys.platform == "win32" else candidate
        if key in seen:
            continue
        seen.add(key)
        if not Path(candidate).is_file():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", f'test -f "{RENDER_SCRIPT.as_posix()}"'],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


def test_render_plantuml_produces_all_required_diagrams() -> None:
    """Running ``scripts/render_plantuml.sh`` emits PNG+SVG for every required diagram."""
    if not RENDER_SCRIPT.is_file():
        pytest.fail(f"render script missing: {RENDER_SCRIPT}")

    bash = _find_usable_bash()
    if bash is None:
        pytest.skip(
            "no usable bash interpreter found; install Git Bash (Windows) or "
            "ensure /bin/bash is on PATH (Linux/macOS) to run this smoke test"
        )

    if not _docker_available():
        pytest.skip(
            "Docker daemon not reachable; this smoke test requires Docker to invoke "
            "the official plantuml/plantuml image"
        )

    # Run the script from the repo root so its relative-path assumptions
    # (REPO_ROOT discovery via BASH_SOURCE) hold regardless of the
    # caller's cwd.
    completed = subprocess.run(
        [bash, str(RENDER_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=RENDER_TIMEOUT_SECONDS,
        env=os.environ.copy(),
    )

    # Surface stdout+stderr in the assertion message so a CI failure is
    # debuggable without re-running the script manually.
    debug_blob = (
        f"exit_code={completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}"
    )

    assert completed.returncode == 0, (
        f"render_plantuml.sh exited non-zero.\n{debug_blob}"
    )

    missing: list[str] = []
    empty: list[str] = []
    for name in REQUIRED_DIAGRAMS:
        for ext in ("png", "svg"):
            artifact = ARCH_DIR / f"{name}.{ext}"
            if not artifact.is_file():
                missing.append(str(artifact.relative_to(REPO_ROOT)))
                continue
            if artifact.stat().st_size <= 0:
                empty.append(str(artifact.relative_to(REPO_ROOT)))

    assert not missing, (
        "render_plantuml.sh exited 0 but the following required artifacts are "
        f"missing: {missing}\n{debug_blob}"
    )
    assert not empty, (
        "render_plantuml.sh exited 0 but the following required artifacts are "
        f"empty (0 bytes): {empty}\n{debug_blob}"
    )
