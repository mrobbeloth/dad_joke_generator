"""Salted SHA-256 hashing for source IP addresses.

This module exposes a single pure, deterministic function that converts a
source IP address into a salted SHA-256 hex digest. The salt is loaded by
the caller from SSM Parameter Store at ``/dadjokes/ip_hash_salt`` (a
SecureString of at least 32 random bytes per ``design.md``); this module
must remain decoupled from SSM/boto3 so it can be unit-tested without AWS
credentials and stays trivially deterministic.

The function MUST never log, print, or otherwise emit the raw ``ip``
value. It returns the digest only. Callers (observability, joke_store,
rate_limiter) record the digest in place of the raw IP.

References:
    - Requirement R16.7: source IPs are never logged in raw form; only the
      salted SHA-256 hash is recorded.
    - Property 34: IP addresses are never logged in raw form (every log
      record, persisted attribute, metric dimension, or response body
      SHALL contain only ``sha256_hex(salt || ip)``).
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["HASH_LENGTH", "hash_ip"]

#: Length in characters of the hex-encoded SHA-256 digest returned by
#: :func:`hash_ip`. SHA-256 produces 32 bytes which encode to 64 lowercase
#: hexadecimal characters.
HASH_LENGTH: int = 64

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def hash_ip(ip: str, *, salt: bytes | str) -> str:
    """Return a salted SHA-256 hex digest for ``ip``.

    Computes ``sha256(salt_bytes + ip.encode("utf-8")).hexdigest()`` and
    returns the result as a 64-character lowercase hexadecimal string.

    Args:
        ip: The source IP address to hash. Must be a non-empty string.
            IP-format validation is performed by ``client_ip.resolve``
            before this function is called; this function only enforces
            non-emptiness.
        salt: The hashing salt. Either ``bytes`` (used directly) or a
            ``str`` (encoded as UTF-8). Must be non-empty. In production
            the salt is loaded from SSM SecureString
            ``/dadjokes/ip_hash_salt``.

    Returns:
        A 64-character lowercase hexadecimal SHA-256 digest.

    Raises:
        ValueError: If ``ip`` is not a non-empty string, or if ``salt``
            is empty.
        TypeError: If ``salt`` is neither ``bytes`` nor ``str``.
    """
    if not isinstance(ip, str) or ip == "":
        raise ValueError("ip must be a non-empty string")

    if isinstance(salt, str):
        salt_bytes = salt.encode("utf-8")
    elif isinstance(salt, (bytes, bytearray)):
        salt_bytes = bytes(salt)
    else:  # pragma: no cover - defensive type guard
        raise TypeError("salt must be bytes or str")

    if len(salt_bytes) == 0:
        raise ValueError("salt must be non-empty")

    digest = hashlib.sha256(salt_bytes + ip.encode("utf-8")).hexdigest()

    # Sanity-check the digest shape. ``hashlib.sha256().hexdigest()`` is
    # specified to return 64 lowercase hex characters; assert it here so a
    # future stdlib change cannot silently violate Property 34's contract.
    assert len(digest) == HASH_LENGTH and digest.islower() and _HEX_RE.match(digest), (
        "sha256 hexdigest did not match expected 64-char lowercase hex shape"
    )

    return digest
