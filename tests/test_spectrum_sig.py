import math, unittest
import spectrum_sig as s
from audio_sig import _numpy


def tones(seconds, freqs, seed=99, noise=0.02):
    """Sum of equal sines at s.RATE plus a little deterministic LCG noise."""
    x, out = seed, []
    for i in range(int(seconds * s.RATE)):
        x = (1103515245 * x + 12345) % 2**31
        t = i / s.RATE
        out.append(sum(0.2 * math.sin(2 * math.pi * f * t) for f in freqs) + noise * (x / 2**31 - 0.5))
    return out


LOW, HIGH = (110, 220, 330), (2500, 5000, 9000)


class Xv3Tests(unittest.TestCase):
    def test_bands_cover_the_capped_range(self):
        self.assertEqual(s.BANDS, 52)
        self.assertEqual(s.PLOT_BYTES, 33)
        self.assertAlmostEqual(sum(s.TILT), 0, places=9)
        self.assertTrue(all(b > a for a, b in zip(s.EDGES, s.EDGES[1:])))
        self.assertLess(s.CENTERS_HZ[0], 45)
        self.assertLessEqual(s.EDGES[-1] * s.RATE / s.FFT_SIZE, s.HIGH_HZ + s.RATE / s.FFT_SIZE)

    def test_loudness_does_not_count(self):
        quiet = s.analyze([x * 0.25 for x in tones(3, LOW)])
        loud = s.analyze(tones(3, LOW))
        self.assertLess(s.distance(quiet.plot, loud.plot), 0.05)
        self.assertAlmostEqual(sum(loud.plot), 0, places=6)

    def test_different_spectra_are_far_apart(self):
        self.assertGreater(s.distance(s.analyze(tones(3, LOW)).plot, s.analyze(tones(3, HIGH)).plot), 10)

    def test_plot_code_round_trips_within_half_a_step(self):
        plot = s.analyze(tones(3, LOW + HIGH)).plot
        code = s.encode_plot(plot)
        back = s.decode_plot(code)
        self.assertEqual(len(back), s.BANDS)
        for x, y, t in zip(plot, back, s.TILT):
            if abs(x - t) < s.STEP_DB * (s.LEVELS // 2 - 1):
                self.assertLessEqual(abs(x - y), s.STEP_DB / 2 + 1e-9)
            else:      # clamped at the code's range
                self.assertAlmostEqual(abs(y - t), s.STEP_DB * (s.LEVELS // 2) - (x < t), delta=s.STEP_DB)
        self.assertEqual(s.decode_plot(s.encode_plot(back)), back)
        self.assertNotIn(s.PLOT_JOIN, code)

    def test_offset_excerpt_is_heard_in_the_song(self):
        # Like a streaming preview: the excerpt's grid segments straddle the song's, but a sliding window matches.
        song = tones(10, LOW) + tones(10, HIGH) + tones(10, (500, 700, 1300))
        excerpt = song[int(3 * s.STEP_SECONDS * s.RATE):]
        a, b = s.analyze(excerpt), s.analyze(song)
        self.assertEqual(s.unmatched(a, b, s.SECTION_MAX_DB), [])
        self.assertEqual((len(a.windows), len(b.windows)), (18, 21))

    def test_numpy_matches_pure_python(self):
        if _numpy() is None:
            self.skipTest("NumPy not installed")
        pcm = tones(2.5, LOW + HIGH)
        fast, slow = s.analyze(pcm, accel=True), s.analyze(pcm, accel=False)
        self.assertEqual(len(fast.segments), len(slow.segments))
        for x, y in zip(fast.plot, slow.plot):
            self.assertAlmostEqual(x, y, places=6)

    def test_added_section_is_reported_as_extra(self):
        song = tones(30, LOW)
        a, b = s.analyze(song), s.analyze(song + tones(10, HIGH))
        self.assertEqual(s.section_diff(a, b, max_db=3.0), {"missing": [], "extra": [[30, 40.0]]})
        self.assertEqual(s.section_diff(b, a, max_db=3.0), {"missing": [[30, 40.0]], "extra": []})

    def test_window_bands_are_floored_but_the_plot_is_not(self):
        spectrum = s.analyze(tones(3, LOW, noise=0.0002))
        segment = spectrum.segments[0]
        self.assertAlmostEqual(max(segment) - min(segment), s.FLOOR_DB, places=6)
        self.assertGreater(max(spectrum.plot) - min(spectrum.plot), s.FLOOR_DB + 10)

    def test_silent_segments_are_skipped(self):
        spectrum = s.analyze(tones(10, LOW) + [0.0] * (10 * s.RATE) + tones(10, LOW))
        self.assertEqual([seg is None for seg in spectrum.segments], [False, True, False])

    def test_rejects_silence_and_short_audio(self):
        with self.assertRaises(ValueError):
            s.analyze([0.0] * (3 * s.RATE))
        with self.assertRaises(ValueError):
            s.analyze([0.1] * (s.FFT_SIZE - 1))

    def test_quiet_edges_are_trimmed_at_most_ten_seconds(self):
        quiet = lambda secs: [x * 0.03 for x in tones(secs, LOW)]   # about 30 dB under the loud part
        pcm = quiet(5) + tones(30, LOW) + quiet(15)
        trimmed = s.analyze(pcm)
        self.assertEqual(trimmed.trimmed, (5.0, float(s.TRIM_MAX_SECONDS)))
        self.assertEqual(trimmed.starts, [5.0, 15.0, 25.0])
        self.assertEqual(s.unmatched(trimmed, s.analyze(tones(30, LOW)), s.SECTION_MAX_DB), [])
        whole = s.analyze(pcm, trim_db=None)
        self.assertEqual((whole.trimmed, whole.starts[0], len(whole.windows)), ((0.0, 0.0), 0.0, len(trimmed.windows)))
        self.assertEqual(s.analyze(tones(12, LOW)).trimmed, (0.0, 0.0))   # steady audio keeps everything

    def test_cache_blobs_round_trip(self):
        spectrum = s.analyze(tones(10, LOW) + [0.0] * (10 * s.RATE) + tones(10, HIGH))
        back = s.from_blobs(spectrum.duration, *s.to_blobs(spectrum), spectrum.trimmed)
        self.assertEqual((back.plot, back.starts, back.trimmed), (spectrum.plot, spectrum.starts, spectrum.trimmed))
        self.assertEqual([w is None for w in back.windows], [w is None for w in spectrum.windows])
        self.assertEqual([w is None for w in back.segments], [False, True, False])
        for x, y in zip(back.windows, spectrum.windows):
            if x is not None:
                self.assertLessEqual(max(abs(p - q) for p, q in zip(x, y)), 0.005 + 1e-9)

    def test_plot_signature(self):
        sig = s.plot_signature(s.TILT, bytes(range(20)))
        plot, root = sig.split(s.PLOT_JOIN)
        self.assertEqual((len(plot), len(root), s.PLOT_JOIN), (48, 29, "-"))

    def test_vocal_weights(self):
        weights = s.band_weights(0.0)
        self.assertEqual(len(weights), s.BANDS)
        for c, w in zip(s.CENTERS_HZ, weights):
            self.assertEqual(w, 0.0 if 60 <= c <= 1050 else 1.0)
        with self.assertRaises(ValueError):
            s.distance([0.0] * s.BANDS, [0.0] * s.BANDS, [0.0] * s.BANDS)


if __name__ == "__main__":
    unittest.main()
