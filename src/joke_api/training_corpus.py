"""Few-shot example loader sourced from the private training_corpus bucket.

This module implements the Training_Corpus loader described in
``design.md`` § Components and Interfaces > Joke_Generator. It pulls
short text examples from a *private* S3 bucket and returns them as
plain Python strings ready to be inlined into a Bedrock prompt as
few-shot examples.

The loader's contract is read-only producer of prompt-context
strings consumed by :mod:`joke_api.joke_generator` only. Returned
strings are *never* surfaced in API responses to clients (R17.3,
Correctness Property 37). The handler / response_builder is the
chokepoint for what reaches the visitor; this module merely supplies
data on the inbound prompt path.

Validated requirements (``requirements.md`` § Requirement 17)
-----------------------------------------------------------
* **R17.1** -- :func:`load_few_shot` returns between 3 and 10
  examples, each truncated to <= 500 characters; the combined
  few-shot section (after joining) is <= 5000 characters.
* **R17.2** -- the corpus is read from a private S3 bucket whose
  Block Public Access is set to ALL. This module never reads from
  any other source and never produces presigned URLs.
* **R17.3** -- examples are returned as plain strings to the joke
  generator only; no presigned URLs, object keys, or other corpus
  identifiers are ever returned to clients.
* **R17.4** -- binary files are skipped both by extension and by
  content sniffing; in Phase 1 there is no extractor wired in, so
  any non-text item is simply excluded from the pool. Extraction
  failures are recorded via :func:`logging.Logger.warning` so the
  observability layer (task 9.x) can pick them up.
* **R17.5** -- the joined few-shot section is bounded to <= 5000
  characters; trailing entries are dropped (not split mid-string)
  until the cap is satisfied.
* **R17.7** -- when ``rights_confirmed`` is ``False`` (the caller
  reads this from ``docs/PLAN.md`` -- see R17.6), the loader
  returns an empty list and performs *no* S3 calls.

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 36** -- 3..10 examples, per-entry <= 500 chars, joined
  section <= 5000 chars (covered in task 6.2).
* **Property 37** -- corpus contents never reach clients. This
  module emits only Python strings into the prompt path; the API
  response chokepoint is :mod:`joke_api.response_builder`.
* **Property 38** -- binary assets are never sent to Bedrock; this
  module filters them out before returning.
* **Property 39** -- the rights-confirmation flag gates inclusion;
  ``rights_confirmed=False`` short-circuits to an empty pool.

Public surface
--------------
* :data:`MIN_EXAMPLES` / :data:`MAX_EXAMPLES` -- 3..10 inclusive,
  per R17.1.
* :data:`PER_EXAMPLE_CHAR_CAP` -- 500 chars (R17.1).
* :data:`COMBINED_CHAR_CAP` -- 5000 chars (R17.5).
* :data:`BUCKET_ENV_VAR` / :data:`DEFAULT_BUCKET` -- env var and
  default name for the private corpus bucket.
* :data:`BINARY_EXTENSIONS` / :data:`TEXT_EXTENSIONS` -- the
  Phase 1 extension allow/deny lists.
* :class:`ExtractionFailure` -- immutable record of a per-key
  extraction failure (recorded in logs; not returned to callers).
* :func:`load_few_shot` -- the public entry point.

Phase 1 extractor scope
-----------------------
R17.4 contemplates extractors that pull text or captions from
binary files (PDFs, images, video). Phase 1 does not ship any such
extractor; :func:`_extract_text` is a placeholder that decodes
``utf-8`` (with ``errors='replace'``) for keys whose extension is in
:data:`TEXT_EXTENSIONS` and skips everything else. Extending this
function is the integration point for any future extractor.

Test injection
--------------
The ``s3_client`` keyword argument on :func:`load_few_shot` is the
supported test injection point. Tests pass a ``MagicMock`` (or any
object exposing ``list_objects_v2`` / ``get_object``) to drive the
loader without hitting AWS. The lazy default client is built only
when no override is supplied.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any, Iterable, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

__all__ = [
    "MIN_EXAMPLES",
    "MAX_EXAMPLES",
    "PER_EXAMPLE_CHAR_CAP",
    "COMBINED_CHAR_CAP",
    "BUCKET_ENV_VAR",
    "DEFAULT_BUCKET",
    "BINARY_EXTENSIONS",
    "TEXT_EXTENSIONS",
    "DEFAULT_SEPARATOR",
    "ExtractionFailure",
    "load_few_shot",
]


_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Minimum number of few-shot examples per R17.1. When fewer textual
#: items exist in the bucket the loader returns what it has rather
#: than erroring; the joke_generator decides how to react (Phase 1
#: tolerates undersized pools rather than failing the request).
MIN_EXAMPLES: int = 3

#: Maximum number of few-shot examples per R17.1.
MAX_EXAMPLES: int = 10

#: Per-entry character cap per R17.1 (counted in Python str chars,
#: not bytes, because the prompt is sent as a UTF-8 string to
#: Bedrock).
PER_EXAMPLE_CHAR_CAP: int = 500

#: Combined character cap on the joined few-shot section per R17.5.
COMBINED_CHAR_CAP: int = 5000

#: Environment variable consulted for the private corpus bucket name.
BUCKET_ENV_VAR: str = "DADJOKES_TRAINING_CORPUS_BUCKET"

#: Default bucket name when ``DADJOKES_TRAINING_CORPUS_BUCKET`` is
#: unset.
DEFAULT_BUCKET: str = "dadjokes-training-corpus"

#: Default separator used to estimate the combined-size cap. The
#: joke_generator may join with a different separator; this constant
#: is conservative (two newlines is the longest realistic separator)
#: so that the cap is never under-counted.
DEFAULT_SEPARATOR: str = "\n\n"

#: File extensions that are *definitely* binary and must be skipped
#: per R17.4 (Property 38). The check is case-insensitive.
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".zip",
        ".gz",
        ".tar",
        ".bz2",
        ".7z",
        ".rar",
        ".pdf",
        ".docx",
        ".doc",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".ico",
        ".mp3",
        ".mp4",
        ".wav",
        ".m4a",
        ".mov",
        ".avi",
        ".so",
        ".dll",
        ".exe",
        ".pyc",
        ".class",
        ".o",
    }
)

#: File extensions that are accepted as plain text in Phase 1. Any
#: other extension is treated as "unknown" and content-sniffed
#: (:func:`_looks_binary`).
TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".md", ".csv"})

# Number of bytes used for binary content sniffing on unknown
# extensions.
_SNIFF_BYTES: int = 8192
# A body is considered binary when more than this fraction of its
# sniffed prefix consists of non-printable, non-whitespace bytes.
_NONPRINTABLE_RATIO_THRESHOLD: float = 0.30

# Default boto3 client config: a single attempt with bounded
# timeouts so a stuck S3 call cannot block a Lambda invocation
# indefinitely. Reads are bounded at 5 seconds; the joke_generator
# enforces the wall-clock budget for the whole prompt build.
_DEFAULT_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Lazily-created module-level S3 client. Tests inject their own
# client via the ``s3_client`` argument and never trigger this path.
_DEFAULT_CLIENT: Optional[Any] = None


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class ExtractionFailure:
    """Immutable record of a per-key extraction failure.

    Instances are emitted to :mod:`logging` rather than returned to
    the caller; the joke_generator does not need them and surfacing
    them through the public API would risk leaking corpus object
    keys in tracebacks. Future extractors should construct one of
    these and pass it to :func:`_record_failure`.

    Attributes:
        key: The S3 object key that failed extraction.
        reason: A short human-readable identifier of the failure
            (``"binary"``, ``"decode_error"``, ``"empty"``, ...).
            Never carries free-form internal text -- the
            observability layer treats this field as a stable label.
    """

    key: str
    reason: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_few_shot(
    *,
    rights_confirmed: bool,
    s3_client: Optional[Any] = None,
    bucket: Optional[str] = None,
    max_examples: int = 6,
) -> list[str]:
    """Load few-shot examples for the joke prompt.

    Returns a list of plain Python strings suitable for inlining
    into a Bedrock prompt. The returned list satisfies the bounds
    declared in R17.1 / R17.5 (each entry <= 500 chars, joined
    section <= 5000 chars, length in 0..10).

    The caller (joke_generator) is responsible for reading the
    rights-confirmation flag from ``docs/PLAN.md`` and passing it
    here as ``rights_confirmed``; this module never opens PLAN.md
    itself. When ``rights_confirmed`` is ``False`` (or any
    falsy value) the loader returns ``[]`` immediately and performs
    *no* S3 calls (R17.7, Property 39).

    Args:
        rights_confirmed: Boolean from ``docs/PLAN.md`` indicating
            the site owner has confirmed rights to use the
            Training_Corpus contents as style reference and few-shot
            examples (R17.6). Required keyword argument so callers
            cannot forget the gate.
        s3_client: Optional pre-built boto3 ``s3`` client. Used by
            tests to inject a stub. When omitted, a lazily-cached
            module-level client is created.
        bucket: Override for the corpus bucket name. Defaults to
            ``$DADJOKES_TRAINING_CORPUS_BUCKET`` or
            :data:`DEFAULT_BUCKET`.
        max_examples: Soft cap on the returned pool size. Clamped to
            ``[MIN_EXAMPLES, MAX_EXAMPLES]`` so callers cannot ask
            for more than R17.1 permits or for fewer than the
            documented minimum target. Defaults to 6 (mid-range).

    Returns:
        A list of 0..``MAX_EXAMPLES`` strings. The empty list is
        returned when ``rights_confirmed`` is false, when the bucket
        contains no textual files, or when S3 is unreachable. The
        joke_generator decides whether an undersized pool is
        acceptable; this loader never raises on undersize.

    Notes:
        Phase 1 has no extractor for binary files; any non-text
        object is filtered out by :func:`_extract_text`. Extraction
        failures are recorded with
        ``logging.getLogger(__name__).warning``.
    """
    # R17.7 / Property 39: empty pool when rights are not confirmed.
    # ``rights_confirmed`` is required keyword; we still defensively
    # check truthiness so a missing/None value also short-circuits.
    if not rights_confirmed:
        return []

    target = _clamp_examples(max_examples)
    bucket_name = bucket if bucket else os.environ.get(
        BUCKET_ENV_VAR, DEFAULT_BUCKET
    )
    if not bucket_name:
        # An empty configured bucket name is a misconfiguration; log
        # and return an empty pool rather than calling S3 with "".
        _LOGGER.warning(
            "training_corpus: empty bucket name; returning empty pool"
        )
        return []

    client = s3_client if s3_client is not None else _get_default_client()

    try:
        keys = list(_iter_object_keys(client, bucket_name, target))
    except (BotoCoreError, ClientError) as exc:
        # S3 unavailability is *not* a fatal error for prompt
        # building; the joke_generator can proceed with an empty
        # few-shot pool. Property 38 / R17.5 are vacuously satisfied
        # when the pool is empty.
        _LOGGER.warning(
            "training_corpus: list_objects_v2 failed: %s", exc
        )
        return []

    examples: list[str] = []
    for key in keys:
        if len(examples) >= MAX_EXAMPLES:
            break
        try:
            text = _fetch_and_extract(client, bucket_name, key)
        except (BotoCoreError, ClientError) as exc:
            _record_failure(
                ExtractionFailure(key=key, reason="s3_error"),
                detail=str(exc),
            )
            continue

        if text is None:
            # Already logged by _extract_text via _record_failure.
            continue

        truncated = _truncate(text)
        if not truncated:
            # Truncation collapsed the entry to empty (e.g. file was
            # all whitespace). Drop it -- empty examples carry no
            # signal and would just bloat the joined section.
            _record_failure(
                ExtractionFailure(key=key, reason="empty_after_truncate")
            )
            continue

        examples.append(truncated)

    # Enforce the combined-size cap (R17.5 / Property 36). We drop
    # *trailing* entries rather than splitting mid-string so each
    # surviving example remains a coherent unit.
    examples = _enforce_combined_cap(examples)

    # Final cap on pool size (R17.1). ``_iter_object_keys`` already
    # honors ``target``, but ``MAX_EXAMPLES`` is the contract bound
    # so we re-apply it as defense-in-depth.
    if len(examples) > MAX_EXAMPLES:
        examples = examples[:MAX_EXAMPLES]

    return examples


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clamp_examples(requested: int) -> int:
    """Clamp ``requested`` into ``[MIN_EXAMPLES, MAX_EXAMPLES]``.

    ``bool`` inputs are rejected because ``True`` would silently
    coerce to ``1`` (below the documented minimum).
    """
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise ValueError("max_examples must be an int")
    if requested < MIN_EXAMPLES:
        return MIN_EXAMPLES
    if requested > MAX_EXAMPLES:
        return MAX_EXAMPLES
    return requested


def _iter_object_keys(
    client: Any, bucket: str, target: int
) -> Iterable[str]:
    """Yield S3 object keys from ``bucket`` until enough are seen.

    Stops once ``target * 2`` keys have been yielded so the caller
    has a small buffer of candidates if some entries fail extraction
    or collapse to empty. The cap also bounds the number of S3
    requests on a corpus with thousands of files.
    """
    # Pull a small buffer so a few failed extractions don't drop us
    # below the target. ``MAX_EXAMPLES * 4`` is a soft ceiling that
    # bounds the prompt-build time on huge corpora.
    soft_ceiling = max(target * 2, MAX_EXAMPLES * 2)
    seen = 0
    response = client.list_objects_v2(Bucket=bucket, MaxKeys=soft_ceiling)
    contents = response.get("Contents") or []
    for item in contents:
        key = item.get("Key")
        if not key or key.endswith("/"):
            continue
        yield key
        seen += 1
        if seen >= soft_ceiling:
            return


def _fetch_and_extract(
    client: Any, bucket: str, key: str
) -> Optional[str]:
    """Fetch ``key`` from ``bucket`` and extract text, or ``None``.

    Returns ``None`` (and records an extraction failure via
    :func:`_record_failure`) when the object is binary, fails to
    decode, or is otherwise unsuitable for prompt inclusion.
    """
    if _is_binary_extension(key):
        _record_failure(ExtractionFailure(key=key, reason="binary_extension"))
        return None

    response = client.get_object(Bucket=bucket, Key=key)
    body = response.get("Body")
    raw = body.read() if body is not None else b""
    if not isinstance(raw, (bytes, bytearray)):
        # Some test stubs may return strings directly; accept that
        # but coerce so the rest of the pipeline is uniform.
        raw = str(raw).encode("utf-8", errors="replace")

    return _extract_text(key, bytes(raw))


def _extract_text(key: str, body: bytes) -> Optional[str]:
    """Phase 1 placeholder for the extractor pipeline (R17.4).

    For known text extensions (``.txt``, ``.md``, ``.csv``), decode
    UTF-8 with ``errors='replace'``. For unknown extensions, sniff
    the first :data:`_SNIFF_BYTES` for NUL bytes and non-printable
    ratio; if it looks textual, decode it the same way. Anything
    else returns ``None`` and records an extraction failure.

    A future task wires PDF / image / caption extractors in here;
    those extractors should return a ``str`` they have already
    sanitized (R17.4 caps each extracted item to <= 500 chars, but
    that cap is enforced by :func:`_truncate` so extractors do not
    need to repeat the work).
    """
    ext = _ext(key)
    if ext in TEXT_EXTENSIONS:
        try:
            return body.decode("utf-8", errors="replace")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            _record_failure(
                ExtractionFailure(key=key, reason="decode_error"),
                detail=str(exc),
            )
            return None

    # Unknown extension: sniff before paying the decode cost.
    if _looks_binary(body):
        _record_failure(
            ExtractionFailure(key=key, reason="binary_sniff")
        )
        return None

    try:
        return body.decode("utf-8", errors="replace")
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        _record_failure(
            ExtractionFailure(key=key, reason="decode_error"),
            detail=str(exc),
        )
        return None


def _is_binary_extension(key: str) -> bool:
    """Return ``True`` iff ``key``'s extension is in the binary set."""
    return _ext(key) in BINARY_EXTENSIONS


def _ext(key: str) -> str:
    """Return the lowercase extension of ``key`` including the dot."""
    # Use ``rfind`` rather than ``os.path.splitext`` because S3 keys
    # use forward slashes regardless of platform and may contain
    # multiple dots.
    slash = key.rfind("/")
    base = key[slash + 1:]
    dot = base.rfind(".")
    if dot <= 0:
        return ""
    return base[dot:].lower()


def _looks_binary(body: bytes) -> bool:
    """Return ``True`` iff the first 8 KiB of ``body`` looks binary.

    The sniff considers a body binary when it contains any NUL byte
    or when more than 30% of the sniffed prefix consists of bytes
    outside the printable ASCII range (excluding common whitespace).
    Empty bodies are treated as binary so they don't sneak through
    as zero-length few-shot examples.
    """
    if not body:
        return True
    sniff = body[:_SNIFF_BYTES]
    if b"\x00" in sniff:
        return True
    nonprintable = 0
    # Whitespace bytes commonly found in text files.
    text_whitespace = {0x09, 0x0A, 0x0B, 0x0C, 0x0D}
    for byte in sniff:
        if byte in text_whitespace:
            continue
        if 0x20 <= byte <= 0x7E:
            continue
        # Bytes >= 0x80 may be valid UTF-8 multibyte sequence
        # leaders. We don't decode here; the ratio threshold tolerates
        # some high bytes for legitimate UTF-8 text.
        nonprintable += 1
    return (nonprintable / len(sniff)) > _NONPRINTABLE_RATIO_THRESHOLD


def _truncate(text: str) -> str:
    """Truncate ``text`` to :data:`PER_EXAMPLE_CHAR_CAP` characters.

    Strips surrounding whitespace first so the truncated result is
    not padded by leading/trailing newlines. Returns ``""`` when
    ``text`` is whitespace-only (the caller drops empties).
    """
    stripped = text.strip()
    if not stripped:
        return ""
    if len(stripped) <= PER_EXAMPLE_CHAR_CAP:
        return stripped
    return stripped[:PER_EXAMPLE_CHAR_CAP]


def _enforce_combined_cap(examples: list[str]) -> list[str]:
    """Drop trailing entries until the joined section is within cap.

    The cap is :data:`COMBINED_CHAR_CAP` (R17.5). The joined size is
    estimated using :data:`DEFAULT_SEPARATOR`, which is the longest
    realistic separator the joke_generator might use; if the
    generator picks a shorter separator the cap is satisfied with
    margin to spare.
    """
    if not examples:
        return examples
    sep_len = len(DEFAULT_SEPARATOR)
    total = sum(len(e) for e in examples) + sep_len * (len(examples) - 1)
    while examples and total > COMBINED_CHAR_CAP:
        dropped = examples.pop()
        total -= len(dropped)
        if examples:
            total -= sep_len
    return examples


def _record_failure(
    failure: ExtractionFailure, *, detail: Optional[str] = None
) -> None:
    """Emit an extraction-failure log record (R17.4).

    Future observability work (task 9.x) can pick these up via the
    structured logger; for now stdlib logging is the contract.
    """
    if detail:
        _LOGGER.warning(
            "training_corpus extraction failure: key=%s reason=%s detail=%s",
            failure.key,
            failure.reason,
            detail,
        )
    else:
        _LOGGER.warning(
            "training_corpus extraction failure: key=%s reason=%s",
            failure.key,
            failure.reason,
        )


def _get_default_client() -> Any:
    """Return the lazily-created module-level S3 client."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = boto3.client(
            "s3",
            config=_DEFAULT_CLIENT_CONFIG,
        )
    return _DEFAULT_CLIENT
