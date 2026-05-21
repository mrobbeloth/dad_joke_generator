"""Property test 17: Daily_Limit configuration is bounded.

**Validates: Requirements 5.7**

Per design.md, Property 17:

    *For any* configured ``daily_limit`` value, configuration loading SHALL
    accept it iff it is an integer in ``[5, 10]``; values outside this range
    SHALL be rejected at startup, and accepted values SHALL be applied
    without code change.

The relevant requirement (R5.7, requirements.md):

    THE Daily_Limit SHALL be configurable to any integer value from 5 to 10
    inclusive through external configuration without modifying or
    redeploying source code, with a default value of 5.

Implementation under test
-------------------------

``joke_api.config`` loads parameters from AWS SSM Parameter Store.  SSM
always returns parameter values as strings, so the ``daily_limit`` rule
in ``_parse_daily_limit`` is:

* Accept iff ``int(raw, 10)`` succeeds AND the result is in ``[5, 10]``.
* Reject otherwise by raising :class:`joke_api.config.ConfigError`.

Per the task brief and the module docstring, ``int(raw, 10)`` is the
authoritative coercion: it tolerates surrounding whitespace and a sign
prefix but rejects ``"5.0"``, ``"5e0"``, ``"0x5"``, and the empty string.
This test mirrors that contract without re-deriving it from first
principles -- it asks Hypothesis to generate inputs and asserts the
implementation's accept/reject decision matches the predicate.

No AWS calls are made.  A ``unittest.mock.MagicMock`` SSM client is
passed through ``config.load(ssm_client=...)`` -- the explicit injection
point documented in ``config.load``'s docstring -- so 100+ Hypothesis
iterations are fast and offline.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

from joke_api.config import (
    PARAM_AD_MODULE_ENABLED,
    PARAM_AD_NETWORK_ID,
    PARAM_BEDROCK_MODEL_ID,
    PARAM_COST_ALARM_THRESHOLD_USD,
    PARAM_DAILY_LIMIT,
    PARAM_IP_HASH_SALT,
    PARAM_POLLY_VOICE_ID,
    Config,
    ConfigError,
    _parse_daily_limit,
    load,
    reset_cache,
)

# ---------------------------------------------------------------------------
# Bounds under test (kept local so changes to the impl constants surface
# loudly here rather than silently passing).
# ---------------------------------------------------------------------------

DAILY_LIMIT_MIN = 5
DAILY_LIMIT_MAX = 10

# Valid placeholder values for every parameter other than ``daily_limit``;
# these never change across iterations so that any failure surfaces
# unambiguously as a ``daily_limit`` rule violation.
_OTHER_PARAM_VALUES: dict[str, str] = {
    PARAM_BEDROCK_MODEL_ID: "anthropic.claude-3-haiku-20240307-v1:0",
    PARAM_POLLY_VOICE_ID: "Joanna",
    PARAM_AD_MODULE_ENABLED: "false",
    PARAM_AD_NETWORK_ID: "",  # may be empty per design
    PARAM_IP_HASH_SALT: "x" * 64,  # 64 bytes >= 32-byte minimum (R16.7)
    PARAM_COST_ALARM_THRESHOLD_USD: "10.00",
}


# ---------------------------------------------------------------------------
# Predicate that defines accept/reject for ``daily_limit``.  Property 17
# says the implementation SHALL match this predicate exactly.
# ---------------------------------------------------------------------------


def _daily_limit_should_accept(raw: str) -> bool:
    """Return ``True`` iff ``raw`` is a decimal integer string in [5, 10].

    This mirrors ``_parse_daily_limit`` in ``joke_api.config``: it parses
    via ``int(raw, 10)`` and bounds-checks the result.  It is the
    machine-checkable form of Property 17.
    """
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        return False
    return DAILY_LIMIT_MIN <= value <= DAILY_LIMIT_MAX


# ---------------------------------------------------------------------------
# SSM stubbing
# ---------------------------------------------------------------------------


def _make_fake_ssm_client(daily_limit_raw: str) -> MagicMock:
    """Return a ``MagicMock`` whose ``get_parameters`` returns a valid
    response with ``daily_limit`` set to ``daily_limit_raw`` and every
    other parameter set to a valid placeholder value.

    The mock asserts that the call uses ``WithDecryption=True`` (per
    design.md and the module docstring), so this is also a soft contract
    test on the loader's request shape.
    """
    response: dict[str, Any] = {
        "Parameters": [
            {"Name": PARAM_DAILY_LIMIT, "Value": daily_limit_raw, "Type": "String"},
        ]
        + [
            {"Name": name, "Value": value, "Type": "String"}
            for name, value in _OTHER_PARAM_VALUES.items()
        ],
        "InvalidParameters": [],
    }
    fake = MagicMock(name="ssm_client")
    fake.get_parameters.return_value = response
    return fake


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# In-range integers ([5, 10]) formatted as plain decimal strings -- the
# canonical SSM representation.  These MUST be accepted.
in_range_int_strings = st.integers(
    min_value=DAILY_LIMIT_MIN, max_value=DAILY_LIMIT_MAX
).map(str)

# Out-of-range integers, formatted as decimal strings.  Excludes the
# accepted band entirely.  These MUST be rejected.
out_of_range_int_strings = st.integers(min_value=-1_000_000, max_value=1_000_000).filter(
    lambda n: not (DAILY_LIMIT_MIN <= n <= DAILY_LIMIT_MAX)
).map(str)

# Arbitrary text that almost certainly does NOT parse as a decimal int.
# We include common SSM-shaped-but-invalid values explicitly.
non_decimal_int_strings = st.one_of(
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=20),
    st.sampled_from(
        [
            "",            # empty
            "abc",         # non-numeric
            "5.5",         # float-shaped
            "5.0",         # float-shaped (looks like an int but isn't)
            "5e0",         # scientific notation
            "0x5",         # hex literal
            "0b101",       # binary literal
            "five",        # word
            " ",           # whitespace only
            "  ",
            "\t",
            "5,5",         # locale-style
            "5 5",         # space-separated
            "+",           # bare sign
            "-",
            "--5",         # double negative
            "5..0",
        ]
    ),
)


# ---------------------------------------------------------------------------
# Property 17 -- direct validator level
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=DAILY_LIMIT_MIN, max_value=DAILY_LIMIT_MAX))
@settings(max_examples=100, deadline=None)
def test_property_17_accepts_in_range_integers(value: int) -> None:
    """**Validates: Requirements 5.7** -- Property 17 (acceptance branch).

    For any integer ``v`` in ``[5, 10]`` formatted as a decimal string,
    ``_parse_daily_limit`` returns ``v`` unchanged.
    """
    raw = str(value)
    assert _parse_daily_limit(raw) == value


@given(value=st.integers(min_value=-1_000_000, max_value=1_000_000))
@settings(max_examples=200, deadline=None)
@example(value=DAILY_LIMIT_MIN - 1)  # 4 -- adjacent to lower bound
@example(value=DAILY_LIMIT_MAX + 1)  # 11 -- adjacent to upper bound
@example(value=0)
@example(value=-1)
def test_property_17_rejects_out_of_range_integers(value: int) -> None:
    """**Validates: Requirements 5.7** -- Property 17 (rejection branch).

    For any integer ``v`` outside ``[5, 10]`` formatted as a decimal
    string, ``_parse_daily_limit`` raises :class:`ConfigError`.
    """
    assume(not (DAILY_LIMIT_MIN <= value <= DAILY_LIMIT_MAX))
    with pytest.raises(ConfigError):
        _parse_daily_limit(str(value))


@given(raw=non_decimal_int_strings)
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much],
)
def test_property_17_rejects_non_decimal_integer_strings(raw: str) -> None:
    """**Validates: Requirements 5.7** -- Property 17 (type-rejection).

    SSM returns parameter values as strings.  For any string that is not
    a decimal integer literal, ``_parse_daily_limit`` raises
    :class:`ConfigError`; in particular ``"5.5"``, ``"abc"``, the empty
    string, and hex/scientific literals are all rejected.
    """
    # Skip strings that happen to be valid decimal ints in [5, 10] -- a
    # whitespace-padded ``"  5  "`` would pass through ``int("  5  ", 10)``
    # successfully, which is consistent with the implementation's
    # contract.  This filter keeps the property focused on the
    # rejection branch.
    assume(not _daily_limit_should_accept(raw))
    with pytest.raises(ConfigError):
        _parse_daily_limit(raw)


# ---------------------------------------------------------------------------
# Property 17 -- end-to-end through ``config.load`` with a stubbed SSM
# ---------------------------------------------------------------------------


@given(value=st.integers(min_value=DAILY_LIMIT_MIN, max_value=DAILY_LIMIT_MAX))
@settings(max_examples=100, deadline=None)
def test_property_17_load_accepts_in_range_via_ssm(value: int) -> None:
    """**Validates: Requirements 5.7** -- Property 17 end-to-end.

    For any in-range value returned by SSM as the ``daily_limit``
    parameter, ``config.load`` returns a :class:`Config` whose
    ``daily_limit`` equals that value.  No AWS calls are made; SSM is
    stubbed via ``unittest.mock.MagicMock`` injected through the
    documented ``ssm_client`` parameter.
    """
    reset_cache()
    raw = str(value)
    fake = _make_fake_ssm_client(raw)

    cfg = load(ssm_client=fake)

    assert isinstance(cfg, Config)
    assert cfg.daily_limit == value
    # Sanity: the loader called SSM exactly once with WithDecryption=True.
    fake.get_parameters.assert_called_once()
    call_kwargs = fake.get_parameters.call_args.kwargs
    assert call_kwargs.get("WithDecryption") is True


@given(value=st.integers(min_value=-1_000_000, max_value=1_000_000))
@settings(max_examples=100, deadline=None)
@example(value=DAILY_LIMIT_MIN - 1)
@example(value=DAILY_LIMIT_MAX + 1)
def test_property_17_load_rejects_out_of_range_via_ssm(value: int) -> None:
    """**Validates: Requirements 5.7** -- Property 17 end-to-end (reject).

    For any out-of-range integer returned by SSM as the ``daily_limit``
    parameter, ``config.load`` raises :class:`ConfigError` at startup.
    """
    assume(not (DAILY_LIMIT_MIN <= value <= DAILY_LIMIT_MAX))
    reset_cache()
    fake = _make_fake_ssm_client(str(value))

    with pytest.raises(ConfigError):
        load(ssm_client=fake)


# ---------------------------------------------------------------------------
# Boundary smoke tests -- explicit examples to guard against the
# strategies above ever degenerating.  These also document the exact
# acceptance band for human readers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("5", 5), ("6", 6), ("10", 10)])
def test_daily_limit_boundary_examples_accepted(raw: str, expected: int) -> None:
    assert _parse_daily_limit(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "4",          # one below lower bound
        "11",         # one above upper bound
        "0",
        "-1",
        "100",
        "5.0",        # float-shaped
        "abc",
        "",
        "0x5",
    ],
)
def test_daily_limit_boundary_examples_rejected(raw: str) -> None:
    with pytest.raises(ConfigError):
        _parse_daily_limit(raw)
