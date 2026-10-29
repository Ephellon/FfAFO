import contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest import mock
import fafo, library_index
from test_audio_sig import synth
from test_spectrum_sig import tones


class FafoCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / "library" / "Album"
        self.library.mkdir(parents=True)
        for name in ("01 One.mp3", "02 Two.flac", "notes.txt"):
            (self.library / name).write_bytes(b"placeholder")
        self.index = self.root / "FAFO" / "index.sqlite3"
        decoded = (synth(11025 * 3), 3.0, 0.2)
        for target in (library_index, fafo):
            patch = mock.patch.object(target, "decode_audio", return_value=decoded)
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fafo.main([*argv])
        return code, [json.loads(l) for l in out.getvalue().splitlines()], err.getvalue()

    def fingerprint(self, *extra, path=None):
        return self.run_cli("fingerprint", str(path or self.library.parent), "--index", str(self.index), *extra)

    def snapshot(self):
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.library.rglob("*")}

    def test_fingerprint_indexes_then_reuses(self):
        before = self.snapshot()
        code, records, err = self.fingerprint()
        self.assertEqual(code, 0)
        self.assertEqual([(Path(r["file"]).name, r["status"]) for r in records], [("01 One.mp3", "indexed"), ("02 Two.flac", "indexed")])
        self.assertEqual(len(records[0]["asfp-v1"]), 30)
        self.assertGreater(records[0]["frames"], 0)
        self.assertNotIn("asfp-v2", records[0])
        self.assertIn('"indexed": 2', err)
        code, again, _ = self.fingerprint()
        self.assertEqual([r["status"] for r in again], ["cached", "cached"])
        self.assertEqual(again[0]["asfp-v1"], records[0]["asfp-v1"])
        self.assertEqual(self.snapshot(), before)

    def test_fingerprint_single_file_subfingerprints_and_quiet(self):
        code, records, _ = self.fingerprint("--subfingerprints", path=self.library / "02 Two.flac")
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["asfp-v2"]), records[0]["frames"])
        code, records, err = self.fingerprint("--quiet")
        self.assertEqual((code, records), (0, []))
        self.assertIn('"indexed": 1, "cached": 1', err)

    def test_fingerprint_without_index_creates_nothing(self):
        code, records, _ = self.fingerprint("--no-index")
        self.assertEqual([r["status"] for r in records], ["computed", "computed"])
        self.assertFalse(self.index.parent.exists())
        code, records, _ = self.fingerprint("--no-index", "--granular", path=self.library / "02 Two.flac")
        self.assertEqual(records[0]["size"], (self.library / "02 Two.flac").stat().st_size)
        self.assertEqual(records[0]["mtime"], (self.library / "02 Two.flac").stat().st_mtime)

    def test_fingerprint_error_is_reported_and_exit_1(self):
        with mock.patch.object(library_index, "decode_audio", side_effect=RuntimeError("bad file")):
            code, records, err = self.fingerprint()
        self.assertEqual((code, records), (1, []))
        self.assertEqual(err.count('"status": "error"'), 2)

    def test_fingerprint_refuses_index_inside_library_and_bad_paths(self):
        for argv in (["fingerprint", str(self.library), "--index", str(self.library / "i.sqlite3")],
                     ["fingerprint", str(self.library / "notes.txt")],
                     ["fingerprint", str(self.root / "missing")]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                fafo.main(argv)
        self.assertFalse((self.library / "i.sqlite3").exists())

    def test_compare_delegates(self):
        with mock.patch.object(fafo.compare, "main", return_value=1) as main:
            self.assertEqual(fafo.main(["compare", "R:\\Local", "--resume"]), 1)
        main.assert_called_once_with(["R:\\Local", "--resume"], prog="fafo.py compare")

    def test_stats(self):
        code, _, err = self.run_cli("stats", "--index", str(self.index))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.fingerprint()
        code, records, _ = self.run_cli("stats", "--index", str(self.index))
        self.assertEqual((code, records[0]["fingerprints"], records[0]["results"]), (0, 2, {}))

    def test_help_and_unknown_service(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            codes = fafo.main(["--help"]), fafo.main([]), fafo.main(["download"])
        self.assertEqual(codes, (0, 2, 2))
        self.assertIn("fafo.py fingerprint", out.getvalue())
        self.assertIn("Unknown service 'download'", err.getvalue())

    def xv3_audio(self, by_name):
        patch = mock.patch.object(fafo.spectrum_sig, "decode", side_effect=lambda p: by_name[Path(p).name])
        patch.start()
        self.addCleanup(patch.stop)

    def test_xv3_signs_every_file_and_writes_nothing(self):
        song = tones(12, (110, 220, 330))
        self.xv3_audio({"01 One.mp3": song, "02 Two.flac": song})
        before = self.snapshot()
        code, records, _ = self.run_cli("xv3", str(self.library), "--bands", "--no-index")
        self.assertEqual(code, 0)
        self.assertEqual([Path(r["file"]).name for r in records], ["01 One.mp3", "02 Two.flac"])
        self.assertEqual(records[0]["xv3"], records[1]["xv3"])
        self.assertEqual((records[0]["segments"], len(records[0]["bands"]), records[0]["status"]), (1, 52, "computed"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "FAFO").exists())

    def test_xv3_spectra_are_cached_in_the_index(self):
        song = tones(12, (110, 220, 330))
        self.xv3_audio({"01 One.mp3": song, "02 Two.flac": song})
        before = self.snapshot()
        _, first, _ = self.run_cli("xv3", str(self.library), "--index", str(self.index))
        _, again, _ = self.run_cli("xv3", str(self.library), "--index", str(self.index))
        self.assertEqual([r["status"] for r in first + again], ["indexed", "indexed", "cached", "cached"])
        self.assertEqual([r["xv3"] for r in first], [r["xv3"] for r in again])
        self.assertEqual(self.snapshot(), before)

    def test_xv3_plot_signed_joins_the_flowerbed_root(self):
        song = tones(12, (110, 220, 330))
        a = self.library / "01 One.mp3"
        self.xv3_audio({a.name: song})
        track = mock.Mock(flowerbed="A/ONE/2024.01")
        with mock.patch.object(fafo.compare, "probe", return_value=track):
            _, [record], _ = self.run_cli("xv3", str(a), "--no-index", "--plot-signed")
        plot, root = record["plot_signed"].split("-")
        self.assertEqual((plot, root, record["flowerbed"]), (record["xv3"], record["root"], "A/ONE/2024.01"))
        self.assertEqual(root, fafo.flowerbed.root_text("A/ONE/2024.01"))

    def test_xv3_pair_reports_added_sections(self):
        song = tones(20, (110, 220, 330))
        a, b = self.library / "01 One.mp3", self.library / "02 Two.flac"
        self.xv3_audio({a.name: song, b.name: song})
        code, [record], _ = self.run_cli("xv3", "--pair", str(a), str(b), "--no-index")
        self.assertEqual((code, record["match"], record["plot_db"]), (0, True, 0.0))
        self.xv3_audio({a.name: song, b.name: song + tones(10, (2500, 5000, 9000))})
        code, [record], _ = self.run_cli("xv3", "--pair", str(a), str(b), "--no-index")
        self.assertEqual((code, record["match"], record["missing"], record["extra"]), (1, False, [], [[20, 30.0]]))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("xv3", "--pair", str(a), "--no-index")


if __name__ == "__main__": unittest.main()
