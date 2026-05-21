"""Curated safe-joke fallback list (Requirement R4.3 / Correctness Property 13).

This module is the safety net used by the generation pipeline when every
Bedrock attempt is rejected by the ``Output_Moderator`` (or the moderator
is unavailable on every attempt). Per Property 13 the curated list MUST
contain at least :data:`MIN_FALLBACK_COUNT` entries, each of which fits
the same length window as a generated joke (10..80 words inclusive,
R1.4) and stays well under the 1500-character Polly synthesis cap (R2.9).

The module intentionally has no AWS dependencies; it is a leaf utility
that holds static data and a thin selector helper. Call sites should
import :data:`FALLBACK_JOKES` and :func:`select`.

All jokes are original, family-friendly one-liners written for this
project. They contain no profanity, slurs, sexual content, drug
references, graphic violence, targeted harassment, political content,
or named brands, people, or copyrighted characters.

An import-time guard (:func:`_validate_jokes`) verifies the list is
well-formed so a corrupted edit fails fast rather than producing an
unusable fallback at request time.
"""

from __future__ import annotations

import random

__all__ = ["FALLBACK_JOKES", "MIN_FALLBACK_COUNT", "select"]


# Documented contract: at least 20 entries are required (Property 13).
# The curated list below carries a cushion above this floor so future
# edits do not accidentally drop below the minimum.
MIN_FALLBACK_COUNT: int = 20


FALLBACK_JOKES: tuple[str, ...] = (
    "Why did the bicycle fall over in the parking lot yesterday afternoon? "
    "Because it was simply two-tired after the long ride home.",
    "I asked the gardener why his plants kept laughing at every joke he told, "
    "and he said it was because they have such great roots in comedy.",
    "What do you call a sleeping dinosaur that snores so loudly the whole "
    "museum trembles whenever the security guard walks by? A stega-snore-us.",
    "I told my kids the joke about the broken pencil, but it was completely "
    "pointless, and even the dog rolled his eyes from across the room.",
    "My friend asked me to help him round up his cattle on Saturday, so I "
    "drew a circle in the dirt and called it good.",
    "Why do scarecrows always win awards every harvest season at the county "
    "fair where the judges wear straw hats? They are outstanding in the field.",
    "I bought new shoes from a blacksmith last week, and the moment I walked "
    "out the door I made a bolt for it down the street.",
    "My calendar is now my best friend at work because it is the only thing "
    "in the office that always has its days numbered without complaint.",
    "What do you call a fish wearing a tiny crown floating around the coral "
    "reef shouting commands at the other fish all day long? Your haddock-ness.",
    "I tried to write a song about a tortilla one evening, but in the end I "
    "realized it was just a wrap and folded the whole project up.",
    "My wife asked me to stop singing the theme to the laundry hamper, so "
    "naturally I switched to humming it loudly while folding towels instead.",
    "Why did the math book look so sad and gloomy when it walked into the "
    "library on Monday morning? Because it was absolutely full of problems.",
    "I told the carpenter that his workshop jokes were dull, and he just "
    "nailed it down with a perfectly straight face and asked me to leaf.",
    "What did the grape say after it got stepped on by a hiker on the trail "
    "one Sunday afternoon? Nothing at all, it merely let out a little wine.",
    "My toaster started telling jokes this morning, but every punchline came "
    "out a little burnt around the edges, so we had to settle for cereal.",
    "Why do bees hum so cheerfully on the way to work in the meadow every "
    "weekday morning? Because none of them can ever remember the words.",
    "I asked the librarian if the building had any books on paranoia, and "
    "she leaned in close and whispered, they are right behind you somewhere.",
    "What do you call a cow that is excellent at solving riddles around the "
    "barn after the chores are completely finished for the day? A moo-ver.",
    "My garden hose got a job as a stand-up comedian last weekend, and so "
    "far the only feedback the audience gave was that he leaks too often.",
    "Why did the trumpet always show up early to every morning rehearsal in "
    "the music hall? Because it never wanted to be the one left flat.",
    "I told the river that I admired its sense of humor, and it just kept "
    "flowing along and said it always tries to go with the current trends.",
    "What do you get when you cross a snowman with a vampire on a cold "
    "winter evening when the moon is full and bright? A case of frostbite.",
    "My dog learned to play chess over the weekend, and now every time he "
    "loses a piece he simply rolls over and pretends he was just napping.",
    "Why did the tomato turn a deep shade of red while crossing the kitchen "
    "counter on its way to the salad bowl? Because it saw the dressing.",
    "I tried to start a band made up entirely of office supplies, but the "
    "stapler kept stealing all the punchlines and the tape stuck to itself.",
    "What do you call a pile of cats sitting quietly in the front row of a "
    "small comedy club waiting for the show to start? A meow-dience.",
    "My uncle keeps a tiny notebook full of bird puns in his pocket, and "
    "every time he opens it the whole family groans loud enough to scare birds.",
    "Why did the pancake refuse to leave the griddle even after the timer "
    "went off and the kitchen smelled like a perfect Sunday breakfast? Attached.",
)


def _validate_jokes(jokes: tuple[str, ...]) -> None:
    """Assert the curated joke list is well-formed.

    Raises :class:`ValueError` on the first violation found. Invoked at
    import time so a corrupted edit fails fast rather than during a
    moderation fallback at request time.
    """
    if len(jokes) < MIN_FALLBACK_COUNT:
        raise ValueError("FALLBACK_JOKES has fewer than 20 entries")

    seen: set[str] = set()
    for index, text in enumerate(jokes):
        if not isinstance(text, str):
            raise ValueError(
                f"FALLBACK_JOKES[{index}] is not a str: {type(text).__name__}"
            )
        word_count = len(text.split())
        if word_count < 10 or word_count > 80:
            raise ValueError(
                f"FALLBACK_JOKES[{index}] has {word_count} words; "
                "must be between 10 and 80 inclusive"
            )
        if len(text) > 1500:
            raise ValueError(
                f"FALLBACK_JOKES[{index}] is {len(text)} characters; "
                "must be at most 1500"
            )
        if text in seen:
            raise ValueError(f"FALLBACK_JOKES[{index}] is a duplicate entry")
        seen.add(text)


def select(rng: random.Random | None = None) -> str:
    """Return a uniformly random fallback joke.

    Parameters
    ----------
    rng:
        Optional :class:`random.Random` instance for deterministic
        selection in tests. When ``None`` the module-level
        :func:`random.choice` is used.
    """
    if rng is None:
        return random.choice(FALLBACK_JOKES)
    return rng.choice(FALLBACK_JOKES)


# Validate at import so a malformed edit cannot reach production.
_validate_jokes(FALLBACK_JOKES)
