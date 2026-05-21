"""Property tests for the alert-dispatcher half of
:mod:`joke_api.observability`.

Implements three correctness properties from ``design.md`` § Correctness
Properties:

* **Property 31: Cost-alert email subject and gating.** *For any*
  CloudWatch alarm-state-change event, the cost-alert email SHALL be
  sent iff the new state is ``ALARM`` (and the previous state was not
  ``ALARM``); the subject SHALL contain the literal token
  ``[COST-ALERT]`` and the breached threshold value formatted as USD;
  and the email SHALL be sent on the cost SNS topic only.
* **Property 32: Cost-email retry caps at three attempts.** *For any*
  sequence of email-delivery outcomes for a single cost alarm event,
  the dispatcher attempts at most ``1 + 3 = 4`` publishes, spaced
  ``COST_ALERT_RETRY_INTERVAL_SECONDS`` (60) seconds apart.
* **Property 33: Ops-alert email subject, channel, and trigger
  thresholds.** *For any* ops alarm event, the ops-alert email SHALL
  be sent on a separate ops SNS topic, the subject SHALL contain
  ``[OPS-ALERT]`` (and SHALL NOT contain the literal token ``cost``
  in the prefix), and the threshold-evaluation contract is delegated
  to CloudWatch.

**Validates: Requirements 16.4, 16.5, 16.6**

Boundary
--------
Property 33's "subject SHALL NOT contain ``cost``" reads strictly: the
**prefix** is the channel marker that receivers route on, so the
prefix must be ``cost``-free. A metric name MAY legitimately contain
``cost`` (e.g. a future ``high_cost_per_hour`` metric); channel
separation is enforced at the prefix and topic-ARN level, not by
string-scrubbing the metric name. The tests therefore assert the
PREFIX is ``cost``-free, not the whole subject.

Property 32 keys on the module-level :data:`_RETRY_SLEEP` seam: tests
monkey-patch it to a no-op (or to a counter) so the cost-alert retry
loop runs in milliseconds rather than four real minutes.

Stub design
-----------
The SNS backend is replaced with hand-rolled stubs (NOT
``MagicMock``) following the pattern used in
``tests/property/test_voice_synthesizer_property.py``. Each stub
captures every ``publish`` call's keyword arguments. A NEW stub
instance is built per Hypothesis example so call counters do not
leak between iterations.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from joke_api import observability
from joke_api.observability import (
    ALARM_STATES,
    COST_ALERT_RETRY_INTERVAL_SECONDS,
    COST_ALERT_SUBJECT_PREFIX,
    MAX_COST_ALERT_ATTEMPTS,
    OPS_ALERT_SUBJECT_PREFIX,
    AlertDispatchResult,
    dispatch_cost_alert,
    dispatch_ops_alert,
    get_failure_count,
    reset_failure_count,
)


# ---------------------------------------------------------------------------
# Hand-rolled stubs
# ---------------------------------------------------------------------------


class _SuccessfulSNSStub:
    """SNS stub whose :py:meth:`publish` always succeeds.

    Captures every call's keyword arguments in :attr:`publish_calls`
    so tests can introspect ``TopicArn`` / ``Subject`` / ``Message``
    without re-implementing the call shape.
    """

    __slots__ = ("publish_calls",)

    def __init__(self) -> None:
        self.publish_calls: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.publish_calls.append(kwargs)
        return {"MessageId": "stub-message-id"}


class _RetryingSNSStub:
    """Fails on the first ``success_after`` publishes, then succeeds.

    A ``success_after`` of 0 means the very first publish succeeds.
    A ``success_after`` >= :data:`MAX_COST_ALERT_ATTEMPTS` means
    every attempt fails (the dispatcher exhausts its retry budget).
    """

    __slots__ = ("publish_calls", "_success_after")

    def __init__(self, success_after: int) -> None:
        self.publish_calls: list[dict[str, Any]] = []
        self._success_after = success_after

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.publish_calls.append(kwargs)
        if len(self.publish_calls) <= self._success_after:
            raise ClientError(
                {"Error": {"Code": "Throttling", "Message": "x"}},
                "Publish",
            )
        return {"MessageId": "stub-message-id"}


class _FailingSNSStub:
    """SNS stub whose :py:meth:`publish` always raises ``ClientError``.

    Used by the ops-alert single-shot test (Property 33, Test 8) to
    show that ops alerts do NOT retry.
    """

    __slots__ = ("publish_calls",)

    def __init__(self) -> None:
        self.publish_calls: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.publish_calls.append(kwargs)
        raise ClientError(
            {"Error": {"Code": "InternalError", "Message": "x"}},
            "Publish",
        )


class _SleepCounter:
    """Records every call to a sleep callable (Property 32, Test 5)."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# Test-only constants and strategies
# ---------------------------------------------------------------------------

_COST_TOPIC_ARN: str = "arn:aws:sns:us-east-1:123456789012:cost"
_OPS_TOPIC_ARN: str = "arn:aws:sns:us-east-1:123456789012:ops"

# Sorted for hypothesis determinism. ALARM_STATES is a frozenset so
# we sort to a stable list before sampling.
_alarm_state_strategy = st.sampled_from(sorted(ALARM_STATES))

# Threshold strategy for Property 31 Test 2. Decimal with two
# fractional digits matches the design's USD formatting and stays
# inside the validator's [0, 10000] range.
_threshold_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("10000"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)

# Topic ARN strategy: AWS account ids are 12-digit strings; we
# generate a small numeric suffix to differentiate per-example ARNs.
_account_suffix_strategy = st.integers(min_value=0, max_value=9999)

# Metric names from the production observability module + the IaC's
# alarm filter (lambda_decision_error_count is the
# DadJokesLambdaErrorAlarm metric).
_metric_name_strategy = st.sampled_from(
    [
        "jokes_per_hour",
        "moderation_rejections_per_hour",
        "rate_limit_rejections_per_hour",
        "observability_failure",
        "lambda_decision_error_count",
    ]
)

# Bounded floats for ops-alert metric values. 0..1e6 covers every
# realistic per-hour count without tripping the validator's
# isfinite check.
_metric_value_strategy = st.floats(
    min_value=0.0,
    max_value=1e6,
    allow_nan=False,
    allow_infinity=False,
)

# Number of failures before the retrying stub starts succeeding.
# Range covers 0 (immediate success) through 5 (well past the retry
# budget) so we exercise both the success and exhaustion branches.
_success_after_strategy = st.integers(min_value=0, max_value=5)


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
# Property 31: Cost-alert email subject and gating
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(previous_state=_alarm_state_strategy, current_state=_alarm_state_strategy)
def test_property_31_cost_alert_gating(
    previous_state: str,
    current_state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 31 (Test 1): the gate fires iff (curr == ALARM and prev != ALARM).

    Drives :func:`dispatch_cost_alert` across every (previous_state,
    current_state) pair in :data:`ALARM_STATES` x 2 with an
    always-succeed SNS stub. Asserts:

    * ``result.delivered == True`` IFF ``(curr == "ALARM" and prev != "ALARM")``.
    * Gate closed: ``attempts == 0``, ``error == "state_not_transitioning_to_alarm"``,
      no SNS calls, ``subject is None`` and ``body is None``.
    * Gate open: ``delivered == True``, ``attempts == 1``, exactly one
      SNS call, ``subject`` contains both ``[COST-ALERT]`` and the
      threshold formatted as ``$X.YY``.

    **Validates: Requirements 16.4** (Property 31).
    """
    # Patch the retry sleep to a no-op so even unexpected retry paths
    # do not slow this test down.
    monkeypatch.setattr(observability, "_RETRY_SLEEP", lambda _seconds: None)
    reset_failure_count()

    stub = _SuccessfulSNSStub()
    threshold = Decimal("10.00")
    result = dispatch_cost_alert(
        breached_threshold_usd=threshold,
        previous_state=previous_state,
        current_state=current_state,
        sns_client=stub,
        cost_topic_arn=_COST_TOPIC_ARN,
    )
    assert isinstance(result, AlertDispatchResult)

    gate_open = current_state == "ALARM" and previous_state != "ALARM"

    if not gate_open:
        assert result.delivered is False, (
            f"gate closed for ({previous_state} -> {current_state}) "
            f"but delivered={result.delivered}"
        )
        assert result.attempts == 0, (
            f"gate closed but attempts={result.attempts} (expected 0)"
        )
        assert result.error == "state_not_transitioning_to_alarm", (
            f"gate closed but error={result.error!r}"
        )
        assert result.subject is None
        assert result.body is None
        assert stub.publish_calls == [], (
            f"gate closed but stub got {len(stub.publish_calls)} calls"
        )
        return

    # Gate open: the dispatcher must have published exactly once.
    assert result.delivered is True, (
        f"gate open for ({previous_state} -> {current_state}) but "
        f"delivered={result.delivered}; result={result!r}"
    )
    assert result.attempts == 1, (
        f"gate open but attempts={result.attempts} (expected 1)"
    )
    assert result.error is None
    assert len(stub.publish_calls) == 1, (
        f"gate open but stub got {len(stub.publish_calls)} calls"
    )
    assert result.subject is not None
    assert COST_ALERT_SUBJECT_PREFIX in result.subject, (
        f"subject missing {COST_ALERT_SUBJECT_PREFIX!r}: "
        f"{result.subject!r}"
    )
    assert f"${threshold:.2f}" in result.subject, (
        f"subject missing USD-formatted threshold: {result.subject!r}"
    )


@PBT_SETTINGS
@given(threshold=_threshold_strategy)
def test_property_31_subject_formatting(
    threshold: Decimal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 31 (Test 2): subject contains the prefix AND ``$X.YY``.

    Forces the gate open with ``(OK, ALARM)`` and varies the
    breached threshold across ``[0, 10000]`` USD with two-decimal
    precision. Asserts the prefix and ``$X.YY`` formatting both
    appear in :attr:`AlertDispatchResult.subject`, and that the
    subject the dispatcher returned is byte-for-byte the same one
    that reached the SNS stub via the ``Subject`` kwarg.

    **Validates: Requirements 16.4** (Property 31).
    """
    monkeypatch.setattr(observability, "_RETRY_SLEEP", lambda _seconds: None)
    reset_failure_count()

    stub = _SuccessfulSNSStub()
    result = dispatch_cost_alert(
        breached_threshold_usd=threshold,
        previous_state="OK",
        current_state="ALARM",
        sns_client=stub,
        cost_topic_arn=_COST_TOPIC_ARN,
    )

    assert result.delivered is True
    assert result.subject is not None
    assert COST_ALERT_SUBJECT_PREFIX in result.subject, (
        f"subject missing {COST_ALERT_SUBJECT_PREFIX!r}: "
        f"{result.subject!r}"
    )
    assert f"${threshold:.2f}" in result.subject, (
        f"subject missing ${threshold:.2f}: {result.subject!r}"
    )

    # The captured Subject kwarg matches the result.subject exactly.
    assert len(stub.publish_calls) == 1
    captured_subject = stub.publish_calls[0]["Subject"]
    assert captured_subject == result.subject, (
        f"captured Subject={captured_subject!r} does not match "
        f"result.subject={result.subject!r}"
    )


@PBT_SETTINGS
@given(account_suffix=_account_suffix_strategy)
def test_property_31_channel_separation_cost_topic(
    account_suffix: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 31 (Test 3): cost dispatcher publishes to the supplied ARN.

    Generates a hypothesis-driven cost topic ARN per example and
    asserts the captured ``TopicArn`` exactly equals the ARN we
    passed in. This pins the channel-separation contract on the
    cost side; the ops counterpart lives in
    :func:`test_property_33_channel_separation_ops_topic`.

    **Validates: Requirements 16.4** (Property 31, channel rule).
    """
    monkeypatch.setattr(observability, "_RETRY_SLEEP", lambda _seconds: None)
    reset_failure_count()

    cost_arn = f"arn:aws:sns:us-east-1:123456789012:cost-{account_suffix}"
    stub = _SuccessfulSNSStub()
    result = dispatch_cost_alert(
        breached_threshold_usd=Decimal("10.00"),
        previous_state="OK",
        current_state="ALARM",
        sns_client=stub,
        cost_topic_arn=cost_arn,
    )

    assert result.delivered is True
    assert len(stub.publish_calls) == 1
    captured_arn = stub.publish_calls[0]["TopicArn"]
    assert captured_arn == cost_arn, (
        f"captured TopicArn={captured_arn!r} does not match "
        f"supplied cost_topic_arn={cost_arn!r}"
    )


# ---------------------------------------------------------------------------
# Property 32: Cost-email retry caps at three attempts
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(success_after=_success_after_strategy)
def test_property_32_retry_count_bound(
    success_after: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 32 (Test 4): attempts never exceed ``MAX_COST_ALERT_ATTEMPTS``.

    Drives :func:`dispatch_cost_alert` against a stub that fails
    ``success_after`` times before succeeding. Asserts:

    * ``result.attempts <= MAX_COST_ALERT_ATTEMPTS`` always.
    * ``success_after < MAX_COST_ALERT_ATTEMPTS`` -> delivered, with
      ``attempts == success_after + 1``.
    * ``success_after >= MAX_COST_ALERT_ATTEMPTS`` -> not delivered,
      ``attempts == MAX_COST_ALERT_ATTEMPTS``,
      ``error == "max_retries_exhausted"``.
    * The stub's call count exactly equals ``result.attempts``.

    **Validates: Requirements 16.5** (Property 32).
    """
    monkeypatch.setattr(observability, "_RETRY_SLEEP", lambda _seconds: None)
    reset_failure_count()

    stub = _RetryingSNSStub(success_after)
    result = dispatch_cost_alert(
        breached_threshold_usd=Decimal("10.00"),
        previous_state="OK",
        current_state="ALARM",
        sns_client=stub,
        cost_topic_arn=_COST_TOPIC_ARN,
    )

    assert result.attempts <= MAX_COST_ALERT_ATTEMPTS, (
        f"attempts={result.attempts} exceeded "
        f"MAX_COST_ALERT_ATTEMPTS={MAX_COST_ALERT_ATTEMPTS}"
    )
    assert len(stub.publish_calls) == result.attempts, (
        f"stub call count={len(stub.publish_calls)} does not match "
        f"result.attempts={result.attempts}"
    )

    if success_after < MAX_COST_ALERT_ATTEMPTS:
        assert result.delivered is True, (
            f"success_after={success_after} should have delivered "
            f"(< MAX_COST_ALERT_ATTEMPTS={MAX_COST_ALERT_ATTEMPTS}): "
            f"{result!r}"
        )
        assert result.attempts == success_after + 1, (
            f"success_after={success_after} expected "
            f"attempts={success_after + 1}, got {result.attempts}"
        )
        assert result.error is None
    else:
        assert result.delivered is False, (
            f"success_after={success_after} should NOT have delivered "
            f"(>= MAX_COST_ALERT_ATTEMPTS={MAX_COST_ALERT_ATTEMPTS}): "
            f"{result!r}"
        )
        assert result.attempts == MAX_COST_ALERT_ATTEMPTS
        assert result.error == "max_retries_exhausted", (
            f"expected error='max_retries_exhausted', got {result.error!r}"
        )


def test_property_32_sleep_between_attempts_full_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 32 (Test 5a): full-failure scenario sleeps N-1 times.

    With ``MAX_COST_ALERT_ATTEMPTS == 4`` failed attempts, the
    dispatcher must call the retry sleep exactly 3 times (one
    between attempts 1->2, 2->3, 3->4) and NOT after the final
    attempt. Each sleep duration must equal
    :data:`COST_ALERT_RETRY_INTERVAL_SECONDS`.

    **Validates: Requirements 16.5** (Property 32, sleep schedule).
    """
    counter = _SleepCounter()
    monkeypatch.setattr(observability, "_RETRY_SLEEP", counter)
    reset_failure_count()

    # success_after >= MAX_COST_ALERT_ATTEMPTS exhausts every attempt.
    stub = _RetryingSNSStub(success_after=MAX_COST_ALERT_ATTEMPTS)
    result = dispatch_cost_alert(
        breached_threshold_usd=Decimal("10.00"),
        previous_state="OK",
        current_state="ALARM",
        sns_client=stub,
        cost_topic_arn=_COST_TOPIC_ARN,
    )

    assert result.delivered is False
    assert result.attempts == MAX_COST_ALERT_ATTEMPTS
    assert len(counter.calls) == MAX_COST_ALERT_ATTEMPTS - 1, (
        f"expected {MAX_COST_ALERT_ATTEMPTS - 1} sleeps after "
        f"{MAX_COST_ALERT_ATTEMPTS} failed attempts, got "
        f"{len(counter.calls)}"
    )
    for index, duration in enumerate(counter.calls):
        assert duration == COST_ALERT_RETRY_INTERVAL_SECONDS, (
            f"sleep #{index} was {duration}s, expected "
            f"{COST_ALERT_RETRY_INTERVAL_SECONDS}s"
        )


def test_property_32_sleep_between_attempts_one_failure_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 32 (Test 5b): 1-failure-then-success sleeps exactly once.

    The dispatcher retries after the first failure, then succeeds
    on attempt 2. There must be exactly one sleep (between
    attempts 1 and 2) and NO sleep after the successful attempt.

    **Validates: Requirements 16.5** (Property 32, no trailing sleep).
    """
    counter = _SleepCounter()
    monkeypatch.setattr(observability, "_RETRY_SLEEP", counter)
    reset_failure_count()

    stub = _RetryingSNSStub(success_after=1)
    result = dispatch_cost_alert(
        breached_threshold_usd=Decimal("10.00"),
        previous_state="OK",
        current_state="ALARM",
        sns_client=stub,
        cost_topic_arn=_COST_TOPIC_ARN,
    )

    assert result.delivered is True
    assert result.attempts == 2
    assert len(counter.calls) == 1, (
        f"expected exactly 1 sleep between attempt 1 and attempt 2, "
        f"got {len(counter.calls)}"
    )
    assert counter.calls[0] == COST_ALERT_RETRY_INTERVAL_SECONDS


# ---------------------------------------------------------------------------
# Property 33: Ops-alert email subject, channel, and trigger thresholds
# ---------------------------------------------------------------------------


def test_property_33_ops_subject_prefix_does_not_contain_cost() -> None:
    """Property 33 (subject prefix is ``cost``-free, structural).

    The OPS prefix is the channel marker that receivers route on,
    so it must not contain the literal ``cost`` (case-insensitive).
    A metric NAME may legitimately contain ``cost`` (e.g. a future
    ``high_cost_per_hour`` metric); the channel separation lives at
    the prefix and topic-ARN level. We assert this once at module
    load (no hypothesis needed) so any constant drift is caught
    immediately.

    **Validates: Requirements 16.6** (Property 33, prefix rule).
    """
    assert "cost" not in OPS_ALERT_SUBJECT_PREFIX.lower(), (
        f"OPS_ALERT_SUBJECT_PREFIX={OPS_ALERT_SUBJECT_PREFIX!r} contains "
        f"'cost' -- channel separation is broken"
    )


@PBT_SETTINGS
@given(
    metric_name=_metric_name_strategy,
    current_value=_metric_value_strategy,
    threshold=_metric_value_strategy,
)
def test_property_33_ops_alert_subject_prefix(
    metric_name: str,
    current_value: float,
    threshold: float,
) -> None:
    """Property 33 (Test 6): ops alerts publish once with the OPS prefix.

    Asserts:

    * ``result.delivered == True`` and ``result.attempts == 1``.
    * ``result.subject`` starts with ``OPS_ALERT_SUBJECT_PREFIX``.

    The whole-subject ``cost``-substring check is intentionally
    omitted because metric names may legitimately contain ``cost``;
    see :func:`test_property_33_ops_subject_prefix_does_not_contain_cost`
    for the structural prefix-only assertion.

    **Validates: Requirements 16.6** (Property 33, subject prefix).
    """
    reset_failure_count()

    stub = _SuccessfulSNSStub()
    result = dispatch_ops_alert(
        metric_name=metric_name,
        current_value=current_value,
        threshold=threshold,
        sns_client=stub,
        ops_topic_arn=_OPS_TOPIC_ARN,
    )

    assert result.delivered is True, f"ops dispatch failed: {result!r}"
    assert result.attempts == 1
    assert result.error is None
    assert result.subject is not None
    assert result.subject.startswith(OPS_ALERT_SUBJECT_PREFIX), (
        f"subject does not start with {OPS_ALERT_SUBJECT_PREFIX!r}: "
        f"{result.subject!r}"
    )
    # Captured publish kwargs reflect the same subject and topic.
    assert len(stub.publish_calls) == 1
    captured = stub.publish_calls[0]
    assert captured["Subject"] == result.subject
    assert captured["TopicArn"] == _OPS_TOPIC_ARN


@PBT_SETTINGS
@given(account_suffix=_account_suffix_strategy)
def test_property_33_channel_separation_ops_topic(
    account_suffix: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 33 (Test 7): ops dispatcher publishes to its own ARN.

    Cross-cutting check: build separate stubs for the cost and ops
    dispatchers, run BOTH, and assert each stub's calls only used
    its own topic. Confirms the two channels never accidentally
    cross-talk regardless of which order they fire.

    **Validates: Requirements 16.6** (Property 33, channel rule).
    """
    monkeypatch.setattr(observability, "_RETRY_SLEEP", lambda _seconds: None)
    reset_failure_count()

    cost_arn = f"arn:aws:sns:us-east-1:123456789012:cost-{account_suffix}"
    ops_arn = f"arn:aws:sns:us-east-1:123456789012:ops-{account_suffix}"
    cost_stub = _SuccessfulSNSStub()
    ops_stub = _SuccessfulSNSStub()

    cost_result = dispatch_cost_alert(
        breached_threshold_usd=Decimal("10.00"),
        previous_state="OK",
        current_state="ALARM",
        sns_client=cost_stub,
        cost_topic_arn=cost_arn,
    )
    ops_result = dispatch_ops_alert(
        metric_name="jokes_per_hour",
        current_value=0.0,
        threshold=10.0,
        sns_client=ops_stub,
        ops_topic_arn=ops_arn,
    )

    # Each dispatcher delivered.
    assert cost_result.delivered is True
    assert ops_result.delivered is True

    # Each stub saw exactly one call, and that call's TopicArn was
    # its own.
    assert len(cost_stub.publish_calls) == 1
    assert cost_stub.publish_calls[0]["TopicArn"] == cost_arn
    assert len(ops_stub.publish_calls) == 1
    assert ops_stub.publish_calls[0]["TopicArn"] == ops_arn

    # Cross-channel sanity: the ops ARN never appears on a cost
    # publish, and vice-versa.
    assert all(
        call["TopicArn"] != ops_arn for call in cost_stub.publish_calls
    ), "cost dispatcher published to the ops topic"
    assert all(
        call["TopicArn"] != cost_arn for call in ops_stub.publish_calls
    ), "ops dispatcher published to the cost topic"


def test_property_33_ops_alert_is_single_shot_no_retry() -> None:
    """Property 33 (Test 8): ops alerts do NOT retry on transport error.

    Contrasts Property 32 explicitly: the cost dispatcher retries up
    to four times, but the ops dispatcher publishes exactly once and
    surfaces the failure as ``error="sns_publish_failed"`` after a
    single attempt. The retry sleep is irrelevant here because the
    dispatcher has no retry loop, but we still call this from a
    plain pytest function (not Hypothesis) because the property is
    a structural one rather than universal over inputs.

    **Validates: Requirements 16.6** (Property 33, single-shot).
    """
    reset_failure_count()

    stub = _FailingSNSStub()
    result = dispatch_ops_alert(
        metric_name="jokes_per_hour",
        current_value=0.0,
        threshold=10.0,
        sns_client=stub,
        ops_topic_arn=_OPS_TOPIC_ARN,
    )

    assert result.delivered is False, (
        f"ops dispatcher delivered against an always-failing stub: "
        f"{result!r}"
    )
    assert result.attempts == 1, (
        f"ops dispatcher attempted {result.attempts} publishes; ops "
        f"alerts must be single-shot"
    )
    assert result.error == "sns_publish_failed", (
        f"expected error='sns_publish_failed', got {result.error!r}"
    )
    assert len(stub.publish_calls) == 1, (
        f"stub saw {len(stub.publish_calls)} calls; ops alerts must "
        f"be single-shot"
    )
    # Soft-fail counter incremented exactly once for the failed publish.
    assert get_failure_count() >= 1
