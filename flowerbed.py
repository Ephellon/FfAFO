"""Flowerbed roots: registry roots derived from a file's own tags instead of issued at random (PLAN.md §9.2).

  key  = "<ARTIST>/<TITLE>/<YYYY.MM>"          every part from the file's metadata, normalized
  root = SHA-1("flowerbed-v1\\n" + key)         exactly ROOT_BYTES (20), so it fits where a random root does

The same tags give the same root on every machine, without asking a registry. Changing anything here
changes every root, so a new recipe must get a new SCHEME.
"""
from __future__ import annotations
import hashlib, re, unicodedata
from audio_sig import ROOT_BYTES, encode_bytes

SCHEME = "flowerbed-v1"
RANDOM = "random"           # scheme of registry-issued roots
_DATE = re.compile(r"\s*(\d{4})(?:[-./](\d{1,2}))?")


def _punctuation(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")   # P* only: "$" (Sc) keeps its spaces, as in "TY DOLLA $IGN"


def normalize(text: str) -> str:
    """NFC, UPPERCASE, whitespace runs to one space, no spaces touching punctuation."""
    text = " ".join(unicodedata.normalize("NFC", text).upper().split())
    out = []
    for i, ch in enumerate(text):
        if ch == " " and (_punctuation(out[-1]) or _punctuation(text[i + 1])):
            continue    # split() left no leading/trailing space, so both neighbours exist
        out.append(ch)
    return "".join(out)


def artists(text: str) -> str:
    """Track artist(s), each trimmed, joined by ';' (multi-value tags may arrive NUL-separated)."""
    return ";".join(a for a in normalize(text.replace("\0", ";")).split(";") if a)


def month(date: str) -> str | None:
    """'2024-08-03' -> '2024.08'; a year alone means January ('2024' -> '2024.01'). None if no year."""
    m = _DATE.match(date or "")
    if not m:
        return None
    mm = int(m.group(2) or 1)
    return f"{m.group(1)}.{mm:02d}" if 1 <= mm <= 12 else None


def key(artist: str, title: str, date: str) -> str:
    parts = artists(artist), normalize(title), month(date)
    if not all(parts):
        raise ValueError("flowerbed needs artist, title and a dated year in the file's tags")
    return "/".join(parts)


def key_from_tags(tags: dict) -> str | None:
    """Key from case-folded ffprobe tags, or None when the file lacks artist, title or date."""
    get = lambda *names: next((str(tags[n]) for n in names if tags.get(n) not in (None, "")), "")
    try:
        return key(get("artist"), get("title"), get("date", "year", "original date", "originaldate"))
    except ValueError:
        return None


def root(flowerbed_key: str) -> bytes:
    digest = hashlib.sha1(f"{SCHEME}\n{flowerbed_key}".encode("utf-8")).digest()
    assert len(digest) == ROOT_BYTES
    return digest


def root_text(flowerbed_key: str) -> str:
    return encode_bytes(root(flowerbed_key))
