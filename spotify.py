"""Spotify catalogue adapter (Web API, client-credentials auth). Metadata reference only: Spotify removed
30 s previews for new apps (Nov 2024), so the audio check for a Spotify reference uses another service.

Credentials come from SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET (environment or `.env`). The secret is only
ever sent to the token endpoint; catalogue calls are GET with a short-lived bearer token.
"""
from __future__ import annotations
import base64, json, os, time, urllib.error, urllib.parse, urllib.request
from typing import Callable
from artists import artist_match as _artist_match
from compare import LibraryTrack, ServiceTrack, normalize_title

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
MIN_INTERVAL = 0.25          # seconds between requests; Spotify rate-limits over a rolling 30 s window
MAX_ALBUMS = 3               # album editions read per library album (e.g. standard + deluxe)
BATCH = 50                   # ids per GET /tracks call (API maximum)
USER_AGENT = "AudioSignatureFFT-compare/1"


class SpotifyUnavailable(RuntimeError):
    """Credentials rejected, or the app may not call the API (e.g. its owner has no Premium subscription)."""


class SpotifyClient:
    """Token handling plus authenticated GET with 401 refresh and 429 back-off."""

    def __init__(self, client_id: str, client_secret: str, sleep=time.sleep, clock=time.monotonic):
        self._id, self._secret, self._sleep, self._clock = client_id, client_secret, sleep, clock
        self._access, self._expires = None, 0.0

    def _token(self) -> str:
        if self._access and self._clock() < self._expires:
            return self._access
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        req = urllib.request.Request(TOKEN_URL, data=b"grant_type=client_credentials", method="POST", headers={
            "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SpotifyUnavailable(f"token request failed (HTTP {exc.code}); check SPOTIFY_CLIENT_ID/SECRET") from None
        self._access = data["access_token"]
        self._expires = self._clock() + int(data.get("expires_in", 3600)) - 60
        return self._access

    def get(self, url: str) -> dict:
        for attempt in range(3):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token()}", "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self._access = None          # token expired early or was revoked: fetch a new one once
                    continue
                if exc.code == 429 and attempt < 2:
                    self._sleep(min(int(exc.headers.get("Retry-After") or 5), 120))
                    continue
                if exc.code == 403:
                    message = exc.read(300).decode("utf-8", "replace").strip()
                    raise SpotifyUnavailable(message or "HTTP 403 from Spotify") from None
                raise
        raise RuntimeError("Spotify kept rate-limiting the request")


class SpotifyAdapter:
    name = "spotify"

    def __init__(self, fetch: Callable[[str], dict], market: str = "US", sleep=time.sleep, clock=time.monotonic):
        self.market, self._fetch, self._sleep, self._clock = market, fetch, sleep, clock
        self._last = None
        self._albums: dict[tuple[str, str], list[ServiceTrack]] = {}

    @classmethod
    def from_env(cls, market: str = "US") -> SpotifyAdapter | None:
        """An adapter when credentials are configured, else None (Spotify is then simply skipped)."""
        client_id, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
        if not (client_id and secret):
            return None
        return cls(SpotifyClient(client_id, secret).get, market)

    def check(self) -> None:
        """One cheap catalogue call; raises (e.g. SpotifyUnavailable) if the API cannot be used."""
        self._get("/search", q="track:test", type="track", limit=1)

    def _get(self, path: str, **params) -> dict:
        if self._last is not None:
            wait = MIN_INTERVAL - (self._clock() - self._last)
            if wait > 0: self._sleep(wait)
        self._last = self._clock()
        return self._fetch(f"{API_URL}{path}?{urllib.parse.urlencode(params)}")

    def _song(self, t: dict, album: str, album_match: bool) -> ServiceTrack:
        millis = t.get("duration_ms")
        isrc = (t.get("external_ids") or {}).get("isrc")
        return ServiceTrack(
            service=self.name, id=t["id"], url=(t.get("external_urls") or {}).get("spotify", ""), title=t.get("name", ""),
            artists=tuple(a.get("name", "") for a in t.get("artists", [])), album=album,
            duration=round(millis / 1000, 3) if millis else None,
            isrc=isrc.upper().replace("-", "") if isrc else None,
            # explicit=false does not say whether this is a clean edition or a song that never had explicit lyrics.
            explicit=True if t.get("explicit") else None,
            track=t.get("track_number"), disc=t.get("disc_number"), album_match=album_match)

    def _albums_for(self, track: LibraryTrack) -> list[dict]:
        base, quals = normalize_title(track.album)
        found = {}
        for query in (f'album:"{track.album}" artist:"{track.album_artist}"', f'album:"{track.album}"'):
            for a in self._get("/search", q=query, type="album", market=self.market, limit=20)["albums"]["items"]:
                if (a and normalize_title(a.get("name", ""))[0] == base
                        and any(_artist_match(track.album_artist, x.get("name", "")) for x in a.get("artists", []))):
                    found.setdefault(a["id"], a)
            if found:
                break   # the artist-qualified query is enough; the plain one rescues symbol-only artist names
        # Same edition qualifiers first (deluxe vs. standard), then full albums before singles/compilations.
        return sorted(found.values(), key=lambda a: (normalize_title(a["name"])[1] != quals, a.get("album_type") != "album"))

    def album_tracks(self, track: LibraryTrack) -> list[ServiceTrack]:
        """All songs from catalogue editions of the library track's album, best edition first. Cached per album."""
        key = (track.album_artist.casefold(), track.album.casefold())
        if key in self._albums:
            return self._albums[key]
        songs: list[ServiceTrack] = []
        if track.album:
            for album in self._albums_for(track)[:MAX_ALBUMS]:
                simple, url = [], f"/albums/{album['id']}/tracks"
                page = self._get(url, market=self.market, limit=50)
                simple.extend(page.get("items", []))
                while page.get("next"):
                    page = self._get(url, market=self.market, limit=50, offset=len(simple))
                    simple.extend(page.get("items", []))
                # Album track listings omit ISRCs; the full track objects carry them.
                full = {}
                for i in range(0, len(simple), BATCH):
                    ids = ",".join(t["id"] for t in simple[i:i + BATCH] if t.get("id"))
                    full.update((t["id"], t) for t in self._get("/tracks", ids=ids, market=self.market).get("tracks", []) if t)
                songs.extend(self._song(full.get(t["id"], t), album["name"], True) for t in simple if t.get("id"))
        self._albums[key] = songs
        return songs

    def _search_tracks(self, query: str) -> list[dict]:
        return [t for t in self._get("/search", q=query, type="track", market=self.market, limit=20)["tracks"]["items"] if t]

    def find(self, track: LibraryTrack) -> list[ServiceTrack]:
        if track.isrc:
            hits = self._search_tracks(f"isrc:{track.isrc}")
            if hits:
                return [self._song(t, (t.get("album") or {}).get("name", ""), False) for t in hits]
        base = normalize_title(track.title)[0]
        same_title = [s for s in self.album_tracks(track) if normalize_title(s.title)[0] == base]
        if same_title:
            # Prefer the same position on the album when an edition repeats a title.
            return sorted(same_title, key=lambda s: (s.disc or 1, s.track) != (track.disc or 1, track.track))
        return [self._song(t, (t.get("album") or {}).get("name", ""), False)
                for t in self._search_tracks(f'track:"{track.title}" artist:"{track.artist}"')
                if any(_artist_match(track.artist, a.get("name", "")) for a in t.get("artists", []))]
