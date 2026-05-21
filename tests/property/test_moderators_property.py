"""Property tests for the layered Input/Output moderator.

This file implements two correctness properties from ``design.md`` plus a
targeted sanity assertion:

* **Property 9: Family-friendliness is the logical OR of denylist and
  classifier flags.** *For any* input text,
  ``Input_Moderator.classify(text).family_friendly`` SHALL equal
  ``not (denylist_match(text) or classifier_flag(text))``.

* **Property 11: Output moderator and Input moderator are equivalent.**
  *For any* text, ``Output_Moderator.classify(text)`` SHALL produce the
  same ``family_friendly`` decision as ``Input_Moderator.classify(text)``.

* **Sanity (denylist short-circuits the classifier).** When the text
  contains a denylist token, the implementation MUST NOT call the
  Comprehend ``detect_toxic_content`` API, because the OR has already
  resolved to ``True``.

**Validates: Requirements 3.3, 4.4**

Approach
--------
The Comprehend backend is replaced with a per-example
:class:`_ComprehendStub` instance: a small hand-rolled class (not a
``MagicMock``) that exposes the single ``detect_toxic_content`` method
the moderator uses. A hand-rolled class is preferred to ``MagicMock``
because the moderator may navigate the response with attribute access on
nested objects and we want any deviation from the documented response
shape to be visible rather than silently auto-stubbed.

The stub takes a configured ``flag_decision: bool`` per call and returns
a Comprehend ``DetectToxicContent``-shaped response with a single
``PROFANITY`` label whose score is 0.99 when ``flag_decision`` is
``True`` and 0.0 otherwise (mirroring the example shape in
``input_moderator._scan_toxicity``). It also tracks how many times its
classifier method was called so the sanity test can verify the
short-circuit.

Hypothesis generates ``(text, flag_decision)`` pairs where ``text`` is
drawn from a mix of:

* Family-friendly content (short alphabetic strings).
* Strings containing a known denylist token sampled from the bundled
  ``denylist.txt`` (e.g. ``"damn"``, ``"hell"``).
* Empty / whitespace-only strings (so the empty-input short-circuit is
  exercised).

A NEW stub instance is built per Hypothesis example so call counters do
not leak between iterations.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from joke_api import denylist_matcher, input_moderator, output_moderator


# ---------------------------------------------------------------------------
# Hand-rolled Comprehend stub
# ---------------------------------------------------------------------------


class _ComprehendStub:
    """Minimal Comprehend client stub for moderator property tests.

    Exposes only the surface the moderator uses
    (:py:meth:`detect_toxic_content`) and tracks how many times it was
    called so tests can assert the denylist short-circuit. The
    ``flag_decision`` argument controls whether the synthesized response
    flags the input as ``PROFANITY`` at a score above
    :data:`joke_api.input_moderator.TOXICITY_THRESHOLD` (0.99) or leaves
    it at 0.0.

    A new stub instance is built per call so internal state (the call
    counter) does not leak between Hypothesis examples.
    """

    __slots__ = ("flag_decision", "call_count", "last_kwargs")

    def __init__(self, flag_decision: bool) -> None:
        self.flag_decision = flag_decision
        self.call_count = 0
        self.last_kwargs: Optional[dict[str, Any]] = None

    def detect_toxic_content(
        self,
        *,
        TextSegments: list[dict[str, str]],
        LanguageCode: str,
    ) -> dict[str, Any]:
        """Return a Comprehend ``DetectToxicContent``-shaped response."""
        self.call_count += 1
        self.last_kwargs = {
            "TextSegments": TextSegments,
            "LanguageCode": LanguageCode,
        }
        score = 0.99 if self.flag_decision else 0.0
        return {
            "ResultList": [
                {
                    "Labels": [
                        {"Name": "PROFANITY", "Score": score},
                    ]
                }
            ]
        }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A small slice of denylisted tokens, sampled from the bundled
# ``denylist.txt``. Sampling here (rather than reading the whole file
# at module import time) keeps Hypothesis's shrinking deterministic and
# the failure messages readable.
_DENYLIST_TOKENS: tuple[str, ...] = (
    "damn",
    "hell",
    "crap",
    "shit",
    "loser",
    "idiot",
    "moron",
)

# Family-friendly text: short alphabetic strings. ``min_size=1`` so the
# Comprehend layer is exercised (the implementation short-circuits to
# family_friendly on empty input -- that case is covered by the
# whitespace strategy below).
_clean_text_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll"),
        whitelist_characters=" ",
    ),
    min_size=1,
    max_size=40,
)

# Text that embeds a denylist token, surrounded by arbitrary clean
# context. ``" {token} "`` ensures the token sits on a word boundary so
# the matcher will hit it.
_denylist_text_strategy = st.builds(
    lambda prefix, token, suffix: f"{prefix} {token} {suffix}",
    st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll")),
        max_size=20,
    ),
    st.sampled_from(_DENYLIST_TOKENS),
    st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll")),
        max_size=20,
    ),
)

# Empty / whitespace-only text. The implementation short-circuits to
# family_friendly without calling Comprehend on these inputs.
_whitespace_text_strategy = st.sampled_from(["", " ", "\t", "   ", "\n", " \t\n "])

# Mixed text strategy combining all three buckets.
_text_strategy = st.one_of(
    _clean_text_strategy,
    _denylist_text_strategy,
    _whitespace_text_strategy,
)


PBT_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_family_friendly(text: str, flag_decision: bool) -> bool:
    """Mirror the implementation's family-friendly predicate.

    Replicates the control flow of
    :func:`joke_api.input_moderator._classify_with_budget`:

    1. Denylist runs first; a hit short-circuits the OR to ``True``
       (so the result is ``not_family_friendly``).
    2. Empty / whitespace-only text short-circuits to family_friendly
       *without* consulting the classifier.
    3. Otherwise the classifier flag (driven by ``flag_decision``)
       contributes to the OR.
    """
    denylist_hit, _ = denylist_matcher.matches(text)
    if denylist_hit:
        return False
    if not text.strip():
        # Implementation short-circuits to family_friendly without
        # calling the classifier on empty/whitespace input.
        return True
    return not flag_decision


# ---------------------------------------------------------------------------
# Property 9: family-friendliness is the logical OR of denylist and
# classifier flags.
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(text=_text_strategy, flag_decision=st.booleans())
def test_property_9_family_friendly_is_or_of_denylist_and_classifier(
    text: str,
    flag_decision: bool,
) -> None:
    """Property 9: ``family_friendly == not (denylist_hit OR classifier_flag)``.

    **Validates: Requirements 3.3** (Property 9).
    """
    expected = _expected_family_friendly(text, flag_decision)

    stub = _ComprehendStub(flag_decision)
    result = input_moderator.classify(text, comprehend_client=stub)

    denylist_hit, denylist_token = denylist_matcher.matches(text)
    assert result.family_friendly == expected, (
        f"Property 9 violated for text={text!r}, flag_decision={flag_decision!r}: "
        f"denylist_hit={denylist_hit} (token={denylist_token!r}), "
        f"expected family_friendly={expected}, got result={result!r}"
    )


# ---------------------------------------------------------------------------
# Property 11: input and output moderator decisions are equivalent.
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(text=_text_strategy, flag_decision=st.booleans())
def test_property_11_input_and_output_moderators_agree(
    text: str,
    flag_decision: bool,
) -> None:
    """Property 11: input and output moderators produce equal decisions.

    **Validates: Requirements 4.4** (Property 11).

    Both modules delegate to the same ``_classify_with_budget`` helper.
    With a fast-returning stub neither call can time out, so the
    ``family_friendly`` and ``reason`` values must match exactly. A
    fresh stub per moderator call prevents call-counter state from
    leaking between calls.
    """
    input_stub = _ComprehendStub(flag_decision)
    output_stub = _ComprehendStub(flag_decision)

    input_result = input_moderator.classify(text, comprehend_client=input_stub)
    output_result = output_moderator.classify(text, comprehend_client=output_stub)

    assert input_result.family_friendly == output_result.family_friendly, (
        f"Property 11 violated for text={text!r}, flag_decision={flag_decision!r}: "
        f"input.family_friendly={input_result.family_friendly}, "
        f"output.family_friendly={output_result.family_friendly}"
    )
    # With a fast-returning stub neither call hits the budget, so the
    # rejection ``reason`` must be byte-identical too. ``latency_ms``
    # may differ slightly because it is wall-clock-dependent.
    assert input_result.reason == output_result.reason, (
        f"Property 11 (reason equivalence) violated for text={text!r}, "
        f"flag_decision={flag_decision!r}: input.reason="
        f"{input_result.reason!r}, output.reason={output_result.reason!r}"
    )


# ---------------------------------------------------------------------------
# Sanity test: denylist hit short-circuits the classifier call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("denylist_token", _DENYLIST_TOKENS)
@pytest.mark.parametrize("flag_decision", [False, True])
def test_denylist_short_circuits_the_classifier(
    denylist_token: str,
    flag_decision: bool,
) -> None:
    """Denylist hits MUST NOT call ``detect_toxic_content``.

    **Validates: Requirements 3.3** (Property 9 short-circuit).

    The implementation is required to short-circuit the OR when the
    denylist hits, since ``True or x == True``. We assert this by
    counting calls on the stub: the call count must remain 0 regardless
    of the configured ``flag_decision``.
    """
    text = f"hello {denylist_token} world"
    stub = _ComprehendStub(flag_decision)

    result = input_moderator.classify(text, comprehend_client=stub)

    assert result.family_friendly is False, (
        f"denylist token {denylist_token!r} did not flag text={text!r}: {result!r}"
    )
    assert result.reason is not None and result.reason.startswith("denylist:"), (
        f"expected denylist reason, got {result.reason!r}"
    )
    assert stub.call_count == 0, (
        f"Comprehend was called {stub.call_count} time(s) despite denylist hit "
        f"on text={text!r}; denylist must short-circuit the classifier."
    )
