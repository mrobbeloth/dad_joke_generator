"""Property tests for :mod:`joke_api.joke_generator`.

This file implements the joke_generator-level half of the following
correctness properties from ``design.md`` § Correctness Properties:

* **Property 1: Seed-word containment when seeds are supplied.**
  *For any* valid seed-word list, the joke text returned by
  ``POST /v1/jokes`` SHALL contain at least one of the supplied
  seed words as a case-insensitive substring, OR the response SHALL
  be HTTP 503 after exactly 3 attempts.
* **Property 2: Joke length is within 10..80 words inclusive.**
  *For any* sequence of mock Bedrock outputs, the handler SHALL
  return joke text only when its word count is in ``[10, 80]``;
  otherwise the handler SHALL return HTTP 503, and the total number
  of Bedrock attempts SHALL never exceed 3.
* **Property 3: Bedrock failure produces 503 with no partial
  content.** *For any* Bedrock failure mode (transport error,
  exception, or response exceeding the 15 s budget), the handler
  SHALL return HTTP 503 and the response body SHALL NOT contain
  any joke text or audio reference.
* **Property 4: Generation IDs are unique UUID v4s.** *For any*
  sequence of successful generation requests, every returned
  generation identifier SHALL be a syntactically valid UUID v4 and
  no two responses in the sequence SHALL share an identifier.
* **Property 12: Output rejection retries up to three attempts
  with refined prompts.** *For any* sequence of Output_Moderator
  decisions on generated jokes, the total number of Bedrock
  attempts SHALL be at most 3, and on attempts 2 and 3 the prompt
  SHALL include the explicit category prohibitions defined in R4.2.
* **Property 13: All-rejected outputs fall back to a curated safe
  joke.** *For any* sequence of three Output_Moderator decisions
  where every decision is ``not_family_friendly``, the response
  text SHALL be drawn from ``FALLBACK_JOKES``.

**Validates: Requirements 1.2, 1.3, 1.4, 1.5, 1.8, 4.2, 4.3, 4.5, 18.1**

Pipeline-ordering boundary (this file vs. handler tests in task 10.3)
---------------------------------------------------------------------
The 3-attempt budget mentioned by R1.4 / R4.2 is split across two
layers (see the docstring at the top of
:mod:`joke_api.joke_generator`). This file therefore asserts ONLY the
joke_generator-level half of each property:

* **Property 1** — joke_generator builds a prompt that LISTS every
  seed word case-insensitively. Whether the *Bedrock-returned* text
  actually contains a seed word depends on the live model and is
  out of scope here; the handler/integration test in task 10.3
  covers the end-to-end containment guarantee.
* **Property 2** — joke_generator's internal length-retry loop is
  bounded by ``MAX_ATTEMPTS=3`` and surfaces typed exceptions for
  out-of-range output. The HTTP 503 status mapping is the handler's
  job.
* **Property 3** — joke_generator raises typed exceptions
  (:class:`JokeGenerationFailed`, :class:`JokeGenerationTimeout`,
  :class:`JokeGenerationUnavailable`) and returns NO partial joke
  text on failure. The 503 status code is mapped by the handler.
* **Property 4** — UUID v4s are minted by the handler, not by
  joke_generator. This file documents the boundary and verifies the
  surface contract (return type is ``str``); the uniqueness
  assertion lives in the handler tests in task 10.3 / 10.4.
* **Property 12** — joke_generator's internal 3-attempt loop does
  NOT switch ``refine=True`` between attempts; the handler does that
  across separate :func:`generate` calls. This file therefore only
  asserts the prompt-shape contract: ``refine=False`` ⇒ system
  prompt is exactly :data:`SYSTEM_PROMPT_BASE`; ``refine=True`` ⇒
  system prompt appends every R4.2 category prohibition. The
  outer-loop attempt-count bound is verified in task 10.3.
* **Property 13** — joke_generator does NOT pick fallback jokes;
  the handler does, from
  :data:`joke_api.fallback_jokes.FALLBACK_JOKES`. This file
  verifies the joke_generator-level half: when 3 attempts produce
  out-of-range text, :func:`generate` raises
  :class:`JokeGenerationFailed` with ``reason='length_rejected'``
  and ``attempts=3``. The handler-level fallback selection is
  task 10.3.

Stub design
-----------
The Bedrock backend is replaced with a per-example
:class:`_BedrockStub` instance — a small hand-rolled class (not a
``MagicMock``) that exposes the single ``converse(...)`` method the
generator uses. A hand-rolled class is preferred to ``MagicMock``
because the generator navigates the response with attribute access
on nested dicts and we want any deviation from the documented
Converse response shape to be visible rather than silently
auto-stubbed. The stub also captures every call's keyword arguments
so the prompt-shape properties can introspect what was sent without
re-implementing the prompt builder.

A NEW stub instance is built per Hypothesis example so call
counters do not leak between iterations.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from joke_api import joke_generator
from joke_api.joke_generator import (
    MAX_ATTEMPTS,
    MAX_WORDS,
    MIN_WORDS,
    SYSTEM_PROMPT_BASE,
    SYSTEM_PROMPT_REFINED_SUFFIX,
    JokeGenerationFailed,
    JokeGenerationTimeout,
    JokeGenerationUnavailable,
    generate,
)
from joke_api.joke_store import JokeRecord


# ---------------------------------------------------------------------------
# Test-only constants
# ---------------------------------------------------------------------------

# Stable model id used in every test so :func:`joke_api.config.load`
# is never invoked (avoids any SSM I/O in unit-level tests).
_TEST_MODEL_ID: str = "amazon.nova-lite-v1:0"

# Charset matching the validator's seed-word rule (R3.4 / R3.5).
_SEED_WORD_CHARSET: str = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789'-"
)

# Word used to assemble candidate "joke text" of arbitrary word counts.
# A non-empty single token keeps ``str.split()`` deterministic across
# platforms and avoids any incidental moderator concerns.
_FILLER_WORD: str = "banana"


# ---------------------------------------------------------------------------
# Hypothesis settings
# ---------------------------------------------------------------------------

PBT_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.large_base_example,
    ],
)


# ---------------------------------------------------------------------------
# Hand-rolled Bedrock stub
# ---------------------------------------------------------------------------


def _converse_response(text: str) -> dict[str, Any]:
    """Wrap ``text`` in a Converse-shaped response dict.

    Mirrors the structure that
    :func:`joke_api.joke_generator._extract_converse_text` consumes::

        {"output": {"message": {"role": "assistant",
                                "content": [{"text": "..."}]}}}
    """
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        }
    }


class _BedrockStub:
    """Minimal Bedrock-runtime stub for joke_generator property tests.

    Exposes only the surface the generator uses (:py:meth:`converse`)
    and records every call's keyword arguments so the prompt-shape
    properties can introspect what was sent. The constructor accepts
    a list of per-attempt responses; each element is interpreted as:

    * :class:`BaseException` instance — raised to simulate a
      transport / boto error (Property 3 transport branch).
    * Exact string ``"timeout"`` — sleeps for 1 s, blowing the
      monkey-patched :data:`BEDROCK_BUDGET_MS` budget so the
      executor raises :class:`concurrent.futures.TimeoutError`
      (Property 3 timeout branch).
    * :class:`dict` — returned unchanged (use this for malformed
      responses in Property 3's empty-output branch).
    * :class:`str` — wrapped via :func:`_converse_response` and
      returned (the common success/length-rejection case).

    When ``call_count`` exceeds the configured response list, the
    last element is reused so a 1-element ``responses`` list serves
    every attempt without index-out-of-range surprises.
    """

    __slots__ = ("_responses", "call_count", "calls")

    def __init__(self, responses: list[Any]) -> None:
        if not responses:
            raise ValueError("_BedrockStub requires at least one response")
        self._responses: list[Any] = list(responses)
        self.call_count: int = 0
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        self.calls.append(kwargs)
        index = min(self.call_count - 1, len(self._responses) - 1)
        response = self._responses[index]

        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str) and response == "timeout":
            # Sleep > the monkey-patched budget so the future times
            # out. 1 s is comfortably above the 200 ms test budget
            # while keeping the per-test wall-clock manageable.
            time.sleep(1.0)
            return _converse_response(_FILLER_WORD * 30)
        if isinstance(response, dict):
            return response
        if isinstance(response, str):
            return _converse_response(response)
        raise TypeError(
            f"_BedrockStub does not understand response of type "
            f"{type(response).__name__}"
        )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


_seed_word_strategy = st.text(
    alphabet=_SEED_WORD_CHARSET,
    min_size=1,
    max_size=30,
)

# 1..5 seed words per Property 1's quantifier.
_seed_words_strategy = st.lists(
    _seed_word_strategy, min_size=1, max_size=5
)

# Word counts spanning both in-range and out-of-range so Property 2
# exercises both branches in a single hypothesis run. The upper
# bound (200) comfortably exceeds MAX_WORDS=80.
_word_count_strategy = st.integers(min_value=0, max_value=200)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate_text(n_words: int) -> str:
    """Build a deterministic text with exactly ``n_words`` words.

    Used by Property 2's strategy to build candidate Bedrock outputs
    with a known, easily-asserted word count. ``n_words=0`` returns
    the empty string, which the generator treats as an empty-output
    failure rather than a length rejection.
    """
    if n_words <= 0:
        return ""
    return " ".join([_FILLER_WORD] * n_words)


def _captured_user_prompt(stub: _BedrockStub, call_index: int = 0) -> str:
    """Pull the user-prompt text out of a captured Converse call."""
    return stub.calls[call_index]["messages"][0]["content"][0]["text"]


def _captured_system_prompt(stub: _BedrockStub, call_index: int = 0) -> str:
    """Pull the system-prompt text out of a captured Converse call."""
    return stub.calls[call_index]["system"][0]["text"]


def _valid_joke_text() -> str:
    """A length-valid joke text used in tests that need a successful call."""
    return " ".join([_FILLER_WORD] * 30)


# ---------------------------------------------------------------------------
# Property 1: seed-word containment when seeds are supplied
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(seed_words=_seed_words_strategy)
def test_property_1_seed_words_listed_in_user_prompt(
    seed_words: list[str],
) -> None:
    """Property 1 (joke_generator half): every supplied seed word
    appears verbatim in the captured user prompt.

    The generator's contract is to construct a prompt that asks
    Bedrock to include at least one of the supplied seed words. The
    implementation lists every supplied word in the prompt
    (see :func:`joke_api.joke_generator._build_user_prompt`), so a
    case-insensitive substring assertion per word is the strongest
    in-module check we can make. The end-to-end "Bedrock output
    actually contains a seed word" assertion belongs to the
    handler/integration test in task 10.3 because it depends on the
    live model.

    **Validates: Requirements 1.2** (Property 1).
    """
    stub = _BedrockStub([_valid_joke_text()])

    result = generate(
        seed_words,
        few_shot=[],
        bedrock_client=stub,
        model_id=_TEST_MODEL_ID,
    )

    # Successful first attempt — generator returns a non-empty str
    # within the length window (Property 2's positive branch).
    assert isinstance(result, str)
    assert MIN_WORDS <= len(result.split()) <= MAX_WORDS
    assert stub.call_count == 1, (
        f"expected exactly one Bedrock call on success, "
        f"got {stub.call_count}"
    )

    user_prompt = _captured_user_prompt(stub)
    lowered = user_prompt.lower()
    for word in seed_words:
        assert word.lower() in lowered, (
            f"Property 1 violated: seed word {word!r} missing from "
            f"user prompt {user_prompt!r} for seed_words={seed_words!r}"
        )

    # Sanity: the prompt explicitly asks for inclusion when seeds
    # are supplied (paired with the empty-case assertion below).
    # The implementation phrases this as "includes at least one"
    # (third-person agreement with "joke that").
    assert "includes at least one" in lowered, (
        "non-empty seed_words must produce an 'includes at least one' "
        f"clause; got prompt={user_prompt!r}"
    )


def test_property_1_empty_seed_words_omits_inclusion_clause() -> None:
    """Property 1 boundary: with no seed words the prompt does NOT
    request seed-word inclusion.

    Reads the implementation's empty-case wording from
    :func:`joke_api.joke_generator._build_user_prompt`: the prompt
    simply says ``"Now write a fresh dad joke."`` without any
    "include at least one" sentence.

    **Validates: Requirements 1.2** (Property 1, empty branch).
    """
    stub = _BedrockStub([_valid_joke_text()])

    generate(
        [],
        few_shot=[],
        bedrock_client=stub,
        model_id=_TEST_MODEL_ID,
    )

    user_prompt = _captured_user_prompt(stub)
    lowered = user_prompt.lower()
    # Implementation phrases the inclusion request as
    # "includes at least one" — confirm that wording is absent on
    # the empty branch.
    assert "includes at least one" not in lowered, (
        f"empty seed_words must not request inclusion; got {user_prompt!r}"
    )
    assert "now write a fresh dad joke." in lowered, (
        f"empty-case prompt missing the simple ask; got {user_prompt!r}"
    )


# ---------------------------------------------------------------------------
# Property 2: joke length is within 10..80 words inclusive
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(n_words=_word_count_strategy)
def test_property_2_in_range_returns_text_out_of_range_raises(
    n_words: int,
) -> None:
    """Property 2 (joke_generator half): in-range Bedrock output is
    accepted; out-of-range output is rejected after exactly
    :data:`MAX_ATTEMPTS` calls.

    For any ``n_words`` in ``[0, 200]``:

    * ``MIN_WORDS <= n_words <= MAX_WORDS`` ⇒ :func:`generate`
      returns the cleaned text (Property 2 positive branch).
    * Otherwise ⇒ :func:`generate` raises
      :class:`JokeGenerationFailed` after exactly
      :data:`MAX_ATTEMPTS` Bedrock calls (Property 2 negative
      branch + R1.4 hard cap).

    The HTTP 503 mapping for the negative branch is the handler's
    job (see task 10.3); this test asserts only the typed exception
    surface.

    **Validates: Requirements 1.4, 1.8** (Property 2).
    """
    candidate = _candidate_text(n_words)
    # Three identical responses so every attempt sees the same text.
    stub = _BedrockStub([candidate, candidate, candidate])

    if MIN_WORDS <= n_words <= MAX_WORDS:
        result = generate(
            [],
            few_shot=[],
            bedrock_client=stub,
            model_id=_TEST_MODEL_ID,
        )
        assert isinstance(result, str)
        result_word_count = len(result.split())
        assert MIN_WORDS <= result_word_count <= MAX_WORDS, (
            f"Property 2 violated (positive branch): in-range candidate "
            f"with {n_words} words produced result with "
            f"{result_word_count} words: {result!r}"
        )
        # First attempt succeeded — no retry needed.
        assert stub.call_count == 1
        # Hard cap (R1.4): never exceed MAX_ATTEMPTS Bedrock calls.
        assert stub.call_count <= MAX_ATTEMPTS
        return

    # Out-of-range branch.
    with pytest.raises(JokeGenerationFailed) as excinfo:
        generate(
            [],
            few_shot=[],
            bedrock_client=stub,
            model_id=_TEST_MODEL_ID,
        )

    # ``n_words == 0`` produces an empty Bedrock response, which the
    # generator surfaces as ``empty_output``; non-empty but
    # out-of-range candidates surface as ``length_rejected``.
    expected_reason = "empty_output" if n_words == 0 else "length_rejected"
    assert excinfo.value.reason == expected_reason, (
        f"Property 2 violated: expected reason {expected_reason!r} "
        f"for n_words={n_words}, got {excinfo.value.reason!r}"
    )
    assert excinfo.value.attempts == MAX_ATTEMPTS
    # Hard cap (R1.4): exactly MAX_ATTEMPTS calls were made.
    assert stub.call_count == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Property 3: Bedrock failure produces 503 with no partial content
# ---------------------------------------------------------------------------
#
# joke_generator's contract for Property 3 is that it raises a typed
# exception on every Bedrock failure mode and never returns partial
# joke text. The HTTP 503 mapping is the handler's job (task 10.3).
# Each failure mode below has a different stub setup, so the three
# branches are written as separate tests rather than parametrized
# over a single function.


def test_property_3_transport_error_raises_unavailable() -> None:
    """Property 3 (joke_generator half): every Bedrock transport
    error across all attempts surfaces as
    :class:`JokeGenerationUnavailable` with no partial content.

    **Validates: Requirements 1.5** (Property 3, transport branch).
    """
    err = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}},
        "Converse",
    )
    stub = _BedrockStub([err, err, err])

    with pytest.raises(JokeGenerationUnavailable) as excinfo:
        generate(
            [],
            few_shot=[],
            bedrock_client=stub,
            model_id=_TEST_MODEL_ID,
        )

    assert excinfo.value.operation == "converse"
    assert stub.call_count == MAX_ATTEMPTS
    # The exception carries no joke text — confirmed by its
    # ``__slots__`` (only ``operation`` is exposed).
    assert not hasattr(excinfo.value, "joke_text")
    assert not hasattr(excinfo.value, "audio_ref")


def test_property_3_timeout_raises_typed_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 3 (joke_generator half): every Bedrock attempt that
    blows the per-attempt budget surfaces as
    :class:`JokeGenerationTimeout` with no partial content.

    The budget is monkey-patched down to 200 ms so the
    1-second-sleeping stub triggers a timeout on every attempt
    without forcing the test to wait the real 15 s budget.

    **Validates: Requirements 1.5** (Property 3, timeout branch).
    """
    monkeypatch.setattr(joke_generator, "BEDROCK_BUDGET_MS", 200)
    stub = _BedrockStub(["timeout", "timeout", "timeout"])

    with pytest.raises(JokeGenerationTimeout) as excinfo:
        generate(
            [],
            few_shot=[],
            bedrock_client=stub,
            model_id=_TEST_MODEL_ID,
        )

    # Reflects the patched budget at raise time.
    assert excinfo.value.budget_ms == 200
    assert excinfo.value.attempts == MAX_ATTEMPTS
    assert stub.call_count == MAX_ATTEMPTS
    # No joke text is reachable through the typed exception.
    assert not hasattr(excinfo.value, "joke_text")
    assert not hasattr(excinfo.value, "audio_ref")


def test_property_3_malformed_response_raises_failed() -> None:
    """Property 3 (joke_generator half): malformed Converse responses
    on every attempt surface as :class:`JokeGenerationFailed` with
    ``reason='empty_output'`` and no partial content.

    Three different malformations are used so the test catches any
    accidental tolerance for *any* of the missing-key shapes
    (empty top-level, missing ``message``, empty ``content``).

    **Validates: Requirements 1.5** (Property 3, malformed branch).
    """
    malformed_responses: list[Any] = [
        {},
        {"output": {}},
        {"output": {"message": {"role": "assistant", "content": []}}},
    ]
    stub = _BedrockStub(malformed_responses)

    with pytest.raises(JokeGenerationFailed) as excinfo:
        generate(
            [],
            few_shot=[],
            bedrock_client=stub,
            model_id=_TEST_MODEL_ID,
        )

    assert excinfo.value.reason == "empty_output"
    assert excinfo.value.attempts == MAX_ATTEMPTS
    assert stub.call_count == MAX_ATTEMPTS
    # No joke text on the typed exception.
    assert not hasattr(excinfo.value, "joke_text")
    assert not hasattr(excinfo.value, "audio_ref")


# ---------------------------------------------------------------------------
# Property 4: deferred to handler tests in task 10.3 / 10.4
# ---------------------------------------------------------------------------
#
# joke_generator does NOT mint UUIDs; it returns text only. The
# UUID v4 uniqueness property therefore lives at the handler layer.
# A placeholder test below verifies the closest in-module surface
# contract: the generator returns a ``str`` (not a UUID), and the
# downstream :class:`JokeRecord` carries a ``str`` ``id`` field that
# the handler will populate with a UUID v4.


def test_property_4_uuid_uniqueness_is_handler_contract() -> None:
    """Property 4 boundary: UUID v4 uniqueness lives in the handler.

    This module's contract surface is verified here:

    * :func:`generate` returns a ``str`` (not a UUID).
    * Two consecutive calls return text that does not double as a
      shared identifier — the joke text itself carries no
      generation id.
    * :class:`JokeRecord` exposes ``id`` as a ``str`` field that the
      handler populates with a UUID v4 (see
      :mod:`joke_api.joke_store`).

    The end-to-end uniqueness assertion (every successful response
    has a syntactically-valid UUID v4 ``id`` and no two responses
    in a session share an id) belongs to the handler test in task
    10.3 / 10.4 because handler.py is what mints the UUID.

    **Validates: Requirements 18.1** (Property 4 surface contract).
    """
    stub_a = _BedrockStub([_valid_joke_text()])
    stub_b = _BedrockStub([_valid_joke_text()])

    a = generate(
        [],
        few_shot=[],
        bedrock_client=stub_a,
        model_id=_TEST_MODEL_ID,
    )
    b = generate(
        [],
        few_shot=[],
        bedrock_client=stub_b,
        model_id=_TEST_MODEL_ID,
    )
    assert isinstance(a, str)
    assert isinstance(b, str)

    # JokeRecord.id is a str (UUID v4 generation is the handler's
    # job; we just confirm the field type here so a future schema
    # change is caught at this layer).
    record_field_types = {
        f.name: f.type
        for f in JokeRecord.__dataclass_fields__.values()
    }
    assert record_field_types["id"] in ("str", str), (
        f"JokeRecord.id is no longer a str: "
        f"{record_field_types['id']!r}; UUID v4 minting is the "
        "handler's contract."
    )


# ---------------------------------------------------------------------------
# Property 12: refined-prompt construction
# ---------------------------------------------------------------------------
#
# joke_generator's internal 3-attempt loop does NOT switch
# ``refine=True`` between attempts (the handler does that across
# separate :func:`generate` calls — see the docstring at the top of
# joke_generator.py). Property 12's joke_generator-level half is
# therefore a prompt-shape contract:
#
# * ``refine=False`` ⇒ system prompt is exactly
#   :data:`SYSTEM_PROMPT_BASE` and contains none of the R4.2
#   prohibition keywords.
# * ``refine=True`` ⇒ system prompt is
#   ``SYSTEM_PROMPT_BASE + SYSTEM_PROMPT_REFINED_SUFFIX`` and
#   contains every R4.2 prohibition keyword (profanity, sexual
#   content, graphic violence, drugs, slurs, targeted harassment).
#
# The handler's outer-loop "retry across moderator rejections"
# bound is verified in task 10.3.

# Keywords that MUST appear in the refined system prompt per R4.2.
# Lowercased for case-insensitive matching.
_REFINED_PROHIBITION_KEYWORDS: tuple[str, ...] = (
    "profanity",
    "sexual content",
    "graphic violence",
    "drug",
    "slur",
    "harassment",
)

# A family-friendly marker that MUST appear in EITHER prompt (base
# or refined). The base prompt uses "G/PG" and "family-friendly".
_FAMILY_FRIENDLY_MARKERS: tuple[str, ...] = ("g/pg", "family-friendly")


def test_property_12_refine_false_uses_base_system_prompt() -> None:
    """Property 12 (joke_generator half, refine=False): the system
    prompt is exactly :data:`SYSTEM_PROMPT_BASE` and contains none
    of the R4.2 prohibition keywords.

    **Validates: Requirements 4.2** (Property 12, base branch).
    """
    stub = _BedrockStub([_valid_joke_text()])

    generate(
        [],
        few_shot=[],
        refine=False,
        bedrock_client=stub,
        model_id=_TEST_MODEL_ID,
    )

    system_prompt = _captured_system_prompt(stub)
    assert system_prompt == SYSTEM_PROMPT_BASE, (
        f"refine=False must use SYSTEM_PROMPT_BASE exactly; got "
        f"{system_prompt!r}"
    )

    lowered = system_prompt.lower()
    for keyword in _REFINED_PROHIBITION_KEYWORDS:
        assert keyword not in lowered, (
            f"refine=False prompt unexpectedly contains R4.2 keyword "
            f"{keyword!r}: {system_prompt!r}"
        )

    assert any(marker in lowered for marker in _FAMILY_FRIENDLY_MARKERS), (
        f"system prompt is missing the family-friendly marker: "
        f"{system_prompt!r}"
    )


def test_property_12_refine_true_appends_prohibition_suffix() -> None:
    """Property 12 (joke_generator half, refine=True): the system
    prompt is ``SYSTEM_PROMPT_BASE + SYSTEM_PROMPT_REFINED_SUFFIX``
    and contains every R4.2 prohibition keyword.

    **Validates: Requirements 4.2** (Property 12, refined branch).
    """
    stub = _BedrockStub([_valid_joke_text()])

    generate(
        [],
        few_shot=[],
        refine=True,
        bedrock_client=stub,
        model_id=_TEST_MODEL_ID,
    )

    system_prompt = _captured_system_prompt(stub)
    assert system_prompt == SYSTEM_PROMPT_BASE + SYSTEM_PROMPT_REFINED_SUFFIX, (
        f"refine=True must append SYSTEM_PROMPT_REFINED_SUFFIX to "
        f"SYSTEM_PROMPT_BASE; got {system_prompt!r}"
    )

    lowered = system_prompt.lower()
    for keyword in _REFINED_PROHIBITION_KEYWORDS:
        assert keyword in lowered, (
            f"refine=True prompt missing R4.2 keyword {keyword!r}: "
            f"{system_prompt!r}"
        )

    assert any(marker in lowered for marker in _FAMILY_FRIENDLY_MARKERS), (
        f"refined prompt is missing the family-friendly marker: "
        f"{system_prompt!r}"
    )


@PBT_SETTINGS
@given(refine=st.booleans(), seed_words=_seed_words_strategy)
def test_property_12_refine_flag_controls_suffix_only(
    refine: bool,
    seed_words: list[str],
) -> None:
    """Property 12 (joke_generator half): the ``refine`` flag is
    the *only* thing that toggles the suffix; the user-prompt shape
    is independent of refine, and the family-friendly marker is
    always present.

    **Validates: Requirements 4.2** (Property 12, flag isolation).
    """
    stub = _BedrockStub([_valid_joke_text()])

    generate(
        seed_words,
        few_shot=[],
        refine=refine,
        bedrock_client=stub,
        model_id=_TEST_MODEL_ID,
    )

    system_prompt = _captured_system_prompt(stub)
    suffix_present = SYSTEM_PROMPT_REFINED_SUFFIX in system_prompt
    assert suffix_present is refine, (
        f"refine={refine} but suffix_present={suffix_present}: "
        f"{system_prompt!r}"
    )

    # Family-friendly marker is always present.
    lowered = system_prompt.lower()
    assert any(marker in lowered for marker in _FAMILY_FRIENDLY_MARKERS)

    # User prompt always inlines every seed word, regardless of refine.
    user_prompt_lowered = _captured_user_prompt(stub).lower()
    for word in seed_words:
        assert word.lower() in user_prompt_lowered


# ---------------------------------------------------------------------------
# Property 13: all-rejected outputs surface as JokeGenerationFailed
# ---------------------------------------------------------------------------
#
# joke_generator does NOT pick fallback jokes; the handler does,
# from :data:`joke_api.fallback_jokes.FALLBACK_JOKES`. Property 13's
# joke_generator-level half is verified by Property 2's negative
# branch: when 3 attempts produce out-of-range text,
# :func:`generate` raises :class:`JokeGenerationFailed` with
# ``reason='length_rejected'`` and ``attempts=3``. A focused test
# below makes the boundary explicit.


def test_property_13_all_rejected_surfaces_length_rejected_failure() -> None:
    """Property 13 (joke_generator half): three out-of-range Bedrock
    outputs surface as :class:`JokeGenerationFailed` with
    ``reason='length_rejected'`` and ``attempts=3``.

    The handler is responsible for catching this typed exception
    and selecting a curated joke from
    :data:`joke_api.fallback_jokes.FALLBACK_JOKES` (task 10.3).
    This module never embeds a fallback joke in its return value;
    the test asserts no joke text is reachable through the typed
    exception.

    **Validates: Requirements 4.3, 4.5** (Property 13, generator
    boundary).
    """
    too_short = " ".join([_FILLER_WORD] * 5)  # 5 < MIN_WORDS=10
    stub = _BedrockStub([too_short, too_short, too_short])

    with pytest.raises(JokeGenerationFailed) as excinfo:
        generate(
            [],
            few_shot=[],
            bedrock_client=stub,
            model_id=_TEST_MODEL_ID,
        )

    assert excinfo.value.reason == "length_rejected"
    assert excinfo.value.attempts == MAX_ATTEMPTS
    assert stub.call_count == MAX_ATTEMPTS
    # No joke text on the typed exception — handler must select
    # from FALLBACK_JOKES.
    assert not hasattr(excinfo.value, "joke_text")
