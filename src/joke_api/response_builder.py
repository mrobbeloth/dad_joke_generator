"""Response and sanitized-error construction for the Joke_API.

This module is the SINGLE chokepoint for all client-facing JSON responses.
It MUST never accept or echo free-form internal text such as Python
tracebacks, file paths, AWS resource ARNs (``arn:aws:...``), or AWS
account IDs.

User-facing error messages are fixed by category. Callers may only attach
a small, allowlisted set of structured fields per category. Any field key
outside its category's allowlist is silently dropped.

Validates / enforces:
    - Requirement 7.5: errors render only an enumerated category and a
      static, human-readable suggested next action; stack traces, cloud
      provider identifiers, and other internal system details never reach
      the client.
    - Requirement 7.6: full technical error detail is the responsibility
      of the logging layer, never the response body.
    - Property 20: error responses are sanitized (and logged in full
      elsewhere).

This module is a leaf utility:
    - Pure function: no logging, no boto3, no clock reads.
    - Standard library only.
    - Imports nothing else from ``joke_api``.
"""

from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------------------
# Public error category constants.
#
# These constants are the ONLY values accepted by ``sanitize_error``.
# Callers MUST reference these constants instead of bare strings so that
# typos are caught at import time rather than at runtime.
# ---------------------------------------------------------------------------

VALIDATION = "validation"
MODERATION = "moderation"
RATE_LIMITED = "rate_limited"
UNAVAILABLE = "unavailable"
MODERATION_UNAVAILABLE = "moderation_unavailable"
MODERATION_TIMEOUT = "moderation_timeout"
CLIENT_IP_UNRESOLVABLE = "client_ip_unresolvable"
INTERNAL_ERROR = "internal_error"
# 404-style category for retrieval misses (R18.3). Distinct from
# ``VALIDATION`` because the request itself parsed correctly -- we
# simply have no record for the supplied id, and 404 is the right
# semantic status code rather than 400.
NOT_FOUND = "not_found"

# ---------------------------------------------------------------------------
# Internal category map: category -> (default_status_code, default_message).
#
# The user-facing message is FIXED by category. Callers cannot override it.
# This is what guarantees R7.5 / Property 20: there is no code path through
# this module that lets internal text reach the response body.
# ---------------------------------------------------------------------------

_CATEGORY_MAP: dict[str, tuple[int, str]] = {
    VALIDATION: (400, "Request did not pass validation."),
    MODERATION: (400, "Input must be G or PG rated."),
    RATE_LIMITED: (429, "Daily limit reached."),
    UNAVAILABLE: (503, "Service temporarily unavailable."),
    MODERATION_UNAVAILABLE: (503, "Moderation service unavailable."),
    MODERATION_TIMEOUT: (504, "Moderation service timeout."),
    CLIENT_IP_UNRESOLVABLE: (400, "Client IP could not be identified."),
    INTERNAL_ERROR: (500, "An unexpected error occurred."),
    NOT_FOUND: (404, "Resource not found."),
}

# ---------------------------------------------------------------------------
# Per-category allowlists for structured extra fields.
#
# Anything OUTSIDE these allowlists is silently dropped. Note the absence
# of any free-form ``detail``/``message``/``trace`` keys: those would be
# leak vectors and are intentionally not permitted on any category.
# ---------------------------------------------------------------------------

_FIELD_ALLOWLIST: dict[str, frozenset[str]] = {
    VALIDATION: frozenset({"rule"}),
    MODERATION: frozenset(),
    RATE_LIMITED: frozenset({"resetAtUtc"}),
    UNAVAILABLE: frozenset(),
    MODERATION_UNAVAILABLE: frozenset(),
    MODERATION_TIMEOUT: frozenset(),
    CLIENT_IP_UNRESOLVABLE: frozenset(),
    INTERNAL_ERROR: frozenset(),
    NOT_FOUND: frozenset(),
}

_JSON_HEADERS: dict[str, str] = {"Content-Type": "application/json"}


def sanitize_error(
    category: str,
    *,
    status: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a sanitized API Gateway-style error response.

    Args:
        category: One of the module-level error category constants
            (``VALIDATION``, ``MODERATION``, ``RATE_LIMITED``,
            ``UNAVAILABLE``, ``MODERATION_UNAVAILABLE``,
            ``MODERATION_TIMEOUT``, ``CLIENT_IP_UNRESOLVABLE``,
            ``INTERNAL_ERROR``).
        status: Optional override for the HTTP status code. When omitted,
            the default status code for ``category`` is used.
        **fields: Structured, allowlisted extra fields to merge into the
            response body. Keys outside the per-category allowlist are
            silently dropped. There is no allowlisted ``message`` or
            ``detail`` key: the user-facing message is fixed by category.

    Returns:
        An API Gateway-style HTTP response dict::

            {
                "statusCode": <int>,
                "headers": {"Content-Type": "application/json"},
                "body": <json_string>,
            }

    Raises:
        ValueError: If ``category`` is not one of the allowlisted
            categories defined in this module.
    """
    if category not in _CATEGORY_MAP:
        raise ValueError(f"Unknown error category: {category!r}")

    default_status, message = _CATEGORY_MAP[category]
    status_code = default_status if status is None else status

    allowed = _FIELD_ALLOWLIST[category]
    extra = {key: value for key, value in fields.items() if key in allowed}

    body: dict[str, Any] = {"error": category, "message": message}
    body.update(extra)

    return {
        "statusCode": status_code,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(body, separators=(",", ":")),
    }


def build_success(
    *,
    joke_id: str,
    text: str,
    audio_url: str | None,
    audio_available: bool,
    remaining: int | None,
    model_id: str,
    voice_id: str,
) -> dict[str, Any]:
    """Build a 200 OK API Gateway-style response for a generated joke.

    Args:
        joke_id: UUID v4 identifying the persisted joke record.
        text: Final, moderation-approved joke text.
        audio_url: Presigned audio URL, or ``None`` when audio is not
            available. When ``audio_available`` is ``False``, the body
            field ``audioUrl`` is forced to ``null`` regardless of this
            argument's value.
        audio_available: Whether Polly synthesis succeeded for this joke.
        remaining: Visitor's remaining daily generation count after this
            request. ``None`` indicates "not applicable" (e.g. an audit
            replay via ``GET /v1/jokes/{id}`` where no quota was
            consumed); when ``None`` the ``remaining`` field is omitted
            from the response body so retrieval responses stay clean.
        model_id: Bedrock model identifier used to generate the joke.
        voice_id: Polly voice identifier used (or that would have been
            used) for synthesis.

    Returns:
        An API Gateway-style HTTP response dict with ``statusCode`` 200,
        ``Content-Type: application/json`` headers, and a compact JSON
        body matching the design's "Response: 200 OK" contract.
    """
    body: dict[str, Any] = {
        "id": joke_id,
        "text": text,
        "audioUrl": audio_url if audio_available else None,
        "audioAvailable": audio_available,
        "modelId": model_id,
        "voiceId": voice_id,
    }
    if remaining is not None:
        # Insert ``remaining`` between ``audioAvailable`` and ``modelId``
        # to preserve the design's documented field ordering on the
        # POST path; rebuild the dict so insertion order is stable.
        body = {
            "id": joke_id,
            "text": text,
            "audioUrl": audio_url if audio_available else None,
            "audioAvailable": audio_available,
            "remaining": remaining,
            "modelId": model_id,
            "voiceId": voice_id,
        }

    return {
        "statusCode": 200,
        "headers": dict(_JSON_HEADERS),
        "body": json.dumps(body, separators=(",", ":")),
    }
