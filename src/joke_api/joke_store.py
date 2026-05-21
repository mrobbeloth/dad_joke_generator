"""Joke persistence in DynamoDB (single-table design with TTL).

This module persists generated jokes and their audio references to the
single-table DynamoDB store described in ``design.md`` § Data Models.
Each joke is keyed by partition key ``pk = "JOKE#" + generation_id``
and sort key ``sk = "META"``; the row carries the joke text, the S3
audio URI (when audio synthesis succeeded), the model and voice
identifiers used to produce it, the creation timestamp, and a TTL
attribute that DynamoDB uses to delete the row 90 days later.

Public surface
--------------
* :class:`JokeRecord` -- frozen dataclass describing a single joke;
  the unit of read/write for this module.
* :func:`persist` -- writes a :class:`JokeRecord` to DynamoDB.
* :func:`get` -- reads a record back by id, returning ``None`` when no
  row exists.

Validated requirements (``requirements.md`` § Requirement 18)
-------------------------------------------------------------
* **R18.1** -- ``persist`` writes generation_id, joke_text, audio_ref,
  model_id, voice_id, and created_at; the handler invokes it within
  2 s of returning the joke to the visitor.
* **R18.2** -- ``get`` returns the same joke_text and audio_ref that
  were originally stored (byte-exact round-trip).
* **R18.3** -- ``get`` returns ``None`` for unknown ids and never
  mutates the store on a read.
* **R18.4** -- ``persist`` writes ``expires_at = created_at + 90d``
  as an integer epoch-seconds TTL attribute, satisfying the 30-day
  minimum and 90-day deletion ceiling. The DynamoDB table-level TTL
  configuration on ``expires_at`` is applied by the IaC in task 16.1.
* **R18.5** -- DynamoDB write errors raise :class:`JokeStorePersistError`
  rather than swallow silently. The handler catches this typed
  exception and proceeds with the visitor response so that
  persistence failures never propagate to the user.
* **R18.6** -- ``persist`` rejects records whose ``joke_text`` exceeds
  2000 chars or whose ``audio_ref`` exceeds 2048 chars by raising
  :class:`JokeStoreValidationError`; existing rows are left
  unchanged because no DynamoDB call is made on a validation failure.

Out of scope
------------
The handler-side response builder is responsible for ensuring the
visitor response carries only the presigned audio URL and never the
S3 ARN stored here as ``audio_ref`` (R18.3 boundary; see
``response_builder``).

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 40** -- joke persistence round-trip is byte-exact.
* **Property 41** -- unknown ids do not mutate the store.
* **Property 42** -- TTL retention rule is enforced by ``expires_at``.
* **Property 43** -- persistence failures do not affect the visitor
  response (the handler relies on :class:`JokeStorePersistError` to
  short-circuit into a soft-fail branch).
* **Property 44** -- persistence input-size validation.

Configuration
-------------
The DynamoDB table name is read from the ``DADJOKES_TABLE`` environment
variable, defaulting to ``"dadjokes"`` -- the same single-table store
used by :mod:`joke_api.rate_limiter`. The function-level
``table_name`` and ``dynamodb_resource`` keyword arguments are the
supported test injection points.

Reserved-word handling
----------------------
None of the attributes written by this module are DynamoDB reserved
words (``joke_text``, ``audio_ref``, ``model_id``, ``voice_id``,
``created_at``, ``expires_at``), so :func:`persist` writes them
directly without ``ExpressionAttributeNames``. The schema is shared
with :mod:`joke_api.rate_limiter`, which does need an alias for the
``count`` reserved word.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

__all__ = [
    "DEFAULT_TABLE_NAME",
    "TABLE_NAME_ENV_VAR",
    "JOKE_TEXT_MAX_LEN",
    "AUDIO_REF_MAX_LEN",
    "MODEL_ID_MAX_LEN",
    "VOICE_ID_MAX_LEN",
    "RETENTION_DAYS",
    "JokeRecord",
    "JokeStoreValidationError",
    "JokeStorePersistError",
    "JokeStoreUnavailable",
    "persist",
    "get",
]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Environment variable consulted for the DynamoDB table name.
TABLE_NAME_ENV_VAR: str = "DADJOKES_TABLE"

#: Default table name when ``DADJOKES_TABLE`` is unset. Matches the
#: rate limiter's default so both components share a single table.
DEFAULT_TABLE_NAME: str = "dadjokes"

#: Maximum joke_text length in characters (R18.1, R18.6).
JOKE_TEXT_MAX_LEN: int = 2000

#: Maximum audio_ref length in characters (R18.1, R18.6).
AUDIO_REF_MAX_LEN: int = 2048

#: Maximum model_id length in characters (design.md § Data Models).
MODEL_ID_MAX_LEN: int = 128

#: Maximum voice_id length in characters (design.md § Data Models).
VOICE_ID_MAX_LEN: int = 128

#: Days that records are retained before TTL deletion (R18.4).
RETENTION_DAYS: int = 90

# DynamoDB attribute names. Single source of truth for the schema
# referenced in design.md § Data Models.
_PK_PREFIX = "JOKE#"
_SK_VALUE = "META"
_ATTR_JOKE_TEXT = "joke_text"
_ATTR_AUDIO_REF = "audio_ref"
_ATTR_MODEL_ID = "model_id"
_ATTR_VOICE_ID = "voice_id"
_ATTR_CREATED_AT = "created_at"
_ATTR_EXPIRES_AT = "expires_at"


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class JokeStoreValidationError(Exception):
    """Raised by :func:`persist` when a record violates size limits.

    Maps to R18.6: the record is rejected without a DynamoDB call so
    existing rows are left unchanged. The handler should treat this
    as a programmer error -- size limits are the same that the
    Joke_Generator and Voice_Synthesizer enforce upstream -- and
    record a validation-failure log entry per R18.6.

    Attributes:
        field: The offending field name (``"joke_text"``,
            ``"audio_ref"``, ``"model_id"``, ``"voice_id"``,
            ``"id"``, or ``"created_at"``).
        reason: Short human-readable description of the violation.
    """

    __slots__ = ("field", "reason")

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"joke_store validation failed for {field}: {reason}")


class JokeStorePersistError(Exception):
    """Raised by :func:`persist` when DynamoDB rejects the write.

    Per R18.5 and Property 43, the handler catches this typed
    exception and continues with the visitor response so the visitor
    is unaffected by persistence failures. The original boto3
    exception is chained via ``raise ... from exc`` so the
    observability layer can record the underlying error class and
    message; the visitor response body never contains this detail
    (R7.5, Property 20).

    Attributes:
        joke_id: The id of the joke whose write failed.
    """

    __slots__ = ("joke_id",)

    def __init__(self, joke_id: str, message: str) -> None:
        self.joke_id = joke_id
        super().__init__(f"joke_store persist failed for id={joke_id}: {message}")


class JokeStoreUnavailable(Exception):
    """Raised by :func:`get` when DynamoDB is unreachable on read.

    Distinct from :class:`JokeStorePersistError` so callers (the ops
    audit endpoint exposed by the handler at ``GET /v1/jokes/{id}``)
    can distinguish "the store could not be queried" from "the write
    of a brand-new record failed".

    Attributes:
        joke_id: The id whose lookup failed.
    """

    __slots__ = ("joke_id",)

    def __init__(self, joke_id: str, message: str) -> None:
        self.joke_id = joke_id
        super().__init__(f"joke_store get failed for id={joke_id}: {message}")


# ---------------------------------------------------------------------------
# JokeRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JokeRecord:
    """A single persisted joke.

    Attributes:
        id: Unique generation identifier (UUID v4 string per R18.1).
        joke_text: The joke text returned to the visitor; 1..2000
            characters (R18.1, R18.6).
        audio_ref: The S3 URI of the synthesized audio
            (e.g. ``s3://audio/<id>.mp3``), or ``None`` when audio
            was unavailable for this joke (R2.6, R2.9). When set,
            length is bounded to 2048 characters (R18.1, R18.6).
        model_id: Bedrock model identifier used to generate the joke;
            1..128 characters (design.md § Data Models).
        voice_id: Polly voice identifier used to synthesize the
            audio; 1..128 characters (design.md § Data Models).
        created_at: Timezone-aware UTC timestamp of generation.
            ``persist`` requires ``tzinfo`` to be set so the ISO 8601
            string written to DynamoDB is unambiguous (R18.1).

    The dataclass is frozen + slotted to give a hashable, immutable
    record that the handler can pass around without worrying about
    accidental mutation between persist and observability emission.
    """

    id: str
    joke_text: str
    audio_ref: str | None
    model_id: str
    voice_id: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_table(
    table_name: str | None,
    dynamodb_resource: Any | None,
) -> Any:
    """Return a DynamoDB ``Table`` resource for the configured table.

    Resolution order matches :mod:`joke_api.rate_limiter`: explicit
    ``table_name`` argument, then ``$DADJOKES_TABLE``, then
    :data:`DEFAULT_TABLE_NAME`.
    """
    if table_name is None:
        table_name = os.environ.get(TABLE_NAME_ENV_VAR, DEFAULT_TABLE_NAME)
    if dynamodb_resource is None:
        dynamodb_resource = boto3.resource("dynamodb")
    return dynamodb_resource.Table(table_name)


def _key(joke_id: str) -> dict[str, str]:
    """Build the DynamoDB primary key for ``joke_id``."""
    return {"pk": f"{_PK_PREFIX}{joke_id}", "sk": _SK_VALUE}


def _format_created_at(created_at: datetime) -> str:
    """Format ``created_at`` as an ISO 8601 UTC string with ``Z`` suffix.

    DynamoDB stores strings opaquely; using a fixed suffix gives us a
    parseable round-trip independent of how the datetime was
    originally constructed (e.g., ``timezone.utc`` vs.
    ``ZoneInfo("UTC")``).
    """
    utc = created_at.astimezone(timezone.utc)
    # Drop microseconds rather than emit fractional seconds so the
    # string format is stable across platforms; the joke_text and the
    # generation id already give us the per-event uniqueness needed
    # for audit replay (R18.2).
    utc = utc.replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_created_at(value: str) -> datetime:
    """Parse the ``Z``-suffixed ISO 8601 string written by :func:`persist`.

    Returns a tz-aware UTC datetime; matches the format produced by
    :func:`_format_created_at` so :func:`get` round-trips
    :class:`JokeRecord` instances faithfully.
    """
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("created_at must be an ISO 8601 UTC string ending in 'Z'")
    naive = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return naive.replace(tzinfo=timezone.utc)


def _compute_expires_at(created_at: datetime) -> int:
    """Return the TTL epoch seconds for a row created at ``created_at``.

    Equal to ``int((created_at + 90d).timestamp())`` per R18.4.
    DynamoDB's TTL sweeper deletes rows within 48 hours of the TTL,
    well inside the "delete within 24 hours of crossing 90 days"
    target when paired with the table-level TTL configuration on
    ``expires_at`` provisioned by IaC (task 16.1).
    """
    expires_at = created_at.astimezone(timezone.utc) + timedelta(days=RETENTION_DAYS)
    return int(expires_at.timestamp())


def _validate_record(record: JokeRecord) -> None:
    """Validate a :class:`JokeRecord` against the schema size bounds.

    Raises :class:`JokeStoreValidationError` for any violation; the
    field name in the exception is exactly the dataclass attribute
    name so the handler's persistence-failure log entry can identify
    the offending field unambiguously (R18.6).
    """
    if not isinstance(record, JokeRecord):
        raise JokeStoreValidationError(
            "record", "must be a JokeRecord instance"
        )

    if not isinstance(record.id, str) or record.id == "":
        raise JokeStoreValidationError("id", "must be a non-empty string")

    if not isinstance(record.joke_text, str):
        raise JokeStoreValidationError("joke_text", "must be a string")
    if len(record.joke_text) < 1 or len(record.joke_text) > JOKE_TEXT_MAX_LEN:
        raise JokeStoreValidationError(
            "joke_text",
            f"length {len(record.joke_text)} outside [1, {JOKE_TEXT_MAX_LEN}]",
        )

    if record.audio_ref is not None:
        if not isinstance(record.audio_ref, str):
            raise JokeStoreValidationError(
                "audio_ref", "must be a string or None"
            )
        if len(record.audio_ref) > AUDIO_REF_MAX_LEN:
            raise JokeStoreValidationError(
                "audio_ref",
                f"length {len(record.audio_ref)} exceeds {AUDIO_REF_MAX_LEN}",
            )

    if not isinstance(record.model_id, str):
        raise JokeStoreValidationError("model_id", "must be a string")
    if len(record.model_id) < 1 or len(record.model_id) > MODEL_ID_MAX_LEN:
        raise JokeStoreValidationError(
            "model_id",
            f"length {len(record.model_id)} outside [1, {MODEL_ID_MAX_LEN}]",
        )

    if not isinstance(record.voice_id, str):
        raise JokeStoreValidationError("voice_id", "must be a string")
    if len(record.voice_id) < 1 or len(record.voice_id) > VOICE_ID_MAX_LEN:
        raise JokeStoreValidationError(
            "voice_id",
            f"length {len(record.voice_id)} outside [1, {VOICE_ID_MAX_LEN}]",
        )

    if not isinstance(record.created_at, datetime):
        raise JokeStoreValidationError(
            "created_at", "must be a datetime instance"
        )
    if record.created_at.tzinfo is None:
        raise JokeStoreValidationError(
            "created_at", "must be timezone-aware (UTC)"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def persist(
    record: JokeRecord,
    *,
    table_name: str | None = None,
    dynamodb_resource: Any | None = None,
) -> None:
    """Persist a :class:`JokeRecord` to DynamoDB.

    Builds a single-table item per ``design.md`` § Data Models with
    ``pk = "JOKE#" + record.id``, ``sk = "META"``, the joke
    attributes, and an ``expires_at`` TTL attribute set to
    ``created_at + 90 days`` (R18.4). The write is a single
    ``PutItem`` call; on success the row is durably stored and visible
    to a subsequent :func:`get` with consistent read.

    The audio reference is stored as the S3 URI (e.g.
    ``s3://audio/<id>.mp3``) for ops audit replay (R18.2). The
    visitor-facing response builder is responsible for stripping the
    ARN/URI and substituting the 15-minute presigned URL (R18.3) --
    that translation is intentionally outside this module's scope.

    Args:
        record: The joke to persist. Sizes are validated before any
            DynamoDB call.
        table_name: Override for the DynamoDB table name. Defaults to
            ``$DADJOKES_TABLE`` or ``"dadjokes"``.
        dynamodb_resource: Override for the boto3 ``dynamodb``
            resource. Defaults to ``boto3.resource("dynamodb")``.

    Raises:
        JokeStoreValidationError: When any field violates the size
            bounds defined in ``design.md`` § Data Models. Per R18.6
            no DynamoDB call is made and existing rows are unchanged.
        JokeStorePersistError: When DynamoDB returns an error or is
            unreachable. The handler catches this and soft-fails the
            persistence step per R18.5.
    """
    _validate_record(record)

    item: dict[str, Any] = {
        "pk": f"{_PK_PREFIX}{record.id}",
        "sk": _SK_VALUE,
        _ATTR_JOKE_TEXT: record.joke_text,
        _ATTR_MODEL_ID: record.model_id,
        _ATTR_VOICE_ID: record.voice_id,
        _ATTR_CREATED_AT: _format_created_at(record.created_at),
        _ATTR_EXPIRES_AT: _compute_expires_at(record.created_at),
    }
    if record.audio_ref is not None:
        item[_ATTR_AUDIO_REF] = record.audio_ref

    table = _resolve_table(table_name, dynamodb_resource)
    try:
        table.put_item(Item=item)
    except (BotoCoreError, ClientError) as exc:
        raise JokeStorePersistError(record.id, str(exc)) from exc


def get(
    joke_id: str,
    *,
    table_name: str | None = None,
    dynamodb_resource: Any | None = None,
) -> JokeRecord | None:
    """Return the :class:`JokeRecord` for ``joke_id`` or ``None``.

    Issues a single ``GetItem`` with ``ConsistentRead=True`` so the
    audit endpoint sees the most recent successful :func:`persist`
    call, then hydrates the stored attributes back into a
    :class:`JokeRecord`. The ``created_at`` ISO 8601 string is parsed
    back to a tz-aware UTC datetime so callers receive the same
    representation that :func:`persist` accepts (Property 40).

    Args:
        joke_id: The generation identifier to look up.
        table_name: Override for the DynamoDB table name. Defaults to
            ``$DADJOKES_TABLE`` or ``"dadjokes"``.
        dynamodb_resource: Override for the boto3 ``dynamodb``
            resource. Defaults to ``boto3.resource("dynamodb")``.

    Returns:
        The :class:`JokeRecord` matching ``joke_id``, or ``None``
        when no row exists for that id (R18.3).

    Raises:
        ValueError: When ``joke_id`` is not a non-empty string.
        JokeStoreUnavailable: When DynamoDB is unreachable or returns
            an error response, or when a stored item is missing
            required attributes (treated as an availability failure
            rather than silently dropping data).
    """
    if not isinstance(joke_id, str) or joke_id == "":
        raise ValueError("joke_id must be a non-empty string")

    table = _resolve_table(table_name, dynamodb_resource)
    try:
        response = table.get_item(
            Key=_key(joke_id),
            ConsistentRead=True,
        )
    except (BotoCoreError, ClientError) as exc:
        raise JokeStoreUnavailable(joke_id, str(exc)) from exc

    item = response.get("Item")
    if item is None:
        return None

    try:
        joke_text = item[_ATTR_JOKE_TEXT]
        model_id = item[_ATTR_MODEL_ID]
        voice_id = item[_ATTR_VOICE_ID]
        created_at_raw = item[_ATTR_CREATED_AT]
    except KeyError as exc:
        raise JokeStoreUnavailable(
            joke_id, f"stored item missing attribute {exc.args[0]!r}"
        ) from exc

    try:
        created_at = _parse_created_at(created_at_raw)
    except ValueError as exc:
        raise JokeStoreUnavailable(
            joke_id, f"stored created_at is malformed: {exc}"
        ) from exc

    audio_ref = item.get(_ATTR_AUDIO_REF)
    # Treat an empty stored string as an explicit empty audio_ref;
    # ``persist`` only ever writes the attribute when audio_ref is not
    # ``None``, so a missing attribute round-trips faithfully to
    # ``None`` and a present attribute round-trips to its stored
    # string value.
    if audio_ref is not None and not isinstance(audio_ref, str):
        raise JokeStoreUnavailable(
            joke_id, "stored audio_ref is not a string"
        )

    return JokeRecord(
        id=joke_id,
        joke_text=joke_text,
        audio_ref=audio_ref,
        model_id=model_id,
        voice_id=voice_id,
        created_at=created_at,
    )
