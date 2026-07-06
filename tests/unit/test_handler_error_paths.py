"""Unit tests for the ``POST /v1/jokes`` handler error paths.

Validates:

* **R1.5** -- Bedrock generation failure surfaces as a sanitized 503 with no
  partial joke content.
* **R1.7** -- request validation rejects malformed bodies with HTTP 400 and
  a structured ``rule`` field.
* **R3.2** -- not-Family_Friendly input triggers HTTP 400 ``moderation``
  before any Bedrock call is made.
* **R3.6** -- moderator unavailability surfaces as HTTP 503
  ``moderation_unavailable``.
* **R3.7** -- moderator timeout surfaces as HTTP 504
  ``moderation_timeout``.
* **R4.5** -- output-moderator soft-fail behaviour is exercised by the
  Bedrock-failure / Polly-failure tests as supporting evidence (the handler
  enforces the stricter "no joke text on hard failure" envelope).
* **R5.3** -- rate-limit rejections surface as HTTP 429 with
  ``resetAtUtc``; rate-limiter unavailability surfaces as HTTP 503.
* **R7.5** -- every error response is sanitized: no Tracebacks, no AWS
  ARNs, no file paths, no AWS account IDs.
* **R18.5** -- persistence failures soft-fail; the visitor still gets the
  joke text.

The test cases also serve as supporting evidence for the following
correctness properties (full PBT coverage lives in the corresponding
property-test files):

* **Property 3** -- Bedrock failure produces 503 with no partial content.
* **Property 8** -- moderation gate precedes Bedrock for all rejected
  inputs.
* **Property 10** -- moderator unavailability fails closed.
* **Property 14** -- rate-limit counters increment only on success and
  soft-fail when the increment write fails.
* **Property 15** -- limit-reached requests are rejected with HTTP 429.
* **Property 20** -- error responses are sanitized.
* **Property 43** -- persistence failures do not affect the visitor
  response.

The tests use plain pytest with ``monkeypatch`` (no Hypothesis -- they are
example-based assertions about specific error paths). Stubs are
hand-rolled rather than ``MagicMock`` so a future refactor that changes
the call signature surfaces as a TypeError rather than a silently passing
test (matches the pattern in
``tests/unit/test_voice_synthesizer_polly_kwargs.py``).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from joke_api import (
    client_ip,
    handler,
    input_moderator,
    joke_generator,
    joke_store,
    observability,
    rate_limiter,
    request_validator,
    voice_synthesizer,
)
from joke_api.observability import LogRecord


# ---------------------------------------------------------------------------
# Shared fixtures and stubs
# ---------------------------------------------------------------------------


_DAILY_LIMIT: int = 5
_VALID_IP: str = "203.0.113.1"
_VALID_IP_HASH = "a" * 64
_GOOD_JOKE_TEXT: str = (
    "This is a perfectly clean dad joke that contains plenty of words "
    "to satisfy the length validator."
)


def _make_fake_config() -> SimpleNamespace:
    """Build the fake :class:`Config` used by every test in this file.

    Uses :class:`types.SimpleNamespace` so we sidestep
    :func:`joke_api.config.load`'s SSM dependency and can scope the fake
    to a single test without touching the frozen ``Config`` dataclass.
    """
    return SimpleNamespace(
        bedrock_model_id="amazon.nova-lite-v1:0",
        polly_voice_id="Joanna",
        daily_limit=_DAILY_LIMIT,
        ad_module_enabled=False,
        ad_network_id="",
        ip_hash_salt="x" * 64,
        cost_alarm_threshold_usd=10.0,
        rights_confirmed=False,
    )


class _ObservabilityCapture:
    """Track-only capture for ``emit_log`` / ``emit_metric`` calls.

    Mirrors the capture pattern used by
    ``tests/property/test_observability_property.py`` but pared down to
    the two methods this file asserts against.
    """

    __slots__ = ("logs", "metrics")

    def __init__(self) -> None:
        self.logs: list[LogRecord] = []
        self.metrics: list[tuple[str, float]] = []

    def emit_log(self, record: LogRecord) -> None:
        self.logs.append(record)

    def emit_metric(
        self,
        name: str,
        value: float = 1.0,
        unit: str = "Count",
        *,
        dimensions: dict[str, str] | None = None,
        cloudwatch_client: Any | None = None,
    ) -> None:  # noqa: D401 - track-only stub
        self.metrics.append((name, value))


class _CallTracker:
    """Hand-rolled call tracker for module-level functions.

    A ``__call__`` returning a fixed value is preferred over ``MagicMock``
    so a signature mismatch raises ``TypeError`` immediately. Tests use
    :attr:`call_count` to assert "this stage was never reached".
    """

    __slots__ = ("call_count", "_return_value", "_raise_exc")

    def __init__(
        self,
        return_value: Any = None,
        *,
        raise_exc: BaseException | None = None,
    ) -> None:
        self.call_count = 0
        self._return_value = return_value
        self._raise_exc = raise_exc

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._return_value


@pytest.fixture(autouse=True)
def _reset_handler_module_state() -> Any:
    """Reset handler-module caches and observability counter per test.

    The autouse fixture ensures cross-test state never leaks: every test
    starts with a fresh config cache (so it is forced to install its own
    fake ``Config`` if it hits :func:`handler._get_config`) and a zeroed
    soft-fail counter.
    """
    handler._reset_config_cache()
    observability.reset_failure_count()
    yield
    handler._reset_config_cache()
    observability.reset_failure_count()


@pytest.fixture
def fake_cfg(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a stub ``Config`` so :func:`handler._get_config` is SSM-free.

    Bypassing :func:`joke_api.config.load` keeps these unit tests fully
    offline and removes a coupling between the handler-error-path suite
    and the SSM-loading code under test in task 5.x.
    """
    cfg = _make_fake_config()
    monkeypatch.setattr(handler, "_CFG", cfg)
    return cfg


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _ObservabilityCapture:
    """Capture every ``emit_log`` / ``emit_metric`` call.

    Patches the two emitters at the ``joke_api.observability`` module
    level (the symbol the handler reaches through ``observability.emit_*``
    name lookup). The capture is returned so tests can assert on
    ``capture.logs`` and ``capture.metrics`` directly.
    """
    capture = _ObservabilityCapture()
    monkeypatch.setattr(observability, "emit_log", capture.emit_log)
    monkeypatch.setattr(observability, "emit_metric", capture.emit_metric)
    return capture


def _make_event(
    seed_words: list[str] | None,
    *,
    ip: str | None = _VALID_IP,
    body_override: str | None = None,
) -> dict[str, Any]:
    """Build an API Gateway HTTP API v2 ``POST /v1/jokes`` event.

    Args:
        seed_words: Seed-word list to JSON-encode into the body. Pass an
            empty list for the zero-seed-word path or a list with
            invalid characters to drive the validation-rejection path.
            Ignored when ``body_override`` is supplied.
        ip: Address to put in ``X-Forwarded-For``. Pass ``None`` to omit
            the header entirely (drives the
            ``client_ip_unresolvable`` path).
        body_override: Pre-encoded JSON string used verbatim as the
            request body. Supports tests that need a malformed body
            without going through ``json.dumps``.

    Returns:
        A dict shaped like an API Gateway HTTP API v2 event with only
        the fields the handler actually consults (``routeKey``,
        ``headers``, ``body``).
    """
    headers: dict[str, str] = {}
    if ip is not None:
        headers["X-Forwarded-For"] = ip
    if body_override is not None:
        body = body_override
    else:
        body = json.dumps({"seedWords": seed_words or []})
    return {
        "routeKey": "POST /v1/jokes",
        "headers": headers,
        "body": body,
    }


# Patterns banned in every error response body per R7.5 / Property 20.
# Captured here so the parametrized sanitization test (Test 13) can
# share them with the per-path assertions throughout the file.
_BANNED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Traceback", re.IGNORECASE),
    re.compile(r"arn:aws:"),
    # Bare unix-style file path with .py suffix.
    re.compile(r"/[A-Za-z][A-Za-z_/]*\.py"),
    # 12-digit AWS account ids.
    re.compile(r"\b\d{12}\b"),
)


def _assert_body_sanitized(body_str: str) -> None:
    """Assert the response body is free of internal text leaks (R7.5)."""
    for pattern in _BANNED_PATTERNS:
        assert pattern.search(body_str) is None, (
            f"sanitization invariant broken: pattern {pattern.pattern!r} "
            f"matched body {body_str!r}"
        )


# ---------------------------------------------------------------------------
# Test 1: validation error -> 400
# ---------------------------------------------------------------------------


class TestValidationPath:
    """R1.7, R7.5 -- malformed seed words short-circuit before any AWS call."""

    def test_invalid_charset_returns_400_with_rule(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        # Tracking stubs on every downstream stage so we can assert the
        # validation error short-circuits before any of them is called.
        rate_check = _CallTracker(return_value=0)
        input_classify = _CallTracker()
        bedrock_generate = _CallTracker(return_value=_GOOD_JOKE_TEXT)
        polly_synthesize = _CallTracker()
        store_persist = _CallTracker()
        monkeypatch.setattr(handler.rate_limiter, "check", rate_check)
        monkeypatch.setattr(
            handler.input_moderator, "classify", input_classify
        )
        monkeypatch.setattr(
            handler.joke_generator, "generate", bedrock_generate
        )
        monkeypatch.setattr(
            handler.voice_synthesizer, "synthesize", polly_synthesize
        )
        monkeypatch.setattr(handler.joke_store, "persist", store_persist)

        # ``bad@word`` violates the seed-word charset rule (only
        # [A-Za-z0-9'-] permitted, no '@').
        event = _make_event(seed_words=["bad@word", "more$"])
        response = handler.lambda_handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == "validation"
        assert body["rule"] == request_validator.seed_word_charset
        _assert_body_sanitized(response["body"])

        # Validation short-circuits the pipeline.
        assert rate_check.call_count == 0
        assert input_classify.call_count == 0
        assert bedrock_generate.call_count == 0
        assert polly_synthesize.call_count == 0
        assert store_persist.call_count == 0

        # Exactly one structured log record with the design-mandated
        # decision string for an error path.
        assert len(capture.logs) == 1
        assert capture.logs[0].decision == "error"


# ---------------------------------------------------------------------------
# Test 2: client IP unresolvable -> 400
# ---------------------------------------------------------------------------


class TestClientIpUnresolvablePath:
    """R5.9, R7.5 -- missing X-Forwarded-For header is rejected."""

    def test_missing_xff_returns_client_ip_unresolvable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        # Patch every downstream stage with a tracker so we can assert
        # they were never reached.
        rate_check = _CallTracker(return_value=0)
        input_classify = _CallTracker()
        bedrock_generate = _CallTracker(return_value=_GOOD_JOKE_TEXT)
        polly_synthesize = _CallTracker()
        monkeypatch.setattr(handler.rate_limiter, "check", rate_check)
        monkeypatch.setattr(
            handler.input_moderator, "classify", input_classify
        )
        monkeypatch.setattr(
            handler.joke_generator, "generate", bedrock_generate
        )
        monkeypatch.setattr(
            handler.voice_synthesizer, "synthesize", polly_synthesize
        )

        event = _make_event(seed_words=["hello"], ip=None)
        response = handler.lambda_handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == "client_ip_unresolvable"
        _assert_body_sanitized(response["body"])

        assert rate_check.call_count == 0
        assert input_classify.call_count == 0
        assert bedrock_generate.call_count == 0
        assert polly_synthesize.call_count == 0


# ---------------------------------------------------------------------------
# Tests 3 & 4: rate-limit paths
# ---------------------------------------------------------------------------


class TestRateLimitPaths:
    """R5.3, R7.5 -- rate-limit gate maps to 429 / 503 cleanly."""

    def test_rate_limit_exceeded_returns_429_with_reset_at(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        def _raise_exceeded(
            ip_hash: str, day: str, limit: int, **_: Any
        ) -> int:
            raise rate_limiter.RateLimitExceeded(
                ip_hash, day, count=10, limit=limit
            )

        bedrock_generate = _CallTracker(return_value=_GOOD_JOKE_TEXT)
        polly_synthesize = _CallTracker()
        monkeypatch.setattr(handler.rate_limiter, "check", _raise_exceeded)
        monkeypatch.setattr(
            handler.joke_generator, "generate", bedrock_generate
        )
        monkeypatch.setattr(
            handler.voice_synthesizer, "synthesize", polly_synthesize
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 429
        body = json.loads(response["body"])
        assert body["error"] == "rate_limited"
        # ``resetAtUtc`` must be the canonical next-midnight UTC string
        # so the SPA can render an unambiguous retry hint (Property 16).
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T00:00:00Z", body["resetAtUtc"]
        )
        _assert_body_sanitized(response["body"])

        # The rate-limit-rejection metric is the only metric recorded on
        # this path (Property 15 / R16.2).
        assert (
            observability.METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR,
            1.0,
        ) in capture.metrics

        # Nothing downstream of the gate was invoked.
        assert bedrock_generate.call_count == 0
        assert polly_synthesize.call_count == 0

        # The structured log carries decision="rate_limited" (R16.1).
        assert len(capture.logs) == 1
        assert capture.logs[0].decision == "rate_limited"

    def test_rate_limiter_unavailable_returns_503(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        def _raise_unavailable(
            ip_hash: str, day: str, limit: int, **_: Any
        ) -> int:
            raise rate_limiter.RateLimiterUnavailable("check", "ddb timeout")

        monkeypatch.setattr(
            handler.rate_limiter, "check", _raise_unavailable
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 503
        body = json.loads(response["body"])
        assert body["error"] == "unavailable"
        _assert_body_sanitized(response["body"])


# ---------------------------------------------------------------------------
# Tests 5, 6, 7: moderation paths
# ---------------------------------------------------------------------------


class TestModerationPaths:
    """R3.2, R3.6, R3.7 -- moderation gate fails closed before Bedrock."""

    @staticmethod
    def _patch_rate_check_passthrough(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Make the rate-limit check a no-op so moderation runs."""
        monkeypatch.setattr(
            handler.rate_limiter,
            "check",
            lambda *_a, **_kw: 0,
        )

    def test_input_moderation_rejection_returns_400(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        self._patch_rate_check_passthrough(monkeypatch)

        bedrock_generate = _CallTracker(return_value=_GOOD_JOKE_TEXT)
        polly_synthesize = _CallTracker()
        monkeypatch.setattr(
            handler.joke_generator, "generate", bedrock_generate
        )
        monkeypatch.setattr(
            handler.voice_synthesizer, "synthesize", polly_synthesize
        )

        rejection = input_moderator.ModerationResult(
            family_friendly=False,
            reason="denylist:test",
            latency_ms=10,
        )
        monkeypatch.setattr(
            handler.input_moderator,
            "classify",
            lambda _text, **_kw: rejection,
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == "moderation"
        _assert_body_sanitized(response["body"])

        # Property 8: moderation precedes Bedrock; on rejection neither
        # Bedrock nor Polly is invoked.
        assert bedrock_generate.call_count == 0
        assert polly_synthesize.call_count == 0

        assert (
            observability.METRIC_MODERATION_REJECTIONS_PER_HOUR,
            1.0,
        ) in capture.metrics
        assert capture.logs[-1].decision == "moderation_rejected"

    def test_input_moderation_timeout_returns_504(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        self._patch_rate_check_passthrough(monkeypatch)

        bedrock_generate = _CallTracker(return_value=_GOOD_JOKE_TEXT)
        monkeypatch.setattr(
            handler.joke_generator, "generate", bedrock_generate
        )

        def _raise_timeout(_text: str, **_kw: Any) -> Any:
            raise input_moderator.ModerationTimeout(3000)

        monkeypatch.setattr(
            handler.input_moderator, "classify", _raise_timeout
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 504
        body = json.loads(response["body"])
        assert body["error"] == "moderation_timeout"
        _assert_body_sanitized(response["body"])

        # Property 10: moderator unavailability fails closed; Bedrock is
        # never invoked.
        assert bedrock_generate.call_count == 0
        assert (
            observability.METRIC_MODERATION_REJECTIONS_PER_HOUR,
            1.0,
        ) in capture.metrics

    def test_input_moderation_unavailable_returns_503(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        self._patch_rate_check_passthrough(monkeypatch)

        bedrock_generate = _CallTracker(return_value=_GOOD_JOKE_TEXT)
        monkeypatch.setattr(
            handler.joke_generator, "generate", bedrock_generate
        )

        def _raise_unavailable(_text: str, **_kw: Any) -> Any:
            raise input_moderator.ModerationUnavailable(
                "detect_toxic_content", "boto error"
            )

        monkeypatch.setattr(
            handler.input_moderator, "classify", _raise_unavailable
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 503
        body = json.loads(response["body"])
        assert body["error"] == "moderation_unavailable"
        _assert_body_sanitized(response["body"])

        # Property 10: moderator unavailability fails closed.
        assert bedrock_generate.call_count == 0


# ---------------------------------------------------------------------------
# Test 8: Bedrock generation failure -> 503
# ---------------------------------------------------------------------------


def _patch_through_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch every pipeline stage from rate-limit through input moderation
    so the test can drive a downstream-stage error path."""
    monkeypatch.setattr(
        handler.rate_limiter, "check", lambda *_a, **_kw: 0
    )
    monkeypatch.setattr(
        handler.input_moderator,
        "classify",
        lambda _t, **_kw: input_moderator.ModerationResult(
            family_friendly=True,
            reason=None,
            latency_ms=1,
        ),
    )
    monkeypatch.setattr(
        handler.training_corpus,
        "load_few_shot",
        lambda **_kw: [],
    )


class TestBedrockFailurePath:
    """R1.5 / Property 3 -- Bedrock failure produces 503 with no joke text."""

    def test_bedrock_unavailable_returns_503_with_no_joke_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        _patch_through_validation(monkeypatch)

        def _raise_bedrock(*_a: Any, **_kw: Any) -> str:
            raise joke_generator.JokeGenerationUnavailable(
                "converse", "boto error"
            )

        polly_synthesize = _CallTracker()
        monkeypatch.setattr(
            handler.joke_generator, "generate", _raise_bedrock
        )
        monkeypatch.setattr(
            handler.voice_synthesizer, "synthesize", polly_synthesize
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 503
        body = json.loads(response["body"])
        assert body["error"] == "unavailable"
        # Property 3 / R1.5: no joke text or id leaks into the body.
        assert "id" not in body
        assert "text" not in body
        _assert_body_sanitized(response["body"])

        # Polly must not be invoked when Bedrock fails outright.
        assert polly_synthesize.call_count == 0


# ---------------------------------------------------------------------------
# Tests 9-11: soft-fail paths (still 200)
# ---------------------------------------------------------------------------


def _stub_passing_output_moderator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make :func:`output_moderator.classify` always return G/PG-friendly.

    Lets the soft-fail tests below skip straight from generation to the
    voice / persist / increment stages.
    """
    monkeypatch.setattr(
        handler.output_moderator,
        "classify",
        lambda _candidate, **_kw: input_moderator.ModerationResult(
            family_friendly=True,
            reason=None,
            latency_ms=1,
        ),
    )


def _stub_successful_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch generator + output moderator so the joke is "approved"."""
    monkeypatch.setattr(
        handler.joke_generator,
        "generate",
        lambda *_a, **_kw: _GOOD_JOKE_TEXT,
    )
    _stub_passing_output_moderator(monkeypatch)


class TestSoftFailPaths:
    """R2.6, R2.7, R5.4, R18.5 -- post-Bedrock failures must not block 200."""

    def test_polly_soft_fail_keeps_200_with_audio_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        _patch_through_validation(monkeypatch)
        _stub_successful_generation(monkeypatch)

        # Polly soft-fails -- ``audio_available=False`` is the only state
        # required by the response builder; the URL must be ``None`` per
        # the contract documented on :class:`SynthesisResult`.
        monkeypatch.setattr(
            handler.voice_synthesizer,
            "synthesize",
            lambda *_a, **_kw: voice_synthesizer.SynthesisResult(
                audio_url=None,
                audio_available=False,
                error="polly_unavailable",
            ),
        )

        # Persistence and rate-limit increment succeed so the only
        # variable on this path is the synthesis outcome.
        monkeypatch.setattr(
            handler.joke_store, "persist", lambda *_a, **_kw: None
        )
        monkeypatch.setattr(
            handler.rate_limiter, "increment", lambda *_a, **_kw: 1
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["audioAvailable"] is False
        assert body["audioUrl"] is None
        # R2.10: no download URL when audio is unavailable.
        assert body["audioDownloadUrl"] is None
        assert body["text"] == _GOOD_JOKE_TEXT

        # Successful joke generation still bumps the success metric --
        # audio is a soft attribute (R2.7).
        assert (
            observability.METRIC_JOKES_PER_HOUR,
            1.0,
        ) in capture.metrics
        assert capture.logs[-1].decision == "accepted"

    def test_persistence_soft_fail_still_returns_200_with_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        _patch_through_validation(monkeypatch)
        _stub_successful_generation(monkeypatch)

        # Synthesis succeeds so the audio fields reflect a real URL,
        # including the R2.10 download variant.
        monkeypatch.setattr(
            handler.voice_synthesizer,
            "synthesize",
            lambda *_a, **_kw: voice_synthesizer.SynthesisResult(
                audio_url="https://example.invalid/audio.mp3",
                audio_available=True,
                error=None,
                audio_download_url="https://example.invalid/audio.mp3?dl=1",
            ),
        )

        # ``joke_store.persist`` raises, exercising the R18.5 / Property 43
        # soft-fail branch in the handler.
        def _raise_persist(record: Any, **_kw: Any) -> None:
            raise joke_store.JokeStorePersistError(record.id, "ddb error")

        monkeypatch.setattr(handler.joke_store, "persist", _raise_persist)
        monkeypatch.setattr(
            handler.rate_limiter, "increment", lambda *_a, **_kw: 1
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["text"] == _GOOD_JOKE_TEXT
        assert body["audioAvailable"] is True
        assert body["audioUrl"] == "https://example.invalid/audio.mp3"
        # R2.10: the download URL flows through the POST success path.
        assert body["audioDownloadUrl"] == "https://example.invalid/audio.mp3?dl=1"

    def test_rate_limit_increment_soft_fail_returns_conservative_remaining(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        _patch_through_validation(monkeypatch)
        _stub_successful_generation(monkeypatch)

        monkeypatch.setattr(
            handler.voice_synthesizer,
            "synthesize",
            lambda *_a, **_kw: voice_synthesizer.SynthesisResult(
                audio_url="https://example.invalid/audio.mp3",
                audio_available=True,
                error=None,
            ),
        )
        monkeypatch.setattr(
            handler.joke_store, "persist", lambda *_a, **_kw: None
        )

        # Increment soft-fails AFTER the joke succeeded; per
        # ``_compute_remaining`` the visitor should see
        # ``daily_limit - 1`` (Property 14, soft-fail half).
        def _raise_increment(*_a: Any, **_kw: Any) -> int:
            raise rate_limiter.RateLimiterUnavailable(
                "increment", "ddb error"
            )

        monkeypatch.setattr(
            handler.rate_limiter, "increment", _raise_increment
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["remaining"] == fake_cfg.daily_limit - 1


# ---------------------------------------------------------------------------
# Test 12: last-resort sanitizer for unexpected exceptions
# ---------------------------------------------------------------------------


class TestUnexpectedException:
    """R7.5 / Property 20 -- the outer try/except is the last-resort sanitizer."""

    def test_runtime_error_in_validator_returns_sanitized_503(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        # ``RuntimeError`` is NOT a ``ValidationError``, so the inner
        # catch in ``_handle_post_jokes`` lets it escape; the outer
        # ``except Exception`` in ``lambda_handler`` is what must
        # catch it and return a sanitized 503.
        def _raise_runtime(_event: dict) -> list[str]:
            raise RuntimeError("unexpected internal failure: arn:aws:secret")

        monkeypatch.setattr(
            handler.request_validator, "validate", _raise_runtime
        )

        response = handler.lambda_handler(
            _make_event(seed_words=["hello"]), None
        )

        assert response["statusCode"] == 503
        body = json.loads(response["body"])
        assert body["error"] == "unavailable"
        _assert_body_sanitized(response["body"])

        # The last-resort sanitizer always logs ``decision="error"``.
        assert len(capture.logs) >= 1
        assert capture.logs[-1].decision == "error"


# ---------------------------------------------------------------------------
# Test 13: parametric sanitization invariant
# ---------------------------------------------------------------------------


def _setup_validation_failure(monkeypatch: pytest.MonkeyPatch) -> dict:
    return _make_event(seed_words=["bad@word"])


def _setup_client_ip_unresolvable(monkeypatch: pytest.MonkeyPatch) -> dict:
    return _make_event(seed_words=["hello"], ip=None)


def _setup_rate_limited(monkeypatch: pytest.MonkeyPatch) -> dict:
    def _raise_exceeded(
        ip_hash: str, day: str, limit: int, **_: Any
    ) -> int:
        raise rate_limiter.RateLimitExceeded(
            ip_hash, day, count=10, limit=limit
        )

    monkeypatch.setattr(handler.rate_limiter, "check", _raise_exceeded)
    return _make_event(seed_words=["hello"])


def _setup_rate_limiter_unavailable(monkeypatch: pytest.MonkeyPatch) -> dict:
    def _raise_unavailable(
        ip_hash: str, day: str, limit: int, **_: Any
    ) -> int:
        raise rate_limiter.RateLimiterUnavailable("check", "ddb timeout")

    monkeypatch.setattr(handler.rate_limiter, "check", _raise_unavailable)
    return _make_event(seed_words=["hello"])


def _setup_moderation_rejected(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(
        handler.rate_limiter, "check", lambda *_a, **_kw: 0
    )
    monkeypatch.setattr(
        handler.input_moderator,
        "classify",
        lambda _t, **_kw: input_moderator.ModerationResult(
            family_friendly=False,
            reason="denylist:test",
            latency_ms=1,
        ),
    )
    return _make_event(seed_words=["hello"])


def _setup_moderation_timeout(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(
        handler.rate_limiter, "check", lambda *_a, **_kw: 0
    )

    def _raise_timeout(_t: str, **_kw: Any) -> Any:
        raise input_moderator.ModerationTimeout(3000)

    monkeypatch.setattr(
        handler.input_moderator, "classify", _raise_timeout
    )
    return _make_event(seed_words=["hello"])


def _setup_moderation_unavailable(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(
        handler.rate_limiter, "check", lambda *_a, **_kw: 0
    )

    def _raise_unavailable(_t: str, **_kw: Any) -> Any:
        raise input_moderator.ModerationUnavailable(
            "detect_toxic_content", "boto error"
        )

    monkeypatch.setattr(
        handler.input_moderator, "classify", _raise_unavailable
    )
    return _make_event(seed_words=["hello"])


def _setup_bedrock_unavailable(monkeypatch: pytest.MonkeyPatch) -> dict:
    _patch_through_validation(monkeypatch)

    def _raise_bedrock(*_a: Any, **_kw: Any) -> str:
        raise joke_generator.JokeGenerationUnavailable(
            "converse", "arn:aws:bedrock:us-east-1:123456789012:model/foo"
        )

    monkeypatch.setattr(
        handler.joke_generator, "generate", _raise_bedrock
    )
    return _make_event(seed_words=["hello"])


def _setup_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> dict:
    def _raise_runtime(_event: dict) -> list[str]:
        raise RuntimeError(
            "Traceback (most recent call last) at "
            "/var/task/joke_api/handler.py:1234 -- "
            "arn:aws:lambda:us-east-1:123456789012:function/joke-api"
        )

    monkeypatch.setattr(
        handler.request_validator, "validate", _raise_runtime
    )
    return _make_event(seed_words=["hello"])


# Each entry is (id, setup_fn, expected_status). The setup_fn applies
# any per-scenario monkeypatches and returns the event to invoke.
_SANITIZATION_SCENARIOS: tuple[tuple[str, Any, int], ...] = (
    ("validation", _setup_validation_failure, 400),
    ("client_ip_unresolvable", _setup_client_ip_unresolvable, 400),
    ("rate_limited", _setup_rate_limited, 429),
    ("rate_limiter_unavailable", _setup_rate_limiter_unavailable, 503),
    ("moderation_rejected", _setup_moderation_rejected, 400),
    ("moderation_timeout", _setup_moderation_timeout, 504),
    ("moderation_unavailable", _setup_moderation_unavailable, 503),
    ("bedrock_unavailable", _setup_bedrock_unavailable, 503),
    ("unexpected_exception", _setup_unexpected_exception, 503),
)


class TestSanitizationInvariants:
    """Property 20 / R7.5 -- defense-in-depth sanitization across every path."""

    @pytest.mark.parametrize(
        "scenario_id,setup_fn,expected_status",
        _SANITIZATION_SCENARIOS,
        ids=[s[0] for s in _SANITIZATION_SCENARIOS],
    )
    def test_error_response_body_is_sanitized(
        self,
        scenario_id: str,
        setup_fn: Any,
        expected_status: int,
        monkeypatch: pytest.MonkeyPatch,
        capture: _ObservabilityCapture,
        fake_cfg: SimpleNamespace,
    ) -> None:
        # Each scenario builds its own event and applies its own patches
        # via the setup function; we then drive the handler and assert
        # the body is free of internal text leaks regardless of which
        # error category was raised.
        event = setup_fn(monkeypatch)
        response = handler.lambda_handler(event, None)

        assert response["statusCode"] == expected_status
        _assert_body_sanitized(response["body"])

        body = json.loads(response["body"])
        # Every error envelope carries an ``error`` field (the
        # sanitized category) and a fixed ``message`` -- ``response_builder``
        # is the single chokepoint that produces them.
        assert "error" in body
        assert "message" in body
