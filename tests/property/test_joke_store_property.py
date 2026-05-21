"""Property tests for ``joke_api.joke_store``.

Implements five correctness properties from ``design.md`` § Correctness
Properties:

* **Property 40: Joke persistence round-trip is byte-exact** -- *For
  any* successful generation ``(id, text, audio_ref, model_id,
  voice_id, created_at)`` returned to the visitor,
  ``JokeStore.get(id)`` SHALL return a record whose ``text`` and
  ``audio_ref`` are byte-for-byte identical to the values returned,
  and whose ``model_id``, ``voice_id``, and ``created_at`` match the
  original record.

* **Property 41: Unknown ids do not mutate the store** -- *For any*
  identifier not present in the ``Joke_Store``, ``get(id)`` SHALL
  return a not-found indication, and a snapshot of all stored records
  taken before and after the call SHALL be equal.

* **Property 42: TTL retention rule is enforced by ``expires_at``** --
  *For any* persisted record with ``created_at = c``, the record's
  ``expires_at`` attribute SHALL equal ``c + 90 days`` expressed as
  epoch seconds.

* **Property 43: Persistence failures do not affect the visitor
  response** -- *For any* ``Joke_Store`` write failure, the visitor
  SHALL still receive a 200 response. At this layer the obligation is
  expressed as: ``persist`` SHALL raise the typed
  :class:`JokeStorePersistError` (carrying the generation id) on any
  underlying boto3 error, so the handler can catch it and continue
  with the visitor response. The handler-level visitor-response
  property is task 10.x.

* **Property 44: Persistence input-size validation** -- *For any*
  ``(joke_text, audio_ref, model_id, voice_id)`` tuple where one
  field exceeds its size cap, ``persist`` SHALL reject the record by
  raising :class:`JokeStoreValidationError` with the offending field
  name AND no DynamoDB call SHALL be made.

**Validates: Requirements 18.1, 18.2, 18.3, 18.4, 18.5, 18.6**

Implementation under test
-------------------------

``joke_api.joke_store`` exposes:

* ``persist(record, *, table_name=None, dynamodb_resource=None)
  -> None`` -- writes a :class:`JokeRecord` to the single-table
  DynamoDB store with TTL.
* ``get(joke_id, *, table_name=None, dynamodb_resource=None)
  -> JokeRecord | None`` -- reads a record back, returning ``None``
  for unknown ids.

Both functions accept a ``dynamodb_resource`` kwarg used by these
tests to inject a ``moto``-backed in-memory DynamoDB (Properties 40,
41, 42) or a hand-rolled stub (Property 43, Property 44).

``created_at`` precision note (Property 40)
-------------------------------------------

``persist`` formats ``created_at`` with whole-second precision (no
microseconds, no fractional-second digits) so the DynamoDB string is
stable across platforms. The round-trip therefore drops any
sub-second precision in the input. The strategy below pre-strips
microseconds from the generated datetime so the test asserts the
intended byte-exact round-trip without conflating it with that
documented precision floor.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
)
from hypothesis import HealthCheck, assume, given, settings, strategies as st
from moto import mock_aws

from joke_api.joke_store import (
    AUDIO_REF_MAX_LEN,
    DEFAULT_TABLE_NAME,
    JOKE_TEXT_MAX_LEN,
    MODEL_ID_MAX_LEN,
    RETENTION_DAYS,
    VOICE_ID_MAX_LEN,
    JokeRecord,
    JokeStorePersistError,
    JokeStoreValidationError,
    get,
    persist,
)

# ---------------------------------------------------------------------------
# Test constants and shared strategies
# ---------------------------------------------------------------------------

TABLE_NAME = DEFAULT_TABLE_NAME
_REGION = "us-east-1"

# DynamoDB strings tolerate any UTF-8 except embedded NULs and
# unpaired surrogates. We exclude the surrogate category and the NUL
# character so the round-trip is well-defined for any string the
# strategy produces; the joke_store contract makes no other charset
# claim.
_safe_text_chars = st.characters(
    blacklist_categories=("Cs",),
    blacklist_characters="\x00",
)

# joke_text: 1..2000 chars per R18.1.
joke_text_strategy = st.text(
    alphabet=_safe_text_chars,
    min_size=1,
    max_size=JOKE_TEXT_MAX_LEN,
)

# audio_ref: optional string up to 2048 chars per R18.1.
# Empty strings are valid: DynamoDB has accepted empty string
# attribute values since 2020, and the validator only rejects on
# length > 2048.
audio_ref_strategy = st.one_of(
    st.none(),
    st.text(
        alphabet=_safe_text_chars,
        min_size=0,
        max_size=AUDIO_REF_MAX_LEN,
    ),
)

# model_id / voice_id: 1..128 chars matching the alphanum + ``.-_:/``
# charset that the SSM-loaded Bedrock model id and Polly voice id
# follow in production (e.g. ``anthropic.claude-3-haiku-20240307-v1:0``).
_id_alphabet = st.sampled_from(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.-_:/"
)
model_id_strategy = st.text(
    alphabet=_id_alphabet, min_size=1, max_size=MODEL_ID_MAX_LEN
)
voice_id_strategy = st.text(
    alphabet=_id_alphabet, min_size=1, max_size=VOICE_ID_MAX_LEN
)

# created_at: tz-aware UTC datetime. Hypothesis ``st.datetimes``
# returns naive datetimes; we attach ``timezone.utc`` and strip
# microseconds because ``persist`` formats with whole-second
# precision (see module docstring).
created_at_strategy = st.datetimes(
    min_value=datetime(2024, 1, 1),
    max_value=datetime(2030, 12, 31),
).map(lambda d: d.replace(tzinfo=timezone.utc, microsecond=0))


def _new_uuid_v4() -> str:
    """Return a fresh UUID v4 string per draw.

    Hypothesis cannot generate UUIDs as freely as ``uuid.uuid4()``;
    using a strategy seeded from ``uuid.uuid4()`` keeps the test
    inputs faithful to the production identifier shape (R18.1).
    """
    return str(uuid.uuid4())


uuid_v4_strategy = st.builds(_new_uuid_v4)


@st.composite
def joke_record_strategy(draw: Any) -> JokeRecord:
    """Compose a valid :class:`JokeRecord` from the field strategies."""
    return JokeRecord(
        id=draw(uuid_v4_strategy),
        joke_text=draw(joke_text_strategy),
        audio_ref=draw(audio_ref_strategy),
        model_id=draw(model_id_strategy),
        voice_id=draw(voice_id_strategy),
        created_at=draw(created_at_strategy),
    )


# ---------------------------------------------------------------------------
# moto-backed table harness
# ---------------------------------------------------------------------------


def _create_table(dynamodb_resource: Any) -> Any:
    """Create the ``dadjokes`` single-table schema in moto.

    Mirrors the production schema documented in design.md § Data
    Models and the rate-limiter test harness: composite primary key
    ``(pk, sk)``, PAY_PER_REQUEST billing, TTL on ``expires_at``.
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
    dynamodb_resource.meta.client.update_time_to_live(
        TableName=TABLE_NAME,
        TimeToLiveSpecification={
            "Enabled": True,
            "AttributeName": "expires_at",
        },
    )
    return table


def _scan_all_items(table: Any) -> list[dict[str, Any]]:
    """Return every item in the table (ConsistentRead).

    Used by Property 41 to take the "before / after" store snapshot
    that the property statement requires.
    """
    response = table.scan(ConsistentRead=True)
    return list(response.get("Items", []))


def _read_raw_item(table: Any, joke_id: str) -> dict[str, Any] | None:
    """Read the raw DynamoDB item for ``joke_id`` (Property 42).

    Bypasses :func:`get` so the test can assert the structural TTL
    attribute directly without going through the JokeRecord
    deserializer.
    """
    response = table.get_item(
        Key={"pk": f"JOKE#{joke_id}", "sk": "META"},
        ConsistentRead=True,
    )
    return response.get("Item")


# ---------------------------------------------------------------------------
# Property 40 -- byte-exact round-trip
# ---------------------------------------------------------------------------


@given(record=joke_record_strategy())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_40_persist_get_round_trip_is_byte_exact(
    record: JokeRecord,
) -> None:
    """**Validates: Requirements 18.1, 18.2** -- Property 40.

    For any well-formed :class:`JokeRecord`, ``persist`` followed by
    ``get`` returns a record whose every field equals the input.
    ``created_at`` is stripped to whole-second precision in the
    strategy (see module docstring) so the assertion captures the
    byte-exact round-trip the property promises.
    """
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(ddb)

        persist(record, dynamodb_resource=ddb)
        loaded = get(record.id, dynamodb_resource=ddb)

        assert loaded is not None
        assert loaded.id == record.id
        assert loaded.joke_text == record.joke_text
        assert loaded.audio_ref == record.audio_ref
        assert loaded.model_id == record.model_id
        assert loaded.voice_id == record.voice_id
        assert loaded.created_at == record.created_at


# ---------------------------------------------------------------------------
# Property 41 -- unknown ids do not mutate the store
# ---------------------------------------------------------------------------


@given(
    record=joke_record_strategy(),
    other_id=uuid_v4_strategy,
)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_41_unknown_id_returns_none_and_leaves_store_unchanged(
    record: JokeRecord, other_id: str
) -> None:
    """**Validates: Requirements 18.3** -- Property 41.

    Persist one record under id ``a``, then look up a different id
    ``b``: ``get(b)`` returns ``None``, the original record is still
    retrievable byte-exact, and a full table scan before vs after the
    unknown-id lookup is equal.
    """
    # UUIDs are 122 bits of entropy, but Hypothesis can in principle
    # produce a collision; ``assume`` is the cheapest correct guard.
    assume(record.id != other_id)

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(ddb)

        persist(record, dynamodb_resource=ddb)

        before_snapshot = _scan_all_items(table)

        # The unknown-id lookup MUST return None and MUST NOT mutate.
        assert get(other_id, dynamodb_resource=ddb) is None

        after_snapshot = _scan_all_items(table)
        assert before_snapshot == after_snapshot

        # The original record is still retrievable byte-exact -- this
        # is the strict reading of "stored records SHALL be equal".
        loaded = get(record.id, dynamodb_resource=ddb)
        assert loaded == record


# ---------------------------------------------------------------------------
# Property 42 -- TTL = created_at + 90 days
# ---------------------------------------------------------------------------


@given(record=joke_record_strategy())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_42_expires_at_equals_created_at_plus_90_days(
    record: JokeRecord,
) -> None:
    """**Validates: Requirements 18.4** -- Property 42.

    For any persisted record, the raw DynamoDB item carries an
    ``expires_at`` attribute equal to
    ``int((created_at + 90 days).timestamp())``. Read the raw item
    via the table resource directly (bypassing :func:`get`) to make
    the assertion structural.
    """
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(ddb)

        persist(record, dynamodb_resource=ddb)

        item = _read_raw_item(table, record.id)
        assert item is not None
        assert "expires_at" in item

        expected_epoch = int(
            (
                record.created_at + timedelta(days=RETENTION_DAYS)
            ).timestamp()
        )
        # DynamoDB returns numeric attributes as ``Decimal``; coerce
        # to int for the equality check (the contract is "epoch
        # seconds", not a particular numeric subtype).
        assert int(item["expires_at"]) == expected_epoch


# ---------------------------------------------------------------------------
# Property 43 -- persistence failures raise the typed exception
# ---------------------------------------------------------------------------


def _make_client_error() -> ClientError:
    """A representative ``ClientError`` raised by put_item."""
    return ClientError(
        {
            "Error": {
                "Code": "InternalServerError",
                "Message": "fake DynamoDB outage",
            }
        },
        "PutItem",
    )


def _make_throttling_error() -> ClientError:
    """A throttling error -- a different ``ClientError`` shape."""
    return ClientError(
        {
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "fake throttling",
            }
        },
        "PutItem",
    )


def _make_endpoint_error() -> EndpointConnectionError:
    """A representative ``BotoCoreError`` -- network unreachable."""
    return EndpointConnectionError(endpoint_url="https://dynamodb.fake/")


_persist_error_strategy = st.sampled_from(
    [
        _make_client_error,
        _make_throttling_error,
        _make_endpoint_error,
    ]
)


def _build_failing_resource(error: BaseException) -> MagicMock:
    """Build a stub DynamoDB resource whose ``put_item`` raises ``error``.

    Mimics ``boto3.resource('dynamodb').Table(...).put_item(...)``;
    no real moto/AWS involvement. ``Table`` is the only attribute
    accessed by :func:`persist`, so this is the minimal stub that
    matches the call shape.
    """
    table_stub = MagicMock(name="Table")
    table_stub.put_item.side_effect = error

    resource_stub = MagicMock(name="DynamoDBResource")
    resource_stub.Table.return_value = table_stub
    return resource_stub


@given(
    record=joke_record_strategy(),
    error_factory=_persist_error_strategy,
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_property_43_persist_failure_raises_typed_persist_error(
    record: JokeRecord, error_factory: Any
) -> None:
    """**Validates: Requirements 18.5** -- Property 43 (handler hook).

    For any underlying boto3 error (``ClientError`` variants and
    ``BotoCoreError`` variants are sampled), :func:`persist` raises
    :class:`JokeStorePersistError` whose ``joke_id`` matches the
    record's id. The handler relies on this typed exception to soft-
    fail the persistence step and still return the joke to the
    visitor, which is the property's user-visible obligation
    (verified end-to-end in handler property tests).
    """
    error = error_factory()
    resource = _build_failing_resource(error)

    with pytest.raises(JokeStorePersistError) as exc_info:
        persist(record, dynamodb_resource=resource)

    assert exc_info.value.joke_id == record.id

    # Sanity: the underlying boto3 error is chained, so the
    # observability layer can record the original error class.
    # Either ClientError or BotoCoreError is acceptable here.
    cause = exc_info.value.__cause__
    assert isinstance(cause, (ClientError, BotoCoreError))

    # The stub's ``put_item`` was invoked exactly once -- the error
    # came from that call, not from a missing/wrong call shape.
    resource.Table.assert_called_once_with(TABLE_NAME)
    resource.Table.return_value.put_item.assert_called_once()


# ---------------------------------------------------------------------------
# Property 44 -- input-size validation
# ---------------------------------------------------------------------------


# Each oversized case names the offending field and provides a text
# strategy that produces a string strictly longer than the cap.
# Using ``min_size = cap + 1`` guarantees the strategy can only
# generate violating inputs; ``max_size`` is set high enough to
# exercise a useful range without blowing up example sizes.
_oversize_cases: tuple[tuple[str, int, int], ...] = (
    ("joke_text", JOKE_TEXT_MAX_LEN, JOKE_TEXT_MAX_LEN + 200),
    ("audio_ref", AUDIO_REF_MAX_LEN, AUDIO_REF_MAX_LEN + 200),
    ("model_id", MODEL_ID_MAX_LEN, MODEL_ID_MAX_LEN + 64),
    ("voice_id", VOICE_ID_MAX_LEN, VOICE_ID_MAX_LEN + 64),
)


def _make_baseline_record() -> JokeRecord:
    """Return a fresh, valid :class:`JokeRecord` to mutate per test.

    Each oversized-field test starts from this baseline and replaces
    exactly one field with an oversized value, so the validator's
    rejection isolates to that field.
    """
    return JokeRecord(
        id=str(uuid.uuid4()),
        joke_text="baseline joke text",
        audio_ref="s3://audio/baseline.mp3",
        model_id="anthropic.claude-3-haiku-20240307-v1:0",
        voice_id="Joanna",
        created_at=datetime.now(tz=timezone.utc).replace(microsecond=0),
    )


@pytest.mark.parametrize("field,cap,upper", _oversize_cases)
@given(data=st.data())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        # Strings strictly longer than 2000/2048 chars are intrinsic
        # to this property's hypothesis (the cap is the property);
        # the "smallest natural example" of >2000 chars is unavoidably
        # large, so suppress the large-base-example heuristic.
        HealthCheck.large_base_example,
    ],
)
def test_property_44_oversized_field_is_rejected_with_no_dynamodb_call(
    field: str, cap: int, upper: int, data: st.DataObject
) -> None:
    """**Validates: Requirements 18.6** -- Property 44.

    For each oversized field (``joke_text``, ``audio_ref``,
    ``model_id``, ``voice_id``), build a :class:`JokeRecord` whose
    only flaw is that the named field exceeds its cap. ``persist``
    raises :class:`JokeStoreValidationError` with ``field`` matching
    the offending attribute, AND no DynamoDB call is made -- the
    rejection short-circuits before any boto interaction so existing
    rows are unchanged (the property's structural obligation).
    """
    oversized_value = data.draw(
        st.text(
            alphabet=_safe_text_chars,
            min_size=cap + 1,
            max_size=upper,
        )
    )
    # Bias-check: hypothesis shrinking can land on the boundary; the
    # property only applies to *strictly* oversized inputs, so guard.
    assume(len(oversized_value) > cap)

    baseline = _make_baseline_record()
    # Build the under-test record by replacing the named field. Use
    # ``dataclasses.replace``-style kwargs since :class:`JokeRecord`
    # is frozen.
    overrides: dict[str, Any] = {field: oversized_value}
    record = JokeRecord(
        id=baseline.id,
        joke_text=overrides.get("joke_text", baseline.joke_text),
        audio_ref=overrides.get("audio_ref", baseline.audio_ref),
        model_id=overrides.get("model_id", baseline.model_id),
        voice_id=overrides.get("voice_id", baseline.voice_id),
        created_at=baseline.created_at,
    )

    # A stub resource whose ``Table`` access would itself be
    # observable; if the validator wrongly issues a DynamoDB call we
    # see it on this MagicMock and the assertion fails.
    resource = MagicMock(name="DynamoDBResource")

    with pytest.raises(JokeStoreValidationError) as exc_info:
        persist(record, dynamodb_resource=resource)

    assert exc_info.value.field == field

    # Critical structural assertion: NO DynamoDB call was made. The
    # validator's rejection happens before ``_resolve_table`` is
    # consulted for the table handle, and the table handle is the
    # only path through which a DynamoDB call could originate.
    resource.Table.assert_not_called()
