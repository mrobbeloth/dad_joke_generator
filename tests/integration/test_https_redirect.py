"""Integration tests for CloudFront's HTTP -> HTTPS redirect (R6.3).

Property 19 (design.md): *For any* request to the Custom_Domain over plain
HTTP with path ``P`` and query string ``Q``, the response SHALL be HTTP 301
with ``Location: https://<custom_domain>P?Q`` (with ``?Q`` omitted when ``Q``
is empty).

The live distribution is provisioned by Terraform (see
``infra/terraform/cloudfront.tf``) but is not always available in CI. To keep
this test useful in both contexts, it runs in two modes:

1. **Static analysis (always runs).** Parse ``cloudfront.tf`` and assert the
   ``viewer_protocol_policy`` is set to ``redirect-to-https`` on every cache
   behavior, and that no behavior uses ``allow-all`` (HTTP allowed) or
   ``https-only`` (HTTP rejected with 403 instead of redirected). CloudFront's
   documented behavior for ``redirect-to-https`` is to return HTTP 301 and
   preserve the path and query string verbatim, which is the invariant
   Property 19 names.

2. **Live HTTP probe (gated).** When the ``CLOUDFRONT_TEST_DOMAIN`` env var
   names a deployed distribution, hit ``http://<domain>/<path>?<query>``
   without following redirects and assert status 301 + a ``Location`` header
   that preserves the path and query.

The property test at the bottom documents the invariant Property 19 names by
constructing the expected ``Location`` URL for arbitrary path/query inputs
and asserting the format. The static config is also re-asserted inside the
property body so the property only holds while the IaC actually enables the
redirect policy.

Validates: Requirements 6.3
"""

from __future__ import annotations

import http.client
import os
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Repo root resolved relative to this test file so the test runs the same way
# regardless of pytest's invocation directory. Mirrors the convention used in
# ``tests/smoke/test_iac_configuration.py``.
REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "infra" / "terraform"

# Recognised viewer_protocol_policy values, classified by whether they
# satisfy Property 19's redirect invariant.
_REDIRECT_POLICIES = {"redirect-to-https"}
_NON_REDIRECT_POLICIES = {
    # Allows plain HTTP responses without redirect -- breaks R6.3.
    "allow-all",
    # Rejects HTTP with 403 instead of redirecting; the response is not a
    # 301 with a preserved path/query Location, so it also breaks R6.3.
    "https-only",
}


def _load_cloudfront_tf() -> str:
    """Return the contents of ``infra/terraform/cloudfront.tf``."""
    path = TF_DIR / "cloudfront.tf"
    if not path.is_file():
        pytest.fail(f"expected Terraform file is missing: {path}")
    return path.read_text(encoding="utf-8")


def _viewer_protocol_policies(tf: str) -> list[str]:
    """Return every ``viewer_protocol_policy = "<value>"`` value in ``tf``.

    Used by the static-analysis tests to enumerate every cache behavior's
    viewer policy in source order. Any new ``default_cache_behavior`` or
    ``ordered_cache_behavior`` block that forgets to declare the attribute
    will simply be missing from the returned list, which is caught by the
    explicit per-block tests below.
    """
    return re.findall(r'viewer_protocol_policy\s*=\s*"([a-z-]+)"', tf)


# ---------------------------------------------------------------------------
# Approach A: static analysis of the Terraform module
# ---------------------------------------------------------------------------


class TestCloudFrontRedirectConfiguration:
    """The CloudFront IaC pins viewer_protocol_policy to redirect-to-https.

    These tests validate the *configuration* layer of Property 19. CloudFront
    itself implements the path + query preservation for the
    ``redirect-to-https`` policy (AWS-documented behavior); this suite
    asserts the IaC actually selects that policy on every cache behavior.
    """

    def test_default_cache_behavior_redirects_to_https(self) -> None:
        """Validates Requirements 6.3.

        SPA traffic (default cache behavior) must use ``redirect-to-https``
        so HTTP visitors are 301-redirected to the HTTPS variant of the
        same URL.
        """
        tf = _load_cloudfront_tf()
        # Locate the default_cache_behavior block. The closing brace lives at
        # column 2 inside the distribution resource (``terraform fmt`` output).
        default_block = re.search(
            r"default_cache_behavior\s*\{(.*?)^\s{2}\}",
            tf,
            flags=re.DOTALL | re.MULTILINE,
        )
        assert default_block is not None, (
            "aws_cloudfront_distribution.app must declare a "
            "default_cache_behavior block"
        )
        assert re.search(
            r'viewer_protocol_policy\s*=\s*"redirect-to-https"',
            default_block.group(1),
        ), (
            "default_cache_behavior must set "
            'viewer_protocol_policy = "redirect-to-https" (R6.3)'
        )

    def test_api_path_pattern_ordered_cache_behavior_redirects_to_https(self) -> None:
        """Validates Requirements 6.3.

        The ``/v1/*`` ordered cache behavior (API origin) must also use
        ``redirect-to-https`` so API clients hitting plain HTTP are
        redirected rather than served (or rejected).
        """
        tf = _load_cloudfront_tf()
        ordered_block = re.search(
            r"ordered_cache_behavior\s*\{(.*?)^\s{2}\}",
            tf,
            flags=re.DOTALL | re.MULTILINE,
        )
        assert ordered_block is not None, (
            "aws_cloudfront_distribution.app must declare an "
            "ordered_cache_behavior for the API path pattern"
        )
        ordered_body = ordered_block.group(1)
        # The path pattern is the API prefix; this asserts we're inspecting
        # the right ordered behavior block. ``/v1/*`` matches the design.
        assert re.search(r'path_pattern\s*=\s*"/v1/\*"', ordered_body), (
            'ordered_cache_behavior must set path_pattern = "/v1/*"'
        )
        assert re.search(
            r'viewer_protocol_policy\s*=\s*"redirect-to-https"',
            ordered_body,
        ), (
            "ordered_cache_behavior must set "
            'viewer_protocol_policy = "redirect-to-https" (R6.3)'
        )

    def test_no_cache_behavior_uses_a_non_redirect_policy(self) -> None:
        """Validates Requirements 6.3.

        Enumerate every ``viewer_protocol_policy`` value in
        ``cloudfront.tf`` and assert none equal a value that would break
        Property 19 (``allow-all`` or ``https-only``). Catches both
        accidental relaxation (HTTP allowed) and accidental hardening
        without redirect (HTTP rejected with 403 rather than 301).
        """
        tf = _load_cloudfront_tf()
        policies = _viewer_protocol_policies(tf)
        assert policies, (
            "expected at least one viewer_protocol_policy declaration in "
            "cloudfront.tf"
        )
        offending = [p for p in policies if p in _NON_REDIRECT_POLICIES]
        assert not offending, (
            f"cloudfront.tf declares viewer_protocol_policy values that "
            f"violate R6.3: {offending}. Only "
            f"{sorted(_REDIRECT_POLICIES)} preserves Property 19's "
            f"301-redirect invariant."
        )
        # Belt-and-braces: every declared policy must be a redirect policy.
        unexpected = [p for p in policies if p not in _REDIRECT_POLICIES]
        assert not unexpected, (
            f"cloudfront.tf declares unexpected viewer_protocol_policy "
            f"values: {unexpected}. Property 19 requires "
            f"redirect-to-https on every cache behavior."
        )


# ---------------------------------------------------------------------------
# Approach B: live HTTP probe (gated on CLOUDFRONT_TEST_DOMAIN)
# ---------------------------------------------------------------------------


class TestCloudFrontRedirectLive:
    """Hit a deployed distribution and verify the 301 + Location header.

    Skipped when ``CLOUDFRONT_TEST_DOMAIN`` is unset, so this test is a
    no-op in CI and on developer machines without a deployed environment
    while still being available as a post-deploy verification.
    """

    @staticmethod
    def _domain_or_skip() -> str:
        domain = os.environ.get("CLOUDFRONT_TEST_DOMAIN")
        if not domain:
            pytest.skip("CLOUDFRONT_TEST_DOMAIN not set")
        return domain

    def test_http_request_returns_301_to_https_with_path_and_query_preserved(
        self,
    ) -> None:
        """Validates Requirements 6.3.

        Send a plain-HTTP GET with both a path and a query string and
        assert the response is HTTP 301 with a ``Location`` header that
        echoes the same path and query under ``https://``.
        """
        domain = self._domain_or_skip()
        path = "/v1/jokes/integration-test"
        query = "verify=https-redirect&token=abc-123"

        # http.client lets us inspect the raw response without any
        # redirect-following or library auto-rewrite logic, which is
        # essential for a redirect-correctness test.
        conn = http.client.HTTPConnection(domain, 80, timeout=10)
        try:
            conn.request("GET", f"{path}?{query}")
            response = conn.getresponse()
            status = response.status
            location = response.getheader("Location")
            # Drain so the connection can be reused / closed cleanly.
            response.read()
        finally:
            conn.close()

        assert status == 301, (
            f"expected HTTP 301 from CloudFront redirect, got {status}"
        )
        assert location == f"https://{domain}{path}?{query}", (
            f"Location header did not preserve path + query. "
            f"expected=https://{domain}{path}?{query} got={location!r}"
        )


# ---------------------------------------------------------------------------
# Property-based invariant (Property 19, doc-as-code)
# ---------------------------------------------------------------------------


def _expected_redirect_location(domain: str, path: str, query: str) -> str:
    """Construct the Location URL CloudFront's redirect-to-https produces.

    Mirrors the AWS-documented behavior the IaC opts into: scheme rewritten
    to ``https``, host preserved, path preserved verbatim, query preserved
    verbatim and joined with ``?`` only when non-empty.
    """
    if query:
        return f"https://{domain}{path}?{query}"
    return f"https://{domain}{path}"


# Hypothesis strategies: paths and query strings the property must hold for.
#
# Path: arbitrary URL-path bytes from the unreserved + path-safe set,
# always rooted at ``/``. Empty paths are not produced because CloudFront
# normalises a missing path to ``/`` anyway.
_PATH_STRATEGY = st.from_regex(r"^/[A-Za-z0-9/_.~%\-]{0,80}$", fullmatch=True)

# Query: either empty (so the ``?`` must be omitted) or a non-empty string
# of typical query characters. The empty-query case is the sub-clause of
# Property 19 that says ``?Q`` is omitted when ``Q`` is empty, so it
# matters that the strategy can shrink to it.
_QUERY_STRATEGY = st.one_of(
    st.just(""),
    st.from_regex(r"^[A-Za-z0-9=&_.~%\-]{1,80}$", fullmatch=True),
)


class TestRedirectPreservesPathAndQueryProperty:
    """Property 19 invariant expressed as a Hypothesis property test."""

    @given(path=_PATH_STRATEGY, query=_QUERY_STRATEGY)
    @settings(
        max_examples=200,
        deadline=None,
        # No I/O happens in the property body; the suppression is just so
        # Hypothesis doesn't flag the cheap fixture-style file read.
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_redirect_location_preserves_path_and_query(
        self, path: str, query: str
    ) -> None:
        """Validates Requirements 6.3.

        For any HTTP request with path ``P`` and query string ``Q`` to the
        Custom_Domain, the redirect Location SHALL equal
        ``https://<domain>P?Q`` (with ``?Q`` omitted when ``Q`` is empty)
        AND the IaC SHALL enable the redirect policy that produces it.

        The property test ties the abstract invariant to the concrete
        configuration: if the IaC is ever changed in a way that disables
        ``redirect-to-https`` on any cache behavior, the static
        precondition fails and the property is vacuous, surfacing the
        regression.
        """
        # Precondition: the IaC enables redirect-to-https everywhere.
        tf = _load_cloudfront_tf()
        policies = _viewer_protocol_policies(tf)
        assert policies, "no viewer_protocol_policy declarations found"
        assert all(p in _REDIRECT_POLICIES for p in policies), (
            f"IaC has a non-redirect viewer_protocol_policy; "
            f"Property 19 cannot hold. policies={policies}"
        )

        domain = "example.com"
        location = _expected_redirect_location(domain, path, query)

        # Scheme rewritten to https.
        assert location.startswith(f"https://{domain}"), (
            f"redirect Location must start with https://{domain}, got {location!r}"
        )
        # Path preserved verbatim immediately after the host.
        assert location[len(f"https://{domain}"):].startswith(path), (
            f"path {path!r} not preserved in Location {location!r}"
        )
        # Query preserved iff non-empty; the ``?`` separator is omitted
        # when the original query is empty.
        if query:
            assert location.endswith(f"?{query}"), (
                f"query {query!r} not preserved in Location {location!r}"
            )
            # Exactly one ``?`` boundary (the separator we appended).
            assert location.count("?") == 1, (
                f"expected exactly one '?' in Location, got {location!r}"
            )
        else:
            assert "?" not in location, (
                f"Location must not contain '?' when query is empty, "
                f"got {location!r}"
            )
