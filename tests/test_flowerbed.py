import hashlib, os, sqlite3, tempfile, unittest
import flowerbed as fb, registry
from audio_sig import PERCEPTUAL_BYTES, ROOT_BYTES, decode_bytes, encode_bytes
from compare import parse_probe


def fp(byte: int, flip: int = 0) -> str:
    """A perceptual ID: all bytes `byte`, with the low `flip` bits of the last byte inverted."""
    raw = bytearray([byte] * PERCEPTUAL_BYTES)
    raw[-1] ^= (1 << flip) - 1
    return encode_bytes(bytes(raw))


class KeyTests(unittest.TestCase):
    def test_normalize_matches_the_decided_examples(self):
        self.assertEqual(fb.normalize("Ty Dolla $ign , Ye"), "TY DOLLA $IGN,YE")
        self.assertEqual(fb.normalize("Song ( Remix )"), "SONG(REMIX)")
        self.assertEqual(fb.normalize("  back\tthat   azz up "), "BACK THAT AZZ UP")
        self.assertEqual(fb.normalize("Don't  Stop - Live"), "DON'T STOP-LIVE")
        self.assertEqual(fb.normalize("café"), "CAFÉ")   # NFC first, so decomposed tags hash the same

    def test_artists_are_trimmed_and_joined(self):
        self.assertEqual(fb.artists(" Kanye West ;  Ty Dolla $ign "), "KANYE WEST;TY DOLLA $IGN")
        self.assertEqual(fb.artists("A\0B"), "A;B")
        self.assertEqual(fb.artists("A;;B;"), "A;B")

    def test_month(self):
        self.assertEqual(fb.month("2024-08-03"), "2024.08")
        self.assertEqual(fb.month("2019"), "2019.01")
        self.assertEqual(fb.month("1998.11"), "1998.11")
        self.assertIsNone(fb.month("2024-13-01"))
        self.assertIsNone(fb.month("unknown"))

    def test_key_and_root(self):
        key = fb.key("¥$", "Slide", "2024-08-03")
        self.assertEqual(key, "¥$/SLIDE/2024.08")
        self.assertEqual(fb.root(key), hashlib.sha1(b"flowerbed-v1\n" + key.encode()).digest())
        self.assertEqual(len(fb.root(key)), ROOT_BYTES)
        self.assertEqual(decode_bytes(fb.root_text(key), ROOT_BYTES), fb.root(key))
        with self.assertRaises(ValueError):
            fb.key("", "Slide", "2024")

    def test_probe_reads_only_tags(self):
        data = {"streams": [{"codec_type": "audio", "duration": "100"}],
                "format": {"tags": {"ARTIST": "Juvenile", "TITLE": "Back That Azz Up", "date": "1998-11-03"}}}
        self.assertEqual(parse_probe("x.mp3", data).flowerbed, "JUVENILE/BACK THAT AZZ UP/1998.11")
        del data["format"]["tags"]["TITLE"]   # the title falls back to the file name, but flowerbed never does
        self.assertIsNone(parse_probe("x.mp3", data).flowerbed)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.db = registry.connect(":memory:")
        self.root = fb.root_text("A/SONG/2024.01")
        self.other = fb.root_text("A/OTHER SONG/2024.01")

    def tearDown(self):
        self.db.close()

    def scheme(self, root):
        return self.db.execute("SELECT scheme FROM roots WHERE root=?", (root,)).fetchone()[0]

    def test_new_flowerbed_root_is_the_tag_hash(self):
        r = registry.resolve(self.db, fp(1), flowerbed=self.root)
        self.assertEqual((r["status"], r["root"]), ("created", self.root))
        self.assertEqual(self.scheme(self.root), fb.SCHEME)

    def test_same_tags_join_without_an_audio_match(self):
        registry.resolve(self.db, fp(1), flowerbed=self.root)
        r = registry.resolve(self.db, fp(200), flowerbed=self.root)
        self.assertEqual((r["status"], r["root"]), ("rooted", self.root))

    def test_random_root_keeps_identity_and_gains_an_alias(self):
        random_root = registry.resolve(self.db, fp(1))["root"]
        self.assertEqual(self.scheme(random_root), fb.RANDOM)
        r = registry.resolve(self.db, fp(1, flip=3), flowerbed=self.root)
        self.assertEqual((r["status"], r["root"]), ("matched", random_root))
        self.assertEqual(registry.canonical_root(self.db, self.root), random_root)
        r = registry.resolve(self.db, fp(150), flowerbed=self.root)   # tags alone now reach the random root too
        self.assertEqual((r["status"], r["root"]), ("rooted", random_root))

    def test_audio_and_tags_disagree_is_ambiguous_and_writes_nothing(self):
        registry.resolve(self.db, fp(1), flowerbed=self.root)
        registry.resolve(self.db, fp(200), flowerbed=self.other)
        before = self.db.execute("SELECT count(*) FROM fingerprints").fetchone()[0]
        r = registry.resolve(self.db, fp(1, flip=3), flowerbed=self.other)
        self.assertEqual((r["status"], r["reason"]), ("ambiguous", "tags"))
        self.assertEqual([c["root"] for c in r["candidates"]], [self.root, self.other])
        self.assertEqual(self.db.execute("SELECT count(*) FROM fingerprints").fetchone()[0], before)
        r = registry.resolve(self.db, fp(1), flowerbed=fb.root_text("A/RETAGGED/2024.01"))   # exact audio, new tags
        self.assertEqual(r["status"], "ambiguous")
        self.assertFalse(self.db.in_transaction)

    def test_without_flowerbed_nothing_changes(self):
        r1 = registry.resolve(self.db, fp(1))
        r2 = registry.resolve(self.db, fp(1, flip=3))
        self.assertEqual((r1["status"], r2["status"], r2["root"]), ("created", "matched", r1["root"]))

    def test_old_database_gets_the_scheme_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite3")
            old = sqlite3.connect(path)
            old.executescript("CREATE TABLE roots (root TEXT PRIMARY KEY, created_at REAL NOT NULL, note TEXT NOT NULL DEFAULT '');"
                              "INSERT INTO roots VALUES ('x', 0, '');")
            old.close()
            db = registry.connect(path)
            self.assertEqual(db.execute("SELECT scheme FROM roots").fetchone()[0], fb.RANDOM)
            db.close()


if __name__ == "__main__":
    unittest.main()
