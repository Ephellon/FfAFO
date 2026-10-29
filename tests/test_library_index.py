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
        opts = {"audio": True, "country": "US"}
        record = {"file": str(self.track), "status": "ok", "mismatches": [], "checked": "2026-01-01T00:00:00+00:00"}
        with LibraryIndex(self.db) as index:
            self.assertIsNone(index.result(str(self.track), opts))
            index.save_result(str(self.track), opts, {k: v for k, v in record.items() if k != "checked"})
            self.assertRegex(index.result(str(self.track), opts)["checked"], r"^\d{4}-\d\d-\d\dT")   # older rows: checked_at
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


class FolderChunkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache = root / "FAFO"
        self.single = self.cache / "library-index.sqlite3"
        self.tracks = []
        for folder in ("Artist A/Post Malone", "Artist B/Post Malone", "Artist A/¥$"):
            (root / folder).mkdir(parents=True)
            track = root / folder / "01 song.mp3"
            track.write_bytes(folder.encode())
            self.tracks.append(str(track))
        patch = mock.patch.object(library_index, "decode_audio", return_value=(synth(11025 * 3), 3.0, 0.2))
        self.decode = patch.start()
        self.addCleanup(patch.stop)

    def test_chunk_names_follow_javascript_escape(self):
        self.assertEqual(library_index.chunk_name("Post Malone"), "Post;20Malone")
        self.assertEqual(library_index.chunk_name("¥$"), ";A5;24")
        self.assertEqual(library_index.chunk_name("カニエ"), ";u30AB;u30CB;u30A8")
        self.assertEqual(library_index.chunk_name("a;b"), "a;3Bb")
        self.assertEqual(library_index.chunk_name("Vol. 1."), "Vol.;201;2E")
        self.assertEqual(library_index.chunk_name("A\U0001F600"), "A;uD83D;uDE00")   # UTF-16 code units, like escape()
        self.assertEqual(library_index.chunk_name("é"), ";E9")                 # NFC first

    def test_same_folder_name_shares_a_database(self):
        with library_index.ChunkedIndex(self.cache, source=self.single) as index:
            for t in self.tracks:
                index.subfingerprints(t)
        self.assertEqual(sorted(p.name for p in self.cache.glob("*.sqlite3")), [";A5;24.sqlite3", "Post;20Malone.sqlite3"])
        self.assertFalse(self.single.exists())

    def test_first_use_copies_from_the_single_index_without_decoding(self):
        opts = {"audio": True}
        with LibraryIndex(self.single) as single:
            for t in self.tracks:
                single.subfingerprints(t)
                single.save_result(t, opts, {"file": t, "status": "ok"})
        before = self.single.read_bytes()
        self.decode.reset_mock()
        with library_index.ChunkedIndex(self.cache, source=self.single) as index:
            self.assertTrue(all(index.cached(t) for t in self.tracks))
            self.assertEqual(index.result(self.tracks[2], opts)["file"], self.tracks[2])
            stats = index.stats()
        self.assertEqual(self.decode.call_count, 0)
        self.assertEqual(self.single.read_bytes(), before)   # left in place, unchanged
        self.assertEqual((stats["chunks"], stats["fingerprints"], stats["results"]), (2, 3, {"ok": 3}))


if __name__ == "__main__": unittest.main()
