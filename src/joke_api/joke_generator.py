"""Bedrock-backed dad-joke generation with retry and length guards.

This module implements the Joke_Generator described in
``design.md`` § Components and Interfaces > Joke_Generator. It calls
Amazon Bedrock's ``Converse`` API to produce a single dad-joke text
string from optional seed words and a list of few-shot examples
sourced upstream by :mod:`joke_api.training_corpus`.

Pipeline ordering note (handler ↔ generator)
--------------------------------------------
The 3-attempt budget mentioned in R1.4 / R4.2 is split across two
layers, exactly as the design's sequence diagram shows:

* **This module** owns up to :data:`MAX_ATTEMPTS` *internal* attempts
  to obtain Bedrock output that satisfies the 10..80-word length
  guard (R1.4, R1.8). Each attempt has its own 15 s hard timeout
  (R1.5).
* **The handler** (:mod:`joke_api.handler`, task 10.1) owns the
  *outer* output-moderation retry loop. When the Output_Moderator
  rejects a generated joke the handler calls
  :func:`generate` again with ``refine=True`` to apply the explicit
  category prohibitions in R4.2; that loop is what spends the
  3-attempt R4.2 budget. Property 12 in ``design.md`` is satisfied
  by the handler counting Bedrock attempts across both layers and
  bounding the total to 3.

This split keeps the generator a pure "produce-one-joke" function
(easy to test without the moderation stack) while the handler is the
single place that knows the moderation budget. The contract is
documented here so future work on the handler does not accidentally
re-loop inside :func:`generate`.

Validated requirements (``requirements.md`` § Requirement 1, § R4)
------------------------------------------------------------------
* **R1.1** -- ``generate(seed_words=[], ...)`` produces one dad joke
  using Amazon Bedrock without seed-word constraints.
* **R1.2** -- when seed words are supplied the generator builds a
  prompt that asks for a joke containing at least one of them; the
  handler verifies containment downstream when needed (Property 1).
* **R1.4** -- output length is constrained to ``[MIN_WORDS,
  MAX_WORDS]`` (10..80 words inclusive). Out-of-range Bedrock
  responses count toward the internal 3-attempt budget.
* **R1.5** -- each Bedrock call has a 15 s hard timeout enforced
  via :class:`concurrent.futures.ThreadPoolExecutor` (matching the
  pattern used by :mod:`joke_api.input_moderator`).
* **R1.6** -- the model id is read from SSM via
  :func:`joke_api.config.load`; tests inject ``model_id=...`` to
  bypass SSM.
* **R1.8** -- after :data:`MAX_ATTEMPTS` failed length checks the
  function raises :class:`JokeGenerationFailed`; the handler maps
  this to HTTP 503.
* **R4.2** -- when ``refine=True`` the system prompt appends the
  explicit prohibitions for profanity, sexual content, graphic
  violence, drugs, slurs, and targeted harassment.

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 1** -- seed-word containment is *requested* in the
  prompt. Final containment is asserted by the handler / property
  test in task 6.4 against actual model output.
* **Property 2** -- joke length is within 10..80 words inclusive;
  enforced by the length guard below.
* **Property 3** -- Bedrock failure produces 503 with no partial
  content. This module raises :class:`JokeGenerationFailed`,
  :class:`JokeGenerationTimeout`, or
  :class:`JokeGenerationUnavailable`; the handler maps each to a
  sanitized 503 with no joke text in the body (R7.5, Property 20).
* **Property 4** -- generation IDs are unique UUID v4s. UUIDs are
  minted by the handler / joke_store layer, not here; this module
  returns text only.
* **Property 12** -- output-rejection retries with refined prompts.
  See the "Pipeline ordering note" above; this module handles the
  refined-prompt construction, the handler drives the loop.
* **Property 13** -- all-rejected outputs fall back to a curated
  safe joke. The fallback selection is the handler's job
  (:mod:`joke_api.fallback_jokes`); this module simply raises so
  the handler can branch.

Public surface
--------------
* :data:`MIN_WORDS` / :data:`MAX_WORDS` -- 10..80 inclusive (R1.4).
* :data:`MAX_ATTEMPTS` -- 3 (R1.4).
* :data:`BEDROCK_BUDGET_MS` -- 15000 (R1.5).
* :data:`SYSTEM_PROMPT_BASE` / :data:`SYSTEM_PROMPT_REFINED_SUFFIX`
  -- the system message tokens.
* :class:`JokeGenerationFailed` -- raised after :data:`MAX_ATTEMPTS`
  length-failed attempts (R1.4, R1.8).
* :class:`JokeGenerationTimeout` -- raised when *every* attempt
  exceeds :data:`BEDROCK_BUDGET_MS` (R1.5).
* :class:`JokeGenerationUnavailable` -- raised on transport / boto
  errors from Bedrock (R1.5).
* :func:`generate` -- public entry point.

Test injection
--------------
The ``bedrock_client`` keyword argument on :func:`generate` is the
supported test injection point; tests pass a stub exposing
``converse(...)``. The lazy default client is built only when no
override is supplied. Tests may also pass ``model_id=`` to bypass
the SSM-backed config loader.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import re
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from joke_api import config as _config

__all__ = [
    "MIN_WORDS",
    "MAX_WORDS",
    "MAX_ATTEMPTS",
    "BEDROCK_BUDGET_MS",
    "SYSTEM_PROMPT_BASE",
    "SYSTEM_PROMPT_REFINED_SUFFIX",
    "JokeGenerationFailed",
    "JokeGenerationTimeout",
    "JokeGenerationUnavailable",
    "generate",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Minimum word count for an accepted joke (R1.4, Property 2). Inclusive.
MIN_WORDS: int = 10

#: Maximum word count for an accepted joke (R1.4, Property 2). Inclusive.
MAX_WORDS: int = 80

#: Maximum number of *internal* Bedrock attempts per call (R1.4, R1.8).
#: Each attempt is a fresh Converse call with its own 15 s timeout.
MAX_ATTEMPTS: int = 3

#: Hard per-attempt budget in milliseconds (R1.5).
BEDROCK_BUDGET_MS: int = 15000

#: Base system prompt -- terse on purpose to keep prompt-token cost low
#: (see ``docs/COST_REPORT.md``). Mirrors the wording in design.md
#: § Components and Interfaces > Joke_Generator.
SYSTEM_PROMPT_BASE: str = (
    "You are a corny but family-friendly dad-joke writer; G/PG only."
)

#: Suffix appended to the system prompt when ``refine=True`` (R4.2).
#: Lists the categories the handler's output moderator already rejects
#: so the model is steered away from them on retry. Phrased as a
#: single sentence to keep token usage minimal.
SYSTEM_PROMPT_REFINED_SUFFIX: str = (
    " Do not include profanity, sexual content, graphic violence,"
    " drug references, slurs, or targeted harassment."
)

#: Inference parameters for Converse. ``maxTokens=200`` comfortably
#: covers MAX_WORDS * ~3 tokens/word with margin; ``temperature`` and
#: ``topP`` are tuned for "corny dad joke" creativity without going
#: off the rails.
_INFERENCE_CONFIG: dict[str, Any] = {
    "maxTokens": 200,
    "temperature": 0.8,
    "topP": 0.9,
}

#: Validation bounds for caller-supplied inputs. These mirror the
#: upstream :mod:`joke_api.request_validator` rules so this module
#: remains testable in isolation (the validator runs first in the
#: handler; defensive validation here protects unit tests and any
#: future caller that bypasses the handler).
_MAX_SEED_WORDS: int = 5
_MAX_SEED_WORD_LEN: int = 30
_MAX_FEW_SHOT_ENTRIES: int = 10
_MAX_FEW_SHOT_ENTRY_LEN: int = 500

# ``str.split()`` with no argument splits on runs of any whitespace
# (spaces, tabs, newlines), which is the natural definition of a
# "word" for length-guarding purposes per R1.4.
_WORD_SPLIT = re.compile(r"\s+")

# Default boto3 client config: a single attempt, with ``read_timeout``
# matching the per-call budget so the underlying socket cannot outlive
# the executor wait. The executor's ``timeout`` is the authoritative
# deadline; this is just a backstop, identical in spirit to the input
# moderator's defense-in-depth pattern.
_DEFAULT_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=BEDROCK_BUDGET_MS / 1000.0,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Lazily-created module-level Bedrock client. Tests inject their own
# client via the ``bedrock_client`` argument and never trigger this
# code path.
_DEFAULT_CLIENT: Optional[Any] = None


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    """Internal record of a single Bedrock attempt.

    Used only inside :func:`generate` to summarize what happened on
    each attempt so the typed exception raised after exhausting
    :data:`MAX_ATTEMPTS` can carry an accurate ``attempts`` count.
    """

    success: bool
    out_of_range: bool
    timed_out: bool
    transport_error: bool


class JokeGenerationFailed(Exception):
    """Raised when no attempt produced a length-valid joke (R1.4, R1.8).

    The handler maps this to HTTP 503
    (``response_builder.UNAVAILABLE``) per R1.5 / R1.8 and Property 3.
    The exception attributes carry the technical detail that the
    observability layer records; they are never surfaced to the
    visitor (R7.5, Property 20).

    Attributes:
        reason: Short stable identifier
            (``"length_rejected"``, ``"empty_output"`` etc.). Never
            free-form internal text.
        attempts: Number of Bedrock calls that were made before giving
            up. Always in ``[1, MAX_ATTEMPTS]``.
    """

    __slots__ = ("reason", "attempts")

    def __init__(self, reason: str, attempts: int) -> None:
        self.reason = reason
        self.attempts = attempts
        super().__init__(
            f"joke generation failed: reason={reason} attempts={attempts}"
        )


class JokeGenerationTimeout(Exception):
    """Raised when every attempt exceeded :data:`BEDROCK_BUDGET_MS` (R1.5).

    The handler maps this to HTTP 503 with no partial content
    (Property 3).

    Attributes:
        budget_ms: The per-attempt budget in milliseconds that was
            exceeded.
        attempts: Number of Bedrock calls that timed out. Always in
            ``[1, MAX_ATTEMPTS]``.
    """

    __slots__ = ("budget_ms", "attempts")

    def __init__(self, budget_ms: int, attempts: int) -> None:
        self.budget_ms = budget_ms
        self.attempts = attempts
        super().__init__(
            f"joke generation timeout: budget_ms={budget_ms} "
            f"attempts={attempts}"
        )


class JokeGenerationUnavailable(Exception):
    """Raised on transport or boto errors from Bedrock (R1.5).

    The handler maps this to HTTP 503
    (``response_builder.UNAVAILABLE``).

    Attributes:
        operation: ``"converse"`` -- the failing Bedrock call name.
    """

    __slots__ = ("operation",)

    def __init__(self, operation: str, message: str) -> None:
        self.operation = operation
        super().__init__(
            f"joke generation unavailable during {operation}: {message}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(
    seed_words: list[str],
    few_shot: list[str],
    *,
    refine: bool = False,
    model_id: Optional[str] = None,
    bedrock_client: Optional[Any] = None,
) -> str:
    """Generate one dad joke via Bedrock Converse.

    Returns the cleaned joke text (no formatting, no JSON wrapping).
    The function makes up to :data:`MAX_ATTEMPTS` Bedrock calls,
    each with its own :data:`BEDROCK_BUDGET_MS` hard timeout, and
    returns the *first* attempt whose word count lies in
    ``[MIN_WORDS, MAX_WORDS]`` inclusive. If every attempt produces
    out-of-range text, raises :class:`JokeGenerationFailed`. If every
    attempt times out, raises :class:`JokeGenerationTimeout`. If
    every attempt fails with a boto / transport error, raises
    :class:`JokeGenerationUnavailable`.

    The handler is responsible for the *outer* output-moderation
    retry loop (R4.2 / Property 12). Pass ``refine=True`` on the
    second and third handler-level calls so this function appends
    the explicit category prohibitions to the system prompt.

    Args:
        seed_words: 0..5 seed words, each 1..30 chars. Validated
            defensively here; the upstream
            :mod:`joke_api.request_validator` is the authoritative
            charset/length gate.
        few_shot: 0..10 few-shot example strings, each <= 500 chars,
            sourced from :mod:`joke_api.training_corpus`. May be
            empty (the rights flag in PLAN.md gates inclusion --
            R17.7).
        refine: When ``True``, the system prompt appends the explicit
            R4.2 category prohibitions. The handler sets this on
            attempts 2 and 3 of the output-moderation retry loop.
        model_id: Optional override for the Bedrock model id. When
            ``None``, :func:`joke_api.config.load` is consulted for
            ``bedrock_model_id``. Tests inject this kwarg to bypass
            SSM.
        bedrock_client: Optional pre-built boto3 ``bedrock-runtime``
            client. Used by tests to inject a stub. When omitted, a
            lazily-cached module-level client is created.

    Returns:
        Cleaned joke text, whitespace-stripped, with
        ``MIN_WORDS <= len(text.split()) <= MAX_WORDS`` (Property 2).

    Raises:
        ValueError: On malformed ``seed_words`` or ``few_shot``.
        JokeGenerationFailed: When all :data:`MAX_ATTEMPTS` attempts
            produced out-of-range or empty text (R1.4, R1.8).
        JokeGenerationTimeout: When every attempt exceeded
            :data:`BEDROCK_BUDGET_MS` (R1.5).
        JokeGenerationUnavailable: When every attempt failed with a
            boto / transport error (R1.5).
    """
    seed_words_clean = _validate_seed_words(seed_words)
    few_shot_clean = _validate_few_shot(few_shot)
    resolved_model_id = _resolve_model_id(model_id)

    system_prompt = _build_system_prompt(refine=refine)
    user_prompt = _build_user_prompt(seed_words_clean, few_shot_clean)

    client = (
        bedrock_client
        if bedrock_client is not None
        else _get_default_client()
    )

    last_outcome: Optional[_AttemptOutcome] = None
    timeouts = 0
    transport_errors = 0

    for attempt_index in range(1, MAX_ATTEMPTS + 1):
        outcome, text = _attempt_once(
            client=client,
            model_id=resolved_model_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        last_outcome = outcome

        if outcome.success:
            assert text is not None  # narrows for type checkers
            return text

        if outcome.timed_out:
            timeouts += 1
        if outcome.transport_error:
            transport_errors += 1
        # Out-of-range / empty outputs simply count toward the budget;
        # we loop and try again.

    # All MAX_ATTEMPTS attempts failed. Pick the most informative
    # exception based on what dominated the failure mode.
    if transport_errors == MAX_ATTEMPTS:
        raise JokeGenerationUnavailable(
            "converse", "all Bedrock attempts failed with transport errors"
        )
    if timeouts == MAX_ATTEMPTS:
        raise JokeGenerationTimeout(BEDROCK_BUDGET_MS, MAX_ATTEMPTS)

    # Mixed or length-rejection-dominated failure: surface as a generic
    # generation failure. ``last_outcome`` is guaranteed non-None here
    # because the loop always runs at least once.
    assert last_outcome is not None
    reason = (
        "empty_output"
        if last_outcome.success is False
        and not last_outcome.out_of_range
        and not last_outcome.timed_out
        and not last_outcome.transport_error
        else "length_rejected"
    )
    raise JokeGenerationFailed(reason, MAX_ATTEMPTS)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _attempt_once(
    *,
    client: Any,
    model_id: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[_AttemptOutcome, Optional[str]]:
    """Run a single Bedrock Converse call under the time budget.

    Returns a tuple ``(outcome, text)``. ``text`` is non-None iff
    ``outcome.success`` is True (i.e. the call returned a length-valid
    joke). For all failure modes the text is ``None`` and the
    outcome's flags identify what happened so the caller can decide
    whether to retry.

    Each call is wrapped in a :class:`concurrent.futures.ThreadPoolExecutor`
    so the wall-clock budget is enforced independently of the
    underlying socket; this matches the pattern used by
    :mod:`joke_api.input_moderator`.
    """
    budget_s = BEDROCK_BUDGET_MS / 1000.0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            client.converse,
            modelId=model_id,
            messages=[
                {
                    "role": "user",
                    "content": [{"text": user_prompt}],
                }
            ],
            system=[{"text": system_prompt}],
            inferenceConfig=_INFERENCE_CONFIG,
        )
        try:
            response = future.result(timeout=budget_s)
        except concurrent.futures.TimeoutError:
            return (
                _AttemptOutcome(
                    success=False,
                    out_of_range=False,
                    timed_out=True,
                    transport_error=False,
                ),
                None,
            )
        except (BotoCoreError, ClientError):
            return (
                _AttemptOutcome(
                    success=False,
                    out_of_range=False,
                    timed_out=False,
                    transport_error=True,
                ),
                None,
            )
    finally:
        # ``cancel_futures=True`` prevents queued tasks from starting;
        # running tasks cannot be interrupted from Python, but
        # ``wait=False`` returns immediately so the request handler
        # can fail fast.
        pool.shutdown(wait=False, cancel_futures=True)

    text = _extract_converse_text(response)
    if not text:
        return (
            _AttemptOutcome(
                success=False,
                out_of_range=False,
                timed_out=False,
                transport_error=False,
            ),
            None,
        )

    cleaned = text.strip()
    word_count = len(_WORD_SPLIT.split(cleaned)) if cleaned else 0
    if word_count < MIN_WORDS or word_count > MAX_WORDS:
        return (
            _AttemptOutcome(
                success=False,
                out_of_range=True,
                timed_out=False,
                transport_error=False,
            ),
            None,
        )

    return (
        _AttemptOutcome(
            success=True,
            out_of_range=False,
            timed_out=False,
            transport_error=False,
        ),
        cleaned,
    )


def _extract_converse_text(response: Any) -> str:
    """Pull the joke text out of a Bedrock Converse response.

    The Converse API returns::

        {
          "output": {
            "message": {
              "role": "assistant",
              "content": [{"text": "..."}]
            }
          },
          ...
        }

    Defensively handles missing keys and content blocks of unexpected
    shape; returns ``""`` when no text is present so the caller can
    treat it as an empty-output failure.
    """
    if not isinstance(response, dict):
        return ""
    output = response.get("output")
    if not isinstance(output, dict):
        return ""
    message = output.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            pieces.append(text)
    return "".join(pieces)


def _build_system_prompt(*, refine: bool) -> str:
    """Build the system message, appending R4.2 prohibitions when refining."""
    if refine:
        return SYSTEM_PROMPT_BASE + SYSTEM_PROMPT_REFINED_SUFFIX
    return SYSTEM_PROMPT_BASE


def _build_user_prompt(
    seed_words: list[str], few_shot: list[str]
) -> str:
    """Assemble the user message inlining few-shot examples and seeds.

    The shape is intentionally simple (a single user turn rather than
    alternating few-shot turns) so the same prompt structure works
    across Anthropic, Amazon Nova, Meta, and Mistral models on the
    Converse API. Bullet lists keep token usage low.
    """
    parts: list[str] = []
    if few_shot:
        parts.append("Here are some example dad jokes for tone:")
        for example in few_shot:
            parts.append(f"- {example}")
        parts.append("")  # blank line between examples and the ask

    if seed_words:
        joined = ", ".join(seed_words)
        parts.append(
            "Now write a fresh dad joke that includes at least one of"
            f" these words (case-insensitive): {joined}."
        )
    else:
        parts.append("Now write a fresh dad joke.")

    parts.append(
        f"Keep the joke between {MIN_WORDS} and {MAX_WORDS} words."
        " Output only the joke text, with no preamble or formatting."
    )
    return "\n".join(parts)


def _validate_seed_words(seed_words: Any) -> list[str]:
    """Defensively validate the ``seed_words`` argument."""
    if not isinstance(seed_words, list):
        raise ValueError("seed_words must be a list of strings")
    if len(seed_words) > _MAX_SEED_WORDS:
        raise ValueError(
            f"seed_words may contain at most {_MAX_SEED_WORDS} entries"
        )
    cleaned: list[str] = []
    for index, word in enumerate(seed_words):
        if not isinstance(word, str):
            raise ValueError(
                f"seed_words[{index}] must be a string"
            )
        if not word or len(word) > _MAX_SEED_WORD_LEN:
            raise ValueError(
                f"seed_words[{index}] must be 1..{_MAX_SEED_WORD_LEN} chars"
            )
        cleaned.append(word)
    return cleaned


def _validate_few_shot(few_shot: Any) -> list[str]:
    """Defensively validate the ``few_shot`` argument."""
    if not isinstance(few_shot, list):
        raise ValueError("few_shot must be a list of strings")
    if len(few_shot) > _MAX_FEW_SHOT_ENTRIES:
        raise ValueError(
            f"few_shot may contain at most {_MAX_FEW_SHOT_ENTRIES} entries"
        )
    cleaned: list[str] = []
    for index, example in enumerate(few_shot):
        if not isinstance(example, str):
            raise ValueError(
                f"few_shot[{index}] must be a string"
            )
        if len(example) > _MAX_FEW_SHOT_ENTRY_LEN:
            raise ValueError(
                f"few_shot[{index}] must be at most "
                f"{_MAX_FEW_SHOT_ENTRY_LEN} chars"
            )
        cleaned.append(example)
    return cleaned


def _resolve_model_id(model_id: Optional[str]) -> str:
    """Return the Bedrock model id, consulting SSM when not overridden."""
    if model_id is not None:
        if not isinstance(model_id, str) or model_id == "":
            raise ValueError("model_id must be a non-empty string")
        return model_id
    cfg = _config.load()
    return cfg.bedrock_model_id


def _get_default_client() -> Any:
    """Return the lazily-created module-level Bedrock runtime client."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = boto3.client(
            "bedrock-runtime",
            config=_DEFAULT_CLIENT_CONFIG,
        )
    return _DEFAULT_CLIENT
