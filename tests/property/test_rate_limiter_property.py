"""Property tests for ``joke_api.rate_limiter``.

Implements three correctness properties from ``design.md``:

* **Property 14: Rate-limit counters increment atomically and only on
  success** -- *For any* concurrency level ``N`` of successful
  generation requests originating from the same source IP within a
  single UTC day, after all requests complete the recorded daily
  counter for that IP SHALL equal ``N``. *For any* failed request
  (validation, moderation, Bedrock error, Polly error, persistence
  error, or any other failure), the counter SHALL be unchanged.

* **Property 15: Limit-reached requests are rejected with HTTP 429**
  -- *For any* ``(current_count, daily_limit)`` pair where
  ``current_count >= daily_limit``, the handler SHALL return HTTP 429
  with a body containing ``resetAtUtc`` set to the next 00:00:00 UTC
  boundary, and SHALL NOT invoke the Input_Moderator, Bedrock, or
  Polly.

* **Property 16: Counters reset across UTC-day boundaries** -- *For
  any* prior-day count ``c_y`` and any request occurring on the
  following UTC day, the rate-limit lookup for that request SHALL
  return ``0`` regardless of ``c_y``.

**Validates: Requirements 5.3, 5.4, 5.5, 5.6**

Implementation under test
-------------------------

``joke_api.rate_limiter`` exposes:

* ``check(ip_hash, day, limit, *, table_name=None,
  dynamodb_resource=None) -> int`` -- returns the current daily count
  and raises :class:`RateLimitExceeded` when ``count >= limit``.
* ``increment(ip_hash, day, *, table_name=None,
  dynamodb_resource=None) -> int`` -- atomically adds one to the
  counter via DynamoDB ``UpdateItem ADD #c :one``.

Both functions accept a ``dynamodb_resource`` kwarg used here to
inject a ``moto``-backed in-memory DynamoDB. The ``moto.mock_aws``
context manager covers all AWS endpoints, so any leak to real AWS
would produce a connection error rather than silently succeed.

Per the Property 15 statement, the "no Bedrock / Polly / Input
moderator invocation" obligation is structurally guaranteed at this
layer: ``check`` performs only a single ``GetItem`` call against
DynamoDB. The handler-level orchestration property (which composes
``check`` with the moderator/Bedrock/Polly call sites) is task 10.3.

moto threading note
-------------------
moto's in-memory DynamoDB simulation does not serialize concurrent
``UpdateItem`` calls the way real DynamoDB does -- two parallel
``ADD #c :one`` calls can interleave a read-compute-write pair and
produce a lost update. Because the implementation under test issues
a single atomic ``UpdateItem`` request (the atomicity guarantee comes
from DynamoDB itself, not from any Python-level locking), this is a
test-fidelity gap in moto, not a defect in
``joke_api.rate_limiter``. The ``_serialize_boto_calls`` autouse
fixture below installs a process-wide ``threading.Lock`` around
``botocore.client.BaseClient._make_api_call`` for the duration of
this test module so moto behaves atomically and the property can be
validated end-to-end through the real ``boto3`` code path.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

import boto3
import botocore.client
import pytest
from botocore.config import Config as BotoConfig
from hypothesis import HealthCheck, assume, given, settings, strategies as st
from moto import mock_aws

from joke_api.rate_limiter import (
    DEFAULT_TABLE_NAME,
    RateLimitExceeded,
    _compute_ttl_epoch,
    check,
    increment,
)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

# Match the production schema documented in design.md § Data Models:
#   pk = "RL#" + ip_hash    (S, hash key)
#   sk = "DAY#" + YYYY-MM-DD (S, range key)
#   count = N (the rate-limit counter)
#   expires_at = TTL epoch seconds
TABLE_NAME = DEFAULT_TABLE_NAME

# A region must be set for boto3/moto; "us-east-1" is the moto default.
_REGION = "us-east-1"

# Common fixed-shape ip_hash strategy: 64 lowercase hex chars matching
# the SHA-256 digest produced by ``ip_hashing.hash_ip``. Constraining
# the input space to the production shape keeps the tests focused on
# the rate-limiter contract rather than re-validating the hasher.
_HEX_ALPHABET = "0123456789abcdef"
ip_hash_strategy = st.text(alphabet=_HEX_ALPHABET, min_size=64, max_size=64)

# A YYYY-MM-DD UTC date string strategy. Bounded to a sane historical
# range so ``datetime.strptime`` always succeeds and TTL math stays in
# 32-bit-friendly territory.
_MIN_DATE = date(2024, 1, 1)
_MAX_DATE = date(2030, 12, 31)


def _format_day(d: date) -> str:
    return d.strftime("%Y-%m-%d")


day_strategy = st.dates(min_value=_MIN_DATE, max_value=_MAX_DATE).map(_format_day)


# Two consecutive UTC days, returned as ``(prior, next)`` formatted
# strings. Used by Property 16 to assert the day-boundary reset.
@st.composite
def consecutive_day_pair(draw: Any) -> tuple[str, str]:
    """Generate (prior_day, next_day) as YYYY-MM-DD strings."""
    prior = draw(
        st.dates(min_value=_MIN_DATE, max_value=_MAX_DATE - timedelta(days=1))
    )
    return _format_day(prior), _format_day(prior + timedelta(days=1))


# ---------------------------------------------------------------------------
# moto-backed table harness
# ---------------------------------------------------------------------------


# Process-wide lock used to serialize boto3 -> moto API calls so the
# in-memory DynamoDB simulation behaves atomically (see module
# docstring). Real DynamoDB does not need this; moto does.
_BOTO_CALL_LOCK = threading.Lock()


@pytest.fixture(autouse=True)
def _serialize_boto_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrap ``BaseClient._make_api_call`` with a process-wide lock.

    moto's in-memory DynamoDB does not serialize concurrent
    ``UpdateItem`` requests, so without this fixture the
    Property 14 concurrent-increment test exhibits lost updates that
    do not occur against real DynamoDB. The lock makes moto behave
    atomically, matching the contractual atomicity guarantee that the
    implementation under test relies upon.
    """
    original = botocore.client.BaseClient._make_api_call

    def serialized(self: Any, operation_name: str, *args: Any, **kwargs: Any) -> Any:
        with _BOTO_CALL_LOCK:
            return original(self, operation_name, *args, **kwargs)

    monkeypatch.setattr(
        botocore.client.BaseClient, "_make_api_call", serialized
    )


def _create_table(dynamodb_resource: Any) -> Any:
    """Create the ``dadjokes`` table in moto with the production schema.

    Returns the ``Table`` resource. The schema mirrors design.md and
    ``infra/terraform/dynamodb.tf``: composite primary key
    ``(pk, sk)``, PAY_PER_REQUEST billing, and TTL on ``expires_at``.
    PAY_PER_REQUEST avoids the ProvisionedThroughputExceeded errors
    that on-demand simulates under high concurrency.
    """
    table = dynamodb_resource.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.wait_until_exists()
    # Enable TTL the same way infra/terraform/dynamodb.tf does. moto
    # accepts the call even though it does not actively expire rows
    # during a test; what matters is that ``increment`` writes the
    # ``expires_at`` attribute.
    dynamodb_resource.meta.client.update_time_to_live(
        TableName=TABLE_NAME,
        TimeToLiveSpecification={
            "Enabled": True,
            "AttributeName": "expires_at",
        },
    )
    return table


def _read_count_directly(table: Any, ip_hash: str, day: str) -> int:
    """Read the counter directly via ``GetItem``.

    Bypasses :func:`check` so verification does not depend on the
    function under test. Returns 0 when no row exists.
    """
    response = table.get_item(
        Key={"pk": f"RL#{ip_hash}", "sk": f"DAY#{day}"},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if item is None:
        return 0
    return int(item.get("count", 0))


def _seed_count(table: Any, ip_hash: str, day: str, count: int) -> None:
    """Pre-populate the table with a specific count for ``(ip_hash, day)``.

    Uses ``PutItem`` to set the count exactly (rather than
    incrementing ``count`` times) so the seeding step is O(1).
    ``expires_at`` is set to next-midnight + 60s, matching the
    production TTL contract.
    """
    parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    next_midnight = parsed + timedelta(days=1)
    expires_at = int((next_midnight + timedelta(seconds=60)).timestamp())
    table.put_item(
        Item={
            "pk": f"RL#{ip_hash}",
            "sk": f"DAY#{day}",
            "count": count,
            "expires_at": expires_at,
        }
    )


# A boto3 client config that allows enough connections in the pool to
# avoid throttling the concurrent-increment harness in Property 14.
_BOTO_CONFIG = BotoConfig(max_pool_connections=64)


# ---------------------------------------------------------------------------
# Property 14 -- atomicity
# ---------------------------------------------------------------------------


@given(
    ip_hash=ip_hash_strategy,
    day=day_strategy,
    workers=st.integers(min_value=2, max_value=12),
)
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_14_concurrent_increments_are_atomic(
    ip_hash: str, day: str, workers: int
) -> None:
    """**Validates: Requirements 5.4** -- Property 14 (success branch).

    For any concurrency level ``N`` in ``[2, 12]``, calling
    :func:`increment` from ``N`` parallel threads against a fresh row
    leaves the recorded counter equal to ``N``. The DynamoDB
    ``ADD #c :one`` action is contractually atomic; this property
    asserts that contract holds end-to-end through ``boto3`` against
    a serialized moto in-memory backend (see ``_serialize_boto_calls``
    fixture for the moto-fidelity rationale).
    """
    with mock_aws():
        ddb = boto3.resource(
            "dynamodb", region_name=_REGION, config=_BOTO_CONFIG
        )
        table = _create_table(ddb)

        # Pre-condition: the row does not exist, so the counter is 0.
        assert _read_count_directly(table, ip_hash, day) == 0

        def _worker() -> int:
            return increment(ip_hash, day, dynamodb_resource=ddb)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_worker) for _ in range(workers)]
            results = [f.result() for f in futures]

        # Every worker received a positive new-count value.
        assert all(isinstance(v, int) and v >= 1 for v in results)
        # The persisted counter equals the number of successful calls --
        # this is the property's actual obligation: "after all requests
        # complete the recorded daily counter for that IP SHALL equal N".
        assert _read_count_directly(table, ip_hash, day) == workers
        # The same contract is observable through ``check``: with a
        # generous limit, the read returns the post-increment count.
        assert check(ip_hash, day, limit=999, dynamodb_resource=ddb) == workers


@given(
    ip_hash=ip_hash_strategy,
    valid_day=day_strategy,
    bad_day=st.sampled_from(
        [
            "",  # empty -> ValueError
            "2024-13-01",  # invalid month
            "2024-02-30",  # invalid day for Feb (non-leap)
            "2024-02-31",  # invalid day for any month
            "not-a-date",
            "2024/01/15",  # wrong separator
            "20240115",  # no separators
        ]
    ),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_14_failed_increment_does_not_change_counter(
    ip_hash: str, valid_day: str, bad_day: str
) -> None:
    """**Validates: Requirements 5.5** -- Property 14 (failure branch).

    For any failed call to :func:`increment` (here: a malformed
    ``day`` argument that triggers :class:`ValueError` before any
    DynamoDB round-trip), the counter for the *valid* day remains
    unchanged. The failure path can occur for any reason -- validation,
    moderation, Bedrock error, Polly error, persistence error -- so we
    use the cheapest reproducible failure (input validation) as a
    representative.
    """
    with mock_aws():
        ddb = boto3.resource(
            "dynamodb", region_name=_REGION, config=_BOTO_CONFIG
        )
        table = _create_table(ddb)

        # Seed a known counter so we can assert it does not change.
        seed = 3
        _seed_count(table, ip_hash, valid_day, seed)
        assert _read_count_directly(table, ip_hash, valid_day) == seed

        # Attempt an increment that fails before reaching DynamoDB.
        with pytest.raises(ValueError):
            increment(ip_hash, bad_day, dynamodb_resource=ddb)

        # The failed call MUST NOT have modified the existing row...
        assert _read_count_directly(table, ip_hash, valid_day) == seed
        # ...nor created a row under the malformed ``bad_day`` key.
        # (Reading via the helper handles the empty/missing case.)
        if bad_day:
            assert _read_count_directly(table, ip_hash, bad_day) == 0


# ---------------------------------------------------------------------------
# Property 15 -- limit-reached requests
# ---------------------------------------------------------------------------


@given(
    ip_hash=ip_hash_strategy,
    day=day_strategy,
    counts=st.tuples(
        st.integers(min_value=1, max_value=20),
        st.integers(min_value=1, max_value=20),
    ),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_15_limit_reached_raises_rate_limit_exceeded(
    ip_hash: str, day: str, counts: tuple[int, int]
) -> None:
    """**Validates: Requirements 5.3** -- Property 15.

    For any ``(current_count, daily_limit)`` pair where
    ``current_count >= daily_limit``, :func:`check` raises
    :class:`RateLimitExceeded` carrying the same ``count`` and
    ``limit``. The handler maps this exception to HTTP 429 with the
    ``resetAtUtc`` body (verified at the response-builder layer in
    Property 20 / task 2.6) and never invokes Input_Moderator,
    Bedrock, or Polly -- structurally guaranteed because
    :func:`check` only performs a single DynamoDB ``GetItem`` call.
    """
    current_count, daily_limit = counts
    assume(current_count >= daily_limit)

    with mock_aws():
        ddb = boto3.resource(
            "dynamodb", region_name=_REGION, config=_BOTO_CONFIG
        )
        table = _create_table(ddb)

        _seed_count(table, ip_hash, day, current_count)

        with pytest.raises(RateLimitExceeded) as exc_info:
            check(ip_hash, day, daily_limit, dynamodb_resource=ddb)

        err = exc_info.value
        assert err.ip_hash == ip_hash
        assert err.day == day
        assert err.count == current_count
        assert err.limit == daily_limit
        # Sanity: the rejection did not mutate the counter.
        assert _read_count_directly(table, ip_hash, day) == current_count


@given(
    ip_hash=ip_hash_strategy,
    day=day_strategy,
    counts=st.tuples(
        st.integers(min_value=0, max_value=20),
        st.integers(min_value=1, max_value=20),
    ),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_15_under_limit_returns_count(
    ip_hash: str, day: str, counts: tuple[int, int]
) -> None:
    """**Validates: Requirements 5.3** -- Property 15 (negative branch).

    The dual of the rejection rule: when ``current_count < daily_limit``
    the call SHALL return the count rather than raise. Tested in the
    same module to keep both halves of the rule colocated.
    """
    current_count, daily_limit = counts
    assume(current_count < daily_limit)

    with mock_aws():
        ddb = boto3.resource(
            "dynamodb", region_name=_REGION, config=_BOTO_CONFIG
        )
        table = _create_table(ddb)

        if current_count > 0:
            _seed_count(table, ip_hash, day, current_count)

        result = check(ip_hash, day, daily_limit, dynamodb_resource=ddb)
        assert result == current_count


# ---------------------------------------------------------------------------
# Property 16 -- UTC-day boundary reset
# ---------------------------------------------------------------------------


@given(
    ip_hash=ip_hash_strategy,
    day_pair=consecutive_day_pair(),
    prior_count=st.integers(min_value=1, max_value=100),
    daily_limit=st.integers(min_value=5, max_value=10),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_16_counter_resets_across_utc_day_boundary(
    ip_hash: str,
    day_pair: tuple[str, str],
    prior_count: int,
    daily_limit: int,
) -> None:
    """**Validates: Requirements 5.6** -- Property 16.

    For any prior-day count ``c_y > 0`` and any request occurring on
    the following UTC day, :func:`check` returns ``0`` regardless of
    ``c_y``. This is the immediate logical-reset rule that does not
    depend on DynamoDB's background TTL sweep -- prior-day rows live
    under a different sort key (``DAY#<prev>`` vs ``DAY#<next>``) and
    are therefore naturally invisible to the next-day lookup.
    """
    prior_day, next_day = day_pair

    with mock_aws():
        ddb = boto3.resource(
            "dynamodb", region_name=_REGION, config=_BOTO_CONFIG
        )
        table = _create_table(ddb)

        _seed_count(table, ip_hash, prior_day, prior_count)
        # Sanity: the prior-day row really exists with the seeded value.
        assert _read_count_directly(table, ip_hash, prior_day) == prior_count

        # Lookup for the next UTC day SHALL return 0 regardless of c_y.
        result = check(ip_hash, next_day, daily_limit, dynamodb_resource=ddb)
        assert result == 0

        # The prior-day row is left intact (TTL-driven cleanup is a
        # DynamoDB background concern, not a check-path side effect).
        assert _read_count_directly(table, ip_hash, prior_day) == prior_count


@given(
    ip_hash=ip_hash_strategy,
    day_pair=consecutive_day_pair(),
    prior_count=st.integers(min_value=1, max_value=100),
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_16_increment_on_new_day_starts_at_one(
    ip_hash: str, day_pair: tuple[str, str], prior_count: int
) -> None:
    """**Validates: Requirements 5.6** -- Property 16 (write-side).

    Companion to the read-side reset: an :func:`increment` call on
    the next UTC day SHALL produce a new count of ``1`` regardless of
    the prior-day count. This catches a regression in which the day
    sort-key were ever stripped from the update-key derivation.
    """
    prior_day, next_day = day_pair

    with mock_aws():
        ddb = boto3.resource(
            "dynamodb", region_name=_REGION, config=_BOTO_CONFIG
        )
        table = _create_table(ddb)

        _seed_count(table, ip_hash, prior_day, prior_count)

        new_count = increment(ip_hash, next_day, dynamodb_resource=ddb)
        assert new_count == 1
        assert _read_count_directly(table, ip_hash, next_day) == 1
        # The prior-day row is untouched.
        assert _read_count_directly(table, ip_hash, prior_day) == prior_count


@given(day=day_strategy)
@settings(
    max_examples=200,
    deadline=None,
)
def test_property_16_ttl_increases_by_one_day_across_consecutive_days(
    day: str,
) -> None:
    """**Validates: Requirements 5.6** -- Property 16 (structural TTL).

    Two adjacent calendar days produce TTL epoch values exactly
    86400 seconds apart. This is the structural guarantee R5.6 needs:
    the TTL anchor advances by exactly one day so a row written on
    day ``d`` cannot survive past midnight UTC + 60s of day ``d + 1``.
    Pure arithmetic; no DynamoDB or moto involvement required.
    """
    parsed = datetime.strptime(day, "%Y-%m-%d").date()
    # Skip the upper boundary of the strategy where ``day + 1`` would
    # roll past _MAX_DATE; the property is well-formed at any date,
    # we just need ``day_next`` to exist as a YYYY-MM-DD string.
    if parsed >= _MAX_DATE:
        return
    next_day = _format_day(parsed + timedelta(days=1))

    ttl_today = _compute_ttl_epoch(day)
    ttl_next = _compute_ttl_epoch(next_day)

    assert ttl_next - ttl_today == 86_400

    # Additional structural sanity: the TTL is at least 60 seconds
    # past the next UTC midnight (the grace window required by R5.6's
    # "within 60 seconds of that boundary" wording).
    parsed_dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    next_midnight_epoch = int((parsed_dt + timedelta(days=1)).timestamp())
    assert ttl_today >= next_midnight_epoch + 60
