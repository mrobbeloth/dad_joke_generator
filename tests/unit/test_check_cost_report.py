"""Unit tests for ``scripts/check_cost_report.py``.

Validates: Requirement 12.6 — the Production_Gate SHALL block deployment
when the Cost_Report is missing, or when the recorded model/voice
selection does not match the configured runtime values. Property 25 in
``design.md`` formalizes the equality check this module enforces.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ is intentionally not a Python package (no ``__init__.py``);
# load the module directly so tests do not depend on PYTHONPATH gymnastics.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_cost_report.py"
_spec = importlib.util.spec_from_file_location(
    "check_cost_report", _SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
check_cost_report = importlib.util.module_from_spec(_spec)
sys.modules["check_cost_report"] = check_cost_report
_spec.loader.exec_module(check_cost_report)


# --------------------------------------------------------------------------
# Fixtures: synthetic Cost_Report markdown
# --------------------------------------------------------------------------

_RECOMMENDATION_TABLE = """\
# Cost Report

## 5. Recommendation

### 5.1 Recommended Bedrock model

**`amazon.nova-lite-v1:0`**

Justification: cheapest in every traffic band.

### 5.2 Recommended Polly voice

**`Joanna`** (Polly standard engine, US English)

### 5.3 Recommendation summary

| Decision | Value |
|---|---|
| Default Bedrock model id | `amazon.nova-lite-v1:0` |
| Default Polly voice id | `Joanna` |
| Default Polly engine | `standard` |
"""

_BOLD_ONLY = """\
# Cost Report

## 5. Recommendation

### 5.1 Recommended Bedrock model

**`amazon.nova-lite-v1:0`**

### 5.2 Recommended Polly voice

**`Joanna`**
"""

_NO_RECOMMENDATION = """\
# Cost Report

## 1. Workload assumptions

Lots of prose about tokens but no recommendation section.
"""

_PARTIAL_ONLY_MODEL = """\
# Cost Report

### 5.3 Recommendation summary

| Decision | Value |
|---|---|
| Default Bedrock model id | `amazon.nova-lite-v1:0` |
"""


# --------------------------------------------------------------------------
# extract_recommended_ids
# --------------------------------------------------------------------------


def test_extract_ids_from_recommendation_table() -> None:
    model_id, voice_id = check_cost_report.extract_recommended_ids(
        _RECOMMENDATION_TABLE
    )
    assert model_id == "amazon.nova-lite-v1:0"
    assert voice_id == "Joanna"


def test_extract_ids_falls_back_to_bolded_recommendation() -> None:
    model_id, voice_id = check_cost_report.extract_recommended_ids(_BOLD_ONLY)
    assert model_id == "amazon.nova-lite-v1:0"
    assert voice_id == "Joanna"


def test_extract_ids_returns_none_when_recommendation_missing() -> None:
    model_id, voice_id = check_cost_report.extract_recommended_ids(
        _NO_RECOMMENDATION
    )
    assert model_id is None
    assert voice_id is None


def test_extract_ids_returns_none_for_missing_voice_only() -> None:
    model_id, voice_id = check_cost_report.extract_recommended_ids(
        _PARTIAL_ONLY_MODEL
    )
    assert model_id == "amazon.nova-lite-v1:0"
    assert voice_id is None


# --------------------------------------------------------------------------
# check_consistency
# --------------------------------------------------------------------------


def _write_report(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "COST_REPORT.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_check_consistency_passes_when_ids_match(tmp_path: Path) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    result = check_cost_report.check_consistency(
        path,
        runtime_model_id="amazon.nova-lite-v1:0",
        runtime_voice_id="Joanna",
    )
    assert result.ok is True
    assert result.errors == []
    assert result.cost_report_model_id == "amazon.nova-lite-v1:0"
    assert result.cost_report_voice_id == "Joanna"


def test_check_consistency_fails_on_model_mismatch(tmp_path: Path) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    result = check_cost_report.check_consistency(
        path,
        runtime_model_id="anthropic.claude-3-haiku-20240307-v1:0",
        runtime_voice_id="Joanna",
    )
    assert result.ok is False
    assert len(result.errors) == 1
    msg = result.errors[0]
    assert "model" in msg.lower()
    assert "amazon.nova-lite-v1:0" in msg
    assert "anthropic.claude-3-haiku-20240307-v1:0" in msg


def test_check_consistency_fails_on_voice_mismatch(tmp_path: Path) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    result = check_cost_report.check_consistency(
        path,
        runtime_model_id="amazon.nova-lite-v1:0",
        runtime_voice_id="Matthew",
    )
    assert result.ok is False
    assert len(result.errors) == 1
    msg = result.errors[0]
    assert "voice" in msg.lower()
    assert "Joanna" in msg
    assert "Matthew" in msg


def test_check_consistency_reports_both_mismatches(tmp_path: Path) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    result = check_cost_report.check_consistency(
        path,
        runtime_model_id="anthropic.claude-3-haiku-20240307-v1:0",
        runtime_voice_id="Matthew",
    )
    assert result.ok is False
    assert len(result.errors) == 2
    joined = "\n".join(result.errors).lower()
    assert "model" in joined
    assert "voice" in joined


def test_check_consistency_fails_when_report_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.md"
    result = check_cost_report.check_consistency(
        missing,
        runtime_model_id="amazon.nova-lite-v1:0",
        runtime_voice_id="Joanna",
    )
    assert result.ok is False
    assert len(result.errors) == 1
    assert "not found" in result.errors[0].lower()
    assert str(missing) in result.errors[0]


def test_check_consistency_fails_when_recommendation_missing(
    tmp_path: Path,
) -> None:
    path = _write_report(tmp_path, _NO_RECOMMENDATION)
    result = check_cost_report.check_consistency(
        path,
        runtime_model_id="amazon.nova-lite-v1:0",
        runtime_voice_id="Joanna",
    )
    assert result.ok is False
    # Both model and voice are missing from the report
    assert len(result.errors) == 2
    joined = "\n".join(result.errors).lower()
    assert "missing recommended bedrock model" in joined
    assert "missing recommended polly voice" in joined


# --------------------------------------------------------------------------
# CLI ``main`` smoke
# --------------------------------------------------------------------------


def test_main_returns_zero_on_match(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    # Strip env so CLI args are the only source.
    monkeypatch.delenv("RUNTIME_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("RUNTIME_POLLY_VOICE_ID", raising=False)

    rc = check_cost_report.main(
        [
            "--cost-report",
            str(path),
            "--runtime-model-id",
            "amazon.nova-lite-v1:0",
            "--runtime-voice-id",
            "Joanna",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "consistency check passed" in captured.out
    assert "amazon.nova-lite-v1:0" in captured.out
    assert "Joanna" in captured.out


def test_main_returns_one_on_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    monkeypatch.delenv("RUNTIME_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("RUNTIME_POLLY_VOICE_ID", raising=False)

    rc = check_cost_report.main(
        [
            "--cost-report",
            str(path),
            "--runtime-model-id",
            "anthropic.claude-3-haiku-20240307-v1:0",
            "--runtime-voice-id",
            "Joanna",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "ERROR" in captured.err


def test_main_falls_back_to_env_vars(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    monkeypatch.setenv("RUNTIME_BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    monkeypatch.setenv("RUNTIME_POLLY_VOICE_ID", "Joanna")

    rc = check_cost_report.main(["--cost-report", str(path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "consistency check passed" in captured.out


def test_main_returns_two_when_runtime_ids_unresolved(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_report(tmp_path, _RECOMMENDATION_TABLE)
    monkeypatch.delenv("RUNTIME_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("RUNTIME_POLLY_VOICE_ID", raising=False)

    rc = check_cost_report.main(["--cost-report", str(path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "ERROR" in captured.err
