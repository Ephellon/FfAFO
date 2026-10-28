"""MusicBrainz client: artist aliases and reference releases for the album TRACKLIST check (GET only).

MusicBrainz asks for at most one request per second and a descriptive User-Agent with a contact
(MUSICBRAINZ_CONTACT in `.env`). Responses are cached in SQLite so re-runs cost no requests.
"""
from __future__ import annotations
import json, os, sqlite3, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path
from typing import Callable
from compare import LibraryTrack, normalize_title

API_URL = "https://musicbrainz.org/ws/2"
MIN_INTERVAL = 1.1           # seconds between requests (MusicBrainz limit: 1 per second)
CACHE_SECONDS = 30 * 86400   # catalogue data changes slowly; refresh cached responses after 30 days
WORLDWIDE = ("XW", "XE")     # release countries that are not a regional edition


def _user_agent() -> str:
    contact = os.environ.get("MUSICBRAINZ_CONTACT", "").strip()
    return f"AudioSignatureFFT/1.0 ( {contact or 'no contact configured'} )"


def http_get_json(url: str, sleep=time.sleep) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(), "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 503 or attempt == 3:
                raise
            sleep(2 ** (attempt + 1))   # MusicBrainz answers rate-limit breaches with 503
    raise RuntimeError("unreachable")


def _quote(text: str) -> str:
    """A Lucene phrase with quotes and backslashes escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MusicBrainz:
    def __init__(self, cache_path: Path | None = None, fetch: Callable[[str], dict] = http_get_json,
                 sleep=time.sleep, clock=time.monotonic, now=time.time):
        self._fetch, self._sleep, self._clock, self._now = fetch, sleep, clock, now
        self._last, self._memory, self._names = None, {}, {}
        self._db = None
        if cache_path is not None:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(cache_path)
            self._db.execute("CREATE TABLE IF NOT EXISTS responses (url TEXT PRIMARY KEY, body TEXT NOT NULL, fetched_at REAL NOT NULL)")

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _get(self, path: str, **params) -> dict:
        url = f"{API_URL}/{path}?{urllib.parse.urlencode({**params, 'fmt': 'json'})}"
        if url in self._memory:
            return self._memory[url]
        if self._db is not None:
            row = self._db.execute("SELECT body, fetched_at FROM responses WHERE url=?", (url,)).fetchone()
            if row and self._now() - row[1] < CACHE_SECONDS:
                self._memory[url] = json.loads(row[0])
                return self._memory[url]
        if self._last is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last)
            if wait > 0: self._sleep(wait)
        self._last = self._clock()
        data = self._fetch(url)
        self._memory[url] = data
        if self._db is not None:
            with self._db:
                self._db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?)", (url, json.dumps(data), self._now()))
        return data

    def artist_names(self, name: str) -> frozenset[str]:
        """Casefolded names and aliases of every artist whose name or alias is exactly `name`."""
        key = name.casefold().strip()
        if key not in self._names:
            names = {key}
            for artist in self._get("artist", query=f"artist:{_quote(name)} OR alias:{_quote(name)}", limit=10).get("artists", []):
                known = {artist.get("name", "").casefold()} | {a.get("name", "").casefold() for a in artist.get("aliases") or []}
                if key in known:   # exact only: searches also return tribute bands and collaborations
                    names |= known
            self._names[key] = frozenset(n for n in names if n)
        return self._names[key]

    def release_group_id(self, track: LibraryTrack) -> str | None:
        """From the file's MusicBrainz tags when present, else a release search (title + alias-aware artist)."""
        if track.mb_release_group_id:
            return track.mb_release_group_id
        if track.mb_album_id:
            return (self._get(f"release/{track.mb_album_id}", inc="release-groups").get("release-group") or {}).get("id")
        if not track.album:
            return None
        base, quals = normalize_title(track.album)
        artist = self.artist_names(track.album_artist)
        found = self._get("release", query=f"release:{_quote(track.album)} AND artist:{_quote(track.album_artist)}", limit=25)
        matches = [r for r in found.get("releases", [])
                   if normalize_title(r.get("title", ""))[0] == base
                   and {c.get("name", "").casefold() for c in r.get("artist-credit", [])} & artist]
        # Same edition qualifiers first, so "Album" does not resolve to "Album (Deluxe)".
        matches.sort(key=lambda r: normalize_title(r["title"])[1] != quals)
        return (matches[0].get("release-group") or {}).get("id") if matches else None

    def reference_release(self, track: LibraryTrack) -> dict | None:
        """Latest Official worldwide release in the album's release group (full tracklist), else the latest release."""
        group = self.release_group_id(track)
        if not group:
            return None
        releases = self._get("release", **{"release-group": group}, limit=100).get("releases", [])
        if not releases:
            return None
        best = max(releases, key=lambda r: (r.get("status") == "Official", r.get("country") in WORLDWIDE, r.get("date") or ""))
        return self._get(f"release/{best['id']}", inc="recordings")
