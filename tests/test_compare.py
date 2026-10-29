import array, contextlib, dataclasses, io, json, tempfile, unittest
from pathlib import Path
from unittest import mock
import compare
from compare import LibraryTrack, ServiceTrack, check_track, normalize_title, parse_probe, pick_reference

MP3_PROBE = {"streams": [{"codec_type": "audio", "duration": "198.086531", "tags": {"encoder": "Lavc61.19"}},
                         {"codec_type": "video", "duration": "198.086533"}],
             "format": {"duration": "198.086531", "tags": {"title": "SLIDE", "artist": "Artist", "album_artist": "Artist",
                        "track": "1/16", "album": "Album", "disc": "1/1", "TSRC": "usabc2400001"}}}
FLAC_PROBE = {"streams": [{"codec_type": "audio", "duration": "200.5"}],
              "format": {"tags": {"TITLE": "ISABELLA", "ARTIST": "Artist", "ALBUM": "Album", "track": "5",
                                  "ISRC": "US-ABC-24-00005", "ITUNESADVISORY": "2"}}}
OPUS_PROBE = {"streams": [{"codec_type": "audio", "duration": "61", "tags": {"TITLE": "Stream Tagged", "EXPLICIT": "1"}}],
              "format": {"duration": "61"}}


def lib(**kw):
    base = dict(path="x.mp3", title="Song", artist="A", album_artist="A", album="B", track=1, disc=1, duration=200.0)
    return LibraryTrack(**{**base, **kw})


def svc(service="spotify", **kw):
    base = dict(service=service, id="1", url="https://example.invalid/1", title="Song", artists=("A",), album="B", duration=200.0)
    return ServiceTrack(**{**base, **kw})


class FakeAdapter:
    def __init__(self, name, results): self.name, self.results, self.calls = name, results, 0
    def find(self, track):
        self.calls += 1
        return self.results(track) if callable(self.results) else self.results


class ParseProbeTests(unittest.TestCase):
    def test_mp3_format_tags(self):
        t = parse_probe("a.mp3", MP3_PROBE)
        self.assertEqual((t.title, t.album_artist, t.track, t.disc, t.duration, t.isrc, t.explicit),
                         ("SLIDE", "Artist", 1, 1, 198.087, "USABC2400001", None))
    def test_flac_uppercase_keys_and_advisory(self):
        t = parse_probe("b.flac", FLAC_PROBE)
        self.assertEqual((t.title, t.album, t.track, t.isrc, t.explicit), ("ISABELLA", "Album", 5, "USABC2400005", False))
        self.assertEqual(t.album_artist, "Artist")  # falls back to artist
    def test_stream_tags_and_explicit_flag(self):
        t = parse_probe("c.opus", OPUS_PROBE)
        self.assertEqual((t.title, t.explicit, t.duration), ("Stream Tagged", True, 61.0))
    def test_no_audio_stream(self):
        with self.assertRaises(ValueError): parse_probe("d.mp4", {"streams": [{"codec_type": "video"}], "format": {}})
    def test_title_falls_back_to_filename(self):
        self.assertEqual(parse_probe(r"C:\m\07 Track.mp3", {"streams": [{"codec_type": "audio", "duration": "9"}]}).title, "07 Track")


class NormalizeTitleTests(unittest.TestCase):
    CASES = [
        ("SLIDE", "slide", set()),
        ("Song (feat. Someone)", "song", set()),
        ("Song [Explicit]", "song", {"explicit"}),
        ("Song - Remastered 2011", "song", {"remaster"}),
        ("Song (2011 Remaster)", "song", {"remaster"}),
        ("Song (Live at Wembley) [feat. X]", "song", {"live"}),
        ("Song - Radio Edit", "song", {"radio", "edit"}),
        ("Love - Hate", "love hate", set()),          # dash without a version word stays in the title
        ("Don't Stop", "dont stop", set()),
    ]
    def test_cases(self):
        for title, base, quals in self.CASES:
            with self.subTest(title=title):
                self.assertEqual(normalize_title(title), (base, frozenset(quals)))


class CheckTrackTests(unittest.TestCase):
    CASES = [
        ("identical", lib(), svc(), []),
        ("duration within tolerance", lib(duration=200.0), svc(duration=201.9), []),
        ("duration over tolerance", lib(duration=200.0), svc(duration=203.5), ["DURATION"]),
        ("service duration unknown", lib(), svc(duration=None), []),
        ("isrc differs", lib(isrc="USA1"), svc(isrc="USA2"), ["ISRC"]),
        ("isrc missing on one side", lib(isrc=None), svc(isrc="USA2"), []),
        ("explicit vs clean", lib(explicit=True), svc(explicit=False), ["EXPLICIT"]),
        ("explicit unknown locally", lib(explicit=None), svc(explicit=False), []),
        ("remaster only on service", lib(title="Song"), svc(title="Song - Remastered"), ["TITLE_VERSION"]),
        ("remaster spelling variants", lib(title="Song (Remaster)"), svc(title="Song - Remastered 2009"), []),
        ("feat credit ignored", lib(title="Song (feat. X)"), svc(title="Song"), []),
        ("several at once", lib(duration=180.0, explicit=False), svc(explicit=True), ["DURATION", "EXPLICIT"]),
    ]
    def test_cases(self):
        for name, track, ref, codes in self.CASES:
            with self.subTest(name):
                self.assertEqual([m["code"] for m in check_track(track, ref)], codes)
    def test_duration_delta_sign(self):
        self.assertEqual(check_track(lib(duration=200.0), svc(duration=205.0))[0]["delta"], 5.0)


class PickReferenceTests(unittest.TestCase):
    def test_precedence_stops_at_first_confident(self):
        spotify = FakeAdapter("spotify", [svc("spotify")])
        apple = FakeAdapter("apple", [svc("apple")])
        self.assertEqual(pick_reference(lib(), [spotify, apple]).service, "spotify")
        self.assertEqual(apple.calls, 0)
    def test_falls_through_when_not_confident(self):
        spotify = FakeAdapter("spotify", [svc("spotify", title="Other Song")])
        apple = FakeAdapter("apple", [svc("apple")])
        self.assertEqual(pick_reference(lib(), [spotify, apple]).service, "apple")
    def test_isrc_decides_over_title(self):
        self.assertIsNone(pick_reference(lib(isrc="USA1"), [FakeAdapter("s", [svc(isrc="USA2")])]))
        self.assertIsNotNone(pick_reference(lib(isrc="USA1", title="X"), [FakeAdapter("s", [svc(isrc="USA1")])]))
    def test_duration_far_off_is_not_confident(self):
        self.assertIsNone(pick_reference(lib(), [FakeAdapter("s", [svc(duration=260.0)])]))
    def test_no_adapters(self):
        self.assertIsNone(pick_reference(lib(), []))


PREVIEW = "https://audio-ssl.itunes.apple.com/itunes-assets/x/preview.m4a"


class AudioCheckTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(compare, "decode_url", return_value=[0.1] * 100),
                        mock.patch.object(compare, "library_subfingerprints", return_value=[1, 2, 3]),
                        mock.patch.object(compare, "align_excerpt", return_value=(0.05, 1325))]
        self.decode, self.libsub, self.align = (p.start() for p in self.patches)
    def tearDown(self):
        for p in self.patches: p.stop()

    def run_compare(self, ref, **kw):
        return compare.compare(lib(), [FakeAdapter("apple", [ref])], **kw)

    def test_matching_preview_is_ok_with_audio_info(self):
        record = self.run_compare(svc("apple", preview_url=PREVIEW))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["audio"], {"service": "apple", "ber": 0.05, "preview_at": 61.53})
        self.decode.assert_called_once_with(PREVIEW)
        self.libsub.assert_called_once_with("x.mp3", None, True)
        self.assertTrue(self.align.call_args.kwargs["accel"])

    def test_no_numpy_is_passed_through(self):
        self.run_compare(svc("apple", preview_url=PREVIEW), accel=False)
        self.libsub.assert_called_once_with("x.mp3", None, False)
        self.assertFalse(self.align.call_args.kwargs["accel"])

    def test_high_ber_is_audio_mismatch(self):
        self.align.return_value = (0.47, 0)
        record = self.run_compare(svc("apple", preview_url=PREVIEW))
        self.assertEqual(record["mismatches"], [{"code": "AUDIO", "service": "apple", "ber": 0.47, "threshold": compare.AUDIO_MAX_BER}])

    def test_threshold_is_inclusive(self):
        self.align.return_value = (compare.AUDIO_MAX_BER, 0)
        self.assertEqual(self.run_compare(svc("apple", preview_url=PREVIEW))["status"], "ok")

    def test_audio_failure_keeps_metadata_mismatches(self):
        self.decode.side_effect = RuntimeError("network down")
        record = self.run_compare(svc("apple", preview_url=PREVIEW, duration=230.0, album_match=True))
        self.assertEqual([m["code"] for m in record["mismatches"]], ["DURATION"])
        self.assertEqual(record["audio"], {"service": "apple", "error": "network down"})

    def test_skipped_without_preview_or_when_disabled(self):
        self.run_compare(svc("apple"))
        self.run_compare(svc("apple", preview_url=PREVIEW), audio=False)
        self.decode.assert_not_called()


class DecodeUrlTests(unittest.TestCase):
    def test_refuses_untrusted_urls_without_running_ffmpeg(self):
        with mock.patch.object(compare.subprocess, "run") as run:
            for url in ("http://audio-ssl.itunes.apple.com/a.m4a", "https://evil.example/a.m4a",
                        "https://apple.com.evil.example/a.m4a", "file:///C:/Windows/win.ini", "concat:a|b"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    compare.decode_url(url)
            run.assert_not_called()

    def test_ffmpeg_is_limited_and_whitelisted(self):
        pcm = array.array("f", [0.25, -0.5]).tobytes()
        with mock.patch.object(compare.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=pcm, stderr=b"")) as run:
            self.assertEqual(compare.decode_url(PREVIEW), [0.25, -0.5])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "https,tls,tcp")
        self.assertLess(cmd.index("-t"), cmd.index("-i"))   # input-side limit: stop reading after 31 s
        self.assertEqual(cmd[cmd.index("-t") + 1], str(compare.PREVIEW_SECONDS))


class MainTests(unittest.TestCase):
    TRACKS = {"01 Good": lib(title="Good"), "02 Long": lib(title="Long", duration=260.0), "03 Missing": lib(title="Missing")}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        (self.library / "Album").mkdir(parents=True)
        for name in ("01 Good.mp3", "02 Long.flac", "03 Missing.mp3", "notes.txt"):
            (self.library / "Album" / name).write_bytes(b"placeholder")
        self.patch_probe(lambda p: dataclasses.replace(self.TRACKS[p.stem], path=str(p)))
        self.service = {"Good": [svc("apple", title="Good")], "Long": [svc("apple", title="Long", duration=260.0)]}
        self.adapter = FakeAdapter("apple", lambda t: self.service.get(t.title, []))

    def tearDown(self):
        self.probe.stop()
        self.tmp.cleanup()

    def patch_probe(self, side_effect):
        if hasattr(self, "probe"): self.probe.stop()
        self.probe = mock.patch.object(compare, "probe", side_effect=side_effect)
        self.probe.start()

    def snapshot(self):
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.library.rglob("*")}

    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = compare.main([str(self.library), "--index", str(self.root / "index.sqlite3"), *argv], adapters=[self.adapter])
        return code, [json.loads(l) for l in stdout.getvalue().splitlines()], stderr.getvalue()

    def test_resume_reuses_stored_results_without_lookups(self):
        self.service["Long"] = [svc("apple", title="Long", duration=257.5)]
        first = self.run_main("--resume")
        calls = self.adapter.calls
        second = self.run_main("--resume")
        self.assertEqual(self.adapter.calls, calls)                  # no service lookups the second time
        self.assertEqual(second[:2], first[:2])                      # same exit code and mismatch records
        self.assertIn('"reused": 3', second[2])

    def test_resume_rechecks_changed_files_and_other_options(self):
        self.run_main("--resume")
        calls = self.adapter.calls
        long = self.library / "Album" / "02 Long.flac"
        long.write_bytes(b"changed size")
        self.run_main("--resume")
        self.assertEqual(self.adapter.calls, calls + 1)              # only the changed file
        self.run_main("--resume", "--no-audio")
        self.assertEqual(self.adapter.calls, calls + 4)              # different options: all rechecked

    def test_without_resume_nothing_is_stored(self):
        self.run_main()
        self.run_main("--resume")
        self.assertEqual(self.adapter.calls, 6)

    def test_index_only_fingerprints_without_lookups(self):
        with mock.patch.object(compare.LibraryIndex, "subfingerprints", return_value=[1]) as sub, \
             mock.patch.object(compare.LibraryIndex, "cached", side_effect=[False, True, False]):
            code, records, err = self.run_main("--index-only")
        self.assertEqual((code, records, self.adapter.calls, sub.call_count), (0, [], 0, 2))
        self.assertIn('"indexed": 2, "cached": 1', err)

    def test_single_files_and_several_paths(self):
        album = self.library / "Album"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            compare.main([str(album / "02 Long.flac"), str(album / "01 Good.mp3"), str(album / "02 Long.flac"),
                          "--index", str(self.root / "i.sqlite3"), "--verbose"], adapters=[self.adapter])
        self.assertEqual([Path(json.loads(l)["file"]).name for l in stdout.getvalue().splitlines()],
                         ["01 Good.mp3", "02 Long.flac"])                    # sorted, de-duplicated
        for argv in ([str(album / "notes.txt")], [str(self.root / "missing")],
                     [str(album / "01 Good.mp3"), "--report", str(album / "r.jsonl")]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                compare.main(argv, adapters=[])
        self.assertFalse((album / "r.jsonl").exists())

    def test_index_inside_library_or_missing_refused(self):
        for argv in (["--index", str(self.library / "i.sqlite3")], ["--no-index", "--resume"], ["--no-index", "--index-only"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                compare.main([str(self.library), *argv], adapters=[])
        self.assertFalse((self.library / "i.sqlite3").exists())

    def test_reports_mismatches_only(self):
        self.service["Long"] = [svc("apple", title="Long", duration=257.5)]  # confident (≤3 s) but over 2 s tolerance
        code, records, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual([(Path(r["file"]).name, r["status"]) for r in records], [("02 Long.flac", "mismatch")])
        self.assertEqual(records[0]["mismatches"][0]["code"], "DURATION")
        self.assertNotIn("size", records[0])                         # size and mtime need --granular
        self.assertRegex(records[0]["checked"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertIn('"unresolved": 1', err)

    def test_clean_run_exit_zero_and_flags(self):
        code, records, _ = self.run_main("--verbose", "--show-unresolved")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(r["status"] for r in records), ["ok", "ok", "unresolved"])

    def test_library_untouched(self):
        before = self.snapshot()
        self.run_main("--report", str(self.root / "report.jsonl"), "--verbose", "--show-unresolved")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len((self.root / "report.jsonl").read_text(encoding="utf-8").splitlines()), 3)

    def test_records_carry_file_state_and_run_status(self):
        self.service["Long"] = [svc("apple", title="Long", duration=257.5)]
        report, status = self.root / "report.jsonl", self.root / "last-run.json"
        for _ in range(2):   # the second run reuses the stored result
            code, records, _ = self.run_main("--resume", "--granular", "--report", str(report), "--run-status", str(status))
            self.assertEqual(records[0]["size"], len(b"placeholder"))
            self.assertEqual(records[0]["mtime"], (self.library / "Album" / "02 Long.flac").stat().st_mtime)
            self.assertEqual([json.loads(l) for l in report.read_text(encoding="utf-8").splitlines()], records)
            self.assertFalse(report.with_name("report.jsonl.tmp").exists())
            run = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual((run["exit"], run["records"]), (code, 1))
            self.assertLessEqual(run["started"], run["finished"])

    def test_interrupted_run_keeps_previous_report(self):
        report, status = self.root / "report.jsonl", self.root / "last-run.json"
        report.write_text("old\n", encoding="utf-8")
        def stop(p): raise KeyboardInterrupt
        self.patch_probe(stop)
        with self.assertRaises(KeyboardInterrupt):
            self.run_main("--report", str(report), "--run-status", str(status))
        self.assertEqual(report.read_text(encoding="utf-8"), "old\n")
        self.assertIsNone(json.loads(status.read_text(encoding="utf-8"))["finished"])

    def test_file_changed_during_check_is_rechecked_then_skipped(self):
        writes = {"02 Long": 1, "03 Missing": 9}   # Long changes once (rechecked), Missing on every check (skipped)
        def probe(p):
            if writes.get(p.stem):
                writes[p.stem] -= 1
                p.write_bytes(p.read_bytes() + b"x")
            return dataclasses.replace(self.TRACKS[p.stem], path=str(p))
        self.patch_probe(probe)
        code, records, err = self.run_main("--verbose", "--granular")
        self.assertEqual([(Path(r["file"]).name, r["size"]) for r in records],
                         [("01 Good.mp3", 11), ("02 Long.flac", 12)])
        self.assertIn('"error": "file changed while it was checked"', err)
        self.assertEqual(writes["03 Missing"], 7)

    def test_report_inside_library_refused(self):
        before = self.snapshot()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            compare.main([str(self.library), "--report", str(self.library / "Album" / "r.jsonl")], adapters=[])
        self.assertEqual(self.snapshot(), before)

    def test_no_audio_flag(self):
        self.service["Good"] = [svc("apple", title="Good", preview_url=PREVIEW)]
        with mock.patch.object(compare, "audio_check") as check:
            self.run_main("--no-audio")
        check.assert_not_called()

    def test_probe_error_reported_not_fatal(self):
        def boom(p): raise RuntimeError("boom")
        self.patch_probe(boom)
        code, records, err = self.run_main()
        self.assertEqual((code, records), (0, []))
        self.assertEqual(err.count('"status": "error"'), 3)


if __name__ == "__main__": unittest.main()
