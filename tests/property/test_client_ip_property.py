"""Property tests for ``joke_api.client_ip.resolve``.

Implements **Property 18: Forwarded-For resolution uses the leftmost
address** from ``design.md``:

  *For any* ``X-Forwarded-For`` header value composed of one or more
  comma-separated IP addresses with arbitrary surrounding whitespace,
  the resolved client IP SHALL equal the trimmed leftmost address;
  *for any* request with a missing, empty, or malformed header (when
  XFF is required), the handler SHALL return an error response and
  SHALL NOT increment any counter.

**Validates: Requirements 5.8, 5.9**

The negative half of Property 18 is exercised here by asserting that
``resolve`` raises :class:`ClientIpUnresolvable`. The "no counter
increment" obligation is structurally guaranteed: ``resolve`` is a
pure parser with no DynamoDB or CloudWatch handle, so a raised
exception cannot have side-effected the rate-limiter. A handler-level
property test in task 10.3 covers the orchestration counterpart.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from joke_api.client_ip import ClientIpUnresolvable, resolve

# ---------------------------------------------------------------------------
# Helpers and strategies
# ---------------------------------------------------------------------------

# Whitespace surrounding each IP / inside the comma-separated header.
# Limited to the ASCII spaces RFC 7239 / common proxies actually emit so
# we exercise the stripping rule without conflating it with line-folding
# or NBSP behavior the implementation does not contract.
_WS = st.text(alphabet=" \t", max_size=4)

# Headers with assorted casings. API Gateway HTTP API normalises to
# lowercase, but the resolver promises a case-insensitive lookup.
_XFF_CASINGS = st.sampled_from(
    [
        "X-Forwarded-For",
        "x-forwarded-for",
        "X-FORWARDED-FOR",
        "x-Forwarded-for",
        "X-forwarded-FOR",
    ]
)


def _ip_str(ip: ipaddress._BaseAddress) -> str:
    """Canonical string form of an ``IPv4Address``/``IPv6Address``.

    The resolver returns ``str(ipaddress.ip_address(leftmost))``, so the
    expected value must use the same canonicalisation (e.g. compressed
    IPv6, no leading zeros) rather than the literal we composed into the
    header.
    """
    return str(ip)


# Mixed v4/v6 addresses. ``unique=True`` is unnecessary; duplicates in
# the header are still well-formed.
_ip_addresses = st.one_of(st.ip_addresses(v=4), st.ip_addresses(v=6))


def _build_event(header_name: str, header_value: str) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event with the given XFF header."""
    return {"headers": {header_name: header_value}}


# ---------------------------------------------------------------------------
# Positive property: leftmost trimmed address wins
# ---------------------------------------------------------------------------


@given(
    leading_ws=_WS,
    leftmost_ip=_ip_addresses,
    trailing_ws=_WS,
    rest_ips=st.lists(_ip_addresses, min_size=0, max_size=5),
    rest_ws=st.lists(_WS, min_size=0, max_size=12),
    header_name=_XFF_CASINGS,
)
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_resolve_returns_canonical_leftmost_address(
    leading_ws: str,
    leftmost_ip: ipaddress._BaseAddress,
    trailing_ws: str,
    rest_ips: list[ipaddress._BaseAddress],
    rest_ws: list[str],
    header_name: str,
) -> None:
    """The resolved IP equals the canonical form of the trimmed leftmost address.

    Validates the positive half of Property 18 and Requirement 5.8.
    """
    # Build the header: arbitrary whitespace around the leftmost IP, then
    # zero or more additional IPs with arbitrary inter-segment whitespace.
    parts = [f"{leading_ws}{_ip_str(leftmost_ip)}{trailing_ws}"]
    for i, ip in enumerate(rest_ips):
        # Pull two whitespace blobs per follower (before/after); fall
        # back to empty if the strategy under-supplied.
        before = rest_ws[2 * i] if 2 * i < len(rest_ws) else ""
        after = rest_ws[2 * i + 1] if 2 * i + 1 < len(rest_ws) else ""
        parts.append(f"{before}{_ip_str(ip)}{after}")
    header_value = ",".join(parts)

    event = _build_event(header_name, header_value)

    resolved = resolve(event)

    assert resolved == _ip_str(leftmost_ip), (
        f"expected canonical leftmost address {_ip_str(leftmost_ip)!r}, "
        f"got {resolved!r} from header {header_value!r}"
    )


@given(
    only_ip=_ip_addresses,
    leading_ws=_WS,
    trailing_ws=_WS,
    header_name=_XFF_CASINGS,
)
@settings(max_examples=100)
def test_resolve_handles_single_address_header(
    only_ip: ipaddress._BaseAddress,
    leading_ws: str,
    trailing_ws: str,
    header_name: str,
) -> None:
    """A header containing exactly one address resolves to that address.

    This is a reduction of the positive property to the single-IP case
    and exercises the no-comma split branch explicitly.
    """
    header_value = f"{leading_ws}{_ip_str(only_ip)}{trailing_ws}"
    event = _build_event(header_name, header_value)

    assert resolve(event) == _ip_str(only_ip)


# ---------------------------------------------------------------------------
# Negative property: malformed/missing/empty headers raise
# ---------------------------------------------------------------------------


def _looks_like_ip(s: str) -> bool:
    """True iff ``s`` parses as an IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(s)
    except ValueError:
        return False
    return True


# Garbage strings whose trimmed form is non-empty and does NOT parse as
# an IP. We constrain to printable ASCII (excluding comma and outer
# whitespace) so the failure mode is unambiguously "leftmost segment is
# not a valid IP" rather than something the strategy accidentally made
# valid.
_invalid_ip_text = (
    st.text(
        alphabet=st.characters(
            min_codepoint=33,
            max_codepoint=126,
            blacklist_characters=",",
        ),
        min_size=1,
        max_size=40,
    )
    .filter(lambda s: not _looks_like_ip(s))
)


@given(
    bad_leftmost=_invalid_ip_text,
    leading_ws=_WS,
    trailing_ws=_WS,
    followers=st.lists(_ip_addresses, min_size=0, max_size=3),
    header_name=_XFF_CASINGS,
)
@settings(max_examples=200)
def test_resolve_raises_on_malformed_leftmost(
    bad_leftmost: str,
    leading_ws: str,
    trailing_ws: str,
    followers: list[ipaddress._BaseAddress],
    header_name: str,
) -> None:
    """A leftmost segment that is not a valid IP raises ``ClientIpUnresolvable``.

    Validates Requirement 5.9: malformed forwarded-for headers must be
    rejected.
    """
    parts = [f"{leading_ws}{bad_leftmost}{trailing_ws}"]
    parts.extend(_ip_str(ip) for ip in followers)
    header_value = ",".join(parts)
    event = _build_event(header_name, header_value)

    with pytest.raises(ClientIpUnresolvable):
        resolve(event)


@given(
    followers=st.lists(_ip_addresses, min_size=0, max_size=3),
    leading_ws=_WS,
    header_name=_XFF_CASINGS,
)
@settings(max_examples=100)
def test_resolve_raises_on_empty_leftmost_segment(
    followers: list[ipaddress._BaseAddress],
    leading_ws: str,
    header_name: str,
) -> None:
    """An empty leftmost segment (e.g. ``",1.2.3.4"``) is malformed.

    Even when subsequent segments are valid IPs, an empty leftmost
    segment must raise per Requirement 5.9.
    """
    rest = ",".join(_ip_str(ip) for ip in followers) if followers else ""
    # Force the leftmost segment to be only whitespace; the comma split
    # then yields an empty leftmost after stripping.
    header_value = f"{leading_ws}," + rest
    event = _build_event(header_name, header_value)

    with pytest.raises(ClientIpUnresolvable):
        resolve(event)


@given(
    blank=st.text(alphabet=" \t", min_size=0, max_size=8),
    header_name=_XFF_CASINGS,
)
@settings(max_examples=50)
def test_resolve_raises_on_empty_or_whitespace_header(
    blank: str,
    header_name: str,
) -> None:
    """An empty or whitespace-only XFF value raises ``ClientIpUnresolvable``.

    Validates Requirement 5.9.
    """
    event = _build_event(header_name, blank)

    with pytest.raises(ClientIpUnresolvable):
        resolve(event)


@given(
    other_header_name=st.text(
        alphabet=st.characters(
            min_codepoint=65,
            max_codepoint=122,
            whitelist_characters="-",
        ),
        min_size=1,
        max_size=20,
    ).filter(lambda s: s.lower() != "x-forwarded-for"),
    other_header_value=st.text(min_size=0, max_size=40),
)
@settings(max_examples=100)
def test_resolve_raises_when_xff_header_absent(
    other_header_name: str,
    other_header_value: str,
) -> None:
    """A request with no ``X-Forwarded-For`` header at all raises.

    Validates Requirement 5.9: source IP "missing when expected" must
    be rejected.
    """
    event = {"headers": {other_header_name: other_header_value}}

    with pytest.raises(ClientIpUnresolvable):
        resolve(event)
