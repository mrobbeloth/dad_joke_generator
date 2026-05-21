"""Property tests for ``joke_api.ip_hashing``.

Implements **Property 34: IP addresses are never logged in raw form**
(`design.md` § Correctness Properties), which validates **Requirement
16.7** (`requirements.md`):

    THE Joke_API SHALL hash source IP addresses using SHA-256 with a
    server-side secret salt of at least 32 bytes before any logging or
    persistence, and SHALL never log, persist, or transmit raw IP
    addresses.

Property 34 (verbatim from design.md):

    *For any* source IP ``ip`` and any request, every log record and
    every persisted attribute SHALL contain only
    ``sha256_hex(salt || ip)`` where ``salt`` is at least 32 bytes; no
    captured log line, persisted attribute, metric dimension, or
    response body SHALL contain ``ip`` as a substring.

Per design.md the production salt is loaded from the SSM SecureString
``/dadjokes/ip_hash_salt`` (32+ random bytes). ``ip_hashing.hash_ip``
deliberately takes the salt as a keyword argument so it can be unit-
tested without AWS credentials, so this test simply passes a fixed
test salt that exceeds the 32-byte minimum.

**Validates: Requirements 16.7**
"""

from __future__ import annotations

import contextlib
import io
import logging
import re

import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from joke_api.ip_hashing import HASH_LENGTH, hash_ip

# ---------------------------------------------------------------------------
# Fixed test salt: 64 ASCII bytes, well above the 32-byte minimum.
# Deliberately constant so determinism can be asserted across calls.
# ---------------------------------------------------------------------------
TEST_SALT: bytes = b"property-test-salt-0123456789abcdef-0123456789abcdef-padding-xyz"
assert len(TEST_SALT) >= 32

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


# A strategy that yields the *string form* of either an IPv4 or an IPv6
# address. We pull the string form because that is exactly what a log
# line, persisted attribute, or response body would contain if the code
# under test ever leaked the raw IP, so it is also what we must search
# for in captured artifacts.
ip_string = st.one_of(
    st.ip_addresses(v=4).map(str),
    st.ip_addresses(v=6).map(str),
)


def _hash_with_capture(ip: str) -> tuple[str, str, str, str]:
    """Call ``hash_ip(ip, salt=TEST_SALT)`` and capture every artifact.

    Returns ``(digest, stdout, stderr, log_output)``. The capture is
    rebuilt for every Hypothesis example so leftover state from a prior
    iteration cannot mask a later leak.
    """
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    log_buf = io.StringIO()

    # Attach a stream handler to the root logger at DEBUG so any log
    # call from any logger in the project (including descendants) is
    # captured. Using a fresh handler per call avoids cross-iteration
    # state.
    handler = logging.StreamHandler(log_buf)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    try:
        with (
            contextlib.redirect_stdout(stdout_buf),
            contextlib.redirect_stderr(stderr_buf),
        ):
            digest = hash_ip(ip, salt=TEST_SALT)
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)
        handler.close()

    return digest, stdout_buf.getvalue(), stderr_buf.getvalue(), log_buf.getvalue()


# ---------------------------------------------------------------------------
# Property 34 -- the core property: no raw IP appears in any artifact.
# Combines all of the per-IP assertions called out in the task brief:
#   1. Digest shape: 64-char lowercase hex.
#   2. Digest does not contain the raw IP string as a substring.
#   3. Captured stdout / stderr / logging output do not contain the raw
#      IP string.
# ---------------------------------------------------------------------------
@given(ip=ip_string)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_34_no_raw_ip_in_any_artifact(ip: str) -> None:
    """**Validates: Requirements 16.7** -- Property 34.

    For any IPv4 or IPv6 address, ``hash_ip`` returns a 64-char
    lowercase hex digest, and the raw IP string never appears in the
    digest itself nor in stdout, stderr, or any logging output captured
    during the call.
    """
    digest, captured_stdout, captured_stderr, captured_log = _hash_with_capture(ip)

    # 1. Digest shape: 64-char lowercase hex.
    assert len(digest) == HASH_LENGTH, f"digest length was {len(digest)}, expected {HASH_LENGTH}"
    assert _HEX_RE.match(digest) is not None, (
        f"digest {digest!r} is not 64 lowercase hex chars"
    )

    # 2. Raw IP must not appear inside the digest.
    #    (Trivially true for IPv4 because `.` is not a hex char and for
    #    IPv6 because `:` is not a hex char, but we assert it anyway so
    #    a future representation change cannot silently violate R16.7.)
    assert ip not in digest, f"raw IP {ip!r} leaked into digest {digest!r}"

    # 3. Raw IP must not appear in any captured artifact.
    for label, artifact in (
        ("stdout", captured_stdout),
        ("stderr", captured_stderr),
        ("logging", captured_log),
    ):
        assert ip not in artifact, (
            f"raw IP {ip!r} leaked into captured {label}: {artifact!r}"
        )


# ---------------------------------------------------------------------------
# Determinism: identical (ip, salt) inputs produce identical digests.
# This is a precondition for using the digest as a stable rate-limit
# partition key (design.md Rate_Limiter) -- without it, the same visitor
# would never hit their limit.
# ---------------------------------------------------------------------------
@given(ip=ip_string)
@settings(max_examples=100, deadline=None)
def test_hash_ip_is_deterministic_for_fixed_salt(ip: str) -> None:
    """**Validates: Requirements 16.7** -- determinism precondition.

    For a fixed salt, ``hash_ip(ip)`` returns the same digest on every
    invocation; this is required for the salted hash to function as a
    stable identifier in DynamoDB and structured logs.
    """
    first = hash_ip(ip, salt=TEST_SALT)
    second = hash_ip(ip, salt=TEST_SALT)
    assert first == second


# ---------------------------------------------------------------------------
# Distinguishability: distinct IPs almost surely hash to distinct
# digests under the same salt. SHA-256's collision resistance makes a
# collision in <1000 examples a vanishingly small probability event.
# ---------------------------------------------------------------------------
@given(a=ip_string, b=ip_string)
@settings(max_examples=100, deadline=None)
def test_hash_ip_distinguishes_distinct_ips(a: str, b: str) -> None:
    """**Validates: Requirements 16.7** -- distinguishability.

    For two distinct IP strings ``a`` and ``b`` and a fixed salt,
    ``hash_ip(a) != hash_ip(b)``. (Holds with overwhelming probability
    given SHA-256's collision resistance.)
    """
    assume(a != b)
    assert hash_ip(a, salt=TEST_SALT) != hash_ip(b, salt=TEST_SALT)


# ---------------------------------------------------------------------------
# A small example-based smoke test guards against the property-test
# strategies silently degenerating (e.g. always producing the same IP).
# Not a property; included for fast feedback during local runs.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ip",
    [
        "192.0.2.1",          # IPv4 (TEST-NET-1)
        "203.0.113.42",       # IPv4 (TEST-NET-3)
        "2001:db8::1",        # IPv6 documentation prefix
        "::1",                # IPv6 loopback
    ],
)
def test_known_ips_produce_well_formed_digest_without_leaking(ip: str) -> None:
    """Sanity check that the helper does not log or print the raw IP."""
    digest, out, err, log = _hash_with_capture(ip)
    assert _HEX_RE.match(digest) is not None
    assert ip not in digest
    assert ip not in out
    assert ip not in err
    assert ip not in log
