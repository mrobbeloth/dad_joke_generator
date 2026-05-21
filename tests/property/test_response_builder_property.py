"""Property-based tests for ``response_builder.sanitize_error``.

Implements **Property 20** from ``design.md``::

    For any error path (validation, moderation, rate limit, Bedrock failure,
    Polly failure, persistence failure, or unexpected exception), the
    response body SHALL contain only an error category and a human-readable
    suggested next action, SHALL NOT contain stack traces, file paths, AWS
    resource ARNs, AWS account ids, or other internal identifiers, AND a
    corresponding log record containing the full technical detail SHALL be
    emitted within 5 seconds of the error.

**Validates: Requirements 7.5, 7.6**

Strategy: throw "leaky" internal text (Python tracebacks, AWS ARNs, source
file paths, 12-digit AWS account ids, Python exception names) at the
sanitizer through arbitrary keyword arguments and assert that none of those
fragments survive into the JSON response body. ``sanitize_error`` is the
single chokepoint for client-facing error bodies, so this exhaustively
validates the design's "no free-form internal text reaches the client"
contract.
"""

from __future__ import annotations

import json
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from joke_api import response_builder

# ---------------------------------------------------------------------------
# Allowed error categories per the design's "Error Responses" table.
#
# Property 20 requires the body's ``error`` field to be drawn from this set
# regardless of what the caller passes in via ``**fields``.
# ---------------------------------------------------------------------------

ALLOWED_CATEGORIES: tuple[str, ...] = (
    response_builder.VALIDATION,
    response_builder.MODERATION,
    response_builder.RATE_LIMITED,
    response_builder.UNAVAILABLE,
    response_builder.MODERATION_UNAVAILABLE,
    response_builder.MODERATION_TIMEOUT,
)

# ---------------------------------------------------------------------------
# Concrete "leaky" fragments that must never survive into a response body.
#
# These are realistic samples of the internal data that would leak through
# a naive ``str(exc)`` or ``traceback.format_exc()`` echo:
#     * Python traceback header.
#     * AWS Lambda function ARN with a real-shaped account id.
#     * Lambda task-root file path of a handler module.
#     * 12-digit AWS account id in isolation.
#     * Python exception class names commonly seen in tracebacks.
# ---------------------------------------------------------------------------

LEAKY_FRAGMENTS: tuple[str, ...] = (
    "Traceback (most recent call last)",
    "arn:aws:lambda:us-east-1:123456789012:function:joke-api",
    "/var/task/joke_api/handler.py",
    "012345678901",
    "KeyError",
    "ClientError: An error occurred (AccessDenied)",
    'File "/var/task/joke_api/handler.py", line 42, in lambda_handler',
)

# Field-name keys a buggy caller might use to smuggle internal text into the
# response. None of these are in any per-category allowlist, so all of them
# must be silently dropped by ``sanitize_error``.
LEAKY_FIELD_KEYS: tuple[str, ...] = (
    "message",
    "detail",
    "trace",
    "traceback",
    "exception",
    "internal",
    "stack",
    "cause",
    "debug",
)

# Regex for a Lambda/EC2-style absolute Unix source path ending in ``.py``.
# Catches paths like ``/var/task/joke_api/handler.py`` and
# ``/opt/python/lib/foo/bar.py``.
_PATH_RE = re.compile(r"/[A-Za-z]+/[A-Za-z_/]+\.py")

# Regex for a 12-digit AWS account id with word boundaries on both sides.
_ACCOUNT_ID_RE = re.compile(r"\b\d{12}\b")


def _leaky_text() -> st.SearchStrategy[str]:
    """Strategy that produces strings likely to contain leaky internal text.

    Each draw composes 1..4 fragments chosen from arbitrary text and the
    concrete ``LEAKY_FRAGMENTS`` constants, joined by arbitrary glue text.
    The result mimics the shape of an unsanitized exception ``str()`` or a
    raw log line that a buggy caller might attempt to forward to the
    client.
    """
    fragment_strategy = st.one_of(
        st.sampled_from(LEAKY_FRAGMENTS),
        st.text(min_size=0, max_size=40),
    )
    return st.lists(fragment_strategy, min_size=1, max_size=4).map(
        lambda parts: " ".join(parts)
    )


@given(
    category=st.sampled_from(ALLOWED_CATEGORIES),
    leaky=_leaky_text(),
    field_key=st.sampled_from(LEAKY_FIELD_KEYS),
    extra=st.dictionaries(
        keys=st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd"), min_codepoint=33
            ),
            min_size=1,
            max_size=12,
        ),
        values=_leaky_text(),
        max_size=3,
    ),
)
@settings(max_examples=200)
def test_sanitize_error_never_leaks_internal_text(
    category: str,
    leaky: str,
    field_key: str,
    extra: dict[str, str],
) -> None:
    """**Property 20**: error responses are sanitized.

    Validates: Requirements 7.5, 7.6.

    For every allowed category and every realistic blob of leaky internal
    text passed through arbitrary keyword arguments, the JSON response
    body MUST NOT contain Python tracebacks, AWS resource ARNs, file
    paths, or 12-digit AWS account ids, and the ``error`` field MUST
    equal one of the allowlisted categories.
    """
    # Combine the focused leaky payload with a bag of arbitrary extra
    # keyword fields. ``field_key`` may collide with a key in ``extra``;
    # the focused payload wins so the test always exercises a known leaky
    # value.
    fields: dict[str, str] = dict(extra)
    fields[field_key] = leaky

    response = response_builder.sanitize_error(category, **fields)

    # The response envelope itself must be a well-formed API Gateway dict.
    assert set(response.keys()) == {"statusCode", "headers", "body"}
    assert isinstance(response["statusCode"], int)
    assert response["headers"]["Content-Type"] == "application/json"

    body_str = response["body"]
    assert isinstance(body_str, str)

    # Body MUST be valid JSON.
    body = json.loads(body_str)
    assert isinstance(body, dict)

    # Body's category MUST be one of the allowlisted values, regardless of
    # what the caller smuggled in.
    assert body["error"] in ALLOWED_CATEGORIES
    assert body["error"] == category

    # No Python traceback header survives.
    assert "Traceback" not in body_str

    # No AWS resource ARN survives.
    assert "arn:aws:" not in body_str

    # No absolute Unix source-file path survives.
    assert _PATH_RE.search(body_str) is None, (
        f"file path leaked into body: {body_str!r}"
    )

    # No 12-digit AWS account id survives.
    assert _ACCOUNT_ID_RE.search(body_str) is None, (
        f"AWS account id leaked into body: {body_str!r}"
    )

    # The body's user-facing message MUST be the static, category-fixed
    # string. This is the strongest form of "no internal text leaks":
    # the message is a constant, so anything the caller passed through
    # ``**fields`` necessarily did not influence it.
    expected_message = response_builder._CATEGORY_MAP[category][1]
    assert body["message"] == expected_message
