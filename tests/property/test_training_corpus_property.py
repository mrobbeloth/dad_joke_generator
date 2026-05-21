"""Property tests for ``joke_api.training_corpus.load_few_shot``.

This file implements four correctness properties from ``design.md``:

* **Property 36: Few-shot prompt construction respects size bounds.**
  *For any* ``Training_Corpus`` content, the few-shot prompt builder
  SHALL return between 3 and 10 examples (or 0 when the pool is
  empty), each example SHALL be at most 500 characters, and the
  joined few-shot section SHALL be at most 5000 characters.
* **Property 37: Training_Corpus contents never reach clients.** The
  loader's public surface is the boundary that downstream code
  consumes; this file asserts the boundary properties (return type,
  by-construction passthrough of body content) that make the
  architectural guarantee hold. The end-to-end "body never reaches
  clients" check belongs at the :mod:`joke_api.response_builder`
  chokepoint and is covered separately in task 10.x.
* **Property 38: Binary corpus assets never reach Bedrock.** *For
  any* corpus item whose extension is in
  :data:`joke_api.training_corpus.BINARY_EXTENSIONS`, the loader
  SHALL NOT issue ``GetObject`` for that key, and the loader SHALL
  exclude that key from the returned pool. Unknown-extension keys
  whose body content sniffs as binary SHALL likewise be excluded.
* **Property 39: Rights-flag gates corpus inclusion.** *For any*
  invocation, when ``rights_confirmed`` is falsy the loader SHALL
  return ``[]`` and SHALL perform NO S3 calls at all
  (``list_objects_v2`` and ``get_object`` call counts MUST both be
  0). When ``rights_confirmed`` is truthy and the bucket holds
  textual content, the loader SHALL return a non-empty list.

**Validates: Requirements 17.1, 17.2, 17.3, 17.4, 17.5, 17.7**

Approach
--------
The S3 backend is replaced with a hand-rolled :class:`_S3Stub` (NOT
``MagicMock``) modelled on ``test_moderators_property.py``'s
``_ComprehendStub``. The stub exposes the two methods
``training_corpus`` actually consumes (``list_objects_v2``,
``get_object``) and tracks every call so Property 38 / Property 39
can assert non-call invariants. ``get_object`` returns a
:class:`_BodyStub` whose ``.read()`` returns ``bytes`` -- this
matches the surface of ``botocore.response.StreamingBody`` that
:func:`joke_api.training_corpus._fetch_and_extract` exercises.

A fresh :class:`_S3Stub` instance is built per Hypothesis example so
internal call counters never leak between iterations.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from joke_api import training_corpus
from joke_api.training_corpus import (
    BINARY_EXTENSIONS,
    COMBINED_CHAR_CAP,
    DEFAULT_SEPARATOR,
    MAX_EXAMPLES,
    MIN_EXAMPLES,
    PER_EXAMPLE_CHAR_CAP,
    TEXT_EXTENSIONS,
    load_few_shot,
)


# ---------------------------------------------------------------------------
# Hand-rolled S3 stub
# ---------------------------------------------------------------------------


class _BodyStub:
    """Minimal stand-in for ``botocore.response.StreamingBody``.

    Exposes only the ``.read()`` method that
    :func:`training_corpus._fetch_and_extract` consumes.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _S3Stub:
    """In-memory S3 client stub for ``training_corpus`` property tests.

    Exposes the two methods the loader uses
    (:py:meth:`list_objects_v2` and :py:meth:`get_object`) and
    tracks every call so tests can assert non-call invariants
    (Property 38: no GetObject for binary keys; Property 39: zero
    S3 calls when the rights flag is false).

    Args:
        objects: Mapping of S3 key to ``bytes`` body. The stub
            returns these in iteration order from
            ``list_objects_v2`` and serves them on
            ``get_object``.
    """

    __slots__ = (
        "_objects",
        "list_objects_v2_calls",
        "get_object_calls",
    )

    def __init__(self, objects: dict[str, bytes]) -> None:
        # ``dict`` preserves insertion order in Python 3.7+, so the
        # stub yields keys back in the same order tests provided.
        self._objects = dict(objects)
        self.list_objects_v2_calls: list[dict[str, Any]] = []
        self.get_object_calls: list[dict[str, Any]] = []

    def list_objects_v2(
        self, *, Bucket: str, MaxKeys: Optional[int] = None
    ) -> dict[str, Any]:
        self.list_objects_v2_calls.append(
            {"Bucket": Bucket, "MaxKeys": MaxKeys}
        )
        contents = [
            {"Key": key, "Size": len(body)}
            for key, body in self._objects.items()
        ]
        if MaxKeys is not None:
            contents = contents[:MaxKeys]
        return {"Contents": contents}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.get_object_calls.append({"Bucket": Bucket, "Key": Key})
        if Key not in self._objects:
            # Mimic boto3's KeyError-ish behavior with a structured
            # response. The loader never reaches this branch on
            # well-formed stubs, but a defensive default keeps
            # surprising failures readable.
            raise KeyError(f"NoSuchKey: {Key}")
        return {"Body": _BodyStub(self._objects[Key])}

    @property
    def fetched_keys(self) -> list[str]:
        """Keys that the loader actually fetched via ``get_object``."""
        return [call["Key"] for call in self.get_object_calls]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# Plain printable ASCII bodies. Bounded to 2000 chars so some entries
# exceed PER_EXAMPLE_CHAR_CAP (500) and some sit comfortably under.
_text_body_strategy = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=0,
    max_size=2000,
)

# Text-extension keys. Note ``MIN_EXAMPLES`` worth of distinct keys is
# the practical minimum, but Hypothesis can generate fewer; the
# loader tolerates undersized pools (R17.1 lower bound is the
# *target*, not a hard invariant -- see the docstring of
# :func:`training_corpus.load_few_shot`).
_text_extensions: tuple[str, ...] = tuple(sorted(TEXT_EXTENSIONS))
_binary_extensions: tuple[str, ...] = tuple(sorted(BINARY_EXTENSIONS))

# Distinct key roots so generated dicts don't collide on the dot.
_key_root_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
    ),
    min_size=1,
    max_size=20,
)


def _build_text_entry(
    root: str, ext: str, body: str
) -> tuple[str, bytes]:
    """Build a ``(key, bytes_body)`` pair for a text-extension entry."""
    return (f"{root}{ext}", body.encode("utf-8"))


def _build_binary_entry(root: str, ext: str) -> tuple[str, bytes]:
    """Build a ``(key, bytes_body)`` pair for a binary-extension entry.

    The body is small synthetic binary content; the loader is
    forbidden from reading it (Property 38), so its exact bytes are
    irrelevant, but we make it clearly non-text (NUL prefix) so a
    bug that *did* read it would be obvious.
    """
    return (f"{root}{ext}", b"\x00\x01\x02BIN\x00")


# Strategy: a single text entry (key, bytes).
_text_entry_strategy = st.builds(
    _build_text_entry,
    _key_root_strategy,
    st.sampled_from(_text_extensions),
    _text_body_strategy,
)

# Strategy: a single binary entry (key, bytes).
_binary_entry_strategy = st.builds(
    _build_binary_entry,
    _key_root_strategy,
    st.sampled_from(_binary_extensions),
)

# Mixed strategy: either kind of entry.
_mixed_entry_strategy = st.one_of(_text_entry_strategy, _binary_entry_strategy)


def _entries_to_objects(
    entries: list[tuple[str, bytes]],
) -> dict[str, bytes]:
    """Collapse a list of ``(key, body)`` pairs into a dict.

    Hypothesis can generate duplicate keys; the dict drops dupes
    deterministically (last write wins) so the stub state is
    well-defined.
    """
    return {key: body for key, body in entries}


PBT_SETTINGS = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ---------------------------------------------------------------------------
# Property 36: size bounds
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(
    entries=st.lists(
        _mixed_entry_strategy,
        min_size=1,
        max_size=30,
    ),
    max_examples=st.integers(min_value=MIN_EXAMPLES, max_value=MAX_EXAMPLES),
)
def test_property_36_few_shot_size_bounds(
    entries: list[tuple[str, bytes]],
    max_examples: int,
) -> None:
    """Property 36: 0..MAX_EXAMPLES entries, per-entry <= 500, joined <= 5000.

    **Validates: Requirements 17.1, 17.5** (Property 36).

    The lower bound (``MIN_EXAMPLES``) is a *target*, not a hard
    invariant: the loader returns whatever passes extraction up to
    the cap, so an undersized pool is permitted (the docstring of
    :func:`training_corpus.load_few_shot` calls this out
    explicitly). The hard invariants are the upper bounds enforced
    by ``_truncate`` and ``_enforce_combined_cap``.
    """
    objects = _entries_to_objects(entries)
    stub = _S3Stub(objects)

    result = load_few_shot(
        rights_confirmed=True,
        s3_client=stub,
        bucket="dadjokes-training-corpus-test",
        max_examples=max_examples,
    )

    # Return-type contract: a list of plain strings (not dicts, not
    # boto resources, not S3 metadata).
    assert isinstance(result, list), f"expected list, got {type(result)!r}"
    for i, entry in enumerate(result):
        assert isinstance(entry, str), (
            f"entry {i}: expected str, got {type(entry)!r}"
        )

    # Hard upper bound on pool size (R17.1).
    assert len(result) <= MAX_EXAMPLES, (
        f"len(result)={len(result)} exceeds MAX_EXAMPLES={MAX_EXAMPLES}"
    )

    # Per-entry character cap (R17.1).
    for i, entry in enumerate(result):
        assert len(entry) <= PER_EXAMPLE_CHAR_CAP, (
            f"entry {i}: len={len(entry)} exceeds PER_EXAMPLE_CHAR_CAP="
            f"{PER_EXAMPLE_CHAR_CAP}"
        )
        # Empty entries would bloat the joined section without
        # carrying signal; the loader is contracted to drop them.
        assert entry, f"entry {i}: unexpected empty string in result"

    # Combined-section cap (R17.5). The loader uses
    # DEFAULT_SEPARATOR for its accounting; the same separator is
    # the correct upper-bound estimate here.
    if result:
        joined = DEFAULT_SEPARATOR.join(result)
        assert len(joined) <= COMBINED_CHAR_CAP, (
            f"joined len={len(joined)} exceeds COMBINED_CHAR_CAP="
            f"{COMBINED_CHAR_CAP}"
        )


# ---------------------------------------------------------------------------
# Property 37: corpus contents never reach clients (boundary properties)
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(
    bodies=st.lists(
        _text_body_strategy,
        min_size=1,
        max_size=10,
    ),
)
def test_property_37_loader_returns_plain_strings_not_uris(
    bodies: list[str],
) -> None:
    """Property 37 (boundary): the loader returns plain strings.

    **Validates: Requirements 17.2, 17.3** (Property 37, boundary).

    The architectural chokepoint that prevents corpus content from
    reaching clients is :mod:`joke_api.response_builder` (covered
    in task 10.x). This test pins the *boundary* properties of the
    loader that make that guarantee possible:

    1. The return type is ``list[str]`` -- not a dict keyed by S3
       object key, not an iterable of S3 metadata, not a presigned
       URL, not an ``s3://`` URI.
    2. Each entry is the content of the corpus file (truncated),
       not a path or URL referencing it. This is verified by
       feeding the loader synthetic bodies containing ``s3://``,
       presigned-URL-like tokens, and ARN-like tokens and asserting
       the loader passes the body content through verbatim
       (modulo whitespace stripping and length truncation).
    """
    # Build text-extension entries from the generated bodies.
    objects: dict[str, bytes] = {}
    expected_truncations: list[str] = []
    for i, body in enumerate(bodies):
        ext = _text_extensions[i % len(_text_extensions)]
        key = f"sample-{i}{ext}"
        objects[key] = body.encode("utf-8")
        # Mirror the loader's per-entry truncation contract so we
        # can assert content passthrough exactly.
        stripped = body.strip()
        if not stripped:
            continue
        expected_truncations.append(
            stripped[:PER_EXAMPLE_CHAR_CAP]
        )

    stub = _S3Stub(objects)
    result = load_few_shot(
        rights_confirmed=True,
        s3_client=stub,
        bucket="dadjokes-training-corpus-test",
    )

    # Boundary contract 1: return type is list[str], not a dict
    # mapping S3 keys to anything.
    assert isinstance(result, list)

    # Boundary contract 2: no entry is an S3 object key from the
    # input. (Object keys would expose the corpus file index to
    # downstream code; the loader must surface content only.)
    input_keys = set(objects.keys())
    for entry in result:
        assert entry not in input_keys, (
            f"loader leaked S3 object key {entry!r} into the result list"
        )

    # Boundary contract 3: no entry has an ``s3://`` *prefix* and
    # no entry looks like a presigned URL. (Bodies generated by
    # the strategy may legitimately contain the substring
    # ``s3://`` -- the loader is contracted to pass content
    # through verbatim, so we test the prefix specifically rather
    # than substring, to avoid contradicting Property 36's
    # passthrough semantics.)
    for entry in result:
        assert not entry.startswith("s3://"), (
            f"entry starts with s3:// (looks like a URI, not content): "
            f"{entry!r}"
        )
        assert "X-Amz-Signature=" not in entry[:PER_EXAMPLE_CHAR_CAP], (
            f"entry contains a presigned-URL signature token: {entry!r}"
        )

    # Boundary contract 4: every returned entry is some prefix of
    # one of the bodies we put in the bucket. This proves the
    # loader is a content-passthrough, not a generator of new
    # opaque references.
    expected_set = set(expected_truncations)
    for entry in result:
        assert entry in expected_set, (
            f"entry {entry!r} is not a truncated body of any input "
            f"(loader fabricated content?)"
        )


def test_property_37_loader_passes_through_bodies_containing_uri_like_tokens() -> None:
    """Property 37 (boundary, deterministic): URI-like body tokens passthrough.

    **Validates: Requirements 17.2, 17.3**.

    Verifies the loader does *not* reinterpret body content as a
    URL or path: when the body itself contains ``s3://`` or an ARN,
    the loader returns the body verbatim. The
    response_builder is what prevents this content from reaching
    clients; the loader's job is to surface content faithfully so
    upstream layers see what's there.
    """
    payload = (
        "Joke style example.\n"
        "Reference: s3://dadjokes-training-corpus/example.txt\n"
        "ARN: arn:aws:s3:::dadjokes-training-corpus/example.txt\n"
    )
    objects = {f"sample-{i}.txt": payload.encode("utf-8") for i in range(5)}
    stub = _S3Stub(objects)

    result = load_few_shot(
        rights_confirmed=True,
        s3_client=stub,
        bucket="dadjokes-training-corpus-test",
    )

    assert result, "expected non-empty result for non-empty text bucket"
    # Every entry equals the stripped/truncated payload -- no entry
    # was rewritten into an S3 URL or path.
    expected = payload.strip()[:PER_EXAMPLE_CHAR_CAP]
    for entry in result:
        assert entry == expected, (
            f"loader did not passthrough body verbatim; got {entry!r}"
        )


# ---------------------------------------------------------------------------
# Property 38: binary corpus assets never reach Bedrock
# ---------------------------------------------------------------------------


@PBT_SETTINGS
@given(
    binary_entries=st.lists(
        _binary_entry_strategy,
        min_size=1,
        max_size=15,
    ),
    text_entries=st.lists(
        _text_entry_strategy,
        min_size=0,
        max_size=10,
    ),
)
def test_property_38_binary_keys_are_never_fetched(
    binary_entries: list[tuple[str, bytes]],
    text_entries: list[tuple[str, bytes]],
) -> None:
    """Property 38: ``get_object`` is never called for binary keys.

    **Validates: Requirements 17.4** (Property 38).

    The implementation catches binary extensions in
    :func:`training_corpus._is_binary_extension` *before* calling
    :py:meth:`_S3Stub.get_object`, so the stub's call log is the
    correct verification surface: every key in
    ``stub.fetched_keys`` MUST have a non-binary extension, and
    every binary key MUST be absent from
    ``stub.fetched_keys``.
    """
    objects = _entries_to_objects(binary_entries + text_entries)
    binary_keys = {
        key
        for key, _ in binary_entries
        # ``_entries_to_objects`` may have dropped a duplicated key
        # in favor of a later text entry; only count keys that
        # actually survive in the stub *and* whose extension is
        # binary.
        if key in objects
        and any(key.lower().endswith(ext) for ext in _binary_extensions)
    }

    stub = _S3Stub(objects)
    result = load_few_shot(
        rights_confirmed=True,
        s3_client=stub,
        bucket="dadjokes-training-corpus-test",
    )

    fetched = set(stub.fetched_keys)
    leaked = binary_keys & fetched
    assert not leaked, (
        f"loader fetched binary key(s) {sorted(leaked)!r} via get_object; "
        f"binary keys must be filtered before get_object."
    )

    # Belt-and-braces: no binary-typed entry's bytes ended up in
    # the result list (would indicate the binary content somehow
    # made it past the extension filter).
    for entry in result:
        for key, body in binary_entries:
            if key not in objects:
                continue
            if not any(
                key.lower().endswith(ext) for ext in _binary_extensions
            ):
                continue
            # The synthetic binary body starts with a NUL byte;
            # it cannot equal a stripped UTF-8 text passthrough.
            decoded = body.decode("utf-8", errors="replace")
            assert decoded.strip() != entry, (
                f"binary body for key {key!r} appears in result"
            )


def test_property_38_unknown_extension_with_nul_bytes_is_excluded() -> None:
    """Property 38 (content-sniff path): NUL-laden bodies are excluded.

    **Validates: Requirements 17.4** (Property 38, content-sniff).

    Keys with extensions outside both ``BINARY_EXTENSIONS`` and
    ``TEXT_EXTENSIONS`` are routed through ``_looks_binary``;
    bodies containing NUL bytes are deemed binary and excluded
    from the few-shot pool. The ``get_object`` call still happens
    (the implementation must read the body to sniff it), but the
    result MUST exclude that key's content.
    """
    nul_body = b"\x00" * 100 + b"This text follows a block of NULs.\n"
    valid_body = b"A perfectly fine plain-text dad joke example.\n"

    objects = {
        "mystery.dat": nul_body,        # unknown ext, sniffs binary
        "joke-a.txt": valid_body,
        "joke-b.txt": valid_body,
        "joke-c.txt": valid_body,
    }
    stub = _S3Stub(objects)

    result = load_few_shot(
        rights_confirmed=True,
        s3_client=stub,
        bucket="dadjokes-training-corpus-test",
    )

    # The mystery body must not appear in the result -- not as the
    # raw bytes, not as a decoded string.
    for entry in result:
        assert "\x00" not in entry, (
            f"NUL byte leaked into a few-shot entry: {entry!r}"
        )
        assert entry != nul_body.decode("utf-8", errors="replace").strip(), (
            f"NUL-laden body appeared in result: {entry!r}"
        )

    # And the valid bodies should populate the result.
    assert result, "expected non-empty result with three valid text entries"


# ---------------------------------------------------------------------------
# Property 39: rights-flag gates corpus inclusion
# ---------------------------------------------------------------------------


# Falsy values that the implementation's ``if not rights_confirmed:``
# check treats as "not confirmed". Listed explicitly so a regression
# that narrows the check to ``rights_confirmed is False`` would be
# caught.
_FALSY_RIGHTS_VALUES: tuple[Any, ...] = (False, None, 0, "", [], {})


@pytest.mark.parametrize("rights_value", _FALSY_RIGHTS_VALUES)
def test_property_39_falsy_rights_short_circuits_with_zero_s3_calls(
    rights_value: Any,
) -> None:
    """Property 39: falsy ``rights_confirmed`` -> ``[]`` and zero S3 calls.

    **Validates: Requirements 17.7** (Property 39).

    The loader MUST short-circuit before any S3 traffic. Both
    ``list_objects_v2`` and ``get_object`` call counts MUST be
    exactly 0; the result MUST be an empty list.
    """
    # Populate the stub with text content so that a buggy
    # implementation that *did* talk to S3 would return non-empty
    # data, making the regression visible.
    objects = {
        "joke-1.txt": b"first sample joke body",
        "joke-2.txt": b"second sample joke body",
        "joke-3.txt": b"third sample joke body",
        "joke-4.txt": b"fourth sample joke body",
    }
    stub = _S3Stub(objects)

    result = load_few_shot(
        rights_confirmed=rights_value,
        s3_client=stub,
        bucket="dadjokes-training-corpus-test",
    )

    assert result == [], (
        f"expected [] when rights_confirmed={rights_value!r}; got {result!r}"
    )
    assert len(stub.list_objects_v2_calls) == 0, (
        f"list_objects_v2 was called {len(stub.list_objects_v2_calls)} "
        f"time(s) despite rights_confirmed={rights_value!r}"
    )
    assert len(stub.get_object_calls) == 0, (
        f"get_object was called {len(stub.get_object_calls)} time(s) "
        f"despite rights_confirmed={rights_value!r}"
    )


@PBT_SETTINGS
@given(
    text_entries=st.lists(
        _text_entry_strategy,
        min_size=MIN_EXAMPLES,
        max_size=MAX_EXAMPLES,
    ),
)
def test_property_39_truthy_rights_with_text_pool_returns_nonempty(
    text_entries: list[tuple[str, bytes]],
) -> None:
    """Property 39 (positive case): truthy rights + text pool -> non-empty.

    **Validates: Requirements 17.7** (Property 39, positive case).

    The contrapositive of the rights gate: when the flag is True
    AND the bucket actually contains usable text bodies, the
    loader MUST return at least one example. Generated bodies are
    constrained to non-whitespace-only content so each entry
    survives ``_truncate``.
    """
    # Force every entry's body to be non-whitespace-only -- a
    # whitespace-only body legitimately collapses to "" and is
    # dropped by ``_truncate``, which would invalidate the
    # post-condition without indicating a bug.
    objects: dict[str, bytes] = {}
    for i, (key, body) in enumerate(text_entries):
        if body.decode("utf-8", errors="replace").strip():
            objects[key] = body
        else:
            objects[key] = f"non-empty body {i}".encode("utf-8")

    stub = _S3Stub(objects)
    result = load_few_shot(
        rights_confirmed=True,
        s3_client=stub,
        bucket="dadjokes-training-corpus-test",
    )

    assert len(stub.list_objects_v2_calls) == 1, (
        f"expected exactly one list_objects_v2 call; got "
        f"{len(stub.list_objects_v2_calls)}"
    )
    assert result, (
        f"expected non-empty result for non-empty text bucket; "
        f"objects={list(objects.keys())!r}"
    )
    assert len(result) <= MAX_EXAMPLES
