import math, unittest
from audio_sig import (ALPHABET, FRAME_SIZE, HOP_SIZE, SUB_BITS, _numpy, align_excerpt, analyze, best_alignment, decode_bytes,
                       encode_bytes, encode_signature, fingerprint, hamming_distance, subfingerprints)

class EncodingTests(unittest.TestCase):
    def test_alphabet_size(self): self.assertEqual(len(ALPHABET), 49)
    def test_round_trip(self):
        for n in (1, 20, 21):
            raw = bytes((i * 17 + 3) % 256 for i in range(n))
            self.assertEqual(decode_bytes(encode_bytes(raw), n), raw)
    def test_signature_length(self):
        sig = encode_signature(bytes(21), bytes(20))
        self.assertNotIn("/", sig)
        left, right = sig.split("_")
        self.assertEqual((len(left), len(right), len(sig)), (30, 29, 60))

class FingerprintTests(unittest.TestCase):
    def test_repeatable(self):
        rate, length = 11025, 11025 * 3
        pcm = [0.35 * math.sin(2 * math.pi * 440 * i / rate) + 0.15 * math.sin(2 * math.pi * 997 * i / rate) for i in range(length)]
        a, b = fingerprint(pcm), fingerprint(pcm)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 21)
        self.assertEqual(hamming_distance(a, b), 0)
    def test_silence_rejected(self):
        with self.assertRaises(ValueError): fingerprint([0.0] * 22050)


def synth(n, seed=12345, f0=220, f1=1375):
    """Deterministic chirp + gated tone + LCG noise at 11,025 Hz."""
    x, out = seed, []
    for i in range(n):
        x = (1103515245 * x + 12345) % 2**31
        t = i / 11025
        out.append(0.3 * math.sin(2 * math.pi * (f0 + 180 * t) * t)
                   + 0.2 * math.sin(2 * math.pi * f1 * t) * (1 if int(t * 4) % 2 else 0.3) + 0.05 * (x / 2**31 - 0.5))
    return out


class V1RegressionTests(unittest.TestCase):
    def test_v1_output_pinned(self):
        # Pinned before the v2 refactor; any change here invalidates every stored asfp-v1 signature.
        self.assertEqual(encode_bytes(fingerprint(synth(11025 * 6))), "EBgcwDkGtw6nw3YxfywmGAfUqbWCYH")


class SubFingerprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.full = synth(11025 * 8)
        cls.ref = subfingerprints(cls.full)

    def test_shape_and_determinism(self):
        frames = (len(self.full) - FRAME_SIZE) // HOP_SIZE + 1
        self.assertEqual(len(self.ref), frames - 1)
        self.assertTrue(all(0 <= v < 1 << SUB_BITS for v in self.ref))
        self.assertEqual(subfingerprints(self.full), self.ref)

    def test_analyze_matches_separate_calls(self):
        v1, sub = analyze(self.full)
        self.assertEqual((v1, sub), (fingerprint(self.full), self.ref))

    def test_frame_aligned_excerpt_is_exact(self):
        start = 40 * HOP_SIZE
        self.assertEqual(best_alignment(subfingerprints(self.full[start:start + 11025 * 3]), self.ref), (0.0, 40))

    def test_unaligned_excerpt_still_matches(self):
        start = 40 * HOP_SIZE + HOP_SIZE // 4
        ber, offset = best_alignment(subfingerprints(self.full[start:start + 11025 * 3]), self.ref)
        self.assertLess(ber, 0.2)
        self.assertEqual(offset, 40)

    def test_align_excerpt_recovers_half_hop_misalignment(self):
        start = 40 * HOP_SIZE + HOP_SIZE // 2
        clip = self.full[start:start + 11025 * 3]
        self.assertGreater(best_alignment(subfingerprints(clip), self.ref)[0], 0.2)
        self.assertEqual(align_excerpt(clip, self.ref), (0.0, 41))

    def test_different_signal_is_far(self):
        ber, _ = best_alignment(subfingerprints(synth(11025 * 3, seed=99, f0=330, f1=2100)), self.ref)
        self.assertGreater(ber, 0.4)

    def test_longer_query_gives_negative_offset(self):
        start = 25 * HOP_SIZE
        ber, offset = best_alignment(self.ref, subfingerprints(self.full[start:start + 11025 * 3]))
        self.assertEqual((ber, offset), (0.0, -25))

    def test_empty_rejected(self):
        with self.assertRaises(ValueError): best_alignment([], self.ref)


@unittest.skipIf(_numpy() is None, "NumPy not installed")
class NumpyParityTests(unittest.TestCase):
    """The NumPy path only accelerates; pure Python stays the reference (PLAN.md §7.2: BER < 0.01)."""
    def test_parity(self):
        for seed, n in ((12345, 11025 * 8), (777, 11025 * 5 + 300)):
            pcm = synth(n, seed=seed)
            (v1_py, sub_py), (v1_np, sub_np) = analyze(pcm), analyze(pcm, accel=True)
            with self.subTest(seed=seed):
                self.assertEqual(v1_np, v1_py)
                self.assertEqual(len(sub_np), len(sub_py))
                errors = sum((a ^ b).bit_count() for a, b in zip(sub_np, sub_py))
                self.assertLess(errors / (len(sub_py) * SUB_BITS), 0.01)

    def test_same_validation(self):
        for bad in ([0.0] * FRAME_SIZE * 2, [0.5] * (FRAME_SIZE - 1)):
            with self.assertRaises(ValueError): analyze(bad, accel=True)
        with self.assertRaises(ValueError): analyze(synth(FRAME_SIZE * 2), sample_rate=44100, accel=True)

    def test_accelerated_excerpt_alignment(self):
        full = synth(11025 * 8)
        ref, clip = subfingerprints(full), full[40 * HOP_SIZE + 256:]
        self.assertEqual(align_excerpt(clip, ref, accel=True), align_excerpt(clip, ref))

if __name__ == "__main__": unittest.main()
