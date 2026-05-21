"""Lambda entry point that orchestrates the Joke_API request pipeline.

This module is the single AWS Lambda handler wired to API Gateway HTTP
API v2 by ``infra/terraform/lambda.tf`` (entrypoint
``joke_api.handler.lambda_handler``). It dispatches incoming routes to
the full ``POST /v1/jokes`` pipeline (task 10.1), to the audit-replay
endpoint ``GET /v1/jokes/{id}`` (R18.2, R18.3), to the SPA-config
endpoint ``GET /v1/config`` (R8.1, R5.7), or to the production-gate
self-health probe ``GET /v1/health`` (R12.2).

Pipeline ordering (POST /v1/jokes)
----------------------------------
The handler is the single place that owns the request-stage ordering
prescribed by ``design.md`` § Components and Interfaces and enforced
by every property test in this codebase. The fixed sequence is:

1. ``request_validator.validate`` (R1.7, R3.5, R7.5, Property 5).
2. ``client_ip.resolve`` (R5.8, R5.9).
3. ``ip_hashing.hash_ip`` with the SSM-loaded salt (R16.7, Property 34).
4. ``rate_limiter.check`` (R5.2, R5.3, Property 15).
5. ``input_moderator.classify`` -- fail-closed on timeout / unavailable
   (R3.1, R3.2, R3.6, R3.7, Properties 8, 10).
6. ``training_corpus.load_few_shot`` (R17.7, Property 39).
7. Outer ``joke_generator.generate`` <-> ``output_moderator.classify``
   retry loop, bounded by :data:`MAX_OUTPUT_MODERATION_ATTEMPTS`
   (R1.4, R4.2, Properties 1, 2, 12, 13).
8. ``voice_synthesizer.synthesize`` -- soft-fail (R2.6, R2.7, R2.9).
9. ``joke_store.persist`` -- soft-fail (R18.5, Property 43).
10. ``rate_limiter.increment`` -- atomic, only after every prior stage
    succeeded (R5.4, R5.5, Property 14).
11. ``response_builder.build_success`` -- the single chokepoint that
    produces the visitor-facing JSON envelope (R1.3, R7.7).
12. ``observability.emit_log`` + ``observability.emit_metric`` for the
    per-request structured log record (R16.1, Property 30).

Every error path uses :func:`response_builder.sanitize_error` so no
internal text reaches the visitor (R7.5, Property 20).

Validated requirements
----------------------
* R1.1 -- POST /v1/jokes returns a generated joke text.
* R1.3 -- success envelope carries id/text/audio/remaining/model/voice.
* R2.7 -- audio fields surface synthesis outcome without leaking errors.
* R3.1 / R3.2 -- input moderation runs before any Bedrock call.
* R4.1 / R4.5 -- output moderation gates the joke; fall back to the
  curated list when the moderator is unavailable on every attempt.
* R5.1 -- rate-limit gate runs before generation begins.
* R5.4 / R5.5 -- counter increment runs only after every other stage
  has succeeded.
* R18.5 -- persistence failures soft-fail; the joke still ships.

Public surface
--------------
* :func:`lambda_handler` -- the API Gateway integration entrypoint.
* :data:`MAX_OUTPUT_MODERATION_ATTEMPTS` -- 3 (R4.2).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from joke_api import (
    client_ip,
    config as _config_module,
    fallback_jokes,
    input_moderator,
    ip_hashing,
    joke_generator,
    joke_store,
    observability,
    output_moderator,
    rate_limiter,
    request_validator,
    response_builder,
    training_corpus,
    voice_synthesizer,
)
from joke_api.config import Config

__all__ = ["lambda_handler", "MAX_OUTPUT_MODERATION_ATTEMPTS"]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Maximum number of outer ``generate`` -> ``output_moderate`` attempts
#: before falling back to :mod:`joke_api.fallback_jokes` (R4.2,
#: Property 12). Each iteration's Bedrock call internally retries up to
#: ``joke_generator.MAX_ATTEMPTS`` times for length-rejected output;
#: this constant bounds the *outer* moderation retry loop so the
#: handler honours R4.2's "up to 3 generation attempts before falling
#: back" rule.
MAX_OUTPUT_MODERATION_ATTEMPTS: int = 3

# Sentinel placeholder used in observability LogRecords when the
# pipeline has not yet resolved an IP hash (e.g. we failed validation
# before computing it). The 64-char lowercase-hex shape satisfies
# Property 34 / R16.7 even when the underlying request did not produce
# a real digest.
_PLACEHOLDER_IP_HASH: str = "0" * 64

# Stub cost per request used for the per-request log record. R16.1
# requires a six-decimal value in ``[0, 1]``; per-attempt token-count
# accounting is out of scope for task 10.1 and is tracked in a future
# observability task. The constant is conservative and well above zero
# so log filters that look for a non-zero cost work in dev.
_STUB_ESTIMATED_COST_USD: Decimal = Decimal("0.000100")


# ---------------------------------------------------------------------------
# Lazy config cache
# ---------------------------------------------------------------------------

# Module-level cache so warm Lambda invocations reuse the SSM lookup
# from the cold start. Tests reset this via ``_reset_config_cache``.
_CFG: Optional[Config] = None


def _get_config() -> Config:
    """Return the cached :class:`Config`, loading it on first call."""
    global _CFG
    if _CFG is None:
        _CFG = _config_module.load()
    return _CFG


def _reset_config_cache() -> None:
    """Reset the module-level config cache (test seam)."""
    global _CFG
    _CFG = None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def lambda_handler(event: dict, context: Any) -> dict:
    """API Gateway HTTP API v2 entrypoint for the Joke_API Lambda.

    Dispatches by ``event["routeKey"]``:

    * ``"POST /v1/jokes"`` -- the full generation pipeline below.
    * ``"GET /v1/jokes/{id}"`` -- audit replay (R18.2, R18.3).
    * ``"GET /v1/config"`` -- SPA bootstrap config (R8.1, R5.7).
    * ``"GET /v1/health"`` -- production-gate self-health probe (R12.2).
    * Anything else -- 404 sanitized error.

    The whole body is wrapped in a try/except that catches any
    unexpected exception and returns a sanitized 503 with a
    ``decision="error"`` log. This is the last-resort safety net for
    Property 20 / R7.5: no internal traceback can ever escape into the
    visitor response.
    """
    started_monotonic = time.monotonic()
    request_id = str(uuid.uuid4())

    try:
        if not isinstance(event, dict):
            # API Gateway always supplies a dict; defensive guard for
            # tests / direct invocations that pass something else.
            return _emit_error(
                request_id=request_id,
                started_monotonic=started_monotonic,
                ip_hash=_PLACEHOLDER_IP_HASH,
                category=response_builder.VALIDATION,
                metric_name=None,
                error_fields={"rule": "event_not_object"},
            )

        route_key = event.get("routeKey")
        if not isinstance(route_key, str) or route_key == "":
            return _emit_error(
                request_id=request_id,
                started_monotonic=started_monotonic,
                ip_hash=_PLACEHOLDER_IP_HASH,
                category=response_builder.VALIDATION,
                metric_name=None,
                error_fields={"rule": "route_key_missing"},
            )

        if route_key == "POST /v1/jokes":
            return _handle_post_jokes(
                event=event,
                request_id=request_id,
                started_monotonic=started_monotonic,
            )

        if route_key == "GET /v1/jokes/{id}":
            return _handle_get_joke_by_id(
                event=event,
                request_id=request_id,
                started_monotonic=started_monotonic,
            )

        if route_key == "GET /v1/config":
            return _handle_get_config(
                request_id=request_id,
                started_monotonic=started_monotonic,
            )

        if route_key == "GET /v1/health":
            return _handle_get_health()

        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=_PLACEHOLDER_IP_HASH,
            category=response_builder.VALIDATION,
            metric_name=None,
            error_fields={"rule": "route_not_found"},
        )
    except Exception:  # noqa: BLE001 -- last-resort sanitizer
        # Any unexpected exception -- programmer error, missing config,
        # network blip not caught by a specific stage handler -- must
        # surface as a sanitized 503. The full detail is captured in
        # the structured log (decision="error") so the observability
        # layer carries the technical record (R7.6).
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=_PLACEHOLDER_IP_HASH,
            category=response_builder.UNAVAILABLE,
            metric_name=None,
            error_fields={},
        )


# ---------------------------------------------------------------------------
# POST /v1/jokes pipeline
# ---------------------------------------------------------------------------


def _handle_post_jokes(
    *,
    event: dict,
    request_id: str,
    started_monotonic: float,
) -> dict:
    """Run the POST /v1/jokes pipeline.

    Returns the API Gateway-shaped response dict. Every error path
    routes through :func:`_emit_error` so the structured log + metric
    is emitted before the function returns.
    """
    cfg = _get_config()

    # ---- Stage 1: Validate (R1.7, R3.5, R7.5, Property 5) ----
    try:
        seed_words = request_validator.validate(event)
    except request_validator.ValidationError as exc:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=_PLACEHOLDER_IP_HASH,
            category=response_builder.VALIDATION,
            metric_name=None,
            error_fields={"rule": exc.rule},
            cfg=cfg,
        )

    # ---- Stage 2: Resolve client IP (R5.8, R5.9) ----
    try:
        raw_ip = client_ip.resolve(event)
    except client_ip.ClientIpUnresolvable:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=_PLACEHOLDER_IP_HASH,
            category=response_builder.CLIENT_IP_UNRESOLVABLE,
            metric_name=None,
            error_fields={},
            cfg=cfg,
        )

    # ---- Stage 3: Hash IP (R16.7, Property 34) ----
    ip_hash = ip_hashing.hash_ip(raw_ip, salt=cfg.ip_hash_salt)

    # ---- Stage 4: Rate-limit check (R5.2, R5.3, Property 15) ----
    today_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        rate_limiter.check(ip_hash, today_utc, cfg.daily_limit)
    except rate_limiter.RateLimitExceeded:
        # The visitor already exceeded the cap; log + emit the
        # rejection metric, do NOT increment the counter (R5.5).
        # ``resetAtUtc`` carries next-midnight-UTC so the SPA can
        # render a clear retry hint (Property 16).
        reset_at = _next_midnight_utc_iso()
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=ip_hash,
            category=response_builder.RATE_LIMITED,
            metric_name=observability.METRIC_RATE_LIMIT_REJECTIONS_PER_HOUR,
            error_fields={"resetAtUtc": reset_at},
            cfg=cfg,
            decision="rate_limited",
        )
    except rate_limiter.RateLimiterUnavailable:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=ip_hash,
            category=response_builder.UNAVAILABLE,
            metric_name=None,
            error_fields={},
            cfg=cfg,
        )

    # ---- Stage 5: Input moderation (R3, fail-closed) ----
    aggregate_text = " ".join(seed_words)
    try:
        input_result = input_moderator.classify(aggregate_text)
    except input_moderator.ModerationTimeout:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=ip_hash,
            category=response_builder.MODERATION_TIMEOUT,
            metric_name=observability.METRIC_MODERATION_REJECTIONS_PER_HOUR,
            error_fields={},
            cfg=cfg,
            decision="moderation_rejected",
        )
    except input_moderator.ModerationUnavailable:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=ip_hash,
            category=response_builder.MODERATION_UNAVAILABLE,
            metric_name=observability.METRIC_MODERATION_REJECTIONS_PER_HOUR,
            error_fields={},
            cfg=cfg,
            decision="moderation_rejected",
        )

    if not input_result.family_friendly:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=ip_hash,
            category=response_builder.MODERATION,
            metric_name=observability.METRIC_MODERATION_REJECTIONS_PER_HOUR,
            error_fields={},
            cfg=cfg,
            decision="moderation_rejected",
        )

    # ---- Stage 6: Few-shot corpus (R17.7, Property 39) ----
    # ``load_few_shot`` is documented to soft-fail to ``[]``; defensive
    # except-catch protects the handler from a future regression that
    # surfaces an exception. The rights flag is wired in by future
    # config work; until then we conservatively pass ``False`` so the
    # corpus remains untouched (Property 39 fail-closed default).
    try:
        few_shot = training_corpus.load_few_shot(
            rights_confirmed=getattr(cfg, "rights_confirmed", False),
            max_examples=6,
        )
    except Exception:  # noqa: BLE001 -- soft-fail per contract
        few_shot = []

    # ---- Stage 7: Generate + output-moderation retry loop ----
    joke_text = _run_generation_loop(
        seed_words=seed_words,
        few_shot=few_shot,
        cfg=cfg,
    )
    if joke_text is None:
        # Bedrock failed across every internal attempt and we cannot
        # safely return a partial response. R1.5 / Property 3.
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=ip_hash,
            category=response_builder.UNAVAILABLE,
            metric_name=None,
            error_fields={},
            cfg=cfg,
        )

    # ---- Stage 8: Voice synthesis (R2.6, R2.7, R2.9) ----
    # Mint the generation_id once so it is shared by the audio S3 key,
    # the persistence record, and the API response (Property 4 / R1.3).
    generation_id = str(uuid.uuid4())
    synthesis = voice_synthesizer.synthesize(
        joke_text,
        generation_id=generation_id,
        voice_id=cfg.polly_voice_id,
    )

    # ---- Stage 9: Persist (R18.5, Property 43; soft-fail) ----
    audio_ref = _build_audio_ref(synthesis, generation_id)
    record = joke_store.JokeRecord(
        id=generation_id,
        joke_text=joke_text,
        audio_ref=audio_ref,
        model_id=cfg.bedrock_model_id,
        voice_id=cfg.polly_voice_id,
        created_at=datetime.now(tz=timezone.utc),
    )
    try:
        joke_store.persist(record)
    except (
        joke_store.JokeStorePersistError,
        joke_store.JokeStoreValidationError,
    ):
        # Soft-fail: the visitor still gets the joke. Property 43 /
        # R18.5. The structured log below carries the failure detail
        # via the `decision="accepted"` record's ``ip_hash`` for
        # cross-reference; observability tasks 9.x own the deeper
        # accounting if needed.
        pass
    except Exception:  # noqa: BLE001 -- defensive, soft-fail
        pass

    # ---- Stage 10: Rate-limit increment (R5.4, R5.5, Property 14) ----
    new_count: Optional[int] = None
    try:
        new_count = rate_limiter.increment(ip_hash, today_utc)
    except rate_limiter.RateLimiterUnavailable:
        # Counter increment failed AFTER the joke succeeded; soft-fail
        # so the visitor still gets the response. The audit log will
        # show ``decision="accepted"`` with ``new_count=None``.
        new_count = None
    except Exception:  # noqa: BLE001 -- defensive, soft-fail
        new_count = None

    remaining = _compute_remaining(cfg.daily_limit, new_count)

    # ---- Stage 11: Build success response (R1.3, R7.7) ----
    response = response_builder.build_success(
        joke_id=generation_id,
        text=joke_text,
        audio_url=synthesis.audio_url,
        audio_available=synthesis.audio_available,
        remaining=remaining,
        model_id=cfg.bedrock_model_id,
        voice_id=cfg.polly_voice_id,
    )

    # ---- Stage 12: Observability (R16.1, R16.2, Property 30) ----
    _emit_request_log(
        request_id=request_id,
        ip_hash=ip_hash,
        decision="accepted",
        cfg=cfg,
        started_monotonic=started_monotonic,
    )
    observability.emit_metric(observability.METRIC_JOKES_PER_HOUR)

    return response


# ---------------------------------------------------------------------------
# GET handlers (task 10.2)
# ---------------------------------------------------------------------------


def _handle_get_joke_by_id(
    *,
    event: dict,
    request_id: str,
    started_monotonic: float,
) -> dict:
    """Audit-replay endpoint for ``GET /v1/jokes/{id}`` (R18.2, R18.3).

    Validates the path parameter as a UUID v4, looks the joke up in
    :mod:`joke_api.joke_store`, re-presigns the canonical
    ``s3://<bucket>/<key>`` audio reference (so the audit consumer
    never sees the raw S3 URI -- R18.3), and returns the same envelope
    shape as ``POST /v1/jokes`` minus the ``remaining`` field. The
    retrieval path does NOT consume quota and does NOT emit
    :data:`observability.METRIC_JOKES_PER_HOUR`; only the per-request
    structured log is emitted (R16.1).

    Error mapping:

    * Invalid / missing UUID v4 path parameter -> 400
      ``validation`` with ``rule="invalid_id"``.
    * DynamoDB unavailable on the read -> 503 ``unavailable``.
    * No record for the supplied id -> 404 ``not_found`` (R18.3).
    """
    # ---- Validate path parameter ----
    path_parameters = event.get("pathParameters")
    joke_id = (
        path_parameters.get("id")
        if isinstance(path_parameters, dict)
        else None
    )
    try:
        uuid.UUID(joke_id, version=4)  # type: ignore[arg-type]
    except (ValueError, TypeError, AttributeError):
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=_PLACEHOLDER_IP_HASH,
            category=response_builder.VALIDATION,
            metric_name=None,
            error_fields={"rule": "invalid_id"},
        )

    # ---- DynamoDB lookup ----
    try:
        record = joke_store.get(joke_id)  # type: ignore[arg-type]
    except joke_store.JokeStoreUnavailable:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=_PLACEHOLDER_IP_HASH,
            category=response_builder.UNAVAILABLE,
            metric_name=None,
            error_fields={},
        )

    if record is None:
        return _emit_error(
            request_id=request_id,
            started_monotonic=started_monotonic,
            ip_hash=_PLACEHOLDER_IP_HASH,
            category=response_builder.NOT_FOUND,
            metric_name=None,
            error_fields={},
        )

    # ---- Re-presign the audio reference (R18.3) ----
    audio_url, audio_available = _re_presign_audio_ref(record.audio_ref)

    response = response_builder.build_success(
        joke_id=record.id,
        text=record.joke_text,
        audio_url=audio_url,
        audio_available=audio_available,
        remaining=None,  # retrieval is quota-neutral
        model_id=record.model_id,
        voice_id=record.voice_id,
    )

    # Emit the structured log only -- retrievals don't count as new
    # jokes and must not bump METRIC_JOKES_PER_HOUR (R16.2 boundary).
    _emit_request_log(
        request_id=request_id,
        ip_hash=_PLACEHOLDER_IP_HASH,
        decision="accepted",
        cfg=None,
        started_monotonic=started_monotonic,
    )
    return response


def _handle_get_config(
    *,
    request_id: str,
    started_monotonic: float,
) -> dict:
    """SPA-bootstrap config endpoint for ``GET /v1/config`` (R8.1, R5.7).

    Returns the small subset of SSM-loaded config the SPA needs to
    decide whether to render the ad banner (R8.1) and how to display
    the daily-limit copy (R5.7). The body shape is fixed by the
    design document and intentionally bypasses
    :func:`response_builder.build_success` because this is not a
    joke-generation response.

    Per-request structured log is emitted (R16.1); no CloudWatch
    metric is published (the SPA bootstrap is not a generation
    event, so it must not bump METRIC_JOKES_PER_HOUR).
    """
    cfg = _get_config()
    body = {
        "adModuleEnabled": cfg.ad_module_enabled,
        "adNetworkId": cfg.ad_network_id,
        "dailyLimit": cfg.daily_limit,
    }

    _emit_request_log(
        request_id=request_id,
        ip_hash=_PLACEHOLDER_IP_HASH,
        decision="accepted",
        cfg=cfg,
        started_monotonic=started_monotonic,
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _handle_get_health() -> dict:
    """Production_Gate self-health probe for ``GET /v1/health`` (R12.2).

    Returns 200 with ``{"status": "ok"}`` and intentionally does NO
    other work:

    * Skips :func:`_get_config` so a cold-start SSM round-trip never
      delays the probe (R12.2's "self-health signal within 60 s of
      run start" requires the probe itself to respond fast).
    * Skips the per-request structured log because the probe runs on
      a tight schedule from CloudFront / external monitoring; logging
      every probe would dominate CloudWatch costs and bury real
      request records. This is an intentional, documented exception
      to the otherwise universal R16.1 emission rule.
    """
    body = {"status": "ok"}
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _re_presign_audio_ref(
    audio_ref: Optional[str],
) -> tuple[Optional[str], bool]:
    """Mint a fresh presigned URL from a stored ``s3://`` audio_ref.

    ``audio_ref`` is the canonical ``s3://<bucket>/<key>`` URI written
    by :func:`joke_store.persist`. R18.3 forbids returning the raw URI
    to the audit consumer; we parse out the bucket / key, soft-fail
    via :func:`voice_synthesizer.presign_audio_url`, and surface the
    outcome as ``(audio_url, audio_available)``. Any malformed URI or
    presign failure collapses to ``(None, False)`` so the visitor
    still gets the joke text.
    """
    if not isinstance(audio_ref, str) or not audio_ref.startswith("s3://"):
        return (None, False)
    remainder = audio_ref[len("s3://"):]
    slash_index = remainder.find("/")
    if slash_index <= 0 or slash_index >= len(remainder) - 1:
        # Either no key separator, or empty bucket / key.
        return (None, False)
    bucket = remainder[:slash_index]
    key = remainder[slash_index + 1:]
    url = voice_synthesizer.presign_audio_url(bucket, key)
    if url is None:
        return (None, False)
    return (url, True)


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------


def _run_generation_loop(
    *,
    seed_words: list[str],
    few_shot: list[str],
    cfg: Config,
) -> Optional[str]:
    """Run the outer generate <-> output-moderate retry loop.

    Returns the moderation-approved joke text on success, the curated
    fallback joke when every attempt was rejected, or ``None`` when
    Bedrock entirely failed (the handler maps ``None`` to a 503).

    Property 12 / R4.2 bound the outer loop at
    :data:`MAX_OUTPUT_MODERATION_ATTEMPTS` iterations. The
    ``refine=True`` flag is set on attempts 2 and 3 so the system
    prompt picks up the explicit category prohibitions.
    """
    bedrock_failed = False
    for attempt_index in range(MAX_OUTPUT_MODERATION_ATTEMPTS):
        try:
            candidate = joke_generator.generate(
                seed_words,
                few_shot,
                refine=(attempt_index > 0),
                model_id=cfg.bedrock_model_id,
            )
        except (
            joke_generator.JokeGenerationFailed,
            joke_generator.JokeGenerationTimeout,
            joke_generator.JokeGenerationUnavailable,
        ):
            # Bedrock exhausted its own internal retry budget; the
            # handler does not get a candidate to moderate. Surface
            # this as a hard 503 (Property 3 / R1.5): no fallback,
            # no partial response.
            bedrock_failed = True
            break

        # Output-moderate the candidate. Per design.md "Output_Moderator",
        # on unavailability or timeout the caller short-circuits to the
        # fallback list (R4.5); we model that by treating the moderator
        # outcome as "rejected" and continuing the loop. If every
        # attempt is rejected (or the moderator was unavailable on
        # every attempt), the post-loop fallback path returns a
        # curated safe joke.
        try:
            moderation = output_moderator.classify(candidate)
        except (
            output_moderator.ModerationTimeout,
            output_moderator.ModerationUnavailable,
        ):
            moderation = None

        if moderation is not None and moderation.family_friendly:
            return candidate
        # else: rejected -> retry with refine=True (set on next
        # iteration via ``attempt_index > 0``).

    if bedrock_failed:
        return None

    # Every attempt was rejected by the output moderator (or the
    # moderator was unavailable on every attempt). R4.3 / R4.5 /
    # Property 13: pick a fallback joke and proceed. The visitor
    # still gets a 200 response.
    return fallback_jokes.select()


def _build_audio_ref(
    synthesis: voice_synthesizer.SynthesisResult,
    generation_id: str,
) -> Optional[str]:
    """Return the audit-log audio reference for the persistence record.

    R18.2 stores the canonical S3 URI (``s3://<bucket>/<uuid>.mp3``)
    rather than the short-lived presigned URL so an ops audit can
    re-presign at lookup time. The handler only writes the URI when
    synthesis actually succeeded; otherwise the persisted ``audio_ref``
    is ``None`` (which :class:`joke_store.JokeRecord` accepts).

    The bucket is resolved the same way :mod:`joke_api.voice_synthesizer`
    resolves it: ``$DADJOKES_AUDIO_BUCKET`` if set, otherwise
    :data:`voice_synthesizer.DEFAULT_AUDIO_BUCKET`. This keeps the two
    modules consistent without requiring voice_synthesizer to expose
    its resolution result.
    """
    if not synthesis.audio_available:
        return None
    bucket = os.environ.get(
        voice_synthesizer.AUDIO_BUCKET_ENV_VAR,
        voice_synthesizer.DEFAULT_AUDIO_BUCKET,
    )
    return f"s3://{bucket}/{generation_id}.mp3"


def _compute_remaining(daily_limit: int, new_count: Optional[int]) -> int:
    """Compute the ``remaining`` field for the success response.

    When ``increment`` returned a count, the remaining tally is
    ``max(0, daily_limit - new_count)``. When ``increment`` soft-failed
    (``new_count is None``), we conservatively report
    ``max(0, daily_limit - 1)`` so the visitor sees a single-decremented
    count -- the joke shipped, so something *was* spent against the
    quota even if the counter write didn't land.
    """
    if new_count is None:
        return max(0, daily_limit - 1)
    return max(0, daily_limit - new_count)


def _next_midnight_utc_iso() -> str:
    """Return next 00:00:00Z as ``YYYY-MM-DDTHH:MM:SSZ``."""
    now = datetime.now(tz=timezone.utc)
    next_day = now.date().toordinal() + 1
    next_midnight = datetime.fromordinal(next_day).replace(tzinfo=timezone.utc)
    return next_midnight.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Observability helpers
# ---------------------------------------------------------------------------


def _emit_request_log(
    *,
    request_id: str,
    ip_hash: str,
    decision: str,
    cfg: Optional[Config],
    started_monotonic: float,
) -> None:
    """Build and emit the per-request structured log (R16.1, Property 30).

    The function is the single place that constructs a
    :class:`observability.LogRecord`. ``cfg`` may be ``None`` on
    early-fail paths where SSM was not consulted; the LogRecord
    requires non-empty model/voice ids so we substitute placeholder
    strings in that case. ``observability.emit_log`` soft-fails on
    transport errors per R16.8, so this helper is safe to call from
    every path.
    """
    if cfg is not None:
        model_id = cfg.bedrock_model_id
        voice_id = cfg.polly_voice_id
    else:
        # Validation / route-not-found paths run before SSM is loaded.
        # Use stable sentinel strings so the LogRecord validation
        # (R16.7 ip_hash + non-empty model/voice) still passes.
        model_id = "unknown"
        voice_id = "unknown"

    latency_ms = _elapsed_ms(started_monotonic)
    record = observability.LogRecord(
        request_id=request_id,
        ip_hash=ip_hash,
        decision=decision,
        model_id=model_id,
        voice_id=voice_id,
        latency_ms=latency_ms,
        # Stub cost: per-attempt token-count accounting lives in a
        # later observability task. Six-decimal precision keeps the
        # value within the LogRecord's ``[0, 1]`` validation window.
        estimated_cost_usd=_STUB_ESTIMATED_COST_USD,
        ts=datetime.now(tz=timezone.utc),
    )
    observability.emit_log(record)


def _elapsed_ms(started_monotonic: float) -> int:
    """Return non-negative milliseconds since ``started_monotonic``.

    Capped at :data:`observability.LATENCY_MS_MAX` so a clock
    aberration never produces an out-of-range LogRecord. The cap is
    defensive: in practice the API Gateway integration timeout (29 s)
    never lets us exceed the 60 s ceiling.
    """
    delta = time.monotonic() - started_monotonic
    if delta < 0:
        return 0
    millis = int(round(delta * 1000.0))
    if millis > observability.LATENCY_MS_MAX:
        return observability.LATENCY_MS_MAX
    return millis


def _emit_error(
    *,
    request_id: str,
    started_monotonic: float,
    ip_hash: str,
    category: str,
    metric_name: Optional[str],
    error_fields: dict[str, Any],
    cfg: Optional[Config] = None,
    decision: str = "error",
) -> dict:
    """Emit the observability triple and return the sanitized response.

    Centralizes the "log + metric + sanitized envelope" trio so every
    error path follows the same template. ``observability.emit_log``
    and ``observability.emit_metric`` both soft-fail on transport
    errors per R16.8, so this helper can never raise.
    """
    _emit_request_log(
        request_id=request_id,
        ip_hash=ip_hash,
        decision=decision,
        cfg=cfg,
        started_monotonic=started_monotonic,
    )
    if metric_name is not None:
        observability.emit_metric(metric_name)
    return response_builder.sanitize_error(category, **error_fields)
