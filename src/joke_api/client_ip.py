"""Client-IP resolution from the API Gateway / CloudFront chain.

Implements the leftmost-X-Forwarded-For resolution rule used by the
Joke_API rate-limiter and audit log. The function is deterministic,
pure, and side-effect free: no logging, no AWS calls, no clock reads.
Only the Python standard library is used.

Validates the following acceptance criteria:

* **R5.8** -- WHERE a generation request arrives with a forwarded-for
  header populated by a trusted proxy or CDN, THE Rate_Limiter SHALL
  treat the leftmost address in that header as the originating client
  IP and apply rate-limiting against that address.
* **R5.9** -- IF the source IP of a generation request cannot be
  determined (forwarded-for header missing when expected, header
  malformed, or request originates from an untrusted proxy), THEN THE
  Joke_API SHALL reject the request with an error response indicating
  that the client IP could not be identified, and THE Rate_Limiter
  SHALL NOT increment any counter for that request.

See also Property 18 in ``design.md`` and the "Components and
Interfaces -> Joke_API -> 2" section.
"""

from __future__ import annotations

import ipaddress

__all__ = ["ClientIpUnresolvable", "resolve"]

# API Gateway HTTP API normalises header names to lowercase, but we do
# not rely on that: the lookup is case-insensitive by design.
_XFF_HEADER = "x-forwarded-for"


class ClientIpUnresolvable(Exception):
    """Raised when the client IP cannot be derived from the event.

    The ``reason`` attribute carries a short, machine-readable tag
    describing why resolution failed so that callers (handler.py) can
    map it to a sanitized error category without leaking internals.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _find_xff_value(headers: dict) -> str | None:
    """Return the X-Forwarded-For value via case-insensitive lookup.

    Returns ``None`` when no header with that name is present.
    """
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == _XFF_HEADER:
            # Header values are expected to be strings; reject anything
            # else (e.g. lists, ints) as malformed.
            if not isinstance(value, str):
                return None
            return value
    return None


def resolve(event: dict) -> str:
    """Resolve the originating client IP from an API Gateway event.

    Parameters
    ----------
    event:
        The Lambda proxy integration event. Only ``event["headers"]``
        is consulted.

    Returns
    -------
    str
        The canonical string form of the leftmost address in the
        ``X-Forwarded-For`` header (``str(ipaddress.ip_address(...))``).

    Raises
    ------
    ClientIpUnresolvable
        When ``headers`` is missing or not a dict, when the header is
        absent, when the value is empty/whitespace-only, when the
        leftmost segment is empty, or when the leftmost segment is not
        a valid IPv4 or IPv6 address.
    """
    headers = event.get("headers") if isinstance(event, dict) else None
    if not isinstance(headers, dict):
        raise ClientIpUnresolvable("headers_missing")

    raw = _find_xff_value(headers)
    if raw is None:
        raise ClientIpUnresolvable("xff_header_missing")

    if not raw.strip():
        raise ClientIpUnresolvable("xff_header_empty")

    # Take the leftmost comma-separated segment with surrounding
    # whitespace removed. An empty segment (e.g. ", 1.2.3.4") is treated
    # as malformed per R5.9.
    leftmost = raw.split(",", 1)[0].strip()
    if not leftmost:
        raise ClientIpUnresolvable("xff_leftmost_empty")

    try:
        return str(ipaddress.ip_address(leftmost))
    except ValueError as exc:
        raise ClientIpUnresolvable("xff_leftmost_invalid") from exc
