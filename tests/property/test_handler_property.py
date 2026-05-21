"""Property tests for the handler's pipeline ordering and fail-closed
contracts.

**Validates: Requirements 3.1, 3.2, 3.6, 3.7**

Implements two properties from design.md § Correctness Properties:

* **Property 8: Moderation gate precedes Bedrock for all accepted
  inputs.** For any request that results in a Bedrock invocation, the
  Input_Moderator SHALL have been invoked first and returned
  family_friendly=True; AND the request SHALL have passed length/charset
  validation.
* **Property 10: Moderator unavailability fails closed.** For any
  moderator unavailability event (timeout or transport error), the
  handler SHALL return a 5xx error and SHALL NOT invoke Bedrock or any
  later pipeline stage.

Boundary: this file tests the HANDLER-orchestration half of these
properties. The Input_Moderator's own classification logic is tested
by Properties 9 and 11 in ``tests/property/test_moderators_property.py``.

Approach
--------
Each test builds a hand-rolled :class:`_OrderingTracker` and a stub
:class:`Config`, then monkey-patches every handler dependency on the
:mod:`joke_api.handler` module's namespace so the entire pipeline runs
without any real AWS calls. The tracker records the order in which
each pipeline stage was invoked; assertions are then made on
``tracker.calls`` (the call sequence) and on the API Gateway response
returned by :func:`joke_api.handler.lambda_handler`.

A NEW tracker is built per Hypothesis example so cross-example state
does not leak.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from joke_api import (
    handler,
    input_moderator,
    joke_generator,
    joke_store,
    output_moderator,
    rate_limiter,
    request_validator,
    response_builder,
    voice_synthesizer,
)
from joke_api.config import Config
from joke_api.input_moderator import (
    ModerationResult,
    ModerationTimeout,
    ModerationUnavailable,
)


# ---------------------------------------------------------------------------
# Hypothesis settings
# ---------------------------------------------------------------------------


PBT_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.large_base_example,
        HealthCheck.function_scoped_fixture,
    ],
)


# ---------------------------------------------------------------------------
# Test-only constants
# ---------------------------------------------------------------------------


# A 64-char lowercase hex digest the IP-hash stub returns. Satisfies
# observability.LogRecord's R16.7 ip_hash format check so the per-
# request log emission inside the handler doesn't raise.
_FAKE_IP_HASH: str = "a" * 64

_VALID_IP: str = "203.0.113.7"

_VALID_JOKE_TEXT: str = " ".join(["banana"] * 30)

# Charset matching the validator's seed-word rule (R3.4 / R3.5).
_SEED_WORD_CHARSET: str = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789'-"
)


# ---------------------------------------------------------------------------
# Hand-rolled stubs
# ---------------------------------------------------------------------------


class _OrderingTracker:
    """Records the sequence of pipeline stages called during a request.

    The 11 tracked stages mirror the 12-stage pipeline declared in
    :mod:`joke_api.handler`'s module docstring (stages 1-10 plus the
    output-moderation classifier; stages 11/12 -- response build and
    observability emit -- are not pipeline gates and are exercised
    via the response-shape assertions).
    """

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, stage: str) -> None:
        self.calls.append(stage)

    def has(self, stage: str) -> bool:
        return stage in self.calls

    def index_of(self, stage: str) -> int:
        return self.calls.index(stage)

    def precedes(self, earlier: str, later: str) -> bool:
        """Return True iff ``earlier`` was called before ``later``."""
        return (
            self.has(earlier)
            and self.has(later)
            and self.index_of(earlier) < self.index_of(later)
        )


def _build_test_config() -> Config:
    """Build a stub :class:`Config` so the handler's lazy SSM load is
    bypassed entirely (tests assign the result to ``handler._CFG``).
    """
    return Config(
        daily_limit=10,
        bedrock_model_id="amazon.nova-lite-v1:0",
        polly_voice_id="Joanna",
        ad_module_enabled=False,
        ad_network_id="",
        ip_hash_salt="test-salt",
        cost_alarm_threshold_usd=10.0,
    )


def _build_event(seed_words: list[str]) -> dict[str, Any]:
    """Build an API Gateway HTTP API v2 event for ``POST /v1/jokes``."""
    body = json.dumps({"seedWords": seed_words}, separators=(",", ":"))
    return {
        "routeKey": "POST /v1/jokes",
        "headers": {"x-forwarded-for": _VALID_IP},
        "body": body,
        "isBase64Encoded": False,
    }


# Stub factories ------------------------------------------------------------


# Capture the real validator before any test patches its module
# attribute. Stubs that need real validation (i.e. just recording
# the call and delegating to the original) bind to this reference
# so monkey-patching ``handler.request_validator.validate`` cannot
# cause the stub to recurse into itself.
_REAL_VALIDATE = request_validator.validate


def _stub_validate(
    tracker: _OrderingTracker,
    *,
    raise_error: Optional[BaseException] = None,
):
    def _impl(event: dict) -> list[str]:
        tracker.record("validate")
        if raise_error is not None:
            raise raise_error
        # Delegate to the captured original so the seedWords list
        # returned to the handler reflects exactly what the visitor
        # sent. ``_REAL_VALIDATE`` is a frozen reference taken at
        # import time, so the monkey-patch of
        # ``request_validator.validate`` to the stub does not cause
        # the stub to recurse into itself.
        return _REAL_VALIDATE(event)

    return _impl


def _stub_resolve_ip(tracker: _OrderingTracker):
    def _impl(event: dict) -> str:
        tracker.record("resolve_ip")
        return _VALID_IP

    return _impl


def _stub_hash_ip(tracker: _OrderingTracker):
    def _impl(ip: str, *, salt: Any) -> str:
        tracker.record("hash_ip")
        return _FAKE_IP_HASH

    return _impl


def _stub_rate_check(
    tracker: _OrderingTracker,
    *,
    raise_error: Optional[BaseException] = None,
):
    def _impl(ip_hash: str, day: str, limit: int) -> None:
        tracker.record("rate_check")
        if raise_error is not None:
            raise raise_error
        return None

    return _impl


def _stub_rate_increment(tracker: _OrderingTracker):
    def _impl(ip_hash: str, day: str) -> int:
        tracker.record("rate_increment")
        return 1

    return _impl


def _stub_input_moderate(
    tracker: _OrderingTracker,
    *,
    decision: bool = True,
    raise_error: Optional[BaseException] = None,
):
    def _impl(text: str, **kwargs: Any) -> ModerationResult:
        tracker.record("input_moderate")
        if raise_error is not None:
            raise raise_error
        return ModerationResult(
            family_friendly=decision,
            reason=None if decision else "denylist:test",
            latency_ms=10,
        )

    return _impl


def _stub_output_moderate(tracker: _OrderingTracker):
    def _impl(text: str, **kwargs: Any) -> ModerationResult:
        tracker.record("output_moderate")
        return ModerationResult(
            family_friendly=True, reason=None, latency_ms=5
        )

    return _impl


def _stub_load_few_shot(tracker: _OrderingTracker):
    def _impl(*, rights_confirmed: bool, max_examples: int = 6, **kwargs: Any):
        tracker.record("load_few_shot")
        return []

    return _impl


def _stub_generate(
    tracker: _OrderingTracker,
    *,
    joke_text: str = _VALID_JOKE_TEXT,
):
    def _impl(seed_words, few_shot, **kwargs: Any) -> str:
        tracker.record("generate")
        return joke_text

    return _impl


def _stub_synthesize(tracker: _OrderingTracker):
    def _impl(joke_text: str, **kwargs: Any):
        tracker.record("synthesize")
        return voice_synthesizer.SynthesisResult(
            audio_url=None,
            audio_available=False,
            error="text_length_out_of_range",
        )

    return _impl


def _stub_persist(tracker: _OrderingTracker):
    def _impl(record, **kwargs: Any) -> None:
        tracker.record("persist")
        return None

    return _impl


# ---------------------------------------------------------------------------
# Wiring helper
# ---------------------------------------------------------------------------


def _wire_handler(
    monkeypatch: pytest.MonkeyPatch,
    tracker: _OrderingTracker,
    *,
    validate_error: Optional[BaseException] = None,
    rate_check_error: Optional[BaseException] = None,
    moderate_decision: bool = True,
    moderate_error: Optional[BaseException] = None,
) -> None:
    """Patch every handler dependency to a tracking stub.

    Also bypasses the lazy SSM load by assigning a stub :class:`Config`
    to :data:`joke_api.handler._CFG` directly.
    """
    cfg = _build_test_config()
    monkeypatch.setattr(handler, "_CFG", cfg)

    # Silence observability writes so a test failure does not pollute
    # captured stdout. emit_log soft-fails on any exception, but a
    # successful write would still go to stdout and clutter -v output.
    monkeypatch.setattr(
        handler.observability, "_emit_to_stdout", lambda line: None
    )
    monkeypatch.setattr(
        handler.observability,
        "emit_metric",
        lambda *a, **kw: None,
    )

    monkeypatch.setattr(
        handler.request_validator,
        "validate",
        _stub_validate(tracker, raise_error=validate_error),
    )
    monkeypatch.setattr(
        handler.client_ip, "resolve", _stub_resolve_ip(tracker)
    )
    monkeypatch.setattr(
        handler.ip_hashing, "hash_ip", _stub_hash_ip(tracker)
    )
    monkeypatch.setattr(
        handler.rate_limiter,
        "check",
        _stub_rate_check(tracker, raise_error=rate_check_error),
    )
    monkeypatch.setattr(
        handler.rate_limiter, "increment", _stub_rate_increment(tracker)
    )
    monkeypatch.setattr(
        handler.input_moderator,
        "classify",
        _stub_input_moderate(
            tracker,
            decision=moderate_decision,
            raise_error=moderate_error,
        ),
    )
    monkeypatch.setattr(
        handler.output_moderator,
        "classify",
        _stub_output_moderate(tracker),
    )
    monkeypatch.setattr(
        handler.training_corpus,
        "load_few_shot",
        _stub_load_few_shot(tracker),
    )
    monkeypatch.setattr(
        handler.joke_generator, "generate", _stub_generate(tracker)
    )
    monkeypatch.setattr(
        handler.voice_synthesizer,
        "synthesize",
        _stub_synthesize(tracker),
    )
    monkeypatch.setattr(handler.joke_store, "persist", _stub_persist(tracker))


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_seed_word_strategy = st.text(
    alphabet=_SEED_WORD_CHARSET,
    min_size=1,
    # 18 char per word * 5 words + 4 single-space separators = 94,
    # comfortably below the validator's 100-char aggregate cap
    # (R3.4) so every generated example passes validation cleanly.
    # The Property 8 test below is interested in pipeline ordering,
    # not in re-exercising the validator's edge cases.
    max_size=18,
)

_seed_words_strategy = st.lists(
    _seed_word_strategy, min_size=0, max_size=5
)


# ---------------------------------------------------------------------------
# Property 8: Moderation gate precedes Bedrock for all accepted inputs
# ---------------------------------------------------------------------------


class TestProperty8MorderationGatePrecedesBedrock:
    """Handler-level half of Property 8.

    For any accepted request that results in a Bedrock invocation
    (``joke_generator.generate``), the Input_Moderator SHALL have been
    invoked first AND returned ``family_friendly=True``. Equivalently,
    if ``input_moderate`` returns ``False``, ``generate`` MUST NOT be
    called. If validation fails up front, NEITHER is called.

    **Validates: Requirements 3.1, 3.2**
    """

    @PBT_SETTINGS
    @given(
        seed_words=_seed_words_strategy,
        moderator_decision=st.booleans(),
    )
    def test_property_8_generate_invocation_implies_moderate_invocation_first(
        self,
        seed_words: list[str],
        moderator_decision: bool,
    ) -> None:
        """When the moderator decides ``True``, ``generate`` runs and
        ``input_moderate`` precedes it. When the moderator decides
        ``False``, ``generate`` is never called.

        **Validates: Requirements 3.1, 3.2** (Property 8 ordering).
        """
        tracker = _OrderingTracker()
        # Manage monkey-patching manually so the lifetime is correct
        # for hypothesis-driven tests (pytest's function-scoped
        # ``monkeypatch`` fixture is used here only as the unwinder).
        mp = pytest.MonkeyPatch()
        try:
            _wire_handler(
                mp,
                tracker,
                moderate_decision=moderator_decision,
            )
            event = _build_event(seed_words)
            response = handler.lambda_handler(event, None)

            # Property 8 (positive branch): generate ⇒ moderate first.
            if tracker.has("generate"):
                assert tracker.has("input_moderate"), (
                    "Property 8 violated: generate was called but "
                    "input_moderate was not. "
                    f"calls={tracker.calls!r}"
                )
                assert tracker.precedes("input_moderate", "generate"), (
                    "Property 8 violated: input_moderate did not "
                    f"precede generate. calls={tracker.calls!r}"
                )

            # Decision-specific corollaries.
            if moderator_decision is True:
                assert tracker.has("generate"), (
                    "moderator allowed the request but generate was "
                    f"not called. calls={tracker.calls!r}"
                )
                assert tracker.precedes("input_moderate", "generate")
                assert response["statusCode"] == 200
            else:
                assert not tracker.has("generate"), (
                    "Property 8 violated: moderator rejected the "
                    "request but generate was still called. "
                    f"calls={tracker.calls!r}"
                )
                assert tracker.has("input_moderate")
                # 400 moderation rejection (R3.2).
                assert response["statusCode"] == 400
                body = json.loads(response["body"])
                assert body["error"] == response_builder.MODERATION
        finally:
            mp.undo()

    def test_property_8_validation_failure_short_circuits_moderate_and_generate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``request_validator.validate`` raises, NEITHER
        ``input_moderate`` NOR ``generate`` is invoked.

        Validation is the first stage of the pipeline (R1.7, R3.5,
        R7.5); a validation failure must short-circuit every later
        stage so no Bedrock cost is incurred for malformed input.

        **Validates: Requirements 3.1** (Property 8, validation
        short-circuit).
        """
        tracker = _OrderingTracker()
        validation_error = request_validator.ValidationError(
            "seed_word_charset",
            "seedWords[0]",
            "seed word may only contain letters, digits, hyphens, or apostrophes",
        )
        _wire_handler(
            monkeypatch,
            tracker,
            validate_error=validation_error,
        )

        # Even though the event is otherwise well-formed, the stubbed
        # validator raises, so the pipeline must stop after stage 1.
        event = _build_event(["banana"])
        response = handler.lambda_handler(event, None)

        assert tracker.calls == ["validate"], (
            "validation failure must short-circuit the pipeline; "
            f"got calls={tracker.calls!r}"
        )
        assert not tracker.has("input_moderate")
        assert not tracker.has("generate")

        # Validation maps to HTTP 400 with category ``validation``.
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == response_builder.VALIDATION
        # The validator's rule code is allowlisted on VALIDATION.
        assert body["rule"] == "seed_word_charset"

    def test_property_8_rate_limit_failure_short_circuits_moderate_and_generate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``rate_limiter.check`` raises ``RateLimitExceeded``,
        ``input_moderate`` and ``generate`` are NOT invoked and the
        increment counter is NOT bumped (R5.5).

        **Validates: Requirements 3.1** (Property 8, pre-moderation
        rate-limit short-circuit).
        """
        tracker = _OrderingTracker()
        rate_error = rate_limiter.RateLimitExceeded(
            ip_hash=_FAKE_IP_HASH,
            day="2025-01-01",
            count=10,
            limit=10,
        )
        _wire_handler(
            monkeypatch,
            tracker,
            rate_check_error=rate_error,
        )

        event = _build_event(["banana"])
        response = handler.lambda_handler(event, None)

        # Stages 1..4 run; 5 (input_moderate) onwards must not.
        assert tracker.calls == [
            "validate",
            "resolve_ip",
            "hash_ip",
            "rate_check",
        ], f"unexpected pipeline calls: {tracker.calls!r}"
        assert not tracker.has("input_moderate")
        assert not tracker.has("generate")
        assert not tracker.has("rate_increment"), (
            "R5.5 violated: rate-limit-rejected request must not "
            "bump the counter"
        )

        assert response["statusCode"] == 429
        body = json.loads(response["body"])
        assert body["error"] == response_builder.RATE_LIMITED


# ---------------------------------------------------------------------------
# Property 10: Moderator unavailability fails closed
# ---------------------------------------------------------------------------


class TestProperty10ModeratorUnavailabilityFailsClosed:
    """Handler-level Property 10.

    For any moderator unavailability event (timeout or transport
    error), the handler SHALL return a 5xx error (504 timeout, 503
    unavailable) and SHALL NOT invoke Bedrock or any later pipeline
    stage. The rate-limit counter is NOT incremented (R5.5: counter
    only increments on full success).

    **Validates: Requirements 3.6, 3.7**
    """

    def test_property_10_moderation_timeout_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ModerationTimeout`` ⇒ HTTP 504, ``generate`` not called.

        **Validates: Requirements 3.7** (Property 10, timeout branch).
        """
        tracker = _OrderingTracker()
        _wire_handler(
            monkeypatch,
            tracker,
            moderate_error=ModerationTimeout(3000),
        )

        event = _build_event(["banana"])
        response = handler.lambda_handler(event, None)

        # Stages 1..5 run; 6 onwards must not.
        assert tracker.has("input_moderate")
        assert not tracker.has("generate"), (
            "Property 10 violated: moderator timeout but generate "
            f"still called. calls={tracker.calls!r}"
        )
        assert not tracker.has("synthesize")
        assert not tracker.has("persist")
        assert not tracker.has("rate_increment"), (
            "R5.5 violated: moderation-timeout request must not "
            "bump the counter"
        )

        # 504 with ``moderation_timeout`` category (R3.7).
        assert response["statusCode"] == 504
        body = json.loads(response["body"])
        assert body["error"] == response_builder.MODERATION_TIMEOUT

    def test_property_10_moderation_unavailable_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``ModerationUnavailable`` ⇒ HTTP 503, ``generate`` not called.

        **Validates: Requirements 3.6** (Property 10, unavailable
        branch).
        """
        tracker = _OrderingTracker()
        _wire_handler(
            monkeypatch,
            tracker,
            moderate_error=ModerationUnavailable(
                "detect_toxic_content", "boto error"
            ),
        )

        event = _build_event(["banana"])
        response = handler.lambda_handler(event, None)

        assert tracker.has("input_moderate")
        assert not tracker.has("generate"), (
            "Property 10 violated: moderator unavailable but "
            f"generate still called. calls={tracker.calls!r}"
        )
        assert not tracker.has("synthesize")
        assert not tracker.has("persist")
        assert not tracker.has("rate_increment"), (
            "R5.5 violated: moderation-unavailable request must "
            "not bump the counter"
        )

        # 503 with ``moderation_unavailable`` category (R3.6).
        assert response["statusCode"] == 503
        body = json.loads(response["body"])
        assert body["error"] == response_builder.MODERATION_UNAVAILABLE

    @pytest.mark.parametrize(
        "moderator_error",
        [
            ModerationTimeout(3000),
            ModerationUnavailable("detect_toxic_content", "boto error"),
        ],
        ids=["timeout", "unavailable"],
    )
    def test_property_10_moderation_failure_does_not_increment_counter(
        self,
        monkeypatch: pytest.MonkeyPatch,
        moderator_error: BaseException,
    ) -> None:
        """Across both moderation failure modes,
        ``rate_limiter.increment`` is NOT called (R5.5: counter only
        increments on full success).

        **Validates: Requirements 3.6, 3.7** (Property 10, counter
        fail-closed).
        """
        tracker = _OrderingTracker()
        _wire_handler(
            monkeypatch,
            tracker,
            moderate_error=moderator_error,
        )

        event = _build_event(["banana"])
        handler.lambda_handler(event, None)

        assert not tracker.has("rate_increment"), (
            "R5.5 violated: moderation failure must not bump the "
            f"counter. calls={tracker.calls!r}"
        )
        assert not tracker.has("generate")
