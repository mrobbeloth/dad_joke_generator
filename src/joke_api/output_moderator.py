"""Output moderation reusing the input classifier under a tighter budget.

This module implements the Output_Moderator described in
``design.md`` § Components and Interfaces ("Output_Moderator"). It is
intentionally a thin wrapper over :mod:`joke_api.input_moderator`:
the classification core is shared so that, by construction, an
identical ``text`` produces an identical ``family_friendly``
decision in either module. Only the per-call time budget differs --
500 ms here vs. 3000 ms for the input stage.

Validated requirements (``requirements.md`` § Requirement 4)
-----------------------------------------------------------
* **R4.1** -- :func:`classify` classifies a generated joke as
  Family_Friendly or not_Family_Friendly within a 500 ms hard
  budget. The budget is enforced by reusing
  :func:`joke_api.input_moderator._classify_with_budget` with
  :data:`OUTPUT_BUDGET_MS`; budget exhaustion surfaces as
  :class:`ModerationTimeout` and the handler short-circuits to the
  fallback list (R4.5).
* **R4.4** -- the classification rules are identical to the
  Input_Moderator's: the same denylist, the same Comprehend
  ``DetectToxicContent`` call, the same
  :data:`~joke_api.input_moderator.TOXICITY_LABELS` set, and the
  same :data:`~joke_api.input_moderator.TOXICITY_THRESHOLD`. There
  is no separate output policy; reuse is the policy.
* **R4.5** -- on classifier unavailability or timeout the module
  raises the same typed exceptions
  (:class:`ModerationUnavailable`, :class:`ModerationTimeout`); the
  handler is responsible for translating those into a fallback-joke
  response per R4.5.

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 11 (Output moderator and Input moderator are
  equivalent)** -- *for any* text, ``output_moderator.classify(text)``
  produces the same ``family_friendly`` decision as
  ``input_moderator.classify(text)``. This holds *by construction*
  because both functions delegate to the same
  :func:`joke_api.input_moderator._classify_with_budget` helper with
  the same denylist + classifier layers; the only difference is the
  numeric ``budget_ms`` argument, which influences whether the call
  *completes* (raises :class:`ModerationTimeout` vs. returns a
  result) but not the value of ``family_friendly`` when it does
  return.

Public surface
--------------
* :data:`OUTPUT_BUDGET_MS` -- 500 ms hard budget (re-exported from
  :mod:`joke_api.input_moderator` so callers have a single import
  source for the output-stage constant).
* :class:`ModerationResult`, :class:`ModerationTimeout`,
  :class:`ModerationUnavailable` -- re-exported from
  :mod:`joke_api.input_moderator`. Tests and the handler import
  these names from ``output_moderator`` so the module is a complete
  output-stage facade.
* :func:`classify` -- public entry point; thin wrapper over
  :func:`joke_api.input_moderator._classify_with_budget` at
  :data:`OUTPUT_BUDGET_MS`.

Test injection
--------------
The ``comprehend_client`` keyword argument on :func:`classify` is
the supported test injection point and matches the input moderator's
signature exactly. Property 11 tests can therefore pass the *same*
stub to both modules and rely on the wrapper to forward it
unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

from joke_api.input_moderator import (
    OUTPUT_BUDGET_MS,
    ModerationResult,
    ModerationTimeout,
    ModerationUnavailable,
    _classify_with_budget,
)

__all__ = [
    "OUTPUT_BUDGET_MS",
    "ModerationResult",
    "ModerationTimeout",
    "ModerationUnavailable",
    "classify",
]


def classify(
    text: str,
    *,
    comprehend_client: Optional[Any] = None,
) -> ModerationResult:
    """Classify generated joke text under the 500 ms output budget.

    Delegates to
    :func:`joke_api.input_moderator._classify_with_budget` with
    :data:`OUTPUT_BUDGET_MS` so the denylist + Comprehend logic is
    byte-identical to the input stage. This is what makes
    Correctness Property 11 hold by construction: any caller that
    invokes ``output_moderator.classify(text)`` and
    ``input_moderator.classify(text)`` against the same Comprehend
    backend gets the same ``family_friendly`` value when both calls
    return.

    Args:
        text: The generated joke text to classify.
        comprehend_client: Optional pre-built boto3 ``comprehend``
            client. Used by tests to inject a stub; forwarded to the
            shared classifier helper unchanged.

    Returns:
        :class:`ModerationResult` carrying the aggregated decision
        (``family_friendly``), the rejection ``reason`` (or ``None``
        when family-friendly), and the wall-clock latency of the
        classification call.

    Raises:
        ModerationTimeout: When the classifier exceeds the 500 ms
            output budget (R4.1, R4.5). The handler maps this to a
            fallback-joke response.
        ModerationUnavailable: When Comprehend is unreachable or
            returns an error response (R4.5). The handler maps this
            to a fallback-joke response.
    """
    return _classify_with_budget(
        text,
        OUTPUT_BUDGET_MS,
        comprehend_client=comprehend_client,
    )
