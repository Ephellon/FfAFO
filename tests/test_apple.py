import unittest
from urllib.parse import parse_qs, urlparse
from apple import AppleAdapter, MIN_INTERVAL
from compare import LibraryTrack, check_track, pick_reference


def lib(**kw):
    base = dict(path="x.mp3", title="Song", artist="Art$", album_artist="Art$", album="Record 2", track=1, disc=1, duration=200.0)
    return LibraryTrack(**{**base, **kw})


def album(cid, name, artist="Art$, Guest", explicitness="explicit"):
    return {"wrapperType": "collection", "collectionId": cid, "collectionName": name, "artistName": artist,
            "collectionExplicitness": explicitness}


def song(tid, cid, name, seconds, number, explicitness="explicit", artist="Art$, Guest"):
    return {"wrapperType": "track", "trackId": tid, "collectionId": cid, "trackName": name, "artistName": artist,
            "collectionName": "Record 2", "trackTimeMillis": int(seconds * 1000), "trackExplicitness": explicitness,
            "trackNumber": number, "discNumber": 1, "previewUrl": f"https://example.invalid/{tid}.m4a",
            "trackViewUrl": f"https://example.invalid/t/{tid}"}


class FakeApi:
    def __init__(self, albums=(), lookups=None, songs=()):
        self.albums, self.lookups, self.songs, self.urls = list(albums), lookups or {}, list(songs), []
    def __call__(self, url):
        self.urls.append(url)
        parsed = urlparse(url); q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert q["country"] == "US"
        if parsed.path == "/lookup":
            cid = int(q["id"])
            return {"results": [album(cid, "Record 2")] + self.lookups.get(cid, [])}
        return {"results": self.albums if q["entity"] == "album" else self.songs}


class Clock:
    def __init__(self): self.now, self.sleeps = 0.0, []
    def __call__(self): return self.now
    def sleep(self, s): self.sleeps.append(s); self.now += s


def adapter(api):
    clock = Clock()
    return AppleAdapter(fetch=api, sleep=clock.sleep, clock=clock), clock


class AlbumLookupTests(unittest.TestCase):
    def test_album_match_and_cache(self):
        api = FakeApi(albums=[album(10, "Record 2")],
                      lookups={10: [song(1, 10, "Song", 205.0, 1), song(2, 10, "Other", 100.0, 2)]})
        a, _ = adapter(api)
        found = a.find(lib())
        self.assertEqual([(s.id, s.duration, s.album_match, s.explicit) for s in found], [("1", 205.0, True, True)])
        a.find(lib(title="Other", track=2))
        self.assertEqual(len(api.urls), 2)   # one album search + one lookup, reused for the second track

    def test_other_artist_with_same_album_title_ignored(self):
        api = FakeApi(albums=[album(20, "Record 2 (Reworked) - Single", artist="Someone Else")],
                      songs=[song(3, 30, "Song", 200.0, 1)])
        a, _ = adapter(api)
        found = a.find(lib())
        self.assertEqual([(s.id, s.album_match) for s in found], [("3", False)])   # fell back to song search
        self.assertFalse(any("/lookup" in u for u in api.urls))

    def test_edition_order_explicit_first_unless_file_is_clean(self):
        api = FakeApi(albums=[album(11, "Record 2", explicitness="cleaned"), album(12, "Record 2")],
                      lookups={11: [song(4, 11, "Song", 200.0, 1, "cleaned")], 12: [song(5, 12, "Song", 200.0, 1)]})
        self.assertEqual([s.id for s in adapter(api)[0].find(lib())], ["5", "4"])
        self.assertEqual([s.id for s in adapter(api)[0].find(lib(explicit=False))], ["4", "5"])

    def test_deluxe_qualifier_preferred_when_library_is_deluxe(self):
        api = FakeApi(albums=[album(13, "Record 2"), album(14, "Record 2 (Deluxe)")],
                      lookups={13: [song(6, 13, "Song", 200.0, 1)], 14: [song(7, 14, "Song", 200.0, 1)]})
        self.assertEqual(adapter(api)[0].find(lib(album="Record 2 (Deluxe)"))[0].id, "7")

    def test_same_position_preferred_for_repeated_titles(self):
        api = FakeApi(albums=[album(15, "Record 2")],
                      lookups={15: [song(8, 15, "Song", 200.0, 3), song(9, 15, "Song", 90.0, 7)]})
        self.assertEqual(adapter(api)[0].find(lib(track=7))[0].id, "9")

    def test_not_explicit_is_unknown(self):
        api = FakeApi(albums=[album(16, "Record 2")], lookups={16: [song(10, 16, "Song", 200.0, 1, "notExplicit")]})
        self.assertIsNone(adapter(api)[0].find(lib())[0].explicit)

    def test_symbol_only_artist_matches(self):
        api = FakeApi(albums=[album(17, "Record 2", artist="¥$, Guest")], lookups={17: [song(11, 17, "Song", 200.0, 1)]})
        self.assertTrue(adapter(api)[0].find(lib(artist="¥$", album_artist="¥$"))[0].album_match)


class ThrottleTests(unittest.TestCase):
    def test_requests_spaced(self):
        api = FakeApi(albums=[album(10, "Record 2")], lookups={10: [song(1, 10, "Song", 200.0, 1)]})
        a, clock = adapter(api)
        a.find(lib())
        self.assertEqual(clock.sleeps, [MIN_INTERVAL])


class ReferenceTests(unittest.TestCase):
    def test_album_match_with_different_length_is_reported_not_unresolved(self):
        api = FakeApi(albums=[album(10, "Record 2")], lookups={10: [song(1, 10, "Song", 331.3, 1, "cleaned")]})
        ref = pick_reference(lib(duration=203.2), [adapter(api)[0]])
        self.assertIsNotNone(ref)
        self.assertEqual([m["code"] for m in check_track(lib(duration=203.2), ref)], ["DURATION"])

    def test_song_search_candidate_needs_close_length(self):
        api = FakeApi(songs=[song(1, 10, "Song", 260.0, 1)])
        self.assertIsNone(pick_reference(lib(album=""), [adapter(api)[0]]))


if __name__ == "__main__": unittest.main()
