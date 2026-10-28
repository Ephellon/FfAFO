import contextlib, io, json, os, unittest, urllib.error, urllib.parse
from unittest import mock
import compare, spotify
from compare import ServiceTrack
from spotify import SpotifyAdapter, SpotifyClient, SpotifyUnavailable
from test_compare import FakeAdapter, lib


def sp_track(tid, name, number, ms=200_000, explicit=False, isrc=None, artist="Artist"):
    t = {"id": tid, "name": name, "track_number": number, "disc_number": 1, "duration_ms": ms, "explicit": explicit,
         "artists": [{"name": artist}], "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"}}
    if isrc: t["external_ids"] = {"isrc": isrc}
    return t


class FakeApi:
    """Answers the Spotify endpoints the adapter uses; records every request path + query."""
    def __init__(self, albums, album_tracks, tracks=(), artist_albums=None):
        self.albums, self.album_tracks, self.tracks = albums, album_tracks, list(tracks)
        self.artist_albums = artist_albums
        self.calls = []

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, q))
        path = parsed.path.removeprefix("/v1")
        if path == "/search" and q["type"] == "album":
            items = self.artist_albums if (self.artist_albums is not None and "artist:" in q["q"]) else self.albums
            return {"albums": {"items": items}}
        if path == "/search":
            return {"tracks": {"items": [t for t in self.tracks if q["q"].startswith("isrc:") == bool(t.get("external_ids"))]}}
        if path.startswith("/albums/"):
            return {"items": self.album_tracks[path.split("/")[2]], "next": None}
        if path == "/tracks":
            by_id = {t["id"]: t for ts in self.album_tracks.values() for t in ts}
            return {"tracks": [{**by_id[i], "external_ids": {"isrc": f"us-abc-24-0000{i[-1]}"}} for i in q["ids"].split(",")]}
        raise AssertionError(url)


ALBUM = {"id": "A1", "name": "Album", "album_type": "album", "artists": [{"name": "Artist"}]}
DELUXE = {"id": "A2", "name": "Album (Deluxe)", "album_type": "album", "artists": [{"name": "Artist"}]}
SINGLE = {"id": "S1", "name": "Album", "album_type": "single", "artists": [{"name": "Artist"}]}
OTHER = {"id": "X1", "name": "Album", "album_type": "album", "artists": [{"name": "Someone Else"}]}


def adapter(api):
    return SpotifyAdapter(api, sleep=lambda s: None, clock=lambda: 0.0)


class SpotifyAdapterTests(unittest.TestCase):
    def test_album_match_with_isrc_from_full_tracks(self):
        api = FakeApi([OTHER, SINGLE, ALBUM], {"A1": [sp_track("t1", "Song", 1, explicit=True), sp_track("t2", "Other", 2)],
                                              "S1": [sp_track("s1", "Song", 1)]})
        found = adapter(api).find(lib(title="Song", album="Album", album_artist="Artist", track=1))
        self.assertEqual([s.id for s in found], ["t1", "s1"])                 # full album before single; other artist dropped
        self.assertEqual((found[0].service, found[0].album_match, found[0].isrc, found[0].duration),
                         ("spotify", True, "USABC2400001", 200.0))
        self.assertTrue(found[0].explicit)
        self.assertIsNone(found[1].explicit)                                    # explicit=false is unknown, not "clean"
        self.assertIsNone(found[0].preview_url)

    def test_album_cached_and_deluxe_preferred(self):
        api = FakeApi([ALBUM, DELUXE], {"A1": [sp_track("t1", "Song", 1)], "A2": [sp_track("d1", "Song", 1)]})
        sp = adapter(api)
        self.assertEqual(sp.find(lib(title="Song", album="Album (Deluxe)", album_artist="Artist"))[0].id, "d1")
        before = len(api.calls)
        sp.find(lib(title="Other", album="Album (Deluxe)", album_artist="Artist"))
        self.assertFalse(any(p.endswith(("/A1/tracks", "/A2/tracks")) for p, _ in api.calls[before:]))

    def test_symbol_artist_falls_back_to_album_only_query(self):
        yen = {"id": "Y1", "name": "VULTURES 2", "album_type": "album", "artists": [{"name": "¥$"}, {"name": "Kanye West"}]}
        api = FakeApi([yen], {"Y1": [sp_track("v1", "SLIDE", 1)]}, artist_albums=[])
        found = adapter(api).find(lib(title="SLIDE", artist="¥$", album_artist="¥$", album="VULTURES 2"))
        self.assertEqual([s.id for s in found], ["v1"])
        self.assertEqual([q["q"] for p, q in api.calls if q.get("type") == "album"],
                         ['album:"VULTURES 2" artist:"¥$"', 'album:"VULTURES 2"'])

    def test_isrc_first(self):
        hit = {**sp_track("i1", "Song (Remastered)", 3, isrc="usabc2400009"), "album": {"name": "Hits"}}
        api = FakeApi([ALBUM], {"A1": []}, tracks=[hit])
        found = adapter(api).find(lib(title="Song", isrc="USABC2400009"))
        self.assertEqual((found[0].id, found[0].isrc, found[0].album, found[0].album_match), ("i1", "USABC2400009", "Hits", False))
        self.assertFalse(any("/albums/" in p for p, _ in api.calls))

    def test_song_search_fallback_filters_artist(self):
        mine = {**sp_track("m1", "Song", 1), "album": {"name": "Single"}}
        theirs = {**sp_track("m2", "Song", 1, artist="Cover Band"), "album": {"name": "Covers"}}
        api = FakeApi([], {}, tracks=[mine, theirs])
        self.assertEqual([s.id for s in adapter(api).find(lib(title="Song", artist="Artist", album=""))], ["m1"])

    def test_throttle(self):
        sleeps = []
        sp = SpotifyAdapter(FakeApi([], {}), sleep=sleeps.append, clock=lambda: 0.0)
        sp.check(); sp.check()
        self.assertEqual(sleeps, [spotify.MIN_INTERVAL])


class SpotifyClientTests(unittest.TestCase):
    def response(self, body):
        r = mock.MagicMock(); r.read.return_value = json.dumps(body).encode()
        r.__enter__.return_value = r
        return r

    def token(self):
        return self.response({"access_token": "tok", "expires_in": 3600})

    def http_error(self, code, body=b"", headers=None):
        return urllib.error.HTTPError("https://api.spotify.com/v1/x", code, "err", headers or {}, io.BytesIO(body))

    def test_token_then_get_and_secret_only_sent_to_token_endpoint(self):
        with mock.patch.object(spotify.urllib.request, "urlopen", side_effect=[self.token(), self.response({"ok": 1})]) as urlopen:
            self.assertEqual(SpotifyClient("id", "s3cret", clock=lambda: 0.0).get(spotify.API_URL + "/search?q=x"), {"ok": 1})
        token_req, get_req = (c.args[0] for c in urlopen.call_args_list)
        self.assertEqual((token_req.full_url, token_req.get_method()), (spotify.TOKEN_URL, "POST"))
        self.assertEqual((get_req.get_method(), get_req.get_header("Authorization")), ("GET", "Bearer tok"))

    def test_401_refreshes_token_once_and_429_backs_off(self):
        sleeps = []
        side = [self.token(), self.http_error(401), self.token(), self.http_error(429, headers={"Retry-After": "7"}),
                self.response({"ok": 1})]
        with mock.patch.object(spotify.urllib.request, "urlopen", side_effect=side):
            client = SpotifyClient("id", "s", sleep=sleeps.append, clock=lambda: 0.0)
            self.assertEqual(client.get("https://api.spotify.com/v1/x"), {"ok": 1})
        self.assertEqual(sleeps, [7])

    def test_403_premium_message_raises_unavailable(self):
        err = self.http_error(403, b"Active premium subscription required for the owner of the app.")
        with mock.patch.object(spotify.urllib.request, "urlopen", side_effect=[self.token(), err]):
            with self.assertRaisesRegex(SpotifyUnavailable, "premium"):
                SpotifyClient("id", "s", clock=lambda: 0.0).get("https://api.spotify.com/v1/x")

    def test_bad_credentials_raise_unavailable(self):
        with mock.patch.object(spotify.urllib.request, "urlopen", side_effect=self.http_error(400)):
            with self.assertRaisesRegex(SpotifyUnavailable, "HTTP 400"):
                SpotifyClient("id", "s").get("https://api.spotify.com/v1/x")


class DefaultAdaptersTests(unittest.TestCase):
    def run_default(self, env, check=None):
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(compare, "load_env"), \
             mock.patch.object(SpotifyAdapter, "check", side_effect=check), contextlib.redirect_stderr(err):
            names = [a.name for a in compare.default_adapters("US")]
        return names, err.getvalue()

    def test_spotify_first_when_usable(self):
        self.assertEqual(self.run_default({"SPOTIFY_CLIENT_ID": "i", "SPOTIFY_CLIENT_SECRET": "s"})[0], ["spotify", "apple"])

    def test_skipped_without_credentials_or_when_unavailable(self):
        self.assertEqual(self.run_default({}), (["apple"], ""))
        names, err = self.run_default({"SPOTIFY_CLIENT_ID": "i", "SPOTIFY_CLIENT_SECRET": "s"},
                                      check=SpotifyUnavailable("Active premium subscription required"))
        self.assertEqual(names, ["apple"])
        self.assertIn("Spotify skipped: Active premium", json.loads(err)["warning"])


PREVIEW = "https://audio-ssl.itunes.apple.com/p.m4a"


class SpotifyReferenceAudioTests(unittest.TestCase):
    """A Spotify reference has no preview; the audio check borrows Apple's, preferring the same ISRC."""
    def setUp(self):
        patches = [mock.patch.object(compare, "decode_url", return_value=[0.1] * 100),
                   mock.patch.object(compare, "library_subfingerprints", return_value=[1]),
                   mock.patch.object(compare, "align_excerpt", return_value=(0.1, 0))]
        self.decode = patches[0].start()
        for p in patches[1:]: p.start()
        for p in patches: self.addCleanup(p.stop)

    def svc(self, service, **kw):
        base = dict(service=service, id=service, url="u", title="Song", artists=("A",), album="B", duration=200.0, album_match=True)
        return ServiceTrack(**{**base, **kw})

    def test_apple_preview_used_for_spotify_reference(self):
        sp = FakeAdapter("spotify", [self.svc("spotify", isrc="ISRC2")])
        apple = FakeAdapter("apple", [self.svc("apple", id="a1", isrc="ISRC1", preview_url=PREVIEW + "?1"),
                                      self.svc("apple", id="a2", isrc="ISRC2", preview_url=PREVIEW + "?2")])
        record = compare.compare(lib(), [sp, apple])
        self.assertEqual(record["reference"]["service"], "spotify")
        self.assertEqual(record["audio"]["service"], "apple")
        self.decode.assert_called_once_with(PREVIEW + "?2")                     # same ISRC as the reference wins
        self.assertEqual((sp.calls, apple.calls), (1, 1))                       # each service asked once

    def test_no_audio_source_means_no_audio_record(self):
        record = compare.compare(lib(), [FakeAdapter("spotify", [self.svc("spotify")]), FakeAdapter("apple", [])])
        self.assertNotIn("audio", record)
        self.decode.assert_not_called()


if __name__ == "__main__": unittest.main()
