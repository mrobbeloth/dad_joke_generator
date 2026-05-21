"""Per-IP daily rate limiter backed by DynamoDB.

This module enforces the per-source-IP daily generation cap defined by
Requirement 5. State lives in the single-table DynamoDB store described
in ``design.md`` § Data Models: the rate-limit row for a visitor on day
``D`` is keyed by partition key ``pk = "RL#" + ip_hash`` and sort key
``sk = "DAY#" + D`` where ``D`` is a ``YYYY-MM-DD`` UTC date string.

Public surface
--------------
* :func:`check` -- returns the current count for ``(ip_hash, day)`` and
  raises :class:`RateLimitExceeded` when ``count >= limit``.
* :func:`increment` -- atomically adds one to the counter and returns
  the new count. Issues a single DynamoDB ``UpdateItem`` with
  ``ADD #c :one`` and ``SET expires_at = if_not_exists(expires_at, :ttl)``.

Validated requirements (``requirements.md`` § Requirement 5)
-----------------------------------------------------------
* **R5.2** -- ``check`` retrieves the count for the supplied
  ``(ip_hash, day)`` only; counters from prior days live under a
  different sort key and are therefore naturally treated as zero.
* **R5.3** -- ``check`` raises :class:`RateLimitExceeded` when
  ``count >= limit``; the handler maps this to HTTP 429.
* **R5.4** -- ``increment`` is a single ``UpdateItem ADD #c :one``
  call, which DynamoDB executes atomically across concurrent
  invocations.
* **R5.5** -- ``increment`` is only invoked by the handler after every
  other pipeline stage has succeeded; the limiter itself never
  increments on a failed request.
* **R5.6** -- the TTL is set to "next UTC midnight + 60 seconds";
  combined with the day-scoped sort key, this gives an immediate
  logical reset at the boundary even if DynamoDB's background TTL
  sweep is delayed.

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 14** -- rate-limit counters increment atomically and only
  on success.
* **Property 15** -- limit-reached requests are rejected with HTTP 429.
* **Property 16** -- counters reset across UTC-day boundaries.

Configuration
-------------
The DynamoDB table name is read from the ``DADJOKES_TABLE`` environment
variable, defaulting to ``"dadjokes"``. The function-level
``table_name`` and ``dynamodb_resource`` keyword arguments are the
supported test injection points; the ``moto``-based property test in
task 3.4 supplies a temporary table via these arguments.

Reserved-word handling
----------------------
The DynamoDB attribute ``count`` is a reserved keyword, so all update
expressions reference it via the ``#c`` alias declared in
``ExpressionAttributeNames``. ``expires_at`` is aliased to ``#t`` for
consistency.

This module is intentionally small and does no logging: the handler
catches the typed exceptions below, maps them to sanitized error
categories via ``response_builder``, and emits the full technical
detail through the observability layer (R7.5, R7.6).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

__all__ = [
    "RateLimitExceeded",
    "RateLimiterUnavailable",
    "DEFAULT_TABLE_NAME",
    "TABLE_NAME_ENV_VAR",
    "check",
    "increment",
]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Environment variable consulted for the DynamoDB table name.
TABLE_NAME_ENV_VAR: str = "DADJOKES_TABLE"

#: Default table name when ``DADJOKES_TABLE`` is unset.
DEFAULT_TABLE_NAME: str = "dadjokes"

# DynamoDB attribute names (single source of truth for the schema
# referenced in design.md § Data Models).
_PK_PREFIX = "RL#"
_SK_PREFIX = "DAY#"
_ATTR_COUNT = "count"  # DynamoDB reserved word; aliased to "#c"
_ATTR_EXPIRES_AT = "expires_at"
_DAY_FORMAT = "%Y-%m-%d"

# Seconds added beyond the next UTC midnight when computing the TTL
# (R5.6 -- "reset every IP daily counter to 0 within 60 seconds of
# that boundary").
_TTL_GRACE_SECONDS = 60


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class RateLimitExceeded(Exception):
    """Raised by :func:`check` when ``count >= limit``.

    The handler maps this to HTTP 429 with a body containing
    ``resetAtUtc`` set to the next 00:00:00 UTC boundary (see
    ``response_builder.RATE_LIMITED``). The exception attributes carry
    the technical detail that the observability layer records; they
    are never surfaced to the visitor (R7.5, Property 20).

    Attributes:
        ip_hash: 64-char lowercase hex SHA-256 digest of the salted
            source IP.
        day: ``YYYY-MM-DD`` UTC date string.
        count: Current count at the time of the rejection.
        limit: Configured daily limit (per ``config.daily_limit``).
    """

    __slots__ = ("ip_hash", "day", "count", "limit")

    def __init__(self, ip_hash: str, day: str, count: int, limit: int) -> None:
        self.ip_hash = ip_hash
        self.day = day
        self.count = count
        self.limit = limit
        super().__init__(
            f"rate limit exceeded: count={count} >= limit={limit}"
        )


class RateLimiterUnavailable(Exception):
    """Raised when DynamoDB is unreachable or returns an error response.

    The handler maps this to a sanitized 503 response
    (``response_builder.UNAVAILABLE``). The original exception is
    chained via ``raise ... from exc`` so the observability layer can
    record the underlying boto3 error class and message; the visitor
    response body never contains this detail (R7.5, Property 20).

    Attributes:
        operation: ``"check"`` or ``"increment"`` -- the failing call.
    """

    __slots__ = ("operation",)

    def __init__(self, operation: str, message: str) -> None:
        self.operation = operation
        super().__init__(
            f"rate limiter unavailable during {operation}: {message}"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_table(
    table_name: str | None,
    dynamodb_resource: Any | None,
) -> Any:
    """Return a DynamoDB ``Table`` resource for the configured table.

    Resolution order for the table name: explicit ``table_name``
    argument, then ``$DADJOKES_TABLE``, then :data:`DEFAULT_TABLE_NAME`.
    """
    if table_name is None:
        table_name = os.environ.get(TABLE_NAME_ENV_VAR, DEFAULT_TABLE_NAME)
    if dynamodb_resource is None:
        dynamodb_resource = boto3.resource("dynamodb")
    return dynamodb_resource.Table(table_name)


def _key(ip_hash: str, day: str) -> dict[str, str]:
    """Build the DynamoDB primary key for ``(ip_hash, day)``."""
    return {"pk": f"{_PK_PREFIX}{ip_hash}", "sk": f"{_SK_PREFIX}{day}"}


def _compute_ttl_epoch(day: str) -> int:
    """Return the TTL epoch seconds for rows tagged with ``day``.

    Equal to ``(midnight_utc(day + 1) + 60s).timestamp()`` per R5.6.

    Raises:
        ValueError: When ``day`` is not a ``YYYY-MM-DD`` calendar date.
    """
    parsed = datetime.strptime(day, _DAY_FORMAT).replace(tzinfo=timezone.utc)
    next_midnight = parsed + timedelta(days=1)
    expires_at = next_midnight + timedelta(seconds=_TTL_GRACE_SECONDS)
    return int(expires_at.timestamp())


def _validate_keys(ip_hash: str, day: str) -> None:
    """Validate ``ip_hash`` and ``day`` arguments shared by the API."""
    if not isinstance(ip_hash, str) or ip_hash == "":
        raise ValueError("ip_hash must be a non-empty string")
    if not isinstance(day, str) or day == "":
        raise ValueError("day must be a non-empty YYYY-MM-DD string")
    # Eagerly parse the date so callers get a clear error before any
    # DynamoDB round-trip; ``_compute_ttl_epoch`` reuses the parser
    # later in :func:`increment`.
    try:
        datetime.strptime(day, _DAY_FORMAT)
    except ValueError as exc:
        raise ValueError(
            "day must be a YYYY-MM-DD UTC calendar date"
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check(
    ip_hash: str,
    day: str,
    limit: int,
    *,
    table_name: str | None = None,
    dynamodb_resource: Any | None = None,
) -> int:
    """Return the current daily count for ``(ip_hash, day)``.

    Reads only the row whose sort key matches ``DAY#<day>``. Rows from
    prior UTC days live under different sort keys and are therefore
    naturally treated as zero, satisfying R5.6's logical-reset rule
    even when DynamoDB's background TTL sweep is delayed.

    A consistent read is used so the count returned reflects the most
    recent successful ``increment`` call; this matters for the
    "limit-reached" decision because increments and checks may
    interleave on different Lambda invocations (R5.3, R5.4).

    Args:
        ip_hash: 64-char lowercase hex SHA-256 digest of the salted
            source IP (produced by ``ip_hashing.hash_ip``).
        day: UTC date in ``YYYY-MM-DD`` format identifying the current
            UTC day.
        limit: Configured daily limit (``config.daily_limit``); must
            be a positive integer. ``bool`` values are rejected
            because ``True`` would silently coerce to ``1``.
        table_name: Override for the DynamoDB table name. Defaults to
            ``$DADJOKES_TABLE`` or ``"dadjokes"``.
        dynamodb_resource: Override for the boto3 ``dynamodb``
            resource. Defaults to ``boto3.resource("dynamodb")``. The
            property test in task 3.4 supplies a ``moto``-backed
            resource here.

    Returns:
        The current count. ``0`` when no row exists for
        ``(ip_hash, day)``.

    Raises:
        ValueError: On malformed ``ip_hash``, ``day``, or non-positive
            ``limit``.
        RateLimitExceeded: When ``count >= limit``.
        RateLimiterUnavailable: When DynamoDB is unreachable or
            returns an error response.
    """
    _validate_keys(ip_hash, day)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")

    table = _resolve_table(table_name, dynamodb_resource)
    try:
        response = table.get_item(
            Key=_key(ip_hash, day),
            ConsistentRead=True,
        )
    except (BotoCoreError, ClientError) as exc:
        raise RateLimiterUnavailable("check", str(exc)) from exc

    item = response.get("Item")
    if item is None:
        return 0

    raw = item.get(_ATTR_COUNT, 0)
    try:
        # boto3 returns numeric attributes as ``decimal.Decimal``; ``int``
        # accepts both ``Decimal`` and ``int`` so this works for moto and
        # real DynamoDB alike.
        count = int(raw)
    except (TypeError, ValueError) as exc:
        raise RateLimiterUnavailable(
            "check", "stored count is not an integer"
        ) from exc

    if count >= limit:
        raise RateLimitExceeded(ip_hash, day, count, limit)

    return count


def increment(
    ip_hash: str,
    day: str,
    *,
    table_name: str | None = None,
    dynamodb_resource: Any | None = None,
) -> int:
    """Atomically increment the daily counter for ``(ip_hash, day)``.

    Issues a single DynamoDB ``UpdateItem`` with the expression::

        ADD #c :one
        SET #t = if_not_exists(#t, :ttl)

    The ``ADD`` action is atomic across concurrent invocations
    (R5.4); even with N parallel callers, the recorded count after
    every ``increment`` resolves equals the actual number of
    successful calls. The ``if_not_exists`` conditional in the SET
    clause writes ``expires_at`` exactly once -- on the first request
    of the day -- so the TTL is not repeatedly bumped forward and
    counters remain bounded to "next UTC midnight + 60 s" (R5.6).

    The DynamoDB attribute ``count`` is a reserved word, so it is
    referenced as ``#c`` via ``ExpressionAttributeNames``;
    ``expires_at`` is aliased to ``#t`` for consistency.

    Args:
        ip_hash: 64-char lowercase hex SHA-256 digest of the salted
            source IP (produced by ``ip_hashing.hash_ip``).
        day: UTC date in ``YYYY-MM-DD`` format identifying the current
            UTC day.
        table_name: Override for the DynamoDB table name. Defaults to
            ``$DADJOKES_TABLE`` or ``"dadjokes"``.
        dynamodb_resource: Override for the boto3 ``dynamodb``
            resource. Defaults to ``boto3.resource("dynamodb")``.

    Returns:
        The new count after the increment.

    Raises:
        ValueError: On malformed ``ip_hash`` or ``day``.
        RateLimiterUnavailable: When DynamoDB is unreachable or
            returns an error response.
    """
    _validate_keys(ip_hash, day)

    table = _resolve_table(table_name, dynamodb_resource)
    ttl_epoch = _compute_ttl_epoch(day)

    try:
        response = table.update_item(
            Key=_key(ip_hash, day),
            UpdateExpression="ADD #c :one SET #t = if_not_exists(#t, :ttl)",
            ExpressionAttributeNames={
                "#c": _ATTR_COUNT,
                "#t": _ATTR_EXPIRES_AT,
            },
            ExpressionAttributeValues={
                ":one": 1,
                ":ttl": ttl_epoch,
            },
            ReturnValues="UPDATED_NEW",
        )
    except (BotoCoreError, ClientError) as exc:
        raise RateLimiterUnavailable("increment", str(exc)) from exc

    attributes = response.get("Attributes") or {}
    raw = attributes.get(_ATTR_COUNT)
    if raw is None:
        # ``ReturnValues="UPDATED_NEW"`` is contractually required to
        # echo the new count. Treat the impossible-shaped response as
        # an availability failure rather than swallow it silently.
        raise RateLimiterUnavailable(
            "increment", "DynamoDB did not return updated count"
        )
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RateLimiterUnavailable(
            "increment", "updated count is not an integer"
        ) from exc
