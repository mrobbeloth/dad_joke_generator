"""Property tests for :mod:`joke_api.observability`.

Implements two correctness properties from ``design.md`` § Correctness
Properties:

* **Property 30: Per-request structured log schema.** *For any* request
  handled by the Joke_API, the captured log records SHALL contain
  exactly one record with fields ``request_id`` (UUID v4), ``ip_hash``
  (64-char lowercase hex), ``decision`` ∈ ``{accepted,
  moderation_rejected, rate_limited, error}``, ``model_id``,
  ``voice_id``, ``latency_ms`` (integer in ``[0, 60000]``), and
  ``estimated_cost_usd`` (decimal in ``[0.000000, 1.000000]``),
  emitted within 2 s of request completion.
* **Property 35: Observability emission failures are soft-failures.**
  *For any* sequence of :func:`emit_log` and :func:`emit_metric` calls,
  if the underlying transport raises, the originating request proceeds,
  the internal observability-failure counter is incremented exactly
  once per failure, and no exception escapes the call.

**Validates: Requirements 16.1, 16.8**

Validation vs. transport split
------------------------------
``joke_api.observability`` pins a strict split that this file relies on:

* **Malformed** :class:`~joke_api.observability.LogRecord` **fields**
  raise :class:`~joke_api.observability.ObservabilityValidationError`
  in ``__post_init__``. These are programmer errors -- the emitter
  never sees a malformed record. Test 2 asserts this.
* **Malformed** :func:`~joke_api.observability.emit_metric`
  **arguments** (empty name, NaN value, unknown unit, etc.) raise
  :class:`~joke_api.observability.ObservabilityValidationError`
  *before* the boto3 call. The internal failure counter is **not**
  incremented for these. Test 5 asserts this.
* **Transport / serialization errors** during the stdout-write or
  ``put_metric_data`` call are soft-failed: the internal counter is
  incremented exactly once, and the function returns ``None``. Tests
  3, 4, and 6 assert this.

Stub design
-----------
The CloudWatch client is replaced with a hand-rolled
:class:`_CloudWatchStub` (NOT ``MagicMock``) that exposes only the
``put_metric_data`` method :func:`emit_metric` calls and tracks every
invocation. Stdout writes are redirected by monkey-patching the
module-private :func:`joke_api.observability._emit_to_stdout` seam.
Each Hypothesis example builds a fresh stub and resets the global
soft-fail counter so cross-example state does not leak.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from joke_api import observability
from joke_api.observability import (
    ALLOWED_DECISIONS,
    LATENCY_MS_MAX,
    LATENCY_MS_MIN,
    METRIC_JOKES_PER_HOUR,
    METRIC_MODERATION_REJECTIONS_PER_HOUR,
    METRIC_OBSERVABILITY_FAILURE,
    METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR,
    LogRecord,
    ObservabilityValidationError,
    emit_log,
    emit_metric,
    get_failure_count,
    reset_failure_count,
)


# ---------------------------------------------------------------------------
# Test-only constants
# ---------------------------------------------------------------------------

_HEX_LOWER: str = "0123456789abcdef"
_IP_HASH_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")
_TS_RE: re.Pattern[str] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)

# A deterministic, valid UUID v4 used as the "good" filler value when
# building per-example LogRecord arg dicts that intentionally corrupt
# exactly one other field. Using a constant (vs. ``uuid.uuid4()``)
# keeps Hypothesis examples deterministic.
_VALID_UUID_V4: str = "12345678-1234-4234-8234-123456789012"

# A canonical UUID v1 string (RFC 4122 example): version digit is the
# first character of the third group, here ``1``. Used as a "bad"
# request_id in Test 2 to ensure the version-4 check fires.
_VALID_UUID_V1: str = "550e8400-e29b-11d4-a716-446655440000"

# All four design-mandated metric name constants in one tuple, used
# as the input strategy for the transport-failure path in Test 4.
_VALID_METRIC_NAMES: tuple[str, ...] = (
    METRIC_JOKES_PER_HOUR,
    METRIC_MODERATION_REJECTIONS_PER_HOUR,
    METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR,
    METRIC_OBSERVABILITY_FAILURE,
)


# ---------------------------------------------------------------------------
# Hand-rolled CloudWatch stub (NOT MagicMock)
# ---------------------------------------------------------------------------


class _CloudWatchStub:
    """Minimal hand-rolled CloudWatch client stub.

    Exposes only the single ``put_metric_data`` method
    :func:`joke_api.observability.emit_metric` calls. ``mode`` selects
    per-call behavior:

    * ``"success"`` -- return an empty dict (the real boto3 response
      shape is irrelevant for the soft-fail tests).
    * ``"throttled"`` -- raise ``ClientError`` with a ``Throttling``
      code, mimicking a CloudWatch back-pressure event.

    ``calls`` records every invocation's keyword arguments so tests can
    assert how many times the stub was hit without re-implementing the
    call shape.
    """

    __slots__ = ("mode", "calls")

    def __init__(self, mode: str = "success") -> None:
        self.mode: str = mode
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.mode == "throttled":
            raise ClientError(
                {"Error": {"Code": "Throttling", "Message": "x"}},
                "PutMetricData",
            )
        return {}


# ---------------------------------------------------------------------------
# Hypothesis settings
# ---------------------------------------------------------------------------

# ``function_scoped_fixture`` suppression is needed because several
# tests inject behavior via the ``monkeypatch`` fixture, which is
# function-scoped; Hypothesis warns by default that the fixture is
# not reset between examples. Our patches are constant for the whole
# test, so cross-example reuse is intentional.
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
# Strategies -- valid values
# ---------------------------------------------------------------------------

# UTF-8 text excluding surrogate halves (which json.dumps cannot
# encode without ``ensure_ascii=True``) and embedded NULs.
_safe_text_chars = st.characters(
    blacklist_categories=("Cs",),
    blacklist_characters="\x00",
)

_request_id_strategy = st.uuids(version=4).map(str)
_ip_hash_strategy = st.text(
    alphabet=_HEX_LOWER, min_size=64, max_size=64
)
_decision_strategy = st.sampled_from(ALLOWED_DECISIONS)
_id_text_strategy = st.text(
    alphabet=_safe_text_chars, min_size=1, max_size=128
)
_latency_strategy = st.integers(
    min_value=LATENCY_MS_MIN, max_value=LATENCY_MS_MAX
)
_cost_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)
_ts_strategy = st.datetimes(
    min_value=datetime(2024, 1, 1),
    max_value=datetime(2030, 12, 31),
).map(lambda d: d.replace(tzinfo=timezone.utc, microsecond=0))


@st.composite
def valid_log_records(draw: st.DrawFn) -> LogRecord:
    """Build a fully-valid :class:`LogRecord` from the field strategies."""
    return LogRecord(
        request_id=draw(_request_id_strategy),
        ip_hash=draw(_ip_hash_strategy),
        decision=draw(_decision_strategy),
        model_id=draw(_id_text_strategy),
        voice_id=draw(_id_text_strategy),
        latency_ms=draw(_latency_strategy),
        estimated_cost_usd=draw(_cost_strategy),
        ts=draw(_ts_strategy),
    )


# ---------------------------------------------------------------------------
# Strategies -- invalid LogRecord fields (Test 2)
# ---------------------------------------------------------------------------

# A bag of bad ``request_id`` values: empty string, a non-UUID token,
# a syntactically-malformed UUID, and a real UUID v1 (so the
# version-4 check, not just the parse, is exercised).
_invalid_request_id_strategy = st.sampled_from(
    [
        "",
        "not-a-uuid",
        "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        _VALID_UUID_V1,
    ]
)


@st.composite
def invalid_ip_hash_strategy(draw: st.DrawFn) -> str:
    """Generate an ``ip_hash`` value guaranteed to fail ``^[0-9a-f]{64}$``."""
    kind = draw(
        st.sampled_from(["uppercase", "too_short", "too_long", "non_hex"])
    )
    if kind == "uppercase":
        # Force at least one uppercase hex letter; an all-digit
        # remainder of 63 chars cannot rescue the regex.
        leading = draw(st.sampled_from(["A", "B", "C", "D", "E", "F"]))
        rest = draw(st.text(alphabet=_HEX_LOWER, min_size=63, max_size=63))
        value = leading + rest
    elif kind == "too_short":
        value = draw(st.text(alphabet=_HEX_LOWER, min_size=0, max_size=63))
    elif kind == "too_long":
        value = draw(st.text(alphabet=_HEX_LOWER, min_size=65, max_size=128))
    else:  # non_hex
        bad = draw(st.sampled_from(["g", "z", "!", " ", "G"]))
        prefix = draw(st.text(alphabet=_HEX_LOWER, min_size=63, max_size=63))
        value = prefix + bad
    # Belt-and-braces: drop any value the strategy somehow produced
    # that still happens to be a valid hash (impossible by construction
    # for the four kinds above, but Hypothesis's shrinker is creative).
    assume(_IP_HASH_RE.fullmatch(value) is None)
    return value


_invalid_decision_strategy = st.text(min_size=1, max_size=20).filter(
    lambda s: s not in ALLOWED_DECISIONS
)

_invalid_latency_strategy = st.one_of(
    st.integers(min_value=-1_000_000, max_value=LATENCY_MS_MIN - 1),
    st.integers(min_value=LATENCY_MS_MAX + 1, max_value=10_000_000),
)

_invalid_cost_strategy = st.one_of(
    st.decimals(
        min_value=Decimal("-1000"),
        max_value=Decimal("-0.000001"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    ),
    st.decimals(
        min_value=Decimal("1.000001"),
        max_value=Decimal("1000"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    ),
)


@st.composite
def log_record_args_with_one_bad_field(
    draw: st.DrawFn,
) -> tuple[dict[str, Any], str]:
    """Return ``(kwargs, corrupted_field)`` with exactly one bad field.

    All other fields are pinned to fixed valid values so the field
    under test is the only possible reason ``__post_init__`` raises.
    """
    field = draw(
        st.sampled_from(
            [
                "request_id",
                "ip_hash",
                "decision",
                "latency_ms",
                "estimated_cost_usd",
            ]
        )
    )
    args: dict[str, Any] = {
        "request_id": _VALID_UUID_V4,
        "ip_hash": "0" * 64,
        "decision": "accepted",
        "model_id": "test-model",
        "voice_id": "test-voice",
        "latency_ms": 100,
        "estimated_cost_usd": Decimal("0.500000"),
        "ts": datetime(2024, 1, 1, tzinfo=timezone.utc),
    }
    if field == "request_id":
        args["request_id"] = draw(_invalid_request_id_strategy)
    elif field == "ip_hash":
        args["ip_hash"] = draw(invalid_ip_hash_strategy())
    elif field == "decision":
        args["decision"] = draw(_invalid_decision_strategy)
    elif field == "latency_ms":
        args["latency_ms"] = draw(_invalid_latency_strategy)
    else:  # estimated_cost_usd
        args["estimated_cost_usd"] = draw(_invalid_cost_strategy)
    return args, field


# ---------------------------------------------------------------------------
# Strategies -- invalid emit_metric arguments (Test 5)
# ---------------------------------------------------------------------------


@st.composite
def invalid_metric_args_strategy(
    draw: st.DrawFn,
) -> tuple[dict[str, Any], str]:
    """Return ``(kwargs, kind)`` for one of the documented bad shapes.

    All other fields are pinned to valid values so the assertion that
    a particular bad field triggers the validation error is unambiguous.
    """
    kind = draw(
        st.sampled_from(
            [
                "empty_name",
                "bad_name_chars",
                "nan_value",
                "inf_value",
                "neg_inf_value",
                "unknown_unit",
                "empty_dim_key",
                "empty_dim_value",
            ]
        )
    )
    args: dict[str, Any] = {
        "name": METRIC_JOKES_PER_HOUR,
        "value": 1.0,
        "unit": "Count",
        "dimensions": None,
    }
    if kind == "empty_name":
        args["name"] = ""
    elif kind == "bad_name_chars":
        args["name"] = draw(
            st.sampled_from(
                ["jokes!per!hour", "bad name", "metric.name", "!", "j!"]
            )
        )
    elif kind == "nan_value":
        args["value"] = float("nan")
    elif kind == "inf_value":
        args["value"] = float("inf")
    elif kind == "neg_inf_value":
        args["value"] = float("-inf")
    elif kind == "unknown_unit":
        args["unit"] = draw(st.sampled_from(["Foo", "Bar", "Joules", "kg"]))
    elif kind == "empty_dim_key":
        args["dimensions"] = {"": "value"}
    else:  # empty_dim_value
        args["dimensions"] = {"key": ""}
    return args, kind


# ---------------------------------------------------------------------------
# Property 30 -- per-request structured log schema
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(record=valid_log_records())
def test_property_30_valid_record_emits_one_well_formed_json_line(
    record: LogRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 30: a valid LogRecord emits exactly one JSON line.

    The emitted line decodes to a dict whose keys are exactly the
    eight design-mandated fields, with values matching the per-field
    shape constraints from ``design.md`` § Data Models > Structured
    Log Record.

    **Validates: Requirements 16.1**
    """
    captured: list[str] = []
    monkeypatch.setattr(
        observability,
        "_emit_to_stdout",
        lambda line: captured.append(line),
    )

    emit_log(record)

    # Exactly one record per request (R16.1, Property 30).
    assert len(captured) == 1, (
        f"emit_log produced {len(captured)} lines, expected exactly 1"
    )

    parsed = json.loads(captured[0])
    expected_keys = {
        "request_id",
        "ip_hash",
        "decision",
        "model_id",
        "voice_id",
        "latency_ms",
        "estimated_cost_usd",
        "ts",
    }
    assert set(parsed.keys()) == expected_keys, (
        f"emitted keys {set(parsed.keys())!r} do not equal "
        f"{expected_keys!r}"
    )

    # request_id round-trips byte-for-byte.
    assert parsed["request_id"] == record.request_id

    # ip_hash is a 64-char lowercase hex string.
    assert isinstance(parsed["ip_hash"], str)
    assert _IP_HASH_RE.fullmatch(parsed["ip_hash"]) is not None, (
        f"ip_hash {parsed['ip_hash']!r} is not 64 lowercase hex chars"
    )

    # decision is one of the four allowed tokens.
    assert parsed["decision"] in ALLOWED_DECISIONS

    # model_id and voice_id are non-empty strings.
    assert isinstance(parsed["model_id"], str) and parsed["model_id"] != ""
    assert isinstance(parsed["voice_id"], str) and parsed["voice_id"] != ""

    # latency_ms is an int in [0, 60_000].
    # ``bool`` is a subclass of int in Python; explicitly reject it
    # so a future regression that emitted ``True``/``False`` would
    # be caught here.
    assert isinstance(parsed["latency_ms"], int)
    assert not isinstance(parsed["latency_ms"], bool)
    assert LATENCY_MS_MIN <= parsed["latency_ms"] <= LATENCY_MS_MAX

    # estimated_cost_usd is a float in [0.0, 1.0]. (R16.1 specifies
    # six decimal places; ``to_json_dict`` quantizes and casts to
    # ``float`` before encoding.)
    assert isinstance(parsed["estimated_cost_usd"], float)
    assert 0.0 <= parsed["estimated_cost_usd"] <= 1.0

    # ts is an ISO 8601 UTC string with whole-second precision.
    assert isinstance(parsed["ts"], str)
    assert _TS_RE.fullmatch(parsed["ts"]) is not None, (
        f"ts {parsed['ts']!r} does not match YYYY-MM-DDTHH:MM:SSZ"
    )


@PBT_SETTINGS
@given(corrupt=log_record_args_with_one_bad_field())
def test_property_30_invalid_log_record_raises_validation_error(
    corrupt: tuple[dict[str, Any], str],
) -> None:
    """Property 30: building a LogRecord with any malformed field raises.

    Exactly one of (request_id, ip_hash, decision, latency_ms,
    estimated_cost_usd) is replaced with a value outside the schema;
    every other field is a known-good constant. The error therefore
    must come from the corrupted field's branch in
    :meth:`LogRecord.__post_init__` and must surface as
    :class:`ObservabilityValidationError` (the validation half of the
    validation-vs-transport split).

    **Validates: Requirements 16.1**
    """
    args, _field = corrupt
    with pytest.raises(ObservabilityValidationError):
        LogRecord(**args)


# ---------------------------------------------------------------------------
# Property 35 -- observability emission failures are soft-failures
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(record=valid_log_records())
def test_property_35_emit_log_soft_fails_on_stdout_write_error(
    record: LogRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 35: emit_log soft-fails when the stdout writer raises.

    The ``_emit_to_stdout`` seam is replaced with a function that
    raises ``OSError`` (simulating a "disk full" / closed-pipe write
    failure inside the Lambda runtime). :func:`emit_log` must NOT
    propagate the exception, must return ``None``, and must increment
    the internal observability-failure counter exactly once.

    **Validates: Requirements 16.8**
    """
    reset_failure_count()

    def failing_writer(line: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(observability, "_emit_to_stdout", failing_writer)

    # No exception escapes; the call returns None.
    result = emit_log(record)
    assert result is None

    # Counter incremented exactly once for the single failure.
    assert get_failure_count() == 1, (
        f"expected get_failure_count() == 1 after one transport "
        f"failure, got {get_failure_count()}"
    )


@PBT_SETTINGS
@given(
    name=st.sampled_from(_VALID_METRIC_NAMES),
    value=st.floats(
        min_value=0.0,
        max_value=1e6,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_property_35_emit_metric_soft_fails_on_transport_error(
    name: str, value: float
) -> None:
    """Property 35: emit_metric soft-fails when put_metric_data raises.

    The CloudWatch stub raises ``ClientError`` with a Throttling code
    on every call. :func:`emit_metric` must NOT propagate the
    exception, must return ``None``, and must increment the internal
    observability-failure counter exactly once. The stub still
    records the single attempt so we can assert the call was actually
    issued.

    **Validates: Requirements 16.8**
    """
    reset_failure_count()
    stub = _CloudWatchStub(mode="throttled")

    result = emit_metric(name, value, cloudwatch_client=stub)

    assert result is None
    assert get_failure_count() == 1
    assert len(stub.calls) == 1, (
        f"stub.put_metric_data was called {len(stub.calls)} times; "
        f"expected exactly 1 attempt before the soft-fail returns"
    )


@PBT_SETTINGS
@given(corrupt=invalid_metric_args_strategy())
def test_property_35_emit_metric_validation_errors_raise_not_soft_fail(
    corrupt: tuple[dict[str, Any], str],
) -> None:
    """Property 35 (corollary): validation errors RAISE; counter unchanged.

    The validation-vs-transport split documented in
    ``observability.py`` says validation errors are programmer bugs
    and must surface eagerly: empty / typo'd metric name, NaN or
    Infinity value, unknown unit, dimensions with empty key/value all
    raise :class:`ObservabilityValidationError` BEFORE
    ``put_metric_data`` is invoked. Because no transport call is
    attempted, the soft-fail counter is NOT incremented and the stub
    is never touched.

    **Validates: Requirements 16.8** (counter only counts transport
    failures, not programmer errors).
    """
    args, _kind = corrupt
    reset_failure_count()
    stub = _CloudWatchStub(mode="success")

    with pytest.raises(ObservabilityValidationError):
        emit_metric(
            args["name"],
            args["value"],
            args["unit"],
            dimensions=args["dimensions"],
            cloudwatch_client=stub,
        )

    assert get_failure_count() == 0, (
        f"validation errors must NOT increment the soft-fail counter; "
        f"got {get_failure_count()}"
    )
    assert stub.calls == [], (
        "validation errors must short-circuit before put_metric_data"
    )


@PBT_SETTINGS
@given(n=st.integers(min_value=1, max_value=10))
def test_property_35_counter_increments_exactly_n_times_for_n_failures(
    n: int,
) -> None:
    """Property 35: N independent transport failures yield counter == N.

    Sequentially invokes :func:`emit_metric` ``n`` times against a
    stub that fails every call; asserts the counter equals ``n``
    afterwards (no double-counting, no skipped increments) and that
    every attempt actually reached the transport before being caught.

    **Validates: Requirements 16.8**
    """
    reset_failure_count()
    stub = _CloudWatchStub(mode="throttled")

    for _ in range(n):
        result = emit_metric(
            METRIC_JOKES_PER_HOUR, 1.0, cloudwatch_client=stub
        )
        assert result is None

    assert get_failure_count() == n, (
        f"expected counter == {n} after {n} transport failures, "
        f"got {get_failure_count()}"
    )
    assert len(stub.calls) == n
