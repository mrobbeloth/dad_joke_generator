"""Request validation for the Joke_API ``POST /v1/jokes`` endpoint.

This module is the single chokepoint that parses and validates the body of an
API Gateway HTTP API event before any downstream work runs. It enforces:

* seed-word count, per-word length, and charset rules (Requirement 1.7),
* aggregate joined seed-word length (Requirements 3.4 and 3.5), and
* sanitized, structured error categorization that the ``response_builder``
  can map to the ``validation`` error category exposed to clients
  (Requirement 7.5).

The validator is a pure function: no logging, no boto3 calls, no I/O. Raising
:class:`ValidationError` short-circuits the pipeline before any
moderator, Bedrock, Polly, persistence, or rate-limiter call is made.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Rule identifiers
# ---------------------------------------------------------------------------
# These string constants are the canonical ``rule`` values surfaced to
# ``response_builder`` and the property tests. Keeping them here as
# module-level names ensures every layer agrees on the spelling.

seed_word_count: str = "seed_word_count"
seed_word_length: str = "seed_word_length"
seed_word_charset: str = "seed_word_charset"
aggregate_length: str = "aggregate_length"
body_invalid: str = "body_invalid"
seed_words_type: str = "seed_words_type"

# ---------------------------------------------------------------------------
# Constraint constants
# ---------------------------------------------------------------------------
MAX_SEED_WORDS: int = 5
MIN_SEED_WORD_LEN: int = 1
MAX_SEED_WORD_LEN: int = 30
MAX_AGGREGATE_LEN: int = 100

# Charset per R1.7: ASCII letters, digits, hyphen, apostrophe. No whitespace,
# no underscore, no other punctuation. ``re.fullmatch`` is used at the call
# site to anchor the pattern across the entire seed word.
_SEED_WORD_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z0-9'\-]+")


class ValidationError(Exception):
    """Raised when an incoming request fails a validator rule.

    The handler catches this exception and hands it to ``response_builder``
    so a sanitized ``{"error": "validation", "rule": <rule>, ...}`` body
    can be returned to the client without leaking internal detail.

    Attributes:
        rule: One of the module-level rule identifiers
            (``seed_word_count``, ``seed_word_length``,
            ``seed_word_charset``, ``aggregate_length``, ``body_invalid``,
            ``seed_words_type``).
        field: JSON-pointer-style path to the offending field, e.g.
            ``"seedWords"`` or ``"seedWords[2]"``.
        message: Optional human-readable detail. Defaults to ``rule``.
    """

    __slots__ = ("rule", "field", "message")

    def __init__(self, rule: str, field: str, message: str | None = None) -> None:
        self.rule = rule
        self.field = field
        self.message = message if message is not None else rule
        super().__init__(f"{rule} at {field}: {self.message}")


def validate(event: dict) -> list[str]:
    """Validate an API Gateway HTTP API event body for ``POST /v1/jokes``.

    Returns the validated ``seedWords`` list (possibly empty). The returned
    list is the value the handler should pass downstream to the input
    moderator and the joke generator.

    Raises:
        ValidationError: with one of the module-level ``rule`` constants
            when the request is malformed or violates a documented rule.
            Raising occurs before any downstream component is invoked.
    """
    if not isinstance(event, dict):
        raise ValidationError(body_invalid, "event", "event must be an object")

    raw_body: Any = event.get("body")
    if raw_body is None:
        raise ValidationError(body_invalid, "body", "request body is required")

    # API Gateway delivers the body as a string. Accept ``bytes`` defensively
    # (e.g., when invoked directly in tests) but reject anything else.
    if isinstance(raw_body, (bytes, bytearray)):
        try:
            raw_body = bytes(raw_body).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                body_invalid, "body", "body is not valid UTF-8"
            ) from exc

    if not isinstance(raw_body, str):
        raise ValidationError(body_invalid, "body", "body must be a string")

    # Honor the API Gateway ``isBase64Encoded`` flag so binary-encoded JSON
    # bodies can still be parsed deterministically.
    if event.get("isBase64Encoded") is True:
        try:
            raw_body = base64.b64decode(raw_body, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(
                body_invalid, "body", "base64-encoded body is invalid"
            ) from exc

    if raw_body == "":
        raise ValidationError(body_invalid, "body", "request body must not be empty")

    try:
        parsed: Any = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(
            body_invalid, "body", "body is not valid JSON"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValidationError(
            body_invalid, "body", "body must be a JSON object"
        )

    # ``seedWords`` is optional per design; absent means an empty list which
    # is a valid 0-seed-word request.
    seed_words_raw: Any = parsed.get("seedWords", [])

    if not isinstance(seed_words_raw, list):
        raise ValidationError(
            seed_words_type, "seedWords", "seedWords must be an array"
        )

    if len(seed_words_raw) > MAX_SEED_WORDS:
        raise ValidationError(
            seed_word_count,
            "seedWords",
            f"seedWords must contain at most {MAX_SEED_WORDS} entries",
        )

    validated: list[str] = []
    for index, word in enumerate(seed_words_raw):
        field = f"seedWords[{index}]"

        # ``isinstance(word, str)`` already excludes bool/int/None/etc.
        if not isinstance(word, str):
            raise ValidationError(
                seed_words_type, field, "seed word must be a string"
            )

        word_len = len(word)
        if word_len < MIN_SEED_WORD_LEN or word_len > MAX_SEED_WORD_LEN:
            raise ValidationError(
                seed_word_length,
                field,
                (
                    f"seed word length must be between {MIN_SEED_WORD_LEN} "
                    f"and {MAX_SEED_WORD_LEN} characters"
                ),
            )

        if _SEED_WORD_PATTERN.fullmatch(word) is None:
            raise ValidationError(
                seed_word_charset,
                field,
                "seed word may only contain letters, digits, hyphens, or apostrophes",
            )

        validated.append(word)

    # Aggregate length matches what the input_moderator will see when it
    # joins the words (R3.4 caps the submitted text at 100 chars).
    aggregate = " ".join(validated)
    if len(aggregate) > MAX_AGGREGATE_LEN:
        raise ValidationError(
            aggregate_length,
            "seedWords",
            (
                f"aggregate seed-word length must be at most "
                f"{MAX_AGGREGATE_LEN} characters"
            ),
        )

    return validated
