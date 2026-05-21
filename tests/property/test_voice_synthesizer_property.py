"""Property tests for :mod:`joke_api.voice_synthesizer`.

Implements the joke-API-side half of two correctness properties from
``design.md`` § Correctness Properties:

* **Property 6: Audio availability mirrors Polly outcome.** *For any*
  Polly outcome (success, transient error, timeout, or skipped because
  joke text length is outside ``[1, 1500]``), the response SHALL
  contain joke text, the ``audioAvailable`` boolean SHALL be ``true``
  iff Polly succeeded, and ``audioUrl`` SHALL be present iff
  ``audioAvailable`` is ``true``.
* **Property 7: Presigned audio URLs are valid for at least 15
  minutes.** *For any* generation that produced audio, the presigned
  GET URL returned to the client SHALL have an ``X-Amz-Expires``
  value greater than or equal to 900 seconds.

**Validates: Requirements 2.1, 2.3, 2.4, 2.6, 2.7, 2.9**

Boundary
--------
``voice_synthesizer.synthesize`` extends Property 6 slightly: success
requires Polly *and* S3 ``put_object`` *and* ``generate_presigned_url``
to all succeed. We assert the strict contract here: ``audio_available``
is ``True`` iff every step in that chain succeeded; on any failure
``audio_url is None`` and ``error`` is one of the stable labels
documented in :mod:`joke_api.voice_synthesizer`. The runtime
"playable for 15 minutes" guarantee is bounded by
:data:`voice_synthesizer.PRESIGN_EXPIRY_SECONDS == 900`; the actual
S3-side enforcement of URL validity is integration-scope and lives
outside this property file.

Stub design
-----------
The Polly and S3 backends are replaced with hand-rolled stubs (NOT
``MagicMock``) following the pattern used in
``tests/property/test_joke_generator_property.py``. Each stub
captures every call's keyword arguments and follows a per-mode
behavior spec so a single hypothesis strategy can drive every
soft-fail branch (Polly timeout, Polly unavailable, Polly empty
audio, S3 upload failure, presign failure) along with the success
path. A NEW stub instance is built per Hypothesis example so call
counters do not leak between iterations.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from joke_api import voice_synthesizer
from joke_api.voice_synthesizer import (
    MAX_TEXT_LEN,
    MIN_TEXT_LEN,
    PRESIGN_EXPIRY_SECONDS,
    SynthesisResult,
    synthesize,
)


# ---------------------------------------------------------------------------
# Test-only constants
# ---------------------------------------------------------------------------

# Stable values used in every test so :func:`joke_api.config.load`
# is never invoked (avoids any SSM I/O in unit-level tests).
_TEST_VOICE_ID: str = "Joanna"
_TEST_BUCKET: str = "dadjokes-test-audio"
_TEST_GENERATION_ID: str = "00000000-0000-4000-8000-000000000000"

# Per-call outcome labels driving the stubs' behavior.
MODE_SUCCESS: str = "success"
MODE_POLLY_TIMEOUT: str = "polly_timeout"
MODE_POLLY_UNAVAILABLE: str = "polly_unavailable"
MODE_POLLY_EMPTY: str = "polly_empty"
MODE_S3_UPLOAD_FAILED: str = "s3_upload_failed"
MODE_PRESIGN_FAILED: str = "presign_failed"

# Mapping from mode label to the stable error label the implementation
# is contracted to surface for that mode (R2.6 / Property 6).
_EXPECTED_ERROR_LABEL: dict[str, str] = {
    MODE_POLLY_TIMEOUT: "polly_timeout",
    MODE_POLLY_UNAVAILABLE: "polly_unavailable",
    MODE_POLLY_EMPTY: "polly_empty_audio",
    MODE_S3_UPLOAD_FAILED: "s3_upload_failed",
    MODE_PRESIGN_FAILED: "presign_failed",
}

# Modes that exercise a *non-success* in-range path (i.e. text length
# is in [1, 1500] but the chain fails somewhere). Excludes
# MODE_SUCCESS and MODE_POLLY_TIMEOUT -- the timeout mode runs in a
# separate test with a reduced max_examples and a monkey-patched
# budget so the per-example sleep stays bounded.
_FAST_FAILURE_MODES: tuple[str, ...] = (
    MODE_POLLY_UNAVAILABLE,
    MODE_POLLY_EMPTY,
    MODE_S3_UPLOAD_FAILED,
    MODE_PRESIGN_FAILED,
)


# ---------------------------------------------------------------------------
# Hand-rolled stubs
# ---------------------------------------------------------------------------


class _StreamStub:
    """Minimal stand-in for ``botocore.response.StreamingBody``.

    Exposes only the ``.read()`` method that
    :func:`joke_api.voice_synthesizer._call_polly` consumes.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _PollyStub:
    """Hand-rolled Polly client stub for voice_synthesizer property tests.

    Exposes only the surface the synthesizer uses
    (:py:meth:`synthesize_speech`) and tracks every call's keyword
    arguments so tests can introspect what was sent without
    re-implementing the call shape. ``mode`` selects per-call
    behavior:

    * ``success`` / ``polly_empty`` / ``s3_upload_failed`` /
      ``presign_failed`` -- return a ``{"AudioStream": _StreamStub(...)}``
      dict; the audio bytes are empty for ``polly_empty`` and the
      configured payload otherwise.
    * ``polly_timeout`` -- sleep 1 s before returning. Pair with
      ``monkeypatch.setattr(voice_synthesizer, "POLLY_BUDGET_MS", 200)``
      so the executor's wall-clock timeout fires.
    * ``polly_unavailable`` -- raise ``ClientError``.
    """

    __slots__ = ("mode", "audio_bytes", "calls")

    def __init__(self, mode: str, audio_bytes: bytes = b"\x00" * 256) -> None:
        self.mode: str = mode
        self.audio_bytes: bytes = audio_bytes
        self.calls: list[dict[str, Any]] = []

    def synthesize_speech(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.mode == MODE_POLLY_TIMEOUT:
            # Sleep > the monkey-patched budget so the future times
            # out. 1 s is comfortably above the 200 ms test budget
            # while keeping per-test wall-clock manageable.
            time.sleep(1.0)
        if self.mode == MODE_POLLY_UNAVAILABLE:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "x"}},
                "SynthesizeSpeech",
            )
        stream_bytes = b"" if self.mode == MODE_POLLY_EMPTY else self.audio_bytes
        return {"AudioStream": _StreamStub(stream_bytes)}


class _S3Stub:
    """Hand-rolled S3 client stub for voice_synthesizer property tests.

    Exposes only the two methods the synthesizer uses
    (:py:meth:`put_object` and :py:meth:`generate_presigned_url`) and
    tracks every call. ``mode`` selects per-call behavior:

    * ``s3_upload_failed`` -- ``put_object`` raises ``ClientError``.
    * ``presign_failed`` -- ``generate_presigned_url`` raises a
      generic ``RuntimeError``.
    * any other mode -- both methods succeed; the presigned URL is
      synthesized as
      ``https://s3.amazonaws.com/<bucket>/<key>?X-Amz-Expires=<n>``
      so Property 7 can parse the expiry from the URL itself.
    """

    __slots__ = ("mode", "put_calls", "presign_calls")

    def __init__(self, mode: str) -> None:
        self.mode: str = mode
        self.put_calls: list[dict[str, Any]] = []
        self.presign_calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.put_calls.append(kwargs)
        if self.mode == MODE_S3_UPLOAD_FAILED:
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "x"}},
                "PutObject",
            )
        return {}

    def generate_presigned_url(
        self, ClientMethod: str, **kwargs: Any
    ) -> str:
        self.presign_calls.append({"ClientMethod": ClientMethod, **kwargs})
        if self.mode == MODE_PRESIGN_FAILED:
            raise RuntimeError("presign failed")
        expires = kwargs.get("ExpiresIn", 0)
        params = kwargs.get("Params", {})
        bucket = params.get("Bucket", "")
        key = params.get("Key", "")
        return (
            f"https://s3.amazonaws.com/{bucket}/{key}"
            f"?X-Amz-Expires={expires}"
        )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# UTF-8 text excluding surrogate pairs and embedded NULs (the same
# constraint the joke_store property tests use). ``min_size=0``
# covers the empty-input branch; ``max_size=2000`` covers both
# in-range (1..1500) and out-of-range (>1500) values.
_safe_text_chars = st.characters(
    blacklist_categories=("Cs",),
    blacklist_characters="\x00",
)
_text_strategy = st.text(
    alphabet=_safe_text_chars,
    min_size=0,
    max_size=2000,
)

# In-range text strictly within [MIN_TEXT_LEN, MAX_TEXT_LEN] for the
# success / Property 7 paths.
_in_range_text_strategy = st.text(
    alphabet=_safe_text_chars,
    min_size=MIN_TEXT_LEN,
    max_size=MAX_TEXT_LEN,
)

# Fast-failure mode strategy used by the cross-cutting property; the
# timeout mode lives in a separate test with reduced examples.
_fast_failure_mode_strategy = st.sampled_from(_FAST_FAILURE_MODES)
_fast_mode_strategy = st.one_of(
    st.just(MODE_SUCCESS),
    _fast_failure_mode_strategy,
)


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

PBT_SETTINGS_TIMEOUT = settings(
    # Each timeout example sleeps 1 s, so keep this small.
    max_examples=10,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.large_base_example,
        # ``monkeypatch`` is function-scoped; hypothesis warns by
        # default that the fixture is not reset between examples.
        # The patch we apply (POLLY_BUDGET_MS = 200) is a constant
        # for the whole test, so the cross-example reuse is
        # intentional.
        HealthCheck.function_scoped_fixture,
    ],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_synthesize(
    text: str,
    *,
    polly_stub: _PollyStub,
    s3_stub: _S3Stub,
) -> SynthesisResult:
    """Invoke ``synthesize`` with the test stubs and stable inputs."""
    return synthesize(
        text,
        generation_id=_TEST_GENERATION_ID,
        voice_id=_TEST_VOICE_ID,
        audio_bucket=_TEST_BUCKET,
        polly_client=polly_stub,
        s3_client=s3_stub,
    )


# ---------------------------------------------------------------------------
# Property 6: audio availability mirrors Polly+S3+presign outcome
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(text=_text_strategy, mode=_fast_mode_strategy)
def test_property_6_outcome_drives_audio_available_and_url(
    text: str, mode: str
) -> None:
    """Property 6 (fast modes): audio_available iff full chain succeeded.

    For every (text, mode) combination across the fast-execution
    modes (success + four immediate failures, excluding the
    sleeping timeout mode), assert:

    * Length out of [1, 1500] short-circuits to
      ``error == "text_length_out_of_range"`` with no Polly call.
    * In-range + success → ``audio_available`` is True, ``audio_url``
      is non-None, ``error`` is None, and Polly was called once.
    * In-range + any failure → ``audio_available`` is False,
      ``audio_url`` is None, ``error`` matches the expected stable
      label.

    **Validates: Requirements 2.1, 2.6, 2.9** (Property 6).
    """
    polly_stub = _PollyStub(mode)
    s3_stub = _S3Stub(mode)

    result = _call_synthesize(text, polly_stub=polly_stub, s3_stub=s3_stub)
    assert isinstance(result, SynthesisResult)

    # ---- Out-of-range branch (R2.9) ---------------------------------------
    if not (MIN_TEXT_LEN <= len(text) <= MAX_TEXT_LEN):
        assert result.error == "text_length_out_of_range", (
            f"out-of-range text (len={len(text)}) should produce "
            f"text_length_out_of_range, got {result!r}"
        )
        assert result.audio_available is False
        assert result.audio_url is None
        # No Polly invocation, no S3 invocation.
        assert polly_stub.calls == [], (
            f"Polly was called {len(polly_stub.calls)} time(s) for "
            f"out-of-range text; the length gate must short-circuit."
        )
        assert s3_stub.put_calls == []
        assert s3_stub.presign_calls == []
        return

    # ---- In-range branch --------------------------------------------------
    if mode == MODE_SUCCESS:
        assert result.audio_available is True, (
            f"success mode must produce audio_available=True, got {result!r}"
        )
        assert result.audio_url is not None
        assert result.error is None
        assert len(polly_stub.calls) == 1
        assert len(s3_stub.put_calls) == 1
        assert len(s3_stub.presign_calls) == 1
        return

    # Failure modes (in-range, fast-execution).
    expected_label = _EXPECTED_ERROR_LABEL[mode]
    assert result.audio_available is False, (
        f"mode={mode} must produce audio_available=False, got {result!r}"
    )
    assert result.audio_url is None
    assert result.error == expected_label, (
        f"mode={mode} expected error={expected_label!r}, got {result.error!r}"
    )


@PBT_SETTINGS_TIMEOUT
@given(text=_in_range_text_strategy)
def test_property_6_timeout_branch_returns_polly_timeout_label(
    text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 6 (timeout branch): blown budget surfaces ``polly_timeout``.

    Run separately from the fast-mode test so the per-example 1 s
    sleep is bounded to a small example count. The budget is
    monkey-patched down to 200 ms so the executor's wall-clock
    timeout fires before the stub's 1 s sleep completes.

    **Validates: Requirements 2.6** (Property 6, timeout branch).
    """
    monkeypatch.setattr(voice_synthesizer, "POLLY_BUDGET_MS", 200)
    polly_stub = _PollyStub(MODE_POLLY_TIMEOUT)
    s3_stub = _S3Stub(MODE_POLLY_TIMEOUT)

    result = _call_synthesize(text, polly_stub=polly_stub, s3_stub=s3_stub)

    assert result.audio_available is False
    assert result.audio_url is None
    assert result.error == "polly_timeout", (
        f"timeout mode expected error='polly_timeout', got {result.error!r}"
    )
    # Polly was invoked once but never produced bytes the synthesizer
    # could upload, so S3 was never touched.
    assert len(polly_stub.calls) == 1
    assert s3_stub.put_calls == []
    assert s3_stub.presign_calls == []


@PBT_SETTINGS
@given(text=_text_strategy, mode=_fast_mode_strategy)
def test_property_6_url_present_iff_audio_available(
    text: str, mode: str
) -> None:
    """Property 6 (cross-cutting biconditional): ``audio_url`` non-None
    IFF ``audio_available`` is True, across every (text, mode) pair
    in the fast-execution set.

    This is the strongest form of Property 6's audioUrl-presence
    rule; it's checked separately from the per-branch outcome test
    so a single property failure pinpoints the biconditional
    violation.

    **Validates: Requirements 2.1, 2.6** (Property 6 biconditional).
    """
    polly_stub = _PollyStub(mode)
    s3_stub = _S3Stub(mode)

    result = _call_synthesize(text, polly_stub=polly_stub, s3_stub=s3_stub)

    has_url = result.audio_url is not None
    assert has_url == result.audio_available, (
        f"Property 6 biconditional violated for (text-len={len(text)}, "
        f"mode={mode}): audio_url is "
        f"{'present' if has_url else 'None'} but audio_available is "
        f"{result.audio_available}; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Property 7: presigned URL valid for at least 15 minutes
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(text=_in_range_text_strategy)
def test_property_7_presigned_url_expires_at_least_900_seconds(
    text: str,
) -> None:
    """Property 7: every successful synthesis returns a URL whose
    ``X-Amz-Expires`` query parameter is at least 900 seconds, AND
    the captured ``ExpiresIn`` argument is at least 900 seconds.

    Both halves are asserted: the captured kwarg pins the
    implementation's intent, and the URL parse pins the
    receiver-visible contract (the URL itself is what reaches the
    browser, so a regression that synthesizes a different URL
    shape would only be caught by the URL parse).

    **Validates: Requirements 2.4** (Property 7).
    """
    polly_stub = _PollyStub(MODE_SUCCESS)
    s3_stub = _S3Stub(MODE_SUCCESS)

    result = _call_synthesize(text, polly_stub=polly_stub, s3_stub=s3_stub)

    assert result.audio_available is True
    assert result.audio_url is not None
    assert len(s3_stub.presign_calls) == 1

    # Captured ``ExpiresIn`` argument.
    presign_call = s3_stub.presign_calls[0]
    expires_in_kwarg = presign_call["ExpiresIn"]
    assert isinstance(expires_in_kwarg, int)
    assert expires_in_kwarg >= 900, (
        f"Property 7 violated: ExpiresIn kwarg was {expires_in_kwarg}, "
        f"must be >= 900 seconds (15 minutes)."
    )

    # URL-level assertion: parse the X-Amz-Expires query parameter.
    parsed = urlparse(result.audio_url)
    qs = parse_qs(parsed.query)
    expires_values = qs.get("X-Amz-Expires", [])
    assert expires_values, (
        f"Property 7 violated: presigned URL has no X-Amz-Expires "
        f"query parameter: {result.audio_url!r}"
    )
    expires_in_url = int(expires_values[0])
    assert expires_in_url >= 900, (
        f"Property 7 violated: URL X-Amz-Expires was {expires_in_url}, "
        f"must be >= 900 seconds. URL={result.audio_url!r}"
    )


def test_property_7_module_constant_is_at_least_900() -> None:
    """Property 7 (structural): :data:`PRESIGN_EXPIRY_SECONDS` >= 900.

    Catches a constant-drift regression with a single targeted
    failure rather than waiting for a hypothesis run to surface it.

    **Validates: Requirements 2.4** (Property 7 module constant).
    """
    assert PRESIGN_EXPIRY_SECONDS >= 900, (
        f"PRESIGN_EXPIRY_SECONDS must be >= 900 (15 minutes); "
        f"got {PRESIGN_EXPIRY_SECONDS}."
    )
