"""Artist-name matching shared by the service adapters, with optional alias lookup (e.g. Kanye West / Ye)."""
from __future__ import annotations
import re
from typing import Callable

# Splits service credits such as "¥$, Kanye West & Ty Dolla $ign" into single names.
_CREDIT_SPLIT = re.compile(r"\s*(?:,|&|\+|/|\bx\b|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\band\b)\s*", re.I)
_alias_source: Callable[[str], frozenset[str]] | None = None


def set_alias_source(source: Callable[[str], frozenset[str]] | None) -> None:
    """Install a lookup returning every casefolded name an artist is known by (None disables aliases)."""
    global _alias_source
    _alias_source = source


def _contains(longer: str, shorter: str) -> bool:
    """Whole-word containment ("ye" is in "ye & drake" but not in "kanye"); works for symbol names like "¥$"."""
    return re.search(rf"(?<!\w){re.escape(shorter)}(?!\w)", longer) is not None


def artist_match(library_artist: str, service_artist: str) -> bool:
    """Whole-word containment either way; failing that, a credited name matching one of the artist's aliases."""
    a, b = library_artist.casefold().strip(), service_artist.casefold().strip()
    if not a or not b:
        return False
    if _contains(b, a) or _contains(a, b):
        return True
    if _alias_source is None:
        return False
    try:
        names = _alias_source(library_artist)
    except Exception:   # alias lookups are a bonus; a network failure must not break matching
        return False
    credited = {b} | {part for part in _CREDIT_SPLIT.split(b) if part}
    return bool(names & credited)
