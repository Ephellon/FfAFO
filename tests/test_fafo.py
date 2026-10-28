import contextlib, io, json, tempfile, unittest
from pathlib import Path
from unittest import mock
import fafo, library_index
from test_audio_sig import synth


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


if __name__ == "__main__": unittest.main()
