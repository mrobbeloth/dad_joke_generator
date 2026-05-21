"""Input moderation: denylist + Comprehend toxicity classifier.

This module implements the Input_Moderator described in
``design.md`` § Components and Interfaces. It composes two layers of
content classification per R3.3 / Correctness Property 9:

1. A fast, in-process word-boundary aware denylist (delegated to
   :mod:`joke_api.denylist_matcher`).
2. AWS Comprehend's ``DetectToxicContent`` (English) classifier.

The aggregated decision is the logical OR of the two layers: a piece
of text is rejected as not Family_Friendly when *either* layer flags
*any* segment.

Validated requirements (``requirements.md`` § Requirement 3)
-----------------------------------------------------------
* **R3.1** -- ``classify`` is the classifier the handler invokes
  before any Bedrock call (the handler enforces ordering; this
  module is the classifier itself).
* **R3.2** -- a not Family_Friendly classification surfaces as
  ``ModerationResult.family_friendly = False``; the handler maps
  that to HTTP 400.
* **R3.3** -- both denylist and Comprehend toxicity layers are
  evaluated and OR'd. Comprehend toxicity labels covered:
  ``PROFANITY``, ``HATE_SPEECH``, ``SEXUAL``, ``VIOLENCE``,
  ``INSULT``, ``GRAPHIC``, ``HARASSMENT_OR_ABUSE``.
* **R3.6** -- transport / boto errors raise
  :class:`ModerationUnavailable` (handler -> HTTP 503).
* **R3.7** -- exceeding the 3-second classifier budget raises
  :class:`ModerationTimeout` (handler -> HTTP 504).

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 9** -- family-friendliness equals the logical OR of
  denylist and classifier flags.
* **Property 10** -- moderator unavailability fails closed (this
  module raises typed exceptions; the handler maps them to non-2xx
  responses without invoking Bedrock or incrementing the rate
  limiter).
* **Property 11** -- input and output moderators share the same
  classifier core. Task 4.3's :mod:`joke_api.output_moderator`
  reuses :func:`_classify_with_budget` at a 500 ms budget so the
  ``family_friendly`` decisions are identical for identical text.

Public surface
--------------
* :data:`INPUT_BUDGET_MS` / :data:`OUTPUT_BUDGET_MS` -- per-stage
  time budgets (3000 ms / 500 ms respectively).
* :data:`TOXICITY_THRESHOLD` -- minimum confidence required to count
  a Comprehend label as a flag.
* :data:`TOXICITY_LABELS` -- frozenset of Comprehend label names
  treated as not Family_Friendly.
* :class:`ModerationResult` -- frozen dataclass returned by
  :func:`classify`.
* :class:`ModerationTimeout`, :class:`ModerationUnavailable` --
  typed exceptions raised on R3.6 / R3.7 paths.
* :func:`classify` -- public entry point (thin wrapper over
  :func:`_classify_with_budget` at the input budget).

Timeout enforcement
-------------------
``boto3``'s synchronous client calls do not accept a per-call
deadline parameter, so the budget is enforced by submitting the
Comprehend call to a :class:`concurrent.futures.ThreadPoolExecutor`
and waiting for the result with ``future.result(timeout=budget_s)``.
A :exc:`concurrent.futures.TimeoutError` on that wait surfaces as
:class:`ModerationTimeout`. The executor is shut down with
``wait=False, cancel_futures=True`` so a slow in-flight call cannot
keep the request handler blocked past the budget. As
defense-in-depth, the boto3 client used when the caller does not
inject one is configured with a matching ``read_timeout`` and a
single attempt so the underlying call cannot hang indefinitely if
the executor is starved.

Test injection
--------------
The ``comprehend_client`` keyword argument on :func:`classify` (and
its internal :func:`_classify_with_budget` helper) is the supported
test injection point. Tests pass a stub object exposing
``detect_toxic_content(TextSegments=..., LanguageCode=...)`` to
drive happy-path and failure-mode coverage without hitting AWS.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import time
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from joke_api import denylist_matcher

__all__ = [
    "INPUT_BUDGET_MS",
    "OUTPUT_BUDGET_MS",
    "TOXICITY_THRESHOLD",
    "TOXICITY_LABELS",
    "ModerationResult",
    "ModerationTimeout",
    "ModerationUnavailable",
    "classify",
]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Per-call budget for the Input_Moderator (R3.7).
INPUT_BUDGET_MS: int = 3000

#: Per-call budget for the Output_Moderator (R4.1, R4.5). Exposed here
#: so task 4.3's :mod:`joke_api.output_moderator` can import the
#: constant and reuse :func:`_classify_with_budget` without redefining
#: the value.
OUTPUT_BUDGET_MS: int = 500

#: Minimum Comprehend confidence required to count a label as a flag.
#: ``design.md`` does not pin a value, so 0.5 is the documented default
#: tuning knob.
TOXICITY_THRESHOLD: float = 0.5

#: Comprehend ``DetectToxicContent`` labels that count as
#: not Family_Friendly per R3.3 (profanity, sexual content, slurs,
#: drug references, graphic violence, targeted harassment).
TOXICITY_LABELS: frozenset[str] = frozenset(
    {
        "PROFANITY",
        "HATE_SPEECH",
        "SEXUAL",
        "VIOLENCE",
        "INSULT",
        "GRAPHIC",
        "HARASSMENT_OR_ABUSE",
    }
)

# Default boto3 client config: a single attempt with ``read_timeout``
# matching the input budget so the underlying socket cannot outlive
# the executor wait. The executor's ``timeout`` is the authoritative
# deadline; this is just a backstop.
_DEFAULT_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=INPUT_BUDGET_MS / 1000.0,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Lazily-created module-level Comprehend client. Tests inject their
# own client via the ``comprehend_client`` argument and never trigger
# this path.
_DEFAULT_CLIENT: Optional[Any] = None


# ---------------------------------------------------------------------------
# Public types / exceptions
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ModerationResult:
    """Outcome of a single :func:`classify` call.

    Attributes:
        family_friendly: ``True`` iff the text is Family_Friendly per
            R3.3 (i.e. neither denylist nor Comprehend flagged any
            segment).
        reason: ``None`` when ``family_friendly`` is ``True``;
            otherwise a short identifier of the offending source
            (``"denylist:<token>"`` or ``"classifier:<label>"``).
            The observability layer records this; sanitized error
            responses never surface it (R7.5, Property 20).
        latency_ms: Wall-clock duration of the classification call,
            measured with :func:`time.monotonic` and rounded to the
            nearest millisecond. Always non-negative.
    """

    family_friendly: bool
    reason: Optional[str]
    latency_ms: int


class ModerationTimeout(Exception):
    """Raised when the classifier exceeds its time budget.

    The handler maps this to ``response_builder.MODERATION_TIMEOUT``
    (HTTP 504) per R3.7.

    Attributes:
        budget_ms: The budget in milliseconds that was exceeded.
    """

    __slots__ = ("budget_ms",)

    def __init__(self, budget_ms: int) -> None:
        self.budget_ms = budget_ms
        super().__init__(f"moderation timeout: budget_ms={budget_ms}")


class ModerationUnavailable(Exception):
    """Raised on transport or boto errors from Comprehend.

    The handler maps this to
    ``response_builder.MODERATION_UNAVAILABLE`` (HTTP 503) per R3.6.

    Attributes:
        operation: ``"detect_toxic_content"`` -- the failing
            Comprehend call name.
    """

    __slots__ = ("operation",)

    def __init__(self, operation: str, message: str) -> None:
        self.operation = operation
        super().__init__(
            f"moderation unavailable during {operation}: {message}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(
    text: str,
    *,
    comprehend_client: Optional[Any] = None,
) -> ModerationResult:
    """Classify input text with the layered Input_Moderator.

    Returns a :class:`ModerationResult` whose ``family_friendly``
    boolean equals
    ``not (denylist_match(text) or classifier_flag(text))``
    (Correctness Property 9).

    Args:
        text: The text to classify. Empty / whitespace-only strings
            short-circuit to a Family_Friendly result without calling
            Comprehend (R3.4 permits 0-100 character inputs; 0 chars
            carries no content to flag).
        comprehend_client: Optional pre-built boto3 ``comprehend``
            client. Used by tests to inject a stub. When omitted, a
            lazily-cached module-level client is created.

    Returns:
        :class:`ModerationResult` with the aggregated decision and
        the wall-clock latency of the call.

    Raises:
        ModerationTimeout: When the classifier exceeds the 3-second
            input budget (R3.7).
        ModerationUnavailable: When Comprehend is unreachable or
            returns an error response (R3.6).
    """
    return _classify_with_budget(
        text,
        INPUT_BUDGET_MS,
        comprehend_client=comprehend_client,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _classify_with_budget(
    text: str,
    budget_ms: int,
    *,
    comprehend_client: Optional[Any] = None,
) -> ModerationResult:
    """Run the layered classifier with an explicit time budget.

    This is the shared implementation used by both
    :func:`classify` (3000 ms) and -- starting with task 4.3 --
    :mod:`joke_api.output_moderator` (500 ms).

    The decision is the logical OR of the denylist match and the
    Comprehend toxicity flag (Property 9). The denylist runs first
    because it is in-process and free; if it flags the text, the
    Comprehend call is skipped (the OR short-circuits and the
    aggregate result is unchanged).

    Args:
        text: The text to classify.
        budget_ms: Maximum total time in milliseconds before the
            classifier raises :class:`ModerationTimeout`. Must be a
            positive integer.
        comprehend_client: Optional injected boto3 ``comprehend``
            client (test seam).

    Returns:
        :class:`ModerationResult` carrying the aggregated decision
        and the wall-clock latency.

    Raises:
        ValueError: When ``budget_ms`` is not a positive integer.
        ModerationTimeout: When the wall-clock duration of the
            Comprehend call exceeds ``budget_ms``.
        ModerationUnavailable: On boto / transport error.
    """
    if (
        isinstance(budget_ms, bool)
        or not isinstance(budget_ms, int)
        or budget_ms <= 0
    ):
        raise ValueError("budget_ms must be a positive integer")
    if not isinstance(text, str):
        raise ValueError("text must be a string")

    started = time.monotonic()

    # Layer 1: in-process denylist (free). A hit short-circuits the
    # OR so no Comprehend call is required (Property 9 is preserved
    # because ``True or x == True`` regardless of ``x``).
    hit, token = denylist_matcher.matches(text)
    if hit:
        return ModerationResult(
            family_friendly=False,
            reason=f"denylist:{token}",
            latency_ms=_elapsed_ms(started),
        )

    # Empty / whitespace-only text carries no content for Comprehend
    # to score. R3.4 permits 0-character inputs; there is nothing to
    # flag, so we return Family_Friendly without paying for an API
    # call (Comprehend rejects empty TextSegments anyway).
    if not text or not text.strip():
        return ModerationResult(
            family_friendly=True,
            reason=None,
            latency_ms=_elapsed_ms(started),
        )

    # Layer 2: Comprehend DetectToxicContent. Wrap in a
    # ThreadPoolExecutor so the wall-clock budget is enforced
    # independently of the underlying socket. ``shutdown`` is called
    # with ``wait=False, cancel_futures=True`` in the timeout path
    # so a slow in-flight call cannot keep the handler blocked past
    # the budget.
    client = (
        comprehend_client
        if comprehend_client is not None
        else _get_default_client()
    )
    budget_s = budget_ms / 1000.0

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            client.detect_toxic_content,
            TextSegments=[{"Text": text}],
            LanguageCode="en",
        )
        try:
            response = future.result(timeout=budget_s)
        except concurrent.futures.TimeoutError as exc:
            raise ModerationTimeout(budget_ms) from exc
        except (BotoCoreError, ClientError) as exc:
            raise ModerationUnavailable(
                "detect_toxic_content", str(exc)
            ) from exc
    finally:
        # ``cancel_futures=True`` prevents queued (but not yet
        # running) tasks from starting; running tasks cannot be
        # interrupted from Python, but ``wait=False`` returns
        # immediately so the request handler can fail fast.
        pool.shutdown(wait=False, cancel_futures=True)

    flagged_label = _scan_toxicity(response)
    latency_ms = _elapsed_ms(started)

    if flagged_label is not None:
        return ModerationResult(
            family_friendly=False,
            reason=f"classifier:{flagged_label}",
            latency_ms=latency_ms,
        )

    return ModerationResult(
        family_friendly=True,
        reason=None,
        latency_ms=latency_ms,
    )


def _scan_toxicity(response: dict) -> Optional[str]:
    """Return the first flagged toxicity label, or ``None``.

    Comprehend's ``DetectToxicContent`` response contains a
    ``ResultList`` with one entry per submitted segment; each entry
    has a ``Labels`` list of ``{"Name": <label>, "Score": <float>}``
    items. A label is considered flagged when its name is in
    :data:`TOXICITY_LABELS` and its score meets or exceeds
    :data:`TOXICITY_THRESHOLD`.

    Unknown label names and malformed score values are skipped (not
    treated as flags) so the classifier never rejects content for
    reasons not enumerated by R3.3.
    """
    result_list = response.get("ResultList") or []
    for entry in result_list:
        for label in entry.get("Labels") or []:
            name = label.get("Name")
            score = label.get("Score", 0.0)
            try:
                score_f = float(score)
            except (TypeError, ValueError):
                continue
            if name in TOXICITY_LABELS and score_f >= TOXICITY_THRESHOLD:
                return name
    return None


def _elapsed_ms(started_monotonic: float) -> int:
    """Return non-negative milliseconds since ``started_monotonic``."""
    delta = time.monotonic() - started_monotonic
    if delta < 0:
        return 0
    return int(round(delta * 1000.0))


def _get_default_client() -> Any:
    """Return the lazily-created module-level Comprehend client."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = boto3.client(
            "comprehend",
            config=_DEFAULT_CLIENT_CONFIG,
        )
    return _DEFAULT_CLIENT
