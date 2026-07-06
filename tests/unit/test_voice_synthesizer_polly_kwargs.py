"""Unit test asserting Polly is invoked with the exact kwargs mandated by R2.2 + R2.8.

**Validates: Requirements 2.2, 2.8.**

Distinct from task 7.2's property tests for Properties 6 and 7 — this file
focuses on the exact ``synthesize_speech`` kwargs (``OutputFormat='mp3'``,
``SampleRate='22050'``, ``Engine='standard'``, ``VoiceId`` from config) so
any drift in those constants is caught with a single targeted failure
rather than only via a property test's strategy.

Stub design mirrors task 7.2's: hand-rolled stubs (NOT ``MagicMock``) that
capture every call's keyword arguments. The test file is self-contained -
each fixture is local rather than imported from the property test module
so the unit-test surface stays independent of changes there.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from joke_api import voice_synthesizer
from joke_api.voice_synthesizer import (
    ENGINE,
    MAX_TEXT_LEN,
    MIN_TEXT_LEN,
    OUTPUT_FORMAT,
    SAMPLE_RATE,
    SynthesisResult,
    synthesize,
)


# ---------------------------------------------------------------------------
# Test-only constants
# ---------------------------------------------------------------------------

_TEST_BUCKET: str = "bucket-x"
_TEST_GENERATION_ID: str = "00000000-0000-4000-8000-000000000000"
_TEST_JOKE_TEXT: str = (
    "Why did the chicken cross the road? To get to the other side."
)


# ---------------------------------------------------------------------------
# Hand-rolled stubs
# ---------------------------------------------------------------------------


class _StreamStub:
    """Minimal stand-in for ``botocore.response.StreamingBody``."""

    __slots__ = ("_payload",)

    def __init__(self, payload: bytes = b"\x00" * 256) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _PollyStub:
    """Captures every ``synthesize_speech`` invocation; always succeeds.

    Returns a Polly-shaped ``{"AudioStream": _StreamStub(...)}`` response
    so the synthesizer reaches the S3 stage. This file's tests assert the
    *kwargs* shape, so every call needs to succeed.
    """

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def synthesize_speech(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"AudioStream": _StreamStub()}


class _S3Stub:
    """No-op S3 client stub. ``put_object`` and ``generate_presigned_url``
    succeed unconditionally so :func:`synthesize` returns
    ``audio_available=True`` and the kwargs assertion can run on a happy
    path.
    """

    __slots__ = ()

    def put_object(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def generate_presigned_url(
        self, ClientMethod: str, **kwargs: Any
    ) -> str:  # noqa: N803 (boto3 keyword)
        params = kwargs.get("Params", {})
        bucket = params.get("Bucket", "")
        key = params.get("Key", "")
        return f"https://s3.amazonaws.com/{bucket}/{key}?X-Amz-Expires=900"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_synthesize(
    *,
    polly_stub: _PollyStub,
    s3_stub: _S3Stub,
    voice_id: str | None = "Joanna",
    joke_text: str = _TEST_JOKE_TEXT,
) -> SynthesisResult:
    """Invoke ``synthesize`` with the per-test stubs and stable inputs."""
    return synthesize(
        joke_text,
        generation_id=_TEST_GENERATION_ID,
        voice_id=voice_id,
        audio_bucket=_TEST_BUCKET,
        polly_client=polly_stub,
        s3_client=s3_stub,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_polly_called_with_required_kwargs() -> None:
    """Polly's ``synthesize_speech`` is invoked with the exact kwargs
    mandated by R2.2 (``OutputFormat='mp3'``, ``SampleRate='22050'``)
    and R2.8 (``Engine='standard'``, configured ``VoiceId``).

    The test passes a deterministic single input to avoid coupling
    the kwargs assertion to any stochastic behavior.
    """
    polly_stub = _PollyStub()
    s3_stub = _S3Stub()

    result = _call_synthesize(polly_stub=polly_stub, s3_stub=s3_stub)
    assert result.audio_available is True

    assert len(polly_stub.calls) == 1, (
        f"expected exactly one Polly call, got {len(polly_stub.calls)}"
    )
    call = polly_stub.calls[0]
    assert call["OutputFormat"] == "mp3", f"got {call.get('OutputFormat')!r}"
    assert call["SampleRate"] == "22050", f"got {call.get('SampleRate')!r}"
    assert call["Engine"] == "standard", f"got {call.get('Engine')!r}"
    assert call["VoiceId"] == "Joanna", f"got {call.get('VoiceId')!r}"
    assert call["Text"] == _TEST_JOKE_TEXT, f"got {call.get('Text')!r}"


def test_polly_voice_id_resolves_from_kwarg() -> None:
    """An explicit ``voice_id`` kwarg overrides any SSM lookup.

    The :func:`joke_api.config.load` import path is intentionally
    NOT patched here -- passing an explicit ``voice_id`` short-
    circuits the SSM path inside ``_resolve_voice_id``, so a config
    lookup must not happen.
    """
    polly_stub = _PollyStub()
    s3_stub = _S3Stub()

    _call_synthesize(
        polly_stub=polly_stub,
        s3_stub=s3_stub,
        voice_id="Matthew",
    )

    assert len(polly_stub.calls) == 1
    assert polly_stub.calls[0]["VoiceId"] == "Matthew"


def test_polly_voice_id_resolves_from_config_when_kwarg_omitted() -> None:
    """When ``voice_id`` is omitted, :func:`config.load` is consulted
    and its ``polly_voice_id`` is forwarded as the ``VoiceId`` kwarg
    (R2.8 SSM-driven configuration).

    The patch targets the ``_config`` alias inside
    :mod:`joke_api.voice_synthesizer` because that's the symbol the
    module actually invokes; patching ``joke_api.config`` directly
    would leave the alias bound to the original module.
    """
    polly_stub = _PollyStub()
    s3_stub = _S3Stub()
    fake_config = SimpleNamespace(polly_voice_id="Salli")

    with patch.object(voice_synthesizer._config, "load", return_value=fake_config):
        result = synthesize(
            _TEST_JOKE_TEXT,
            generation_id=_TEST_GENERATION_ID,
            audio_bucket=_TEST_BUCKET,
            polly_client=polly_stub,
            s3_client=s3_stub,
        )

    assert result.audio_available is True
    assert len(polly_stub.calls) == 1
    assert polly_stub.calls[0]["VoiceId"] == "Salli"


def test_polly_constants_match_design() -> None:
    """Module-level structural assertion: the three Polly constants
    match the design's mandated values (R2.2, R2.8).

    Catches a silent constant edit with a single targeted failure
    rather than relying on the kwargs test to surface it indirectly.
    """
    assert OUTPUT_FORMAT == "mp3", f"got {OUTPUT_FORMAT!r}"
    assert SAMPLE_RATE == "22050", f"got {SAMPLE_RATE!r}"
    assert ENGINE == "standard", f"got {ENGINE!r}"


@pytest.mark.parametrize(
    "joke_text",
    [
        "",                                       # length 0  -> below MIN_TEXT_LEN
        "x" * (MAX_TEXT_LEN + 1),                 # length 1501 -> above MAX_TEXT_LEN
    ],
    ids=["empty", "over-1500"],
)
def test_polly_not_called_when_text_out_of_range(joke_text: str) -> None:
    """The R2.9 length gate keeps Polly untouched when ``joke_text``
    falls outside ``[MIN_TEXT_LEN, MAX_TEXT_LEN]``.

    Defensive cross-check that complements the kwargs assertion: if
    a future refactor accidentally moves the gate after the Polly
    call, this test fails immediately rather than silently spending
    money on a synthesis that gets thrown away.
    """
    polly_stub = _PollyStub()
    s3_stub = _S3Stub()

    result = synthesize(
        joke_text,
        generation_id=_TEST_GENERATION_ID,
        voice_id="Joanna",
        audio_bucket=_TEST_BUCKET,
        polly_client=polly_stub,
        s3_client=s3_stub,
    )

    assert result.audio_available is False
    assert result.audio_url is None
    assert result.error == "text_length_out_of_range"
    assert polly_stub.calls == [], (
        f"R2.9 violated: Polly was invoked {len(polly_stub.calls)} time(s) "
        f"for out-of-range text (len={len(joke_text)})."
    )


# ---------------------------------------------------------------------------
# R2.10 / Property 45: download URL variant
# ---------------------------------------------------------------------------


class _RecordingS3Stub:
    """S3 stub that records every presign call and can be told to fail
    the Nth presign, so the download-only-failure degradation path can
    be exercised independently of the playback presign."""

    __slots__ = ("presign_calls", "fail_on_call")

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.presign_calls: list[dict[str, Any]] = []
        # 1-indexed call number to fail on; None = never fail.
        self.fail_on_call = fail_on_call

    def put_object(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def generate_presigned_url(
        self, ClientMethod: str, **kwargs: Any
    ) -> str:  # noqa: N803 (boto3 keyword)
        self.presign_calls.append({"ClientMethod": ClientMethod, **kwargs})
        if self.fail_on_call is not None and len(self.presign_calls) == self.fail_on_call:
            raise RuntimeError("presign failed")
        params = kwargs.get("Params", {})
        bucket = params.get("Bucket", "")
        key = params.get("Key", "")
        disp = params.get("ResponseContentDisposition", "")
        return (
            f"https://s3.amazonaws.com/{bucket}/{key}"
            f"?X-Amz-Expires=900&disp={disp}"
        )


def test_success_returns_download_url_with_attachment_disposition() -> None:
    """R2.10 / Property 45: a successful synthesis returns a distinct
    download URL, and exactly one of the two presign calls carries a
    ``Content-Disposition: attachment; filename="dad-joke-<id>.mp3"``
    override.
    """
    polly_stub = _PollyStub()
    s3_stub = _RecordingS3Stub()

    result = synthesize(
        _TEST_JOKE_TEXT,
        generation_id=_TEST_GENERATION_ID,
        voice_id="Matthew",
        audio_bucket=_TEST_BUCKET,
        polly_client=polly_stub,
        s3_client=s3_stub,
    )

    assert result.audio_available is True
    assert result.audio_url is not None
    assert result.audio_download_url is not None
    assert result.audio_url != result.audio_download_url

    # Two presigns: one plain playback, one download with disposition.
    assert len(s3_stub.presign_calls) == 2
    dispositions = [
        c["Params"].get("ResponseContentDisposition")
        for c in s3_stub.presign_calls
    ]
    non_null = [d for d in dispositions if d]
    assert len(non_null) == 1
    assert non_null[0] == (
        f'attachment; filename="dad-joke-{_TEST_GENERATION_ID}.mp3"'
    )


def test_download_presign_failure_degrades_without_breaking_playback() -> None:
    """When only the download presign fails, playback is unaffected:
    ``audio_available`` stays True, ``audio_url`` is set, and
    ``audio_download_url`` is None (R2.10 graceful degradation)."""
    polly_stub = _PollyStub()
    # Playback presign is call #1 (succeeds); download presign is call
    # #2 (fails).
    s3_stub = _RecordingS3Stub(fail_on_call=2)

    result = synthesize(
        _TEST_JOKE_TEXT,
        generation_id=_TEST_GENERATION_ID,
        voice_id="Matthew",
        audio_bucket=_TEST_BUCKET,
        polly_client=polly_stub,
        s3_client=s3_stub,
    )

    assert result.audio_available is True
    assert result.audio_url is not None
    assert result.audio_download_url is None
    assert len(s3_stub.presign_calls) == 2


def test_download_disposition_helper_shape() -> None:
    """``download_disposition`` produces the exact attachment header
    string R2.10 mandates."""
    disp = voice_synthesizer.download_disposition("xyz-1")
    assert disp == 'attachment; filename="dad-joke-xyz-1.mp3"'
