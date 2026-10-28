"""Shared audio fingerprint DSP and compact alphabet encoding (algorithm v1 hash, v2 sub-fingerprints)."""
from __future__ import annotations
import math

ALPHABET = "346789ABCDEFGHJKLMNPQRTUVWXYabcdefghijkmnpqrtwxyz"
BASE = len(ALPHABET)
PERCEPTUAL_BYTES = 21
ROOT_BYTES = 20
SIGNATURE_SEPARATOR = "_"   # not in ALPHABET, so a signature always splits unambiguously
ALGORITHM_VERSION = "asfp-v1"
TARGET_RATE = 11025
FRAME_SIZE = 2048
HOP_SIZE = 512
SEGMENTS = 6
BANDS = 29
ALGORITHM_V2 = "asfp-v2"
SUB_BITS = BANDS - 1         # bits per v2 sub-fingerprint (one per adjacent band pair)
COARSE_STEP = 8              # best_alignment: query frames sampled in the coarse pass
REFINE_CANDIDATES = 5        # best_alignment: coarse winners rescored on every frame
EXCERPT_PHASES = (0, 128, 256, 384)   # align_excerpt: sample shifts covering the 512-sample hop grid


def encode_bytes(data: bytes) -> str:
    """Encode bytes as fixed-width base-N text, preserving leading zero bytes."""
    n = int.from_bytes(data, "big")
    width = math.ceil(len(data) * 8 / math.log2(BASE))
    chars = []
    for _ in range(width):
        n, r = divmod(n, BASE)
        chars.append(ALPHABET[r])
    if n:
        raise ValueError("Alphabet width cannot represent this byte sequence")
    return "".join(reversed(chars))


def decode_bytes(text: str, size: int) -> bytes:
    """Decode fixed-width base-N text; reject invalid or overflowing values."""
    expected = math.ceil(size * 8 / math.log2(BASE))
    if len(text) != expected:
        raise ValueError(f"Expected {expected} alphabet characters")
    n = 0
    for ch in text:
        try:
            digit = ALPHABET.index(ch)
        except ValueError as exc:
            raise ValueError(f"Invalid alphabet character: {ch!r}") from exc
        n = n * BASE + digit
    if n >= 1 << (size * 8):
        raise ValueError("Encoded value exceeds requested byte length")
    return n.to_bytes(size, "big")


def encode_signature(perceptual: bytes, root: bytes) -> str:
    """Return the readable form: 30 chars, underscore, then 29 chars (URL- and filename-safe)."""
    if len(perceptual) != PERCEPTUAL_BYTES or len(root) != ROOT_BYTES:
        raise ValueError("Expected a 21-byte perceptual ID and 20-byte root")
    return f"{encode_bytes(perceptual)}{SIGNATURE_SEPARATOR}{encode_bytes(root)}"


def _fft_real_power(frame: list[float]) -> list[float]:
    """Radix-2 FFT power spectrum; kept explicit to mirror the JS implementation."""
    n = len(frame)
    re = frame[:]
    im = [0.0] * n
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
    size = 2
    while size <= n:
        half = size // 2
        angle = -2.0 * math.pi / size
        wmr, wmi = math.cos(angle), math.sin(angle)
        for start in range(0, n, size):
            wr, wi = 1.0, 0.0
            for k in range(half):
                even = start + k
                odd = even + half
                tr = wr * re[odd] - wi * im[odd]
                ti = wr * im[odd] + wi * re[odd]
                re[odd], im[odd] = re[even] - tr, im[even] - ti
                re[even], im[even] = re[even] + tr, im[even] + ti
                wr, wi = wr * wmr - wi * wmi, wr * wmi + wi * wmr
        size *= 2
    return [re[i] * re[i] + im[i] * im[i] for i in range(n // 2 + 1)]


def _band_edges(sample_rate: int) -> list[int]:
    """Log-spaced FFT-bin edges, clamped to valid bins."""
    nyquist = sample_rate / 2
    low, high = 80.0, min(5000.0, nyquist * 0.95)
    edges = [round((low * (high / low) ** (i / BANDS)) * FRAME_SIZE / sample_rate)
             for i in range(BANDS + 1)]
    edges = [max(1, min(FRAME_SIZE // 2, e)) for e in edges]
    # At the selected rate and FFT size, adjacent edges should be distinct.
    for i in range(1, len(edges)):
        edges[i] = max(edges[i], edges[i - 1] + 1)
    edges[-1] = min(edges[-1], FRAME_SIZE // 2)
    return edges


def _numpy():
    """NumPy if installed, else None. It only ever accelerates; the pure-Python path is the reference."""
    try:
        import numpy
    except ImportError:
        return None
    return numpy


def _validate(samples, sample_rate: int, rms: float) -> None:
    if sample_rate != TARGET_RATE:
        raise ValueError(f"Input must be resampled to {TARGET_RATE} Hz")
    if len(samples) < FRAME_SIZE:
        raise ValueError(f"Audio must be at least {FRAME_SIZE / TARGET_RATE:.3f} seconds")
    if not math.isfinite(rms) or rms < 1e-5:
        raise ValueError("Audio is silent or too quiet to fingerprint")


def _band_energies_numpy(np, samples, sample_rate: int) -> list[list[float]]:
    """Same frames, window, bands and log1p as _band_energies, vectorised (float64 throughout)."""
    x = np.asarray(samples, dtype=np.float64)
    _validate(x, sample_rate, math.sqrt(float(np.dot(x, x)) / len(x)) if len(x) else 0.0)
    edges = _band_edges(sample_rate)
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FRAME_SIZE) / (FRAME_SIZE - 1))
    frames = np.lib.stride_tricks.sliding_window_view(x, FRAME_SIZE)[::HOP_SIZE]
    out = []
    for start in range(0, len(frames), 1024):   # bounded memory: 1024 frames × 2048 samples per batch
        power = np.abs(np.fft.rfft(frames[start:start + 1024] * window, axis=1)) ** 2
        out.append(np.log1p(np.add.reduceat(power[:, :edges[-1]], edges[:-1], axis=1)))
    return np.concatenate(out).tolist()


def _band_energies(samples: list[float], sample_rate: int = TARGET_RATE, accel: bool = False) -> list[list[float]]:
    """Validate PCM and return log band energies (BANDS values) for every analysis frame.

    Input must be mono float PCM already resampled to TARGET_RATE. Silence is rejected
    because its all-zero hash would create misleading matches. `accel` uses NumPy when installed.
    """
    np = _numpy() if accel else None
    if np is not None:
        return _band_energies_numpy(np, samples, sample_rate)
    _validate(samples, sample_rate, math.sqrt(sum(x * x for x in samples) / len(samples)) if len(samples) else 0.0)

    edges = _band_edges(sample_rate)
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (FRAME_SIZE - 1)) for i in range(FRAME_SIZE)]
    frames = []
    for start in range(0, len(samples) - FRAME_SIZE + 1, HOP_SIZE):
        frame = [samples[start + i] * window[i] for i in range(FRAME_SIZE)]
        power = _fft_real_power(frame)
        frames.append([math.log1p(sum(power[edges[b]:edges[b + 1]])) for b in range(BANDS)])
    return frames


def _v1_from_energies(energies: list[list[float]], n_samples: int) -> bytes:
    sums = [[0.0] * BANDS for _ in range(SEGMENTS)]
    counts = [0] * SEGMENTS
    for index, frame in enumerate(energies):
        center_fraction = (index * HOP_SIZE + FRAME_SIZE / 2) / n_samples
        seg = min(SEGMENTS - 1, int(center_fraction * SEGMENTS))
        for b in range(BANDS):
            sums[seg][b] += frame[b]
        counts[seg] += 1

    features = []
    for seg in range(SEGMENTS):
        divisor = max(1, counts[seg])
        features.append([v / divisor for v in sums[seg]])

    bits = []
    for seg in range(SEGMENTS):
        bits.extend(1 if features[seg][b] > features[seg][b + 1] else 0 for b in range(BANDS - 1))
    # 6 regions × 28 adjacent-band comparisons = 168 bits = 21 bytes.
    out = bytearray(PERCEPTUAL_BYTES)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (7 - (i % 8))
    return bytes(out)


def fingerprint(samples: list[float], sample_rate: int = TARGET_RATE) -> bytes:
    """Create the v1 168-bit perceptual hash from six time regions and 29 log bands."""
    return _v1_from_energies(_band_energies(samples, sample_rate), len(samples))


def _subfingerprints_from_energies(energies: list[list[float]]) -> list[int]:
    out = []
    for prev, cur in zip(energies, energies[1:]):
        value = 0
        for m in range(SUB_BITS):
            value = (value << 1) | ((cur[m] - cur[m + 1]) - (prev[m] - prev[m + 1]) > 0)
        out.append(value)
    return out


def subfingerprints(samples: list[float], sample_rate: int = TARGET_RATE, accel: bool = False) -> list[int]:
    """asfp-v2: one 28-bit sub-fingerprint per frame (after the first), MSB = lowest band pair.

    Bit m is the sign of the change, from the previous frame, in the energy difference between
    bands m and m+1. Being local in time, a sequence from an excerpt lines up with the full track.
    """
    return _subfingerprints_from_energies(_band_energies(samples, sample_rate, accel))


def analyze(samples: list[float], sample_rate: int = TARGET_RATE, accel: bool = False) -> tuple[bytes, list[int]]:
    """One FFT pass producing both the v1 hash and the v2 sub-fingerprint sequence."""
    energies = _band_energies(samples, sample_rate, accel)
    return _v1_from_energies(energies, len(samples)), _subfingerprints_from_energies(energies)


def _errors(query: list[int], reference: list[int], offset: int, step: int = 1) -> int:
    return sum((query[i] ^ reference[offset + i]).bit_count() for i in range(0, len(query), step))


def best_alignment(query: list[int], reference: list[int]) -> tuple[float, int]:
    """Slide `query` along `reference`; return (lowest bit error rate, frame offset into reference).

    A coarse pass scores every offset on every COARSE_STEP-th query frame, then the best few
    offsets (and neighbours) are rescored on all frames. If the query is the longer sequence the
    roles swap and the offset is negative (reference starts that many frames into the query).
    """
    if not query or not reference:
        raise ValueError("Sub-fingerprint sequences must not be empty")
    sign = 1
    if len(query) > len(reference):
        query, reference, sign = reference, query, -1
    offsets = range(len(reference) - len(query) + 1)
    coarse = sorted(offsets, key=lambda o: _errors(query, reference, o, COARSE_STEP))[:REFINE_CANDIDATES]
    refine = {o + d for o in coarse for d in (-2, -1, 0, 1, 2) if 0 <= o + d < len(offsets)}
    errors, offset = min((_errors(query, reference, o), o) for o in refine)
    return errors / (len(query) * SUB_BITS), sign * offset


def align_excerpt(samples: list[float], reference: list[int], sample_rate: int = TARGET_RATE,
                  phases: tuple[int, ...] = EXCERPT_PHASES, accel: bool = False) -> tuple[float, int]:
    """Best (BER, frame offset) of an excerpt's PCM against a reference sub-fingerprint sequence.

    An excerpt rarely starts on the reference's hop grid; up to half a hop of misalignment can push
    BER from ~0 to ~0.25. Re-analysing the excerpt at sub-hop sample shifts recovers the grid.
    """
    return min(best_alignment(subfingerprints(samples[p:], sample_rate, accel), reference) for p in phases)


def hamming_distance(a: bytes, b: bytes) -> int:
    if len(a) != len(b):
        raise ValueError("Fingerprints must have equal lengths")
    return sum((x ^ y).bit_count() for x, y in zip(a, b))
