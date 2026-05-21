"""Property test 5: Input validation rejection short-circuits the pipeline.

**Validates: Requirements 1.7, 3.4, 3.5**

Per design.md, Property 5:

  *For any* request whose seed-word input violates a rule (more than 5 entries,
  any entry exceeding 30 chars, any entry containing characters outside
  ``[A-Za-z0-9'-]``, or aggregate length exceeding 100 chars), the handler
  SHALL return HTTP 400 identifying the violated rule, and SHALL NOT invoke
  the Input_Moderator, Bedrock, Polly, or the Rate_Limiter increment.

The Lambda handler does not exist yet (task 10.1 lives in wave 5), so this
test verifies the validator's contract -- the only piece of the pipeline
in scope right now -- and asserts that no downstream module
(``input_moderator``, ``joke_generator``, ``voice_synthesizer``,
``rate_limiter``) is ever invoked while bad input is being rejected.

Spies are installed via ``unittest.mock.patch.object`` on the parent
``joke_api`` package so that any future code path that accidentally calls
into a downstream module from inside the validator would surface as a
``mock_calls`` entry and fail the test.
"""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from unittest.mock import MagicMock, patch

import joke_api

# Import the downstream modules so that they become attributes of the
# ``joke_api`` package (Python does not auto-load submodules on package
# import). This lets ``patch.object(joke_api, "input_moderator", ...)``
# resolve cleanly regardless of which submodule the validator might
# accidentally invoke.
from joke_api import (  # noqa: F401  -- imported for spy installation
    input_moderator,
    joke_generator,
    rate_limiter,
    request_validator,
    voice_synthesizer,
)
from joke_api.request_validator import (
    ValidationError,
    aggregate_length,
    seed_word_charset,
    seed_word_count,
    seed_word_length,
)

# ---------------------------------------------------------------------------
# Charset and helper definitions
# ---------------------------------------------------------------------------

# Allowed charset per R1.7: ASCII letters, digits, hyphen, apostrophe.
ALLOWED_CHARS: str = string.ascii_letters + string.digits + "'-"

# Disallowed printable ASCII (excluding whitespace control chars so the
# JSON encoding stays well-defined). Includes things like " ", "*", "_",
# "@", "/", "?", etc. that the validator must reject under R1.7.
_PRINTABLE_DISALLOWED = [
    c
    for c in string.printable
    if c not in ALLOWED_CHARS and c not in "\r\n\t\x0b\x0c"
]
DISALLOWED_CHAR = st.sampled_from(_PRINTABLE_DISALLOWED)

# A "valid" seed word: length 1..30, all chars in the allowed set.
valid_seed_word = st.text(alphabet=ALLOWED_CHARS, min_size=1, max_size=30)


def _event(seed_words: object) -> dict:
    """Wrap a ``seedWords`` payload in a synthetic API Gateway event body."""
    return {"body": json.dumps({"seedWords": seed_words})}


# ---------------------------------------------------------------------------
# Strategies, one per invalid-input bucket required by Property 5
# ---------------------------------------------------------------------------

# (a) Lists with more than 5 entries.
too_many_seed_words = st.lists(valid_seed_word, min_size=6, max_size=20)


# (b) Lists where at least one entry exceeds 30 chars (count <= 5 so that
# the count rule is not what fires).
@st.composite
def overlong_seed_word_list(draw) -> list[str]:
    n = draw(st.integers(min_value=1, max_value=5))
    bad_index = draw(st.integers(min_value=0, max_value=n - 1))
    words: list[str] = []
    for i in range(n):
        if i == bad_index:
            words.append(
                draw(st.text(alphabet=ALLOWED_CHARS, min_size=31, max_size=200))
            )
        else:
            words.append(draw(valid_seed_word))
    return words


# (c) Lists where at least one entry contains a char outside [A-Za-z0-9'-].
# Word length stays in [1, 30] (prefix <=14, 1 bad char, suffix <=14) so
# the length rule cannot fire instead of the charset rule.
@st.composite
def disallowed_charset_list(draw) -> list[str]:
    n = draw(st.integers(min_value=1, max_value=5))
    bad_index = draw(st.integers(min_value=0, max_value=n - 1))
    words: list[str] = []
    for i in range(n):
        if i == bad_index:
            prefix = draw(st.text(alphabet=ALLOWED_CHARS, min_size=0, max_size=14))
            bad = draw(DISALLOWED_CHAR)
            suffix = draw(st.text(alphabet=ALLOWED_CHARS, min_size=0, max_size=14))
            words.append(prefix + bad + suffix)
        else:
            words.append(draw(valid_seed_word))
    return words


# (d) Lists whose joined aggregate length exceeds 100 chars while every
# word stays within 1..30 chars and the count stays within 1..5.
# 4 words of min length 25 -> 4*25 + 3 spaces = 103 chars.
# 5 words of min length 21 -> 5*21 + 4 spaces = 109 chars.
@st.composite
def aggregate_too_long_list(draw) -> list[str]:
    n = draw(st.integers(min_value=4, max_value=5))
    min_word_len = 25 if n == 4 else 21
    return [
        draw(st.text(alphabet=ALLOWED_CHARS, min_size=min_word_len, max_size=30))
        for _ in range(n)
    ]


# ---------------------------------------------------------------------------
# Shared hypothesis settings: at least 100 iterations per task spec.
# ---------------------------------------------------------------------------

PBT_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spies():
    """Install spies on every downstream module via patch.object on the package.

    Any access that resolves through the ``joke_api`` package (e.g.
    ``from joke_api import input_moderator``) will resolve to a MagicMock
    while the patches are active, so a stray downstream call would record
    a non-empty ``mock_calls`` and fail the test.
    """
    return (
        patch.object(joke_api, "input_moderator", MagicMock(name="input_moderator")),
        patch.object(joke_api, "joke_generator", MagicMock(name="joke_generator")),
        patch.object(joke_api, "voice_synthesizer", MagicMock(name="voice_synthesizer")),
        patch.object(joke_api, "rate_limiter", MagicMock(name="rate_limiter")),
    )


def _assert_no_downstream_calls() -> None:
    """Assert every downstream module is still untouched after validation."""
    for name in ("input_moderator", "joke_generator", "voice_synthesizer", "rate_limiter"):
        module = getattr(joke_api, name)
        # MagicMock answers ``assert_not_called`` for the module-as-callable
        # check; ``mock_calls`` covers attribute-style invocations such as
        # ``rate_limiter.increment(...)`` that the handler will ultimately use.
        assert isinstance(module, MagicMock), (
            f"spy not installed on joke_api.{name}: {type(module)!r}"
        )
        module.assert_not_called()
        assert module.mock_calls == [], (
            f"validator must not invoke joke_api.{name}; "
            f"observed calls: {module.mock_calls!r}"
        )


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(seed_words=too_many_seed_words)
def test_more_than_five_seed_words_short_circuits(seed_words: list[str]) -> None:
    """Property 5 (a): lists with > 5 entries raise ValidationError before any
    downstream call.
    """
    spies = _spies()
    with spies[0], spies[1], spies[2], spies[3]:
        with pytest.raises(ValidationError) as exc_info:
            request_validator.validate(_event(seed_words))

        assert exc_info.value.rule == seed_word_count
        _assert_no_downstream_calls()


@PBT_SETTINGS
@given(seed_words=overlong_seed_word_list())
def test_overlong_seed_word_short_circuits(seed_words: list[str]) -> None:
    """Property 5 (b): a seed word longer than 30 chars raises ValidationError
    before any downstream call.
    """
    spies = _spies()
    with spies[0], spies[1], spies[2], spies[3]:
        with pytest.raises(ValidationError) as exc_info:
            request_validator.validate(_event(seed_words))

        assert exc_info.value.rule == seed_word_length
        _assert_no_downstream_calls()


@PBT_SETTINGS
@given(seed_words=disallowed_charset_list())
def test_disallowed_charset_short_circuits(seed_words: list[str]) -> None:
    """Property 5 (c): a seed word containing chars outside ``[A-Za-z0-9'-]``
    raises ValidationError before any downstream call.
    """
    # Strategy guarantees every word stays within 1..30 chars, so the
    # length rule cannot fire first; this assume is a defensive guard.
    assume(all(1 <= len(w) <= 30 for w in seed_words))

    spies = _spies()
    with spies[0], spies[1], spies[2], spies[3]:
        with pytest.raises(ValidationError) as exc_info:
            request_validator.validate(_event(seed_words))

        assert exc_info.value.rule == seed_word_charset
        _assert_no_downstream_calls()


@PBT_SETTINGS
@given(seed_words=aggregate_too_long_list())
def test_aggregate_length_short_circuits(seed_words: list[str]) -> None:
    """Property 5 (d): aggregate joined length > 100 chars raises
    ValidationError before any downstream call.
    """
    # Sanity check the strategy: aggregate must exceed 100 chars while
    # individual word and count constraints are still satisfied.
    assume(len(" ".join(seed_words)) > 100)
    assume(all(1 <= len(w) <= 30 for w in seed_words))
    assume(len(seed_words) <= 5)

    spies = _spies()
    with spies[0], spies[1], spies[2], spies[3]:
        with pytest.raises(ValidationError) as exc_info:
            request_validator.validate(_event(seed_words))

        assert exc_info.value.rule == aggregate_length
        _assert_no_downstream_calls()
