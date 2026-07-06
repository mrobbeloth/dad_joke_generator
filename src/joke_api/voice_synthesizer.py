"""Polly-backed voice synthesis with S3 storage and presigned URLs.

This module implements the Voice_Synthesizer described in
``design.md`` § Components and Interfaces > Voice_Synthesizer. It
turns a piece of joke text into a short MP3 by calling Amazon
Polly's ``SynthesizeSpeech`` API, writes the resulting audio to a
private S3 bucket, and returns a 15-minute presigned ``GET`` URL the
handler can hand to the visitor's browser.

The contract is "always return, never raise on the soft-fail path".
Every failure mode the synthesis pipeline can surface -- text length
out of range, Polly timeout, Polly transport error, S3 write error,
presign error -- is reported back to the handler via
:class:`SynthesisResult` with ``audio_available=False`` and a short
stable ``error`` label. Programmer errors (missing config, invalid
arguments) still raise :class:`ValueError` because they cannot be
"soft-failed" without masking misconfiguration.

Validated requirements (``requirements.md`` § Requirement 2)
-----------------------------------------------------------
* **R2.1** -- Polly is invoked synchronously from the handler within
  1 s of joke generation completion. The handler controls timing;
  this module's contract is "respond promptly when called". The
  10 s synthesis budget enforced here keeps the entire R2.1 + R2.6
  envelope under the design's 11-second ceiling.
* **R2.2** -- ``OutputFormat='mp3'`` and ``SampleRate='22050'`` keep
  Polly's standard-engine output around 32 kbps, well under the
  64 kbps cap. The cap is documented as
  :data:`BITRATE_CAP_KBPS`; no runtime check is needed because
  Polly never exceeds the configured sample-rate's bitrate.
* **R2.3** -- when synthesis completes within 10 s the result has
  ``audio_available=True`` and a non-None ``audio_url``.
* **R2.4** -- the presigned ``GET`` URL is generated with
  ``ExpiresIn=900`` (= 15 minutes); see
  :data:`PRESIGN_EXPIRY_SECONDS`.
* **R2.6** -- *every* synthesis failure mode returns
  ``SynthesisResult(audio_url=None, audio_available=False,
  error=<label>)``. The function never raises on the soft-fail
  path so the handler can keep the joke text in the response.
* **R2.8** -- ``Engine='standard'`` and the voice id is sourced from
  SSM (``/dadjokes/polly_voice_id``) via
  :func:`joke_api.config.load`; tests inject ``voice_id=`` to
  bypass SSM.
* **R2.9** -- ``len(joke_text)`` outside ``[1, 1500]`` skips Polly
  entirely; no API call is made and no S3 object is written.

Validated correctness properties (``design.md`` § Correctness Properties)
------------------------------------------------------------------------
* **Property 6** -- audio availability mirrors Polly outcome.
  ``audio_available`` is ``True`` iff Polly succeeded *and* the S3
  write *and* the presign succeeded; ``audio_url`` is non-None iff
  ``audio_available`` is ``True``.
* **Property 7** -- presigned audio URLs are valid for at least
  15 minutes. :data:`PRESIGN_EXPIRY_SECONDS` is 900 and is the only
  ``ExpiresIn`` value passed to ``generate_presigned_url``.

Public surface
--------------
* :data:`MIN_TEXT_LEN` / :data:`MAX_TEXT_LEN` -- 1..1500 inclusive
  (R2.1, R2.9).
* :data:`POLLY_BUDGET_MS` -- 10 000 (R2.6).
* :data:`BITRATE_CAP_KBPS` -- 64 (R2.2). Documentation only; Polly
  output at the configured sample rate is well under this cap.
* :data:`SAMPLE_RATE` -- ``"22050"`` (R2.2; design's "Polly research"
  note).
* :data:`OUTPUT_FORMAT` -- ``"mp3"`` (R2.2).
* :data:`ENGINE` -- ``"standard"`` (R2.8; Phase 1 standard voice).
* :data:`PRESIGN_EXPIRY_SECONDS` -- 900 (R2.4).
* :data:`AUDIO_BUCKET_ENV_VAR` / :data:`DEFAULT_AUDIO_BUCKET` -- env
  var and default name for the audio bucket; the IaC-provisioned
  bucket name is wired in by infra outputs / Lambda env vars.
* :class:`SynthesisResult` -- frozen + slotted dataclass returned
  from every call.
* :func:`synthesize` -- public entry point.

Test injection
--------------
The ``polly_client`` and ``s3_client`` keyword arguments on
:func:`synthesize` are the supported test injection points. Tests
pass stub objects exposing ``synthesize_speech`` (Polly) and
``put_object`` / ``generate_presigned_url`` (S3) to drive happy-path
and failure-mode coverage without hitting AWS. The lazy default
clients are built only when no override is supplied.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import os
import uuid
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from joke_api import config as _config

__all__ = [
    "MIN_TEXT_LEN",
    "MAX_TEXT_LEN",
    "POLLY_BUDGET_MS",
    "BITRATE_CAP_KBPS",
    "SAMPLE_RATE",
    "OUTPUT_FORMAT",
    "ENGINE",
    "PRESIGN_EXPIRY_SECONDS",
    "AUDIO_BUCKET_ENV_VAR",
    "DEFAULT_AUDIO_BUCKET",
    "SynthesisResult",
    "synthesize",
    "presign_audio_url",
    "download_disposition",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Minimum joke text length accepted for synthesis (R2.1, R2.9).
MIN_TEXT_LEN: int = 1

#: Maximum joke text length accepted for synthesis (R2.9). Polly's
#: synchronous ``SynthesizeSpeech`` API supports up to 3,000
#: characters, so 1,500 leaves comfortable margin and matches the
#: design's documented cap.
MAX_TEXT_LEN: int = 1500

#: Hard wall-clock budget for the Polly call in milliseconds (R2.6).
POLLY_BUDGET_MS: int = 10_000

#: Documentation-only ceiling for the audio bitrate (R2.2). Polly
#: standard-engine MP3 at :data:`SAMPLE_RATE` is roughly 32 kbps,
#: well under the cap; no runtime check is required because Polly
#: never produces output above the rate implied by the configured
#: ``SampleRate``.
BITRATE_CAP_KBPS: int = 64

#: Polly sample rate in Hz, sent as a string per the SDK contract.
#: 22 050 Hz is the highest standard-engine MP3 sample rate and the
#: value the design's "Polly research" note pins (R2.2).
SAMPLE_RATE: str = "22050"

#: Polly output format (R2.2).
OUTPUT_FORMAT: str = "mp3"

#: Polly engine. Phase 1 is restricted to standard (non-neural)
#: voices per R2.8.
ENGINE: str = "standard"

#: Presigned ``GET`` URL expiry in seconds (R2.4 / Property 7). 900 s
#: is exactly 15 minutes; this is the only value passed to
#: :func:`generate_presigned_url` so Property 7's lower-bound
#: assertion is trivially satisfied.
PRESIGN_EXPIRY_SECONDS: int = 15 * 60

#: Environment variable consulted for the audio bucket name when no
#: explicit ``audio_bucket`` argument is passed. The IaC-provisioned
#: bucket name is set into this variable on the Lambda's environment.
AUDIO_BUCKET_ENV_VAR: str = "DADJOKES_AUDIO_BUCKET"

#: Default audio bucket name when ``$DADJOKES_AUDIO_BUCKET`` is
#: unset. Matches the dev-environment bucket name implied by
#: ``infra/terraform/s3.tf`` (``<project>-<env>-audio-<suffix>``)
#: under ``project=dadjokes`` and ``env=dev``; the ``-<suffix>`` is
#: appended at provisioning time and the real value is injected via
#: the Lambda env var, so this default is only useful for local
#: smoke tests.
DEFAULT_AUDIO_BUCKET: str = "dadjokes-dev-audio"

# Stable error labels surfaced via :class:`SynthesisResult.error`.
# The handler / observability layer treats these as opaque tokens.
_ERR_TEXT_LEN = "text_length_out_of_range"
_ERR_POLLY_TIMEOUT = "polly_timeout"
_ERR_POLLY_UNAVAILABLE = "polly_unavailable"
_ERR_POLLY_EMPTY = "polly_empty_audio"
_ERR_S3_UPLOAD = "s3_upload_failed"
_ERR_PRESIGN = "presign_failed"

# Default boto3 client config: a single attempt with a ``read_timeout``
# matching the synthesis budget so the underlying socket cannot
# outlive the executor wait. The executor's ``timeout`` is the
# authoritative deadline; this is just a backstop, identical in
# spirit to the input moderator's defense-in-depth pattern.
_DEFAULT_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=POLLY_BUDGET_MS / 1000.0,
    retries={"max_attempts": 1, "mode": "standard"},
)

# Lazily-created module-level clients. Tests inject their own clients
# via the ``polly_client`` / ``s3_client`` arguments and never trigger
# these paths.
_DEFAULT_POLLY_CLIENT: Optional[Any] = None
_DEFAULT_S3_CLIENT: Optional[Any] = None


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Outcome of a single :func:`synthesize` call.

    Attributes:
        audio_url: 15-minute presigned ``GET`` URL for inline
            playback when synthesis succeeded; ``None`` on every
            soft-fail path.
        audio_download_url: 15-minute presigned ``GET`` URL that
            forces a browser download via a
            ``Content-Disposition: attachment`` response header with a
            ``dad-joke-<id>.mp3`` filename (R2.10 / Property 45).
            Non-None whenever ``audio_available`` is ``True`` and the
            download-variant presign succeeded; ``None`` on soft-fail
            or if only the download presign failed (playback is
            unaffected in the latter case).
        audio_available: ``True`` iff Polly returned audio, S3
            accepted the upload, and the (playback) presigned URL was
            generated (Property 6). ``audio_url`` is non-None iff this
            flag is ``True``.
        error: ``None`` on success; otherwise a short stable label
            (``"text_length_out_of_range"``, ``"polly_timeout"``,
            ``"polly_unavailable"``, ``"polly_empty_audio"``,
            ``"s3_upload_failed"``, ``"presign_failed"``). The
            observability layer records this; sanitized API error
            responses never surface it (R7.5, Property 20).
    """

    audio_url: Optional[str]
    audio_available: bool
    error: Optional[str]
    # Last with a default so existing constructors that predate R2.10
    # (and soft-fail paths) remain valid; None means "no download URL".
    audio_download_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def synthesize(
    joke_text: str,
    *,
    generation_id: Optional[str] = None,
    voice_id: Optional[str] = None,
    audio_bucket: Optional[str] = None,
    polly_client: Optional[Any] = None,
    s3_client: Optional[Any] = None,
) -> SynthesisResult:
    """Synthesize joke audio with Polly and return a presigned URL.

    The function performs four steps:

    1. Length gate (R2.9): if ``len(joke_text)`` is outside
       ``[MIN_TEXT_LEN, MAX_TEXT_LEN]``, return a soft-fail
       result with ``error="text_length_out_of_range"`` and skip
       Polly entirely.
    2. Polly synthesis under a 10 s wall-clock budget (R2.6).
       Timeouts and transport errors soft-fail with stable labels.
    3. S3 upload of the MP3 bytes to
       ``s3://<audio_bucket>/<generation_id>.mp3``. Errors
       soft-fail with ``error="s3_upload_failed"``.
    4. Presigned ``GET`` URL generation with ``ExpiresIn=900``
       (R2.4 / Property 7). Errors soft-fail with
       ``error="presign_failed"``.

    Programmer errors (missing voice id config, invalid argument
    types) raise :class:`ValueError` because they signal a
    misconfiguration that should not be masked.

    Args:
        joke_text: The text to synthesize. Length must be in
            ``[1, 1500]`` characters; otherwise the soft-fail
            branch returns immediately without calling Polly.
        generation_id: UUID v4 string used to key the audio object
            in S3. When ``None``, a fresh UUID is minted internally.
            The handler passes the same generation_id used in the
            API response so the audit endpoint
            (:mod:`joke_api.joke_store`) can cross-reference the
            audio file by id.
        voice_id: Override for the Polly voice id. When ``None``,
            :func:`joke_api.config.load` is consulted for
            ``polly_voice_id``. Tests inject this kwarg to bypass
            SSM.
        audio_bucket: Override for the audio bucket name. Defaults
            to ``$DADJOKES_AUDIO_BUCKET`` or
            :data:`DEFAULT_AUDIO_BUCKET`. The IaC-provisioned
            bucket name is wired in via the Lambda's env var.
        polly_client: Optional pre-built boto3 ``polly`` client.
            Used by tests to inject a stub. When omitted, a
            lazily-cached module-level client is created.
        s3_client: Optional pre-built boto3 ``s3`` client. Used by
            tests to inject a stub. When omitted, a lazily-cached
            module-level client is created.

    Returns:
        :class:`SynthesisResult` with the synthesis outcome. On the
        success path ``audio_available`` is ``True`` and
        ``audio_url`` is a presigned ``GET`` URL valid for at least
        :data:`PRESIGN_EXPIRY_SECONDS` (Property 7); on every
        soft-fail path ``audio_available`` is ``False``,
        ``audio_url`` is ``None``, and ``error`` is a short stable
        label describing what failed.

    Raises:
        ValueError: When ``joke_text`` is not a ``str``, when
            ``voice_id`` is explicitly passed but empty, when
            ``generation_id`` is explicitly passed but empty, when
            no voice id can be resolved (missing config), or when
            no audio bucket can be resolved.
    """
    # ---- Argument shape (programmer errors -- raise) ----
    if not isinstance(joke_text, str):
        raise ValueError("joke_text must be a string")

    # ---- R2.9 length gate (soft-fail; never call Polly) ----
    if len(joke_text) < MIN_TEXT_LEN or len(joke_text) > MAX_TEXT_LEN:
        return _soft_fail(_ERR_TEXT_LEN)

    resolved_voice_id = _resolve_voice_id(voice_id)
    resolved_bucket = _resolve_audio_bucket(audio_bucket)
    resolved_generation_id = _resolve_generation_id(generation_id)
    object_key = f"{resolved_generation_id}.mp3"

    # ---- Polly synthesis (R2.2, R2.6, R2.8) ----
    polly = (
        polly_client
        if polly_client is not None
        else _get_default_polly_client()
    )
    audio_bytes = _call_polly(polly, joke_text, resolved_voice_id)
    if isinstance(audio_bytes, str):
        # ``_call_polly`` returns the soft-fail label as a str on
        # the failure path so we don't need a second exception type.
        return _soft_fail(audio_bytes)

    if not audio_bytes:
        # Polly returned a stream but it was empty; treat as a
        # soft-fail rather than uploading a 0-byte object.
        return _soft_fail(_ERR_POLLY_EMPTY)

    # ---- S3 upload (R2.6 soft-fail) ----
    s3 = s3_client if s3_client is not None else _get_default_s3_client()
    try:
        s3.put_object(
            Bucket=resolved_bucket,
            Key=object_key,
            Body=audio_bytes,
            ContentType="audio/mpeg",
        )
    except (BotoCoreError, ClientError):
        return _soft_fail(_ERR_S3_UPLOAD)

    # ---- Presigned playback URL (R2.4 / Property 7) ----
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": resolved_bucket, "Key": object_key},
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
        )
    except (BotoCoreError, ClientError, Exception):  # noqa: BLE001
        # ``generate_presigned_url`` can raise generic exceptions on
        # bad credentials. Any failure here is a soft-fail because
        # the joke text is still valid and the handler should keep
        # the response 200 with ``audio_available=False`` (R2.6).
        return _soft_fail(_ERR_PRESIGN)

    if not isinstance(url, str) or url == "":
        # Defensive: a stub or future SDK version returning an empty
        # URL counts as a presign failure.
        return _soft_fail(_ERR_PRESIGN)

    # ---- Presigned download URL (R2.10 / Property 45) ----
    # A distinct presigned URL that overrides the response
    # Content-Disposition so the browser downloads the MP3 with a
    # friendly name instead of streaming it. If ONLY this presign
    # fails, playback is unaffected: we keep audio_available=True and
    # audio_url set, and surface audio_download_url=None so the
    # frontend simply hides the download control. This is a strictly
    # weaker failure than the playback presign, so it never soft-fails
    # the whole synthesis.
    download_url = _presign_download(s3, resolved_bucket, object_key, resolved_generation_id)

    return SynthesisResult(
        audio_url=url,
        audio_download_url=download_url,
        audio_available=True,
        error=None,
    )


def _presign_download(
    s3: Any,
    bucket: str,
    key: str,
    generation_id: str,
) -> Optional[str]:
    """Presign a download-variant GET URL, or ``None`` on any failure.

    Adds ``ResponseContentDisposition`` so S3 serves the object with a
    ``Content-Disposition: attachment; filename="dad-joke-<id>.mp3"``
    header (R2.10). Never raises: a download-presign failure degrades
    gracefully to "no download link" without affecting playback.
    """
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ResponseContentDisposition": download_disposition(generation_id),
            },
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
        )
    except (BotoCoreError, ClientError, Exception):  # noqa: BLE001
        return None
    if not isinstance(url, str) or url == "":
        return None
    return url


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _soft_fail(error_label: str) -> SynthesisResult:
    """Return the canonical soft-fail :class:`SynthesisResult`."""
    return SynthesisResult(
        audio_url=None,
        audio_download_url=None,
        audio_available=False,
        error=error_label,
    )


def download_disposition(generation_id: str) -> str:
    """Build the ``Content-Disposition`` value for the download URL.

    R2.10 mandates an ``attachment`` disposition with a
    ``dad-joke-<id>.mp3`` filename so a browser saves the audio with a
    friendly name instead of streaming it inline. Centralized here so
    both :func:`synthesize` and :func:`presign_audio_url` produce the
    identical header.
    """
    return f'attachment; filename="dad-joke-{generation_id}.mp3"'


def _call_polly(
    polly: Any,
    joke_text: str,
    voice_id: str,
) -> "bytes | str":
    """Invoke Polly under the time budget; return audio bytes or label.

    Wraps ``synthesize_speech`` in a
    :class:`concurrent.futures.ThreadPoolExecutor` so the
    :data:`POLLY_BUDGET_MS` budget is enforced independently of the
    underlying socket. Returns:

    * ``bytes`` -- raw MP3 payload on success.
    * ``str`` -- one of the ``_ERR_*`` labels on the soft-fail path
      (timeout, transport error). Returning the label keeps
      :func:`synthesize` free of nested exception handlers.
    """
    budget_s = POLLY_BUDGET_MS / 1000.0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            polly.synthesize_speech,
            OutputFormat=OUTPUT_FORMAT,
            SampleRate=SAMPLE_RATE,
            VoiceId=voice_id,
            Engine=ENGINE,
            Text=joke_text,
        )
        try:
            response = future.result(timeout=budget_s)
        except concurrent.futures.TimeoutError:
            return _ERR_POLLY_TIMEOUT
        except (BotoCoreError, ClientError):
            return _ERR_POLLY_UNAVAILABLE
    finally:
        # ``cancel_futures=True`` prevents queued tasks from
        # starting; running tasks cannot be interrupted from Python,
        # but ``wait=False`` returns immediately so the request
        # handler can fail fast.
        pool.shutdown(wait=False, cancel_futures=True)

    stream = response.get("AudioStream") if isinstance(response, dict) else None
    if stream is None:
        return _ERR_POLLY_UNAVAILABLE

    try:
        audio_bytes = stream.read()
    except (BotoCoreError, ClientError, OSError):
        return _ERR_POLLY_UNAVAILABLE

    if not isinstance(audio_bytes, (bytes, bytearray)):
        # Stub returned something exotic; treat as transport error
        # rather than corrupting the S3 upload.
        return _ERR_POLLY_UNAVAILABLE
    return bytes(audio_bytes)


def _resolve_voice_id(voice_id: Optional[str]) -> str:
    """Return the Polly voice id, consulting SSM when not overridden.

    Programmer error (missing config, empty override) raises
    :class:`ValueError`; this is *not* a soft-fail because a missing
    voice id is misconfiguration the operator must fix.
    """
    if voice_id is not None:
        if not isinstance(voice_id, str) or voice_id == "":
            raise ValueError("voice_id must be a non-empty string")
        return voice_id
    cfg = _config.load()
    resolved = cfg.polly_voice_id
    if not isinstance(resolved, str) or resolved == "":
        raise ValueError("polly_voice_id is not configured")
    return resolved


def _resolve_audio_bucket(audio_bucket: Optional[str]) -> str:
    """Return the audio bucket name from arg, env, or default."""
    if audio_bucket is not None:
        if not isinstance(audio_bucket, str) or audio_bucket == "":
            raise ValueError("audio_bucket must be a non-empty string")
        return audio_bucket
    env_value = os.environ.get(AUDIO_BUCKET_ENV_VAR)
    if env_value:
        return env_value
    return DEFAULT_AUDIO_BUCKET


def _resolve_generation_id(generation_id: Optional[str]) -> str:
    """Return the generation id, minting a fresh UUID v4 if needed."""
    if generation_id is not None:
        if not isinstance(generation_id, str) or generation_id == "":
            raise ValueError("generation_id must be a non-empty string")
        return generation_id
    return str(uuid.uuid4())


def _get_default_polly_client() -> Any:
    """Return the lazily-created module-level Polly client."""
    global _DEFAULT_POLLY_CLIENT
    if _DEFAULT_POLLY_CLIENT is None:
        _DEFAULT_POLLY_CLIENT = boto3.client(
            "polly",
            config=_DEFAULT_CLIENT_CONFIG,
        )
    return _DEFAULT_POLLY_CLIENT


def _get_default_s3_client() -> Any:
    """Return the lazily-created module-level S3 client."""
    global _DEFAULT_S3_CLIENT
    if _DEFAULT_S3_CLIENT is None:
        _DEFAULT_S3_CLIENT = boto3.client(
            "s3",
            config=_DEFAULT_CLIENT_CONFIG,
        )
    return _DEFAULT_S3_CLIENT


# ---------------------------------------------------------------------------
# Public re-presign helper (used by handler GET /v1/jokes/{id})
# ---------------------------------------------------------------------------


def presign_audio_url(
    bucket: str,
    key: str,
    *,
    s3_client: Optional[Any] = None,
    expires_in: int = PRESIGN_EXPIRY_SECONDS,
    download_generation_id: Optional[str] = None,
) -> Optional[str]:
    """Re-presign an S3 audio object key for the audit-replay endpoint.

    Used by ``GET /v1/jokes/{id}`` to mint a fresh 15-minute presigned
    URL from the canonical ``s3://<bucket>/<key>`` reference stored by
    :func:`joke_api.joke_store.persist`. The handler must NOT return
    the raw S3 URI to the visitor (R18.3); this helper centralizes the
    re-presign so handler code does not have to import the lazy
    private ``_get_default_s3_client`` symbol.

    Soft-failure: any exception from ``generate_presigned_url`` --
    bad credentials, transport error, malformed bucket/key -- is
    swallowed and ``None`` is returned. The handler maps ``None``
    back to ``audio_available=False`` so the visitor still gets the
    joke text on the retrieval response (R18.3 boundary).

    Args:
        bucket: S3 bucket name. Must be a non-empty string.
        key: S3 object key. Must be a non-empty string.
        s3_client: Optional pre-built boto3 ``s3`` client (test seam).
            When ``None``, the lazily-cached module-level client is
            used.
        expires_in: Presigned URL lifetime in seconds. Defaults to
            :data:`PRESIGN_EXPIRY_SECONDS` (900 == 15 minutes,
            R2.4 / Property 7).

    Returns:
        A presigned ``GET`` URL string on success, or ``None`` on any
        validation or transport error.
    """
    if not isinstance(bucket, str) or bucket == "":
        return None
    if not isinstance(key, str) or key == "":
        return None
    if not isinstance(expires_in, int) or expires_in <= 0:
        return None

    s3 = s3_client if s3_client is not None else _get_default_s3_client()
    params: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if download_generation_id is not None:
        # Download variant (R2.10): force an attachment disposition
        # with the friendly dad-joke-<id>.mp3 filename.
        params["ResponseContentDisposition"] = download_disposition(
            download_generation_id
        )
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in,
        )
    except (BotoCoreError, ClientError, Exception):  # noqa: BLE001
        return None

    if not isinstance(url, str) or url == "":
        return None
    return url
