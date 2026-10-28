"""Apple Music catalogue adapter using the public iTunes Search/Lookup API (no auth, GET only)."""
from __future__ import annotations
import json, time, urllib.error, urllib.parse, urllib.request
from typing import Callable
from compare import LibraryTrack, ServiceTrack, normalize_title

SEARCH_URL = "https://itunes.apple.com/search"
LOOKUP_URL = "https://itunes.apple.com/lookup"
MIN_INTERVAL = 3.1           # seconds between requests; Apple allows roughly 20 per minute
MAX_COLLECTIONS = 3          # album editions looked up per library album (e.g. explicit + clean + deluxe)
EXPLICITNESS = {"explicit": True, "cleaned": False}   # "notExplicit" says nothing about which edition we have


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "AudioSignatureFFT-compare/1"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt == 0:
                time.sleep(60)   # Apple answers rate-limit breaches with 403/429
                continue
            raise
    raise RuntimeError("unreachable")


def _artist_match(library_artist: str, service_artist: str) -> bool:
    a, b = library_artist.casefold().strip(), service_artist.casefold()
    return bool(a) and (a in b or b in a)


class AppleAdapter:
    name = "apple"

    def __init__(self, country: str = "US", fetch: Callable[[str], dict] = http_get_json, sleep=time.sleep, clock=time.monotonic):
        self.country, self._fetch, self._sleep, self._clock = country, fetch, sleep, clock
        self._last = None
        self._albums: dict[tuple[str, str], list[ServiceTrack]] = {}

    def _get(self, base: str, **params) -> list[dict]:
        if self._last is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last)
            if wait > 0: self._sleep(wait)
        self._last = self._clock()
        return self._fetch(base + "?" + urllib.parse.urlencode({**params, "country": self.country})).get("results", [])

    def _song(self, r: dict, album_match: bool) -> ServiceTrack:
        millis = r.get("trackTimeMillis")
        return ServiceTrack(
            service=self.name, id=str(r["trackId"]), url=r.get("trackViewUrl", ""), title=r.get("trackName", ""),
            artists=(r.get("artistName", ""),), album=r.get("collectionName", ""),
            duration=round(millis / 1000, 3) if millis else None, explicit=EXPLICITNESS.get(r.get("trackExplicitness")),
            preview_url=r.get("previewUrl"), track=r.get("trackNumber"), disc=r.get("discNumber"), album_match=album_match)

    def album_tracks(self, track: LibraryTrack) -> list[ServiceTrack]:
        """All songs from catalogue editions of the library track's album, best edition first. Cached per album."""
        key = (track.album_artist.casefold(), track.album.casefold())
        if key in self._albums:
            return self._albums[key]
        songs: list[ServiceTrack] = []
        if track.album:
            base, quals = normalize_title(track.album)
            albums = [a for a in self._get(SEARCH_URL, term=f"{track.album_artist} {track.album}", entity="album",
                                           media="music", limit=25)
                      if normalize_title(a.get("collectionName", ""))[0] == base
                      and _artist_match(track.album_artist, a.get("artistName", ""))]
            # Same edition qualifiers first (deluxe vs. standard), then explicit before clean unless the file says clean.
            prefer_clean = track.explicit is False
            albums.sort(key=lambda a: (normalize_title(a["collectionName"])[1] != quals,
                                       (a.get("collectionExplicitness") == "explicit") == prefer_clean))
            for album in albums[:MAX_COLLECTIONS]:
                results = self._get(LOOKUP_URL, id=album["collectionId"], entity="song")
                songs.extend(self._song(r, True) for r in results if r.get("wrapperType") == "track")
        self._albums[key] = songs
        return songs

    def find(self, track: LibraryTrack) -> list[ServiceTrack]:
        base = normalize_title(track.title)[0]
        same_title = [s for s in self.album_tracks(track) if normalize_title(s.title)[0] == base]
        if same_title:
            # Prefer the same position on the album when an edition repeats a title.
            return sorted(same_title, key=lambda s: (s.disc or 1, s.track) != (track.disc or 1, track.track))
        results = self._get(SEARCH_URL, term=f"{track.artist} {track.title}", entity="song", media="music", limit=25)
        return [self._song(r, False) for r in results
                if r.get("wrapperType") == "track" and _artist_match(track.artist, r.get("artistName", ""))]
