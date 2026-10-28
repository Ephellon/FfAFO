import os, tempfile, unittest
from pathlib import Path
from unittest import mock
import library_index
from library_index import LibraryIndex, _pack, _unpack
from test_audio_sig import synth


class LibraryIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.track = self.root / "song.mp3"
        self.track.write_bytes(b"placeholder")
        self.db = self.root / "index.sqlite3"
        self.pcm = synth(11025 * 3)
        patch = mock.patch.object(library_index, "decode_audio", return_value=(self.pcm, 3.0, 0.2))
        self.decode = patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_pack_round_trip(self):
        values = [0, 1, (1 << 28) - 1, 12345678]
        self.assertEqual(_unpack(_pack(values)), values)

    def test_lazy_no_file_until_used(self):
        LibraryIndex(self.db).close()
        self.assertFalse(self.db.exists())

    def test_creates_missing_cache_folder(self):
        db = self.root / "FAFO" / "index.sqlite3"
        with LibraryIndex(db) as index:
            index.subfingerprints(str(self.track))
        self.assertTrue(db.exists())

    def test_accel_flag_reaches_analysis(self):
        with mock.patch.object(library_index, "analyze", wraps=library_index.analyze) as analyze:
            with LibraryIndex(self.db, accel=False) as index:
                index.subfingerprints(str(self.track))
        self.assertFalse(analyze.call_args.kwargs["accel"])

    def test_computed_once_then_cached_across_instances(self):
        with LibraryIndex(self.db) as index:
            first = index.subfingerprints(str(self.track))
            self.assertTrue(index.cached(str(self.track)))
        with LibraryIndex(self.db) as index:
            self.assertEqual(index.subfingerprints(str(self.track)), first)
        self.assertEqual(self.decode.call_count, 1)
        self.assertEqual(first, library_index.analyze(self.pcm)[1])

    def test_changed_file_is_recomputed(self):
        with LibraryIndex(self.db) as index:
            index.subfingerprints(str(self.track))
            st = self.track.stat()
            os.utime(self.track, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            self.assertFalse(index.cached(str(self.track)))
            index.subfingerprints(str(self.track))
        self.assertEqual(self.decode.call_count, 2)

    def test_decode_failure_stores_nothing(self):
        self.decode.side_effect = RuntimeError("bad file")
        with LibraryIndex(self.db) as index:
            with self.assertRaises(RuntimeError): index.subfingerprints(str(self.track))
            self.assertFalse(index.cached(str(self.track)))

    def test_results_keyed_by_file_state_and_options(self):
        opts, record = {"audio": True, "country": "US"}, {"file": str(self.track), "status": "ok", "mismatches": []}
        with LibraryIndex(self.db) as index:
            self.assertIsNone(index.result(str(self.track), opts))
            index.save_result(str(self.track), opts, record)
            self.assertEqual(index.result(str(self.track), {"country": "US", "audio": True}), record)   # key order irrelevant
            self.assertIsNone(index.result(str(self.track), {**opts, "audio": False}))
            self.track.write_bytes(b"different size")
            self.assertIsNone(index.result(str(self.track), opts))

    def test_library_file_unchanged(self):
        before = (self.track.read_bytes(), self.track.stat().st_mtime_ns)
        with LibraryIndex(self.db) as index:
            index.subfingerprints(str(self.track))
        self.assertEqual((self.track.read_bytes(), self.track.stat().st_mtime_ns), before)


if __name__ == "__main__": unittest.main()
