import contextlib, dataclasses, io, json, tempfile, unittest, urllib.parse
from pathlib import Path
from unittest import mock
import artists, compare, musicbrainz
from compare import album_check, parse_probe, tracklist_check
from musicbrainz import MusicBrainz
from test_compare import MP3_PROBE, FakeAdapter, lib

YE = {"name": "Ye", "aliases": [{"name": "Kanye West"}, {"name": "Yeezy"}]}
TRIBUTE = {"name": "Kanye West Tribute Band", "aliases": []}


def release(rid, status, date, country="XW", titles=("One", "Two", "Three")):
    return {"id": rid, "status": status, "date": date, "country": country,
            "media": [{"position": 1, "tracks": [{"position": i, "title": t} for i, t in enumerate(titles, 1)]}]}


class FakeMB:
    def __init__(self):
        self.calls = []
        self.releases = [release("old", "Withdrawn", "2024-08-03"), release("jp", "Official", "2024-12-01", "JP"),
                         release("new", "Official", "2024-11-10")]

    def __call__(self, url):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((parsed.path, q))
        path = parsed.path.removeprefix("/ws/2/")
        if path == "artist":
            return {"artists": [TRIBUTE, YE]}
        if path == "release" and "release-group" in q:
            return {"releases": self.releases}
        if path == "release":
            return {"releases": [{"title": "Album (Deluxe)", "artist-credit": [{"name": "Ye"}], "release-group": {"id": "rg-x"}},
                                 {"title": "Album", "artist-credit": [{"name": "Other"}], "release-group": {"id": "rg-y"}},
                                 {"title": "Album", "artist-credit": [{"name": "Ye"}], "release-group": {"id": "rg-1"}}]}
        if path.startswith("release/") and q.get("inc") == "release-groups":
            return {"release-group": {"id": "rg-from-release"}}
        if path.startswith("release/"):
            rid = path.split("/")[1]
            return next(r for r in self.releases if r["id"] == rid)
        raise AssertionError(url)


def client(api=None):
    return MusicBrainz(fetch=api or FakeMB(), sleep=lambda s: None, clock=lambda: 0.0)


class MusicBrainzTests(unittest.TestCase):
    def test_artist_names_exact_alias_only(self):
        self.assertEqual(client().artist_names("Kanye West"), {"kanye west", "ye", "yeezy"})   # tribute band excluded

    def test_release_group_from_tags_release_or_search(self):
        mb = client()
        self.assertEqual(mb.release_group_id(lib(mb_release_group_id="rg-tag")), "rg-tag")
        self.assertEqual(mb.release_group_id(lib(mb_album_id="rel-1")), "rg-from-release")
        self.assertEqual(mb.release_group_id(lib(album="Album", album_artist="Kanye West")), "rg-1")
        self.assertIsNone(mb.release_group_id(lib(album="")))

    def test_reference_release_latest_official_worldwide(self):
        self.assertEqual(client().reference_release(lib(mb_release_group_id="rg"))["id"], "new")

    def test_responses_cached_on_disk_and_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache, api, sleeps = Path(tmp) / "mb" / "cache.sqlite3", FakeMB(), []
            mb = MusicBrainz(cache, fetch=api, sleep=sleeps.append, clock=lambda: 0.0)
            mb.artist_names("Kanye West"); mb.reference_release(lib(mb_release_group_id="rg"))
            mb.close()
            self.assertEqual(sleeps, [musicbrainz.MIN_INTERVAL] * 2)
            calls = len(api.calls)
            again = MusicBrainz(cache, fetch=api, sleep=sleeps.append, clock=lambda: 0.0)
            again.artist_names("Kanye West"); again.reference_release(lib(mb_release_group_id="rg"))
            again.close()
            self.assertEqual(len(api.calls), calls)                          # second process: all from cache

    def test_query_quotes_are_escaped(self):
        api = FakeMB()
        client(api).artist_names('The "Band"')
        self.assertIn('artist:"The \\"Band\\""', api.calls[0][1]["query"])


class ArtistMatchTests(unittest.TestCase):
    def tearDown(self):
        artists.set_alias_source(None)

    def test_aliases_extend_substring_matching(self):
        self.assertFalse(artists.artist_match("Kanye West", "Ye"))
        artists.set_alias_source(client().artist_names)
        self.assertTrue(artists.artist_match("Kanye West", "Ye"))
        self.assertTrue(artists.artist_match("Kanye West", "¥$, Ye & Ty Dolla $ign"))
        self.assertFalse(artists.artist_match("Kanye West", "Drake"))

    def test_alias_failure_is_not_a_match(self):
        artists.set_alias_source(mock.Mock(side_effect=OSError("offline")))
        self.assertFalse(artists.artist_match("Kanye West", "Ye"))


def track(title, number, **kw):
    return lib(**{"title": title, "track": number, "path": f"{number:02d} {title}.mp3", "album": "Album", **kw})


class TracklistTests(unittest.TestCase):
    REF = release("new", "Official", "2024-11-10", titles=("One", "Two", "Three"))

    def test_matching_album_is_clean(self):
        self.assertEqual(tracklist_check([track("One", 1), track("Two", 2), track("Three", 3)], self.REF), [])

    def test_missing_extra_duplicate_moved(self):
        files = [track("One", 1), track("Three", 2), track("Bonus", 4), track("Bonus", 4, path="04 Bonus.m4a")]
        [m] = tracklist_check(files, self.REF)
        self.assertEqual((m["code"], m["library_count"], m["service_count"]), ("TRACKLIST", 4, 3))
        self.assertEqual(m["missing"], ["02 Two"])
        self.assertEqual(m["extra"], ["04 Bonus.m4a", "04 Bonus.mp3"])
        self.assertEqual(m["duplicates"], ["Bonus"])
        self.assertEqual(m["moved"], [{"title": "Three", "library": "1-2", "service": "1-3"}])

    def test_missing_named_like_library_files_on_multi_disc(self):
        ref = {"media": [{"position": 1, "tracks": [{"position": 1, "title": "One"}]},
                         {"position": 2, "tracks": [{"position": 1, "title": "Dose"}]}]}
        [m] = tracklist_check([track("One", 1)], ref)
        self.assertEqual(m["missing"], ["01 Dose (CD 02)"])

    def test_withdrawn_release_flagged(self):
        mb = mock.Mock(reference_release=mock.Mock(return_value=self.REF))
        files = [track(t, i, mb_album_id="old", mb_album_status="withdrawn") for i, t in enumerate(("One", "Two", "Three"), 1)]
        record = album_check(files, mb)
        self.assertEqual(record["status"], "mismatch")
        self.assertEqual(record["mismatches"], [{"code": "RELEASE_WITHDRAWN", "library": "old", "service": "new"}])
        self.assertEqual(record["reference"]["url"], "https://musicbrainz.org/release/new")

    def test_unresolved_album(self):
        record = album_check([track("One", 1)], mock.Mock(reference_release=mock.Mock(return_value=None)))
        self.assertEqual(record["status"], "unresolved")

    def test_probe_reads_musicbrainz_tags(self):
        data = json.loads(json.dumps(MP3_PROBE))
        data["format"]["tags"].update({"MusicBrainz Album Id": "rel", "MusicBrainz Release Group Id": "rg",
                                       "MusicBrainz Album Status": "Withdrawn"})
        t = parse_probe("x.mp3", data)
        self.assertEqual((t.mb_album_id, t.mb_release_group_id, t.mb_album_status), ("rel", "rg", "withdrawn"))


class AlbumChecksCliTests(unittest.TestCase):
    def test_album_records_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "lib" / "Album"
            folder.mkdir(parents=True)
            for name in ("01 One.mp3", "02 Two.mp3"):
                (folder / name).write_bytes(b"x")
            titles = {"01 One": ("One", 1), "02 Two": ("Two", 2)}
            probe = lambda p: track(*titles[p.stem], path=str(p))
            adapter = FakeAdapter("apple", lambda t: [compare.ServiceTrack("apple", "1", "u", t.title, ("A",), "Album", 200.0,
                                                                            album_match=True)])
            fake_mb = mock.Mock(reference_release=mock.Mock(return_value=TracklistTests.REF))
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(compare, "probe", side_effect=probe), \
                 mock.patch("musicbrainz.MusicBrainz", return_value=fake_mb), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = compare.main([str(folder.parent), "--index", str(root / "i.sqlite3"), "--album-checks"], adapters=[adapter])
            records = [json.loads(l) for l in out.getvalue().splitlines()]
            self.assertEqual(code, 1)
            self.assertEqual([r.get("album") for r in records], ["Album"])
            self.assertEqual(records[0]["mismatches"][0]["missing"], ["03 Three"])
            self.assertIn('"albums": {"ok": 0, "mismatch": 1', err.getvalue())
            fake_mb.close.assert_called_once()


if __name__ == "__main__": unittest.main()
