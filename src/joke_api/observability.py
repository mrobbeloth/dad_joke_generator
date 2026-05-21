"""Structured logging and CloudWatch metrics for the Joke_API.

This module is the umbrella observability layer described in
``design.md`` § Components and Interfaces. It owns three concerns:

* **Structured logging** -- :func:`emit_log` writes a single
  JSON-encoded record per request to CloudWatch Logs (via Lambda's
  stdout pipe) so the audit trail in
  ``design.md`` § Data Models > Structured Log Record (R16.1) is
  produced exactly once per request.
* **CloudWatch metrics** -- :func:`emit_metric` calls
  ``cloudwatch:PutMetricData`` for the four request-decision and
  failure counters mandated by R16.2 plus an internal
  observability-failure counter (R16.8).
* **Soft-fail accounting** -- :func:`get_failure_count` and
  :func:`reset_failure_count` expose a process-local counter that
  :func:`emit_log` and :func:`emit_metric` increment when their
  underlying transport raises. Callers (the handler) never have to
  catch observability errors; they cannot fail a request.

Cost-alert and ops-alert dispatchers (R16.3..R16.6) are implemented
in this same module by task 9.3; the alerting half lives below the
logs/metrics half so the two concerns can evolve independently
behind the shared module surface.

Validated requirements (``requirements.md`` § Requirement 16)
-------------------------------------------------------------
* **R16.1** -- :func:`emit_log` produces one structured JSON record
  per request with the eight design-mandated fields
  (``request_id``, ``ip_hash``, ``decision``, ``model_id``,
  ``voice_id``, ``latency_ms``, ``estimated_cost_usd``, ``ts``).
  The "within 2 s of request completion" budget is a *handler*
  obligation -- this module's contract is "respond promptly when
  called". The handler invokes :func:`emit_log` as the last step
  of every request; this module does no I/O beyond writing one
  JSON line, so the 2 s envelope is satisfied trivially.
* **R16.2** -- :func:`emit_metric` is the single entry point for
  the four CloudWatch metric names exposed as module constants:
  :data:`METRIC_JOKES_PER_HOUR`,
  :data:`METRIC_MODERATION_REJECTIONS_PER_HOUR`,
  :data:`METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR`, and
  :data:`METRIC_OBSERVABILITY_FAILURE`. All four are published in
  the same :data:`CLOUDWATCH_NAMESPACE` (``"dadjokes"``).
* **R16.7** -- :class:`LogRecord` validates ``ip_hash`` as a 64-char
  lowercase hex string in ``__post_init__`` and rejects anything
  else with :class:`ObservabilityValidationError`. A malformed
  hash never reaches the JSON encoder, which means a raw IP can
  never accidentally be written to CloudWatch Logs through this
  module.
* **R16.8** -- transport failures inside :func:`emit_log` and
  :func:`emit_metric` are *soft-failures*: the originating request
  proceeds, the internal observability-failure counter is
  incremented exactly once, and no exception escapes. The counter
  is itself published as the
  :data:`METRIC_OBSERVABILITY_FAILURE` CloudWatch metric so
  operators can alarm on observability blackouts.
* **R16.3** -- :func:`dispatch_cost_alert` is the SNS publish path
  for the daily cost alarm. The breached USD threshold and the
  cost SNS topic ARN are passed in by the alarm-event consumer
  (task 16.7's IaC), so this module enforces the *email shape*
  contract and not the alarm-evaluation contract.
* **R16.4** -- :func:`dispatch_cost_alert` only publishes on the
  ``OK -> ALARM`` transition; an ALARM-state retransmission or any
  ``OK -> OK`` / ``INSUFFICIENT_DATA -> *`` event short-circuits
  with ``delivered=False, attempts=0``. The subject always carries
  :data:`COST_ALERT_SUBJECT_PREFIX` and the threshold formatted as
  ``$X.YY``.
* **R16.5** -- on transport error, :func:`dispatch_cost_alert`
  retries the SNS publish up to 3 additional times (4 total
  attempts) at :data:`COST_ALERT_RETRY_INTERVAL_SECONDS` second
  intervals. The retry sleep is performed via the module-level
  :data:`_RETRY_SLEEP` callable so tests can monkey-patch it to a
  no-op without waiting four real minutes per test.
* **R16.6** -- :func:`dispatch_ops_alert` publishes on a SEPARATE
  ops SNS topic with a subject carrying
  :data:`OPS_ALERT_SUBJECT_PREFIX`. The prefix itself does not
  contain the token ``cost``, satisfying the routing-by-channel
  rule in ``design.md`` § Property 33; the metric name and body
  are otherwise free-form.

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 30 (per-request structured log schema)** -- the
  validation rules in :class:`LogRecord` and the JSON shape produced
  by :meth:`LogRecord.to_json_dict` are the single source of truth
  for the eight-field record. The property test in task 9.2 asserts
  the captured log matches exactly.
* **Property 35 (observability emission failures are soft-failures)**
  -- :func:`emit_log` and :func:`emit_metric` catch every transport
  error, increment :data:`get_failure_count` exactly once, and never
  re-raise. Validation errors are intentionally *not* soft-failures:
  they signal a programmer bug (e.g. building a malformed
  :class:`LogRecord`) and must be surfaced eagerly, before any
  emission is attempted. See "Validation vs. transport split" below.
* **Property 31 (cost-alert email subject and gating)** --
  :func:`dispatch_cost_alert` builds the cost-alert subject from
  :data:`COST_ALERT_SUBJECT_PREFIX` + ``$X.YY`` and gates publication
  on ``current_state == "ALARM" and previous_state != "ALARM"``.
* **Property 32 (cost-email retry caps at three attempts)** --
  :func:`dispatch_cost_alert` performs at most
  :data:`MAX_COST_ALERT_ATTEMPTS` SNS publishes (1 initial + 3
  retries) per call, with successive attempts spaced
  :data:`COST_ALERT_RETRY_INTERVAL_SECONDS` seconds apart via the
  injectable :data:`_RETRY_SLEEP` seam.
* **Property 33 (ops-alert email subject, channel, and trigger
  thresholds)** -- :func:`dispatch_ops_alert` builds an
  :data:`OPS_ALERT_SUBJECT_PREFIX`-prefixed subject, publishes only
  to the ops SNS topic, and never re-uses the cost topic. The
  subject prefix does not contain the literal ``cost``, so any
  receiver that routes on subject text can disambiguate cost vs
  ops alerts. The threshold-evaluation contract itself lives in
  CloudWatch (provisioned by task 16.7); this module is the
  email-shape chokepoint.

Validation vs. transport split
------------------------------
R16.8 / Property 35 talks about *emission failures*, by which the
spec means network/IO/serialization errors during the call to
CloudWatch Logs or :func:`PutMetricData`. Building a
:class:`LogRecord` with a malformed UUID or non-hex ``ip_hash`` is a
programmer error -- soft-failing it would let bad records silently
disappear and would risk leaking a raw IP address into the log
stream (a direct R16.7 violation). The split is therefore:

* :class:`LogRecord` validation runs in ``__post_init__`` and
  raises :class:`ObservabilityValidationError`.
* :func:`emit_log` only handles transport / serialization errors;
  on failure it increments the internal counter and returns
  ``None``.
* :func:`emit_metric` validates the metric name shape (rejecting
  obviously bad input via :class:`ObservabilityValidationError`),
  then soft-fails on any boto3 error.

Public surface
--------------
* :data:`ALLOWED_DECISIONS` -- the four ``decision`` outcomes
  permitted by R16.1.
* :data:`CLOUDWATCH_NAMESPACE` -- ``"dadjokes"``.
* :data:`METRIC_JOKES_PER_HOUR`,
  :data:`METRIC_MODERATION_REJECTIONS_PER_HOUR`,
  :data:`METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR`,
  :data:`METRIC_OBSERVABILITY_FAILURE` -- metric name constants.
* :data:`LATENCY_MS_MIN` / :data:`LATENCY_MS_MAX` -- 0..60000.
* :data:`COST_USD_MIN` / :data:`COST_USD_MAX` -- 0.0..1.0.
* :data:`COST_USD_DECIMALS` -- 6 decimal places (R16.1).
* :class:`LogRecord` -- frozen + slotted dataclass with
  ``__post_init__`` validation and a :meth:`to_json_dict` method.
* :class:`ObservabilityValidationError` -- raised on programmer
  errors building a :class:`LogRecord` or calling
  :func:`emit_metric` with bad arguments.
* :func:`emit_log` -- write one structured JSON record.
* :func:`emit_metric` -- publish one CloudWatch metric data point.
* :func:`get_failure_count` -- read the internal counter.
* :func:`reset_failure_count` -- reset the internal counter.
* :data:`COST_ALERT_SUBJECT_PREFIX` (``"[COST-ALERT]"``),
  :data:`OPS_ALERT_SUBJECT_PREFIX` (``"[OPS-ALERT]"``),
  :data:`MAX_COST_ALERT_ATTEMPTS` (``4``),
  :data:`COST_ALERT_RETRY_INTERVAL_SECONDS` (``60``),
  :data:`COST_TOPIC_ARN_ENV_VAR`, :data:`OPS_TOPIC_ARN_ENV_VAR`,
  :data:`ALARM_STATES` -- alert dispatcher constants.
* :class:`AlertDispatchResult` -- frozen + slotted dataclass
  reporting the outcome of an alert publish.
* :func:`dispatch_cost_alert` -- transition-gated, retrying SNS
  publish for the cost alarm (R16.3, R16.4, R16.5; Properties 31,
  32).
* :func:`dispatch_ops_alert` -- single-shot SNS publish for ops
  alarms on a separate topic (R16.6; Property 33).

Test injection
--------------
Tests inject behavior via three seams:

* :func:`emit_metric` accepts a ``cloudwatch_client=`` keyword so
  unit tests can pass a stub exposing ``put_metric_data``.
* :func:`emit_log` writes to stdout via the module-private
  :func:`_emit_to_stdout` helper. Tests that want to simulate a
  transport failure monkey-patch this helper to raise.
* :func:`dispatch_cost_alert` and :func:`dispatch_ops_alert` accept
  a ``sns_client=`` keyword exposing ``publish``; the cost-alert
  retry sleep is performed via the module-level
  :data:`_RETRY_SLEEP` callable so tests can monkey-patch the
  inter-attempt wait to a no-op.

This file is intentionally the single home for the
``joke_api.observability`` namespace; alert dispatchers live below
the logs/metrics half so the surface stays browsable.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

__all__ = [
    "ALLOWED_DECISIONS",
    "CLOUDWATCH_NAMESPACE",
    "METRIC_JOKES_PER_HOUR",
    "METRIC_MODERATION_REJECTIONS_PER_HOUR",
    "METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR",
    "METRIC_OBSERVABILITY_FAILURE",
    "LATENCY_MS_MIN",
    "LATENCY_MS_MAX",
    "COST_USD_MIN",
    "COST_USD_MAX",
    "COST_USD_DECIMALS",
    "LogRecord",
    "ObservabilityValidationError",
    "emit_log",
    "emit_metric",
    "get_failure_count",
    "reset_failure_count",
    # Alert dispatchers (task 9.3, R16.3..R16.6, Properties 31, 32, 33).
    "ALARM_STATES",
    "AlertDispatchResult",
    "COST_ALERT_RETRY_INTERVAL_SECONDS",
    "COST_ALERT_SUBJECT_PREFIX",
    "COST_TOPIC_ARN_ENV_VAR",
    "MAX_COST_ALERT_ATTEMPTS",
    "OPS_ALERT_SUBJECT_PREFIX",
    "OPS_TOPIC_ARN_ENV_VAR",
    "dispatch_cost_alert",
    "dispatch_ops_alert",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Permitted ``decision`` values for a :class:`LogRecord` (R16.1). A
#: tuple constant matches the project's other enum-style constants
#: (e.g. ``KNOWN_STATUSES`` in ``scripts/plan_parser.py``) and is
#: cheap to import without forcing a separate ``Enum`` class.
ALLOWED_DECISIONS: Final[tuple[str, ...]] = (
    "accepted",
    "moderation_rejected",
    "rate_limited",
    "error",
)

#: CloudWatch namespace shared by every metric the Joke_API emits
#: (R16.2). Matches the namespace used by the alarms provisioned by
#: IaC (task 16.x); changing this constant requires a paired infra
#: change.
CLOUDWATCH_NAMESPACE: Final[str] = "dadjokes"

#: Metric name -- successful joke generations (R16.2).
METRIC_JOKES_PER_HOUR: Final[str] = "jokes_per_hour"

#: Metric name -- moderation rejections (R16.2).
METRIC_MODERATION_REJECTIONS_PER_HOUR: Final[str] = (
    "moderation_rejections_per_hour"
)

#: Metric name -- rate-limit rejections (R16.2).
METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR: Final[str] = (
    "rate_limit_rejections_per_hour"
)

#: Metric name -- internal observability emission failures (R16.8).
#: Operators alarm on a non-zero rate of this metric to detect a
#: silent observability blackout.
METRIC_OBSERVABILITY_FAILURE: Final[str] = "observability_failure"

#: Inclusive lower bound for ``latency_ms`` (R16.1).
LATENCY_MS_MIN: Final[int] = 0

#: Inclusive upper bound for ``latency_ms`` (R16.1). 60 000 ms == 60 s,
#: which is the API Gateway integration timeout ceiling.
LATENCY_MS_MAX: Final[int] = 60_000

#: Inclusive lower bound for ``estimated_cost_usd`` (R16.1).
COST_USD_MIN: Final[Decimal] = Decimal("0.000000")

#: Inclusive upper bound for ``estimated_cost_usd`` (R16.1).
COST_USD_MAX: Final[Decimal] = Decimal("1.000000")

#: Decimal precision applied to ``estimated_cost_usd`` when the JSON
#: record is built (R16.1: "decimal, 0.000000 to 1.000000, six
#: decimal places").
COST_USD_DECIMALS: Final[int] = 6

#: Permitted CloudWatch metric units. CloudWatch accepts a fixed
#: vocabulary; we expose the small subset the Joke_API actually uses
#: so callers cannot pass a typo'd unit string that ``put_metric_data``
#: would later reject.
_ALLOWED_METRIC_UNITS: Final[frozenset[str]] = frozenset(
    {"Count", "Milliseconds", "Seconds", "Bytes", "None"}
)

# Compiled regex for the salted-IP-hash format check (R16.7). The
# hash is the lowercase hex digest of a SHA-256, so exactly 64
# characters drawn from ``[0-9a-f]``.
_IP_HASH_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

# Compiled regex for CloudWatch metric names. The CloudWatch service
# limits metric names to 1..255 chars from a generous character set;
# we restrict to ``[A-Za-z0-9_]`` (matching the design's metric
# constants) so callers cannot accidentally publish a typo'd name.
_METRIC_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]{1,255}$")

# Format string for the ``ts`` field. Whole-second precision matches
# joke_store.py's ISO 8601 format for cross-module consistency
# (Property 40 round-trips on the same string shape).
_TS_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"

# Dedicated module logger. ``propagate=False`` keeps observability
# log lines out of any handler the application installs on the root
# logger; we want exactly one CloudWatch log line per :func:`emit_log`
# call, written via :func:`_emit_to_stdout`. Tests can rebind
# :func:`_emit_to_stdout` directly to inspect or fail the write.
_logger = logging.getLogger("joke_api.observability")
_logger.propagate = False


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class ObservabilityValidationError(ValueError):
    """Raised when a :class:`LogRecord` or metric argument is malformed.

    These are *programmer errors* -- a malformed ``ip_hash``, an
    unknown ``decision`` token, an out-of-range ``latency_ms`` -- not
    transport failures. The split between validation errors (raised)
    and transport errors (soft-failed) is documented in the module
    docstring above.

    Attributes:
        field: The offending field name.
        reason: Short description of the violation.
    """

    __slots__ = ("field", "reason")

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"observability validation failed for {field}: {reason}")


# ---------------------------------------------------------------------------
# LogRecord
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class LogRecord:
    """A single per-request structured log record (R16.1, Property 30).

    The eight fields mirror the JSON schema in ``design.md`` § Data
    Models > Structured Log Record. All fields are validated in
    :meth:`__post_init__` so an invalid record cannot reach
    :func:`emit_log` -- this guarantees, in particular, that a raw
    IP address can never be smuggled through the ``ip_hash`` field
    (R16.7).

    Attributes:
        request_id: Per-request UUID v4, formatted as a 36-char
            hex-with-dashes string. Validated via :func:`uuid.UUID`.
        ip_hash: Salted SHA-256 hash of the source IP, encoded as a
            64-character lowercase hex string (R16.7). Raw IPs and
            uppercase hex are rejected.
        decision: One of :data:`ALLOWED_DECISIONS`.
        model_id: Bedrock model identifier; non-empty string.
        voice_id: Polly voice identifier; non-empty string.
        latency_ms: End-to-end request latency in milliseconds;
            integer in ``[LATENCY_MS_MIN, LATENCY_MS_MAX]``.
        estimated_cost_usd: Estimated request cost in USD;
            ``Decimal`` or ``float`` in ``[COST_USD_MIN, COST_USD_MAX]``.
            Stored verbatim; rounded to :data:`COST_USD_DECIMALS`
            places only when :meth:`to_json_dict` builds the JSON
            payload.
        ts: Timezone-aware UTC datetime of request completion.
    """

    request_id: str
    ip_hash: str
    decision: str
    model_id: str
    voice_id: str
    latency_ms: int
    estimated_cost_usd: Decimal | float
    ts: datetime

    def __post_init__(self) -> None:
        # ---- request_id: UUID v4 ----
        if not isinstance(self.request_id, str) or self.request_id == "":
            raise ObservabilityValidationError(
                "request_id", "must be a non-empty string"
            )
        try:
            parsed = uuid.UUID(self.request_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ObservabilityValidationError(
                "request_id", f"not a valid UUID: {exc}"
            ) from exc
        if parsed.version != 4:
            raise ObservabilityValidationError(
                "request_id",
                f"must be a UUID v4 (got version {parsed.version})",
            )

        # ---- ip_hash: 64-char lowercase hex (R16.7) ----
        if not isinstance(self.ip_hash, str):
            raise ObservabilityValidationError(
                "ip_hash", "must be a string"
            )
        if not _IP_HASH_RE.fullmatch(self.ip_hash):
            raise ObservabilityValidationError(
                "ip_hash",
                "must be a 64-character lowercase hex SHA-256 digest",
            )

        # ---- decision: enum-style allowed set ----
        if self.decision not in ALLOWED_DECISIONS:
            raise ObservabilityValidationError(
                "decision",
                f"must be one of {ALLOWED_DECISIONS!r}",
            )

        # ---- model_id / voice_id: non-empty strings ----
        if not isinstance(self.model_id, str) or self.model_id == "":
            raise ObservabilityValidationError(
                "model_id", "must be a non-empty string"
            )
        if not isinstance(self.voice_id, str) or self.voice_id == "":
            raise ObservabilityValidationError(
                "voice_id", "must be a non-empty string"
            )

        # ---- latency_ms: int in [0, 60000] ----
        # ``bool`` is a subclass of ``int`` in Python; reject it
        # explicitly so ``True`` / ``False`` cannot slip through.
        if not isinstance(self.latency_ms, int) or isinstance(
            self.latency_ms, bool
        ):
            raise ObservabilityValidationError(
                "latency_ms", "must be an int"
            )
        if (
            self.latency_ms < LATENCY_MS_MIN
            or self.latency_ms > LATENCY_MS_MAX
        ):
            raise ObservabilityValidationError(
                "latency_ms",
                f"must be in [{LATENCY_MS_MIN}, {LATENCY_MS_MAX}]",
            )

        # ---- estimated_cost_usd: Decimal/float in [0, 1] ----
        if isinstance(self.estimated_cost_usd, bool):
            raise ObservabilityValidationError(
                "estimated_cost_usd",
                "must be a Decimal or float (got bool)",
            )
        if isinstance(self.estimated_cost_usd, (Decimal, int, float)):
            cost_decimal = _coerce_cost_to_decimal(self.estimated_cost_usd)
            if cost_decimal is None:
                raise ObservabilityValidationError(
                    "estimated_cost_usd",
                    "must be finite (NaN and Infinity are rejected)",
                )
            if cost_decimal < COST_USD_MIN or cost_decimal > COST_USD_MAX:
                raise ObservabilityValidationError(
                    "estimated_cost_usd",
                    f"must be in [{COST_USD_MIN}, {COST_USD_MAX}]",
                )
        else:
            raise ObservabilityValidationError(
                "estimated_cost_usd",
                "must be a Decimal, int, or float",
            )

        # ---- ts: tz-aware UTC datetime ----
        if not isinstance(self.ts, datetime):
            raise ObservabilityValidationError(
                "ts", "must be a datetime instance"
            )
        if self.ts.tzinfo is None:
            raise ObservabilityValidationError(
                "ts", "must be timezone-aware (UTC)"
            )

    def to_json_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped record per ``design.md`` § Data Models.

        ``ts`` is formatted as ``YYYY-MM-DDTHH:MM:SSZ`` (whole-second
        precision, UTC, matching the format used by
        :mod:`joke_api.joke_store`). ``estimated_cost_usd`` is
        rounded to :data:`COST_USD_DECIMALS` places and emitted as a
        ``float`` so the JSON payload is exactly the schema in the
        design document. The dict is returned with insertion order
        matching the design's documented field order; callers that
        want sorted keys (e.g. :func:`emit_log`) pass
        ``sort_keys=True`` to :func:`json.dumps`.
        """
        cost_decimal = _coerce_cost_to_decimal(self.estimated_cost_usd)
        # ``_coerce_cost_to_decimal`` returned non-None during
        # ``__post_init__`` validation, so it cannot be None here.
        assert cost_decimal is not None
        rounded = cost_decimal.quantize(
            Decimal(10) ** -COST_USD_DECIMALS
        )
        utc_ts = self.ts.astimezone(timezone.utc).replace(microsecond=0)
        return {
            "request_id": self.request_id,
            "ip_hash": self.ip_hash,
            "decision": self.decision,
            "model_id": self.model_id,
            "voice_id": self.voice_id,
            "latency_ms": self.latency_ms,
            "estimated_cost_usd": float(rounded),
            "ts": utc_ts.strftime(_TS_FORMAT),
        }


# ---------------------------------------------------------------------------
# Internal soft-fail counter
# ---------------------------------------------------------------------------

# Process-local observability-failure counter (R16.8). Lambda runs
# one invocation at a time per execution environment, but the sandbox
# is reused across requests; the lock guards against future container
# images that might run multiple workers in one process and against
# tests that exercise the counter from worker threads.
_failure_counter_lock: Final[threading.Lock] = threading.Lock()
_internal_failure_counter: int = 0


def get_failure_count() -> int:
    """Return the current internal observability-failure count (R16.8).

    Tests use this to assert that a soft-fail path incremented the
    counter exactly once per failure (Property 35). Production code
    reads it indirectly via the
    :data:`METRIC_OBSERVABILITY_FAILURE` CloudWatch metric the
    handler emits at the end of every request.
    """
    with _failure_counter_lock:
        return _internal_failure_counter


def reset_failure_count() -> None:
    """Reset the internal counter to zero.

    Intended for tests; production code should never call this. The
    counter is process-local, so a Lambda cold start naturally
    resets it.
    """
    global _internal_failure_counter
    with _failure_counter_lock:
        _internal_failure_counter = 0


def _increment_failure_counter() -> None:
    """Atomically increment the internal observability-failure counter."""
    global _internal_failure_counter
    with _failure_counter_lock:
        _internal_failure_counter += 1


# ---------------------------------------------------------------------------
# emit_log
# ---------------------------------------------------------------------------


def _emit_to_stdout(line: str) -> None:
    """Write one already-encoded log line to stdout.

    Lambda streams ``sys.stdout`` to CloudWatch Logs verbatim, so a
    single ``print``-style write lands as a single log record. Tests
    monkey-patch this function to simulate a write failure
    (Property 35); production code never calls it directly outside
    of :func:`emit_log`.
    """
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_log(record: LogRecord) -> None:
    """Emit one structured JSON log record (R16.1, Property 30).

    The ``record`` argument has already been validated in
    :meth:`LogRecord.__post_init__`, so the only failure modes here
    are JSON serialization errors (theoretically impossible given
    the dataclass fields are all primitives) and stdout-write
    failures. Both are caught, the internal observability-failure
    counter is incremented (R16.8), and the function returns
    normally so the originating request is unaffected
    (Property 35).

    Args:
        record: The :class:`LogRecord` to emit. Must be an actual
            instance of :class:`LogRecord` -- ``isinstance`` is
            enforced because :func:`emit_log` is the chokepoint
            that protects the log stream from accidental raw-IP
            leaks (R16.7).

    Raises:
        ObservabilityValidationError: When ``record`` is not a
            :class:`LogRecord` instance. This is a programmer error
            (the soft-fail path is for transport, not bad arguments).
    """
    if not isinstance(record, LogRecord):
        raise ObservabilityValidationError(
            "record", "must be a LogRecord instance"
        )
    try:
        # ``sort_keys=True`` makes the encoded line stable across
        # runs, which makes property tests (Property 30) easier to
        # write and CloudWatch Logs Insights queries easier to read.
        line = json.dumps(
            record.to_json_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        _emit_to_stdout(line)
    except Exception:  # noqa: BLE001 -- soft-fail per R16.8
        _increment_failure_counter()
        return None
    return None


# ---------------------------------------------------------------------------
# emit_metric
# ---------------------------------------------------------------------------


# Lazy default CloudWatch client. Built only when no override is
# passed to :func:`emit_metric`; tests inject their own client and
# never trigger this path.
_DEFAULT_CLOUDWATCH_CLIENT: Any | None = None

# CloudWatch client config: connect/read timeouts of 2 s and a single
# retry attempt because observability is best-effort fast-fail. A
# slow CloudWatch call must not delay the visitor response.
_DEFAULT_CLOUDWATCH_CONFIG: Final[Config] = Config(
    connect_timeout=2,
    read_timeout=2.0,
    retries={"max_attempts": 1, "mode": "standard"},
)


def _get_default_cloudwatch_client() -> Any:
    """Return the lazily-created module-level CloudWatch client."""
    global _DEFAULT_CLOUDWATCH_CLIENT
    if _DEFAULT_CLOUDWATCH_CLIENT is None:
        _DEFAULT_CLOUDWATCH_CLIENT = boto3.client(
            "cloudwatch",
            config=_DEFAULT_CLOUDWATCH_CONFIG,
        )
    return _DEFAULT_CLOUDWATCH_CLIENT


def emit_metric(
    name: str,
    value: float = 1.0,
    unit: str = "Count",
    *,
    dimensions: dict[str, str] | None = None,
    cloudwatch_client: Any | None = None,
) -> None:
    """Publish one CloudWatch metric data point (R16.2, R16.8).

    Calls ``cloudwatch:PutMetricData`` for the
    :data:`CLOUDWATCH_NAMESPACE` namespace with a single
    ``MetricData`` entry. Transport failures are soft-failed per
    R16.8: the function catches every boto3 / network error,
    increments the internal observability-failure counter exactly
    once, and returns ``None`` so the originating request is
    unaffected (Property 35).

    Args:
        name: Metric name; must match ``[A-Za-z0-9_]{1,255}``. The
            four design-mandated names are exposed as constants
            (:data:`METRIC_JOKES_PER_HOUR`,
            :data:`METRIC_MODERATION_REJECTIONS_PER_HOUR`,
            :data:`METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR`,
            :data:`METRIC_OBSERVABILITY_FAILURE`); callers should
            prefer the constants over string literals so a typo
            triggers a Python ``NameError`` rather than a silently
            mis-published metric.
        value: Numeric metric value. Defaults to ``1.0`` so the
            common "count one event" call stays terse:
            ``emit_metric(METRIC_JOKES_PER_HOUR)``.
        unit: CloudWatch metric unit. One of
            ``{"Count", "Milliseconds", "Seconds", "Bytes", "None"}``;
            defaults to ``"Count"`` to match the design's
            ``per_hour`` counters.
        dimensions: Optional dict of CloudWatch dimensions
            (e.g. ``{"Decision": "accepted"}``). Each key/value is a
            non-empty string; the dict is converted to the
            ``[{"Name": k, "Value": v}, ...]`` shape CloudWatch
            requires.
        cloudwatch_client: Optional pre-built boto3 ``cloudwatch``
            client. Used by tests to inject a stub. When omitted, a
            lazily-cached module-level client is created.

    Raises:
        ObservabilityValidationError: When ``name``, ``value``,
            ``unit``, or ``dimensions`` is malformed. Validation
            errors are programmer errors and are *not* soft-failed.
    """
    if not isinstance(name, str) or not _METRIC_NAME_RE.fullmatch(name):
        raise ObservabilityValidationError(
            "name",
            "must match [A-Za-z0-9_]{1,255}",
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservabilityValidationError(
            "value", "must be an int or float"
        )
    # CloudWatch silently drops NaN/Infinity values from
    # ``put_metric_data``; surface this as a programmer error so the
    # caller catches the bug at test time.
    if not math.isfinite(float(value)):
        raise ObservabilityValidationError(
            "value", "must be finite (NaN and Infinity are rejected)"
        )
    if unit not in _ALLOWED_METRIC_UNITS:
        raise ObservabilityValidationError(
            "unit",
            f"must be one of {sorted(_ALLOWED_METRIC_UNITS)!r}",
        )

    metric_dimensions: list[dict[str, str]] = []
    if dimensions is not None:
        if not isinstance(dimensions, dict):
            raise ObservabilityValidationError(
                "dimensions", "must be a dict[str, str] or None"
            )
        for dim_name, dim_value in dimensions.items():
            if (
                not isinstance(dim_name, str)
                or dim_name == ""
                or not isinstance(dim_value, str)
                or dim_value == ""
            ):
                raise ObservabilityValidationError(
                    "dimensions",
                    "each key and value must be a non-empty string",
                )
            metric_dimensions.append({"Name": dim_name, "Value": dim_value})

    client = (
        cloudwatch_client
        if cloudwatch_client is not None
        else _get_default_cloudwatch_client()
    )

    metric_datum: dict[str, Any] = {
        "MetricName": name,
        "Value": float(value),
        "Unit": unit,
        "Timestamp": datetime.now(tz=timezone.utc),
    }
    if metric_dimensions:
        metric_datum["Dimensions"] = metric_dimensions

    try:
        client.put_metric_data(
            Namespace=CLOUDWATCH_NAMESPACE,
            MetricData=[metric_datum],
        )
    except (BotoCoreError, ClientError):
        _increment_failure_counter()
        return None
    except Exception:  # noqa: BLE001 -- soft-fail per R16.8
        # Defensive: a stub client raising a non-boto exception (or a
        # future SDK version surfacing a new error type) must still
        # follow the soft-fail contract.
        _increment_failure_counter()
        return None
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_cost_to_decimal(value: Decimal | int | float) -> Decimal | None:
    """Convert ``value`` to :class:`Decimal`, returning None on NaN/Inf.

    ``Decimal(float)`` reflects the exact float value (which may be
    e.g. ``Decimal('0.10000000000000000555')``); for the purposes of
    range-checking against ``[0, 1]`` this is fine. The rounding to
    six decimal places happens later, in :meth:`LogRecord.to_json_dict`.

    Returns ``None`` when the input is NaN or Infinity so the caller
    can raise an explicit validation error instead of letting the
    bad value propagate to the JSON encoder.
    """
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            return None
        return value
    # int / float branch.
    if not math.isfinite(float(value)):
        return None
    return Decimal(str(float(value)))


# ===========================================================================
# Alert dispatchers (task 9.3, R16.3..R16.6, Properties 31, 32, 33)
# ===========================================================================
#
# This section is the SNS-publish half of the observability layer. It is
# kept under the same module name as the logs/metrics half so callers
# import everything from ``joke_api.observability`` regardless of
# whether they are emitting a log line or sending an alert email.
#
# Architectural notes:
#
# * **Two SNS topics, two dispatchers.** The design pins cost alerts to
#   their own SNS topic so the email subject line is the *only*
#   identifier a receiver needs to route the message; ops alerts go to
#   a separate topic. The two dispatchers therefore take separate
#   topic-ARN inputs and do not share retry state.
# * **Cost-alert retries; ops-alerts don't.** Property 32 caps the
#   cost-alert path at 1 + 3 = 4 attempts, 60 s apart. Ops alerts
#   inherit retry behavior from CloudWatch's own alarm-event redelivery
#   loop, so a single failed publish here is acceptable.
# * **Validation vs. transport split** matches :func:`emit_log` /
#   :func:`emit_metric`: bad arguments raise
#   :class:`ObservabilityValidationError`; transport errors return an
#   :class:`AlertDispatchResult` with ``delivered=False`` and bump the
#   shared :data:`_internal_failure_counter` so the existing
#   ``observability_failure`` CloudWatch metric (R16.8) reflects alert
#   blackouts.

#: Subject prefix for cost alerts (R16.4, Property 31).
COST_ALERT_SUBJECT_PREFIX: Final[str] = "[COST-ALERT]"

#: Subject prefix for ops alerts (R16.6, Property 33). The literal
#: ``cost`` does not appear in the prefix, satisfying the channel-
#: routing requirement that ops subjects must not be confusable with
#: cost subjects.
OPS_ALERT_SUBJECT_PREFIX: Final[str] = "[OPS-ALERT]"

#: Maximum total cost-alert publish attempts (R16.5, Property 32):
#: one initial publish plus up to three retries.
MAX_COST_ALERT_ATTEMPTS: Final[int] = 4

#: Seconds to wait between cost-alert retry attempts (R16.5).
COST_ALERT_RETRY_INTERVAL_SECONDS: Final[int] = 60

#: Environment variable holding the cost SNS topic ARN. The handler /
#: alarm-event consumer reads it from IaC-provisioned config and
#: passes it explicitly; this env var is the fallback so a one-off
#: operator script can call :func:`dispatch_cost_alert` without
#: re-deriving the ARN.
COST_TOPIC_ARN_ENV_VAR: Final[str] = "DADJOKES_COST_TOPIC_ARN"

#: Environment variable holding the ops SNS topic ARN. Mirrors
#: :data:`COST_TOPIC_ARN_ENV_VAR` for the ops channel.
OPS_TOPIC_ARN_ENV_VAR: Final[str] = "DADJOKES_OPS_TOPIC_ARN"

#: Permitted CloudWatch alarm-state-change states (Property 31). The
#: cost-alert gate is "current is ALARM AND previous is not ALARM",
#: which means we must accept all three CloudWatch alarm states as
#: input -- including ``INSUFFICIENT_DATA`` -- and reject anything
#: else as a programmer error.
ALARM_STATES: Final[frozenset[str]] = frozenset(
    {"OK", "ALARM", "INSUFFICIENT_DATA"}
)

#: Module-level retry sleep seam (R16.5, Property 32). Tests
#: monkey-patch this to a no-op so the cost-alert retry path can be
#: exercised in milliseconds rather than four real minutes per test.
#: Production code never reassigns this.
_RETRY_SLEEP: Callable[[float], None] = time.sleep


@dataclasses.dataclass(frozen=True, slots=True)
class AlertDispatchResult:
    """Outcome of a single :func:`dispatch_cost_alert` /
    :func:`dispatch_ops_alert` invocation.

    The dataclass is frozen + slotted to match :class:`LogRecord`
    style and so callers / tests can rely on identity-stable fields.
    ``attempts`` is always in ``[0, MAX_COST_ALERT_ATTEMPTS]``;
    ``attempts == 0`` means the dispatcher short-circuited before any
    SNS publish was attempted (e.g. the cost-alert state-transition
    gate denied the call).

    Attributes:
        delivered: ``True`` iff at least one SNS publish call
            returned successfully.
        attempts: Number of SNS publish calls actually issued.
        error: Short label describing the failure, or ``None`` on
            success. Stable values include
            ``"state_not_transitioning_to_alarm"``,
            ``"max_retries_exhausted"``, ``"sns_publish_failed"``.
        subject: The email subject that was (or would have been)
            sent. ``None`` when the dispatcher short-circuited
            before subject assembly.
        body: The email body that was (or would have been) sent.
            ``None`` when the dispatcher short-circuited before body
            assembly.
    """

    delivered: bool
    attempts: int
    error: str | None
    subject: str | None
    body: str | None


# Lazy default SNS client. Built only when neither the explicit
# ``sns_client`` kwarg nor a previously-cached client is available.
# Tests inject their own client and never trigger this path.
_DEFAULT_SNS_CLIENT: Any | None = None

# SNS client config: mirrors :data:`_DEFAULT_CLOUDWATCH_CONFIG` for
# consistency. ``read_timeout`` is slightly larger because SNS
# publishes are bigger payloads than ``put_metric_data`` calls. The
# botocore-internal retry budget is set to 1 because we run our own
# explicit retry loop in :func:`dispatch_cost_alert`; allowing
# botocore to retry would silently inflate Property 32's attempt
# count beyond 4.
_DEFAULT_SNS_CONFIG: Final[Config] = Config(
    connect_timeout=2,
    read_timeout=5.0,
    retries={"max_attempts": 1, "mode": "standard"},
)


def _get_default_sns_client() -> Any:
    """Return the lazily-created module-level SNS client."""
    global _DEFAULT_SNS_CLIENT
    if _DEFAULT_SNS_CLIENT is None:
        _DEFAULT_SNS_CLIENT = boto3.client(
            "sns",
            config=_DEFAULT_SNS_CONFIG,
        )
    return _DEFAULT_SNS_CLIENT


def _resolve_topic_arn(
    explicit_arn: str | None,
    env_var: str,
    field_name: str,
) -> str:
    """Resolve an SNS topic ARN from kwarg-or-env, raising on miss.

    Resolution order matches the design's "explicit kwarg wins,
    env var is the fallback" rule:

    1. ``explicit_arn`` if non-empty.
    2. ``os.environ[env_var]`` if set and non-empty.
    3. :class:`ObservabilityValidationError` -- this is a programmer
       / infra error, not a transport one. The Lambda was deployed
       without its required topic ARN; the right behavior is to
       surface that loudly so deploys fail fast rather than silently
       dropping every alert.
    """
    if isinstance(explicit_arn, str) and explicit_arn != "":
        return explicit_arn
    if explicit_arn is not None and not isinstance(explicit_arn, str):
        raise ObservabilityValidationError(
            field_name, "must be a string or None"
        )
    env_value = os.environ.get(env_var, "")
    if env_value != "":
        return env_value
    raise ObservabilityValidationError(
        field_name,
        (
            f"required SNS topic ARN not provided "
            f"(pass explicitly or set {env_var})"
        ),
    )


def _validate_alarm_state(value: Any, field: str) -> str:
    """Validate an alarm-state token, returning it unchanged on success."""
    if not isinstance(value, str):
        raise ObservabilityValidationError(field, "must be a string")
    if value not in ALARM_STATES:
        raise ObservabilityValidationError(
            field,
            f"must be one of {sorted(ALARM_STATES)!r}",
        )
    return value


def _validate_threshold_usd(value: Any) -> Decimal:
    """Validate ``breached_threshold_usd`` and return it as a Decimal.

    Reuses the :func:`_coerce_cost_to_decimal` helper that already
    lives in this module for the structured-log cost field, so the
    NaN/Infinity rejection rules are identical across the file. The
    upper bound is :data:`COST_USD_MAX` (1.0) for the per-record
    cost field; the *cost-alert* threshold is a daily aggregate
    that the design caps at 10 000 USD (R16.3 says
    1.00..10000.00). We therefore range-check against
    ``[0, 10_000]`` here rather than reusing :data:`COST_USD_MAX`.
    """
    if isinstance(value, bool):
        raise ObservabilityValidationError(
            "breached_threshold_usd", "must be a Decimal or float (got bool)"
        )
    if not isinstance(value, (Decimal, int, float)):
        raise ObservabilityValidationError(
            "breached_threshold_usd",
            "must be a Decimal, int, or float",
        )
    threshold = _coerce_cost_to_decimal(value)
    if threshold is None:
        raise ObservabilityValidationError(
            "breached_threshold_usd",
            "must be finite (NaN and Infinity are rejected)",
        )
    if threshold < Decimal("0") or threshold > Decimal("10000"):
        raise ObservabilityValidationError(
            "breached_threshold_usd",
            "must be in [0, 10000] USD",
        )
    return threshold


def _validate_finite_float(value: Any, field: str) -> float:
    """Validate that ``value`` is a finite int/float, returning float(value)."""
    if isinstance(value, bool):
        raise ObservabilityValidationError(field, "must be a number (got bool)")
    if not isinstance(value, (int, float)):
        raise ObservabilityValidationError(field, "must be an int or float")
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ObservabilityValidationError(
            field, "must be finite (NaN and Infinity are rejected)"
        )
    return coerced


def dispatch_cost_alert(
    *,
    breached_threshold_usd: Decimal | float,
    previous_state: str,
    current_state: str,
    sns_client: Any | None = None,
    cost_topic_arn: str | None = None,
) -> AlertDispatchResult:
    """Publish a cost-alert email on the cost SNS topic.

    Implements R16.3 / R16.4 / R16.5 and Properties 31 and 32:

    * Gates publication on the ``OK -> ALARM`` (or
      ``INSUFFICIENT_DATA -> ALARM``) transition only. An ALARM-state
      retransmission, an ``OK -> OK`` event, or any non-ALARM
      ``current_state`` short-circuits with
      ``AlertDispatchResult(delivered=False, attempts=0,
      error="state_not_transitioning_to_alarm", ...)`` and no SNS
      call is made.
    * Builds the subject as
      ``f"{COST_ALERT_SUBJECT_PREFIX} cost threshold breached: $X.YY"``.
      The body is a short multi-line string repeating the prefix and
      threshold so receivers without subject visibility (e.g. SMS
      delivery) still see the alert classification.
    * Retries failed SNS publishes up to
      :data:`MAX_COST_ALERT_ATTEMPTS` total attempts (1 initial + 3
      retries), waiting :data:`COST_ALERT_RETRY_INTERVAL_SECONDS`
      seconds between attempts via the injectable
      :data:`_RETRY_SLEEP` callable. After all attempts fail, returns
      ``error="max_retries_exhausted"`` and the
      :data:`_internal_failure_counter` is incremented for each
      failed attempt so the existing
      ``observability_failure`` CloudWatch metric reflects every
      missed publish.

    Args:
        breached_threshold_usd: The dollar threshold the alarm
            tripped on. Decimal or float in ``[0, 10000]`` USD;
            NaN/Infinity rejected. Formatted as ``$X.YY`` in both
            the subject and body.
        previous_state: The CloudWatch alarm state immediately
            *before* this transition. Must be in :data:`ALARM_STATES`.
        current_state: The CloudWatch alarm state *after* the
            transition. Must be in :data:`ALARM_STATES`. The
            dispatcher only publishes when this is ``"ALARM"`` and
            ``previous_state != "ALARM"``.
        sns_client: Optional pre-built boto3 ``sns`` client. Used by
            tests to inject a stub exposing ``publish``. When
            omitted, a lazily-cached module-level client is created.
        cost_topic_arn: The SNS topic ARN to publish to. Optional;
            falls back to ``$DADJOKES_COST_TOPIC_ARN`` and finally
            raises :class:`ObservabilityValidationError`.

    Returns:
        :class:`AlertDispatchResult` summarizing whether the publish
        succeeded, how many attempts were made, the subject and
        body strings, and a short error label on failure.

    Raises:
        ObservabilityValidationError: When any argument is malformed
            or no topic ARN can be resolved. Programmer / infra
            errors are *not* soft-failed.
    """
    # ---- validate inputs ---------------------------------------------------
    threshold_decimal = _validate_threshold_usd(breached_threshold_usd)
    previous_state = _validate_alarm_state(previous_state, "previous_state")
    current_state = _validate_alarm_state(current_state, "current_state")
    topic_arn = _resolve_topic_arn(
        cost_topic_arn, COST_TOPIC_ARN_ENV_VAR, "cost_topic_arn"
    )

    # ---- transition gate (R16.4, Property 31) ------------------------------
    # Cost alert fires iff current is ALARM AND previous is not ALARM.
    # An ALARM-state retransmission (ALARM -> ALARM), an OK -> OK, or
    # any path where current is not ALARM all short-circuit. Tests
    # rely on attempts=0 here so they can distinguish "we deliberately
    # did not call SNS" from "we tried and failed".
    if not (current_state == "ALARM" and previous_state != "ALARM"):
        return AlertDispatchResult(
            delivered=False,
            attempts=0,
            error="state_not_transitioning_to_alarm",
            subject=None,
            body=None,
        )

    # ---- build subject + body (R16.4, Property 31) -------------------------
    # The subject must contain the literal ``[COST-ALERT]`` prefix and
    # the threshold formatted as USD with two decimals so subject-only
    # receivers (e.g. SES filter rules) can route on it. Decimal
    # supports ``f"{value:.2f}"`` directly as of Python 3.6.
    subject = (
        f"{COST_ALERT_SUBJECT_PREFIX} cost threshold breached: "
        f"${threshold_decimal:.2f}"
    )
    body_lines = [
        f"{COST_ALERT_SUBJECT_PREFIX} Daily AWS cost threshold breached.",
        f"Threshold: ${threshold_decimal:.2f} USD",
        f"Alarm state: {previous_state} -> {current_state}",
        "Triggered by the dadjokes cost CloudWatch alarm.",
    ]
    body = "\n".join(body_lines)

    client = sns_client if sns_client is not None else _get_default_sns_client()

    # ---- publish with retry (R16.5, Property 32) ---------------------------
    # Loop bound is MAX_COST_ALERT_ATTEMPTS (4); we sleep AFTER a
    # failure but only if there is at least one attempt remaining.
    # The sleep goes through the module-level _RETRY_SLEEP seam so
    # tests can patch it to a no-op.
    last_error: str | None = None
    attempts = 0
    for attempt_index in range(MAX_COST_ALERT_ATTEMPTS):
        attempts += 1
        try:
            client.publish(
                TopicArn=topic_arn,
                Subject=subject,
                Message=body,
            )
        except (BotoCoreError, ClientError) as exc:
            last_error = "sns_publish_failed"
            _increment_failure_counter()
            # Defensive ``repr`` cap: we never put the exception into
            # the AlertDispatchResult to avoid leaking internal
            # details to a caller that might surface this struct in a
            # response, but we do log it via emit_log if the caller
            # wires one up; for now the failure counter is the
            # observable signal.
            del exc
        except Exception:  # noqa: BLE001 -- soft-fail per R16.8
            # A stub client raising a non-boto exception (or a future
            # SDK version surfacing a new error type) must still
            # follow the soft-fail contract.
            last_error = "sns_publish_failed"
            _increment_failure_counter()
        else:
            return AlertDispatchResult(
                delivered=True,
                attempts=attempts,
                error=None,
                subject=subject,
                body=body,
            )

        # Sleep between attempts only if at least one attempt remains.
        if attempt_index < MAX_COST_ALERT_ATTEMPTS - 1:
            try:
                _RETRY_SLEEP(COST_ALERT_RETRY_INTERVAL_SECONDS)
            except Exception:  # noqa: BLE001 -- never let sleep escape
                # If the test seam itself blows up, treat it as a
                # transport error so the loop continues to its bound
                # rather than letting the exception propagate.
                pass

    return AlertDispatchResult(
        delivered=False,
        attempts=attempts,
        error="max_retries_exhausted" if last_error else last_error,
        subject=subject,
        body=body,
    )


def dispatch_ops_alert(
    *,
    metric_name: str,
    current_value: float,
    threshold: float,
    sns_client: Any | None = None,
    ops_topic_arn: str | None = None,
) -> AlertDispatchResult:
    """Publish an ops-alert email on the ops SNS topic.

    Implements R16.6 and Property 33:

    * Builds an :data:`OPS_ALERT_SUBJECT_PREFIX`-prefixed subject;
      the prefix itself does not contain the literal ``cost``.
    * Publishes to a SEPARATE SNS topic (the ops topic) so receivers
      that route on subject text or topic ARN can disambiguate cost
      vs ops alerts.
    * Single-shot. Property 32's retry cap is specifically for cost
      alerts; ops alerts inherit retry behavior from CloudWatch's
      own alarm-event redelivery loop, so a transient publish
      failure is acceptable here.
    * Soft-fails transport errors via the shared
      :data:`_internal_failure_counter` so the existing
      ``observability_failure`` metric reflects ops-alert blackouts
      too.

    Args:
        metric_name: The CloudWatch metric whose threshold was
            breached. Must match :data:`_METRIC_NAME_RE`. Note that
            a metric name *may* legitimately contain the substring
            ``cost`` (e.g. a ``high_cost_per_hour`` health metric);
            the channel separation is enforced at the subject-prefix
            and topic-ARN level (Property 33), not by string-scrub
            of the metric name.
        current_value: The metric's measured value. Finite int/float.
        threshold: The configured alarm threshold. Finite int/float.
        sns_client: Optional pre-built boto3 ``sns`` client used by
            tests to inject a stub. When omitted, a lazily-cached
            module-level client is created (shared with the cost
            dispatcher; both call ``client.publish``).
        ops_topic_arn: The SNS topic ARN to publish to. Optional;
            falls back to ``$DADJOKES_OPS_TOPIC_ARN`` and finally
            raises :class:`ObservabilityValidationError`.

    Returns:
        :class:`AlertDispatchResult`. ``attempts`` is always ``1``
        on a transport error and ``1`` on success; this dispatcher
        never short-circuits with ``attempts=0`` because ops alerts
        have no transition-gate.

    Raises:
        ObservabilityValidationError: When ``metric_name``,
            ``current_value``, ``threshold``, or the topic ARN is
            malformed / unresolvable.
    """
    # ---- validate inputs ---------------------------------------------------
    if not isinstance(metric_name, str) or not _METRIC_NAME_RE.fullmatch(
        metric_name
    ):
        raise ObservabilityValidationError(
            "metric_name", "must match [A-Za-z0-9_]{1,255}"
        )
    current_value_f = _validate_finite_float(current_value, "current_value")
    threshold_f = _validate_finite_float(threshold, "threshold")
    topic_arn = _resolve_topic_arn(
        ops_topic_arn, OPS_TOPIC_ARN_ENV_VAR, "ops_topic_arn"
    )

    # ---- build subject + body (R16.6, Property 33) -------------------------
    # The subject prefix is the channel marker; the metric / value /
    # threshold are appended verbatim so subscribers see the
    # full context.
    subject = (
        f"{OPS_ALERT_SUBJECT_PREFIX} {metric_name} = "
        f"{current_value_f:.2f} (threshold {threshold_f:.2f})"
    )
    body_lines = [
        f"{OPS_ALERT_SUBJECT_PREFIX} Operational metric threshold breached.",
        f"Metric: {metric_name}",
        f"Current value: {current_value_f:.2f}",
        f"Threshold: {threshold_f:.2f}",
        "Triggered by a dadjokes ops CloudWatch alarm.",
    ]
    body = "\n".join(body_lines)

    client = sns_client if sns_client is not None else _get_default_sns_client()

    # ---- single-shot publish (R16.6) ---------------------------------------
    try:
        client.publish(
            TopicArn=topic_arn,
            Subject=subject,
            Message=body,
        )
    except (BotoCoreError, ClientError):
        _increment_failure_counter()
        return AlertDispatchResult(
            delivered=False,
            attempts=1,
            error="sns_publish_failed",
            subject=subject,
            body=body,
        )
    except Exception:  # noqa: BLE001 -- soft-fail per R16.8
        _increment_failure_counter()
        return AlertDispatchResult(
            delivered=False,
            attempts=1,
            error="sns_publish_failed",
            subject=subject,
            body=body,
        )

    return AlertDispatchResult(
        delivered=True,
        attempts=1,
        error=None,
        subject=subject,
        body=body,
    )
