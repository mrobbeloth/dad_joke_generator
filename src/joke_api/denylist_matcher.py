"""Word-boundary aware, case-insensitive denylist matcher.

This module implements the denylist half of the layered Input_Moderator
and Output_Moderator (see ``design.md`` -> "Components and Interfaces").
Together with Amazon Comprehend's ``DetectToxicContent`` (wired up in
task 4.2 / 4.3), it satisfies R3.3 and Correctness Property 9
("Family-friendliness is the logical OR of denylist and classifier
flags").

Design notes
------------
- The denylist file (``denylist.txt``, co-located with this module) is
  intentionally short. It is a fast, deterministic tripwire for obvious
  offenders; the long tail of toxic content is delegated to Comprehend.
  Real production deployments MUST expand and review the denylist per
  R3.3 on a regular cadence.
- Matching is **case-insensitive** via :py:meth:`str.casefold`, which is
  the Unicode-aware lowercasing operation recommended for caseless
  comparison (handles e.g. German ``ß`` -> ``ss``).
- Matching is **word-boundary aware**: the input text is tokenized via
  ``re.findall(r"[\\w']+", lowered)`` so that punctuation does not hide
  banned tokens (e.g. ``"hell,"`` still matches ``hell``) and so that
  banned substrings inside larger words do *not* trigger a false
  positive (e.g. ``"hello"`` does not match ``hell``).
- This module is a pure stdlib utility: no AWS calls, no logging, no
  network I/O. It is safe to import in any layer.

Public API
----------
- :data:`DEFAULT_DENYLIST_PATH` -- :class:`pathlib.Path` to the bundled
  denylist file.
- :func:`load_denylist` -- read a denylist file into a
  :class:`frozenset`.
- :func:`matches` -- check whether a piece of text contains any
  denylisted token.
- :func:`reset_cache` -- clear the lazily-loaded default denylist (for
  tests).
"""

from __future__ import annotations

import pathlib
import re
from typing import Optional, Union

__all__ = [
    "DEFAULT_DENYLIST_PATH",
    "load_denylist",
    "matches",
    "reset_cache",
]


#: Path to the bundled denylist file shipped alongside this module.
DEFAULT_DENYLIST_PATH: pathlib.Path = pathlib.Path(__file__).with_name(
    "denylist.txt"
)

# Tokenizer pattern: capture runs of word characters plus apostrophes
# (so contractions like "don't" stay together). Punctuation acts as a
# token boundary, giving us word-boundary-aware matching without the
# false positives of naive substring search.
_TOKEN_PATTERN: re.Pattern[str] = re.compile(r"[\w']+")

# Lazily populated cache for the default denylist. Tests can reset this
# via :func:`reset_cache`.
_DEFAULT_DENYLIST: Optional[frozenset[str]] = None


def load_denylist(
    path: Union[pathlib.Path, str, None] = None,
) -> frozenset[str]:
    """Load a denylist file into a :class:`frozenset` of lowercased entries.

    Parameters
    ----------
    path:
        Path to the denylist file. When ``None`` (the default), the
        bundled :data:`DEFAULT_DENYLIST_PATH` is used.

    Returns
    -------
    frozenset[str]
        Immutable set of denylist entries. Each entry has been stripped
        of surrounding whitespace and lowercased via
        :py:meth:`str.casefold`. Blank lines and lines beginning with
        ``#`` are ignored.

    Raises
    ------
    FileNotFoundError
        If the file at ``path`` does not exist.
    """
    resolved: pathlib.Path = (
        DEFAULT_DENYLIST_PATH if path is None else pathlib.Path(path)
    )

    # ``read_text`` raises FileNotFoundError naturally; we let it
    # propagate so callers can distinguish "missing file" from
    # "well-formed but empty file".
    raw = resolved.read_text(encoding="utf-8")

    entries: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.add(stripped.casefold())

    return frozenset(entries)


def matches(
    text: str,
    denylist: Optional[frozenset[str]] = None,
) -> tuple[bool, Optional[str]]:
    """Return whether ``text`` contains any denylisted token.

    The text is lowercased with :py:meth:`str.casefold` (Unicode-aware
    caseless comparison) and tokenized with the pattern ``[\\w']+`` so
    that punctuation acts as a word boundary. Each token is then looked
    up in ``denylist`` directly; the lookup short-circuits on the first
    hit.

    Parameters
    ----------
    text:
        The text to scan. Empty strings and strings containing only
        punctuation produce no tokens and therefore never match.
    denylist:
        An explicit denylist to use. When ``None`` (the default), the
        bundled :data:`DEFAULT_DENYLIST_PATH` is loaded once and cached
        in a module-level variable.

    Returns
    -------
    tuple[bool, str | None]
        ``(True, token)`` on the first denylist hit (where ``token`` is
        the offending lowercased token from ``text``), otherwise
        ``(False, None)``.
    """
    active = denylist if denylist is not None else _get_default_denylist()

    # Empty denylist -> nothing can ever match. Short-circuit before
    # paying the tokenization cost.
    if not active:
        return False, None

    lowered = text.casefold()
    for token in _TOKEN_PATTERN.findall(lowered):
        if token in active:
            return True, token

    return False, None


def reset_cache() -> None:
    """Clear the lazily-loaded default denylist cache.

    Provided for tests that need to swap the denylist file at runtime.
    """
    global _DEFAULT_DENYLIST
    _DEFAULT_DENYLIST = None


def _get_default_denylist() -> frozenset[str]:
    """Return the cached default denylist, loading it on first access."""
    global _DEFAULT_DENYLIST
    if _DEFAULT_DENYLIST is None:
        _DEFAULT_DENYLIST = load_denylist(DEFAULT_DENYLIST_PATH)
    return _DEFAULT_DENYLIST
