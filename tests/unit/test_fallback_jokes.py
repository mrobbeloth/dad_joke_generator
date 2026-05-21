"""Unit tests for the curated fallback joke list.

Validates: Requirement 4.3 — the Joke_API SHALL return a fallback joke
randomly selected from a curated safe-joke list containing at least 20
entries. Also enforces the joint length contract from Requirement 1.4
(10..80 word window) so that fallback jokes satisfy the same length
guarantee as generated jokes.
"""

from __future__ import annotations

from joke_api.fallback_jokes import FALLBACK_JOKES


def test_fallback_jokes_has_at_least_20_entries() -> None:
    """R4.3: curated safe-joke list must contain at least 20 entries."""
    assert len(FALLBACK_JOKES) >= 20


def test_fallback_jokes_are_unique() -> None:
    """Duplicates would weaken the 'randomly selected' guarantee in R4.3."""
    assert len(set(FALLBACK_JOKES)) == len(FALLBACK_JOKES)


def test_fallback_jokes_are_non_empty_strings() -> None:
    """Every entry must be a non-empty ``str``."""
    for index, joke in enumerate(FALLBACK_JOKES):
        assert isinstance(joke, str), (
            f"FALLBACK_JOKES[{index}] is not a str: {type(joke).__name__}"
        )
        assert joke, f"FALLBACK_JOKES[{index}] is empty"


def test_fallback_jokes_word_count_within_bounds() -> None:
    """R1.4: each fallback joke must have between 10 and 80 words inclusive."""
    for index, joke in enumerate(FALLBACK_JOKES):
        word_count = len(joke.split())
        assert 10 <= word_count <= 80, (
            f"FALLBACK_JOKES[{index}] has {word_count} words; "
            "must be between 10 and 80 inclusive"
        )
