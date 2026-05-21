"""SSM Parameter Store-backed configuration loader for the Joke_API.

This module fetches all runtime configuration values for the Joke_API from
AWS Systems Manager Parameter Store in a single ``ssm.get_parameters`` call
with ``WithDecryption=True``, validates each value, and exposes the result
as an immutable :class:`Config` dataclass.

Validated requirements:

* **R5.7**  - ``daily_limit`` is an integer in ``[5, 10]`` inclusive.
* **R8.1**  - ``ad_module_enabled`` is a strict boolean (``"true"``/``"false"``).
* **R8.4**  - ``ad_network_id`` is a (possibly empty) string.
* **R16.3** - ``cost_alarm_threshold_usd`` is a float in ``[1.00, 10000.00]``.
* **R16.7** - ``ip_hash_salt`` is a non-empty SecureString.

Validated correctness properties:

* **Property 17** - "Daily_Limit configuration is bounded": configuration
  loading accepts ``daily_limit`` iff it is an integer in ``[5, 10]``.

Security notes:

* Parameter VALUES are never logged or included in :class:`ConfigError`
  messages. The ``ip_hash_salt`` SecureString in particular must never
  appear outside the returned :class:`Config` object.
* Error messages reference parameter NAMES and the violated rule only.
  AWS account IDs and ARNs are never included.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import boto3

# ---------------------------------------------------------------------------
# Parameter names (single source of truth for the SSM key namespace).
# ---------------------------------------------------------------------------

PARAM_PREFIX = "/dadjokes/"

PARAM_DAILY_LIMIT = "/dadjokes/daily_limit"
PARAM_BEDROCK_MODEL_ID = "/dadjokes/bedrock_model_id"
PARAM_POLLY_VOICE_ID = "/dadjokes/polly_voice_id"
PARAM_AD_MODULE_ENABLED = "/dadjokes/ad_module_enabled"
PARAM_AD_NETWORK_ID = "/dadjokes/ad_network_id"
PARAM_IP_HASH_SALT = "/dadjokes/ip_hash_salt"
PARAM_COST_ALARM_THRESHOLD_USD = "/dadjokes/cost_alarm_threshold_usd"

_ALL_PARAM_NAMES: tuple[str, ...] = (
    PARAM_DAILY_LIMIT,
    PARAM_BEDROCK_MODEL_ID,
    PARAM_POLLY_VOICE_ID,
    PARAM_AD_MODULE_ENABLED,
    PARAM_AD_NETWORK_ID,
    PARAM_IP_HASH_SALT,
    PARAM_COST_ALARM_THRESHOLD_USD,
)

# Validation bounds (kept as module-level constants so they can be referenced
# in error messages and unit tests without redefining magic numbers).
_DAILY_LIMIT_MIN = 5
_DAILY_LIMIT_MAX = 10
_COST_ALARM_MIN_USD = 1.00
_COST_ALARM_MAX_USD = 10000.00


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when configuration loading or validation fails.

    Messages identify the parameter name and the violated rule, but never
    contain parameter values, AWS account IDs, or ARNs.
    """


@dataclass(frozen=True)
class Config:
    """Immutable, validated runtime configuration for the Joke_API."""

    daily_limit: int
    bedrock_model_id: str
    polly_voice_id: str
    ad_module_enabled: bool
    ad_network_id: str  # may be empty string
    ip_hash_salt: str  # SecureString contents (decrypted); never log this
    cost_alarm_threshold_usd: float


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load(ssm_client=None) -> Config:
    """Load and validate Joke_API configuration from SSM Parameter Store.

    A single ``ssm.get_parameters(Names=[...], WithDecryption=True)`` call
    fetches every required parameter. Any parameter listed in the response's
    ``InvalidParameters`` field is treated as missing and raises
    :class:`ConfigError`.

    Parameters
    ----------
    ssm_client:
        An optional pre-configured boto3 SSM client. When ``None`` (the
        default) a client is created via ``boto3.client("ssm")``. Passing a
        client is the supported injection point for tests using ``moto`` or
        an in-memory fake.

    Returns
    -------
    Config
        Fully populated, validated configuration.

    Raises
    ------
    ConfigError
        On any missing parameter, parse failure, or out-of-range value.
    """
    if ssm_client is None:
        ssm_client = boto3.client("ssm")

    response = ssm_client.get_parameters(
        Names=list(_ALL_PARAM_NAMES),
        WithDecryption=True,
    )

    invalid = list(response.get("InvalidParameters") or [])
    if invalid:
        # Sort for deterministic error messages and easier testing. We list
        # only parameter NAMES (never values).
        missing = ", ".join(sorted(invalid))
        raise ConfigError(f"missing SSM parameters: {missing}")

    # Build a name -> value map. SSM returns values as strings regardless of
    # the underlying SSM parameter Type; SecureString values are decrypted
    # in-place because we passed WithDecryption=True.
    values: dict[str, str] = {
        param["Name"]: param.get("Value", "")
        for param in response.get("Parameters", [])
    }

    # Defensive check: if SSM omitted any name without listing it as invalid,
    # surface it as a missing parameter rather than blowing up later.
    omitted = [name for name in _ALL_PARAM_NAMES if name not in values]
    if omitted:
        missing = ", ".join(sorted(omitted))
        raise ConfigError(f"missing SSM parameters: {missing}")

    daily_limit = _parse_daily_limit(values[PARAM_DAILY_LIMIT])
    bedrock_model_id = _parse_non_empty_string(
        PARAM_BEDROCK_MODEL_ID, values[PARAM_BEDROCK_MODEL_ID]
    )
    polly_voice_id = _parse_non_empty_string(
        PARAM_POLLY_VOICE_ID, values[PARAM_POLLY_VOICE_ID]
    )
    ad_module_enabled = _parse_bool(
        PARAM_AD_MODULE_ENABLED, values[PARAM_AD_MODULE_ENABLED]
    )
    ad_network_id = values[PARAM_AD_NETWORK_ID]  # may be empty; pass through
    ip_hash_salt = _parse_non_empty_string(
        PARAM_IP_HASH_SALT, values[PARAM_IP_HASH_SALT]
    )
    cost_alarm_threshold_usd = _parse_cost_alarm_threshold(
        values[PARAM_COST_ALARM_THRESHOLD_USD]
    )

    return Config(
        daily_limit=daily_limit,
        bedrock_model_id=bedrock_model_id,
        polly_voice_id=polly_voice_id,
        ad_module_enabled=ad_module_enabled,
        ad_network_id=ad_network_id,
        ip_hash_salt=ip_hash_salt,
        cost_alarm_threshold_usd=cost_alarm_threshold_usd,
    )


# ---------------------------------------------------------------------------
# Process-level cache
# ---------------------------------------------------------------------------

_CACHE: Optional[Config] = None


def load_cached(ssm_client=None) -> Config:
    """Return the cached :class:`Config`, loading it on first call.

    The Lambda cold-start path calls this once so the result is reused
    across warm invocations. Use :func:`reset_cache` in tests to clear the
    module-level cache between cases.
    """
    global _CACHE
    if _CACHE is None:
        _CACHE = load(ssm_client=ssm_client)
    return _CACHE


def reset_cache() -> None:
    """Clear the module-level configuration cache (test helper)."""
    global _CACHE
    _CACHE = None


# ---------------------------------------------------------------------------
# Per-parameter validators
# ---------------------------------------------------------------------------


def _parse_daily_limit(raw: str) -> int:
    """Parse and bounds-check ``daily_limit`` (R5.7, Property 17)."""
    try:
        # int(..., 10) rejects values like "5.0", " 5 ", or "0x5"; any
        # non-decimal-integer representation is a config error.
        value = int(raw, 10)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{PARAM_DAILY_LIMIT} must be an integer in "
            f"[{_DAILY_LIMIT_MIN}, {_DAILY_LIMIT_MAX}]"
        ) from None

    if not (_DAILY_LIMIT_MIN <= value <= _DAILY_LIMIT_MAX):
        raise ConfigError(
            f"{PARAM_DAILY_LIMIT} must be in "
            f"[{_DAILY_LIMIT_MIN}, {_DAILY_LIMIT_MAX}]"
        )
    return value


def _parse_non_empty_string(name: str, raw: str) -> str:
    """Require a non-empty string for the given parameter."""
    if not isinstance(raw, str) or raw == "":
        raise ConfigError(f"{name} must be a non-empty string")
    return raw


def _parse_bool(name: str, raw: str) -> bool:
    """Parse ``"true"``/``"false"`` (case-insensitive) to a bool (R8.1)."""
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise ConfigError(
        f'{name} must be "true" or "false" (case-insensitive)'
    )


def _parse_cost_alarm_threshold(raw: str) -> float:
    """Parse and bounds-check ``cost_alarm_threshold_usd`` (R16.3)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"{PARAM_COST_ALARM_THRESHOLD_USD} must be a number in "
            f"[{_COST_ALARM_MIN_USD:.2f}, {_COST_ALARM_MAX_USD:.2f}]"
        ) from None

    # Reject NaN / +-inf explicitly so they cannot pass the range check by
    # accident on platforms that do not order them as expected.
    if value != value or value in (float("inf"), float("-inf")):
        raise ConfigError(
            f"{PARAM_COST_ALARM_THRESHOLD_USD} must be a finite number in "
            f"[{_COST_ALARM_MIN_USD:.2f}, {_COST_ALARM_MAX_USD:.2f}]"
        )

    if not (_COST_ALARM_MIN_USD <= value <= _COST_ALARM_MAX_USD):
        raise ConfigError(
            f"{PARAM_COST_ALARM_THRESHOLD_USD} must be in "
            f"[{_COST_ALARM_MIN_USD:.2f}, {_COST_ALARM_MAX_USD:.2f}]"
        )
    return value
