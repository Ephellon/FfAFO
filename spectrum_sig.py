"""xv3 (experimental): spectrum-plot signatures, after the averaged PSD of an audio spectrum plot.

Not promoted: `compare` does not use it and its thresholds are still being measured (PLAN.md §9.3).
A plot is the whole file's average power per 1/6-octave band, relative to its own mean so loudness
does not count. Segment spectra (one per SEGMENT_SECONDS) keep coarse time order, so an added or
missing section shows up even when it barely moves the whole-file average.
"""
from __future__ import annotations
import array, math, subprocess, sys
from dataclasses import dataclass
from pathlib import Path
from audio_sig import _fft_real_power, _numpy, decode_bytes, encode_bytes

ALGORITHM = "xv3"
RATE = 32000                 # Nyquist 16 kHz: room for the 15 kHz cap, cheaper to decode than 44.1 kHz
FFT_SIZE = 8192              # 3.9 Hz bins at RATE
HOP = FFT_SIZE // 2
LOW_HZ = 40.0
HIGH_HZ = 15000.0            # MP3 encoders low-pass near 16 kHz; above this, plots compare encoders, not songs
BANDS_PER_OCTAVE = 6
SEGMENT_SECONDS = 10
STEP_SECONDS = 1.25          # match candidates: SEGMENT_SECONDS windows starting every STEP_SECONDS
SILENT_DBFS = -60.0          # windows quieter than this (RMS) are silence or fades, not content
FLOOR_DB = 40.0              # window bands this far below the window's loudest are "empty": encoders fill
                             # them with noise (AAC quiet intros measured 12-15 dB apart without it)
TILT_DB_PER_OCTAVE = -5.0    # typical music slope (library mean -5.0, SD 0.7); codes store the residual
STEP_DB = 1.0                # plot quantization: 32 levels x 1 dB = +/-16 dB around the tilt
LEVEL_BITS = 5
PLOT_JOIN = "$"              # reserved for --plot-signed (plot $ flowerbed root); not used yet
PLOT_MAX_DB = 1.0            # provisional: largest whole-file plot distance for "same song" (PLAN.md §9.3)
SECTION_MAX_DB = 1.5         # provisional: largest segment distance for "same section" (PLAN.md §9.3)


def band_edges() -> list[int]:
    """FFT-bin edges of the 1/6-octave bands from LOW_HZ to HIGH_HZ, each at least one bin wide."""
    count = math.ceil(math.log2(HIGH_HZ / LOW_HZ) * BANDS_PER_OCTAVE)
    hz = [LOW_HZ * 2 ** (k / BANDS_PER_OCTAVE) for k in range(count)] + [HIGH_HZ]
    edges = [round(f * FFT_SIZE / RATE) for f in hz]
    for i in range(1, len(edges)):
        edges[i] = max(edges[i], edges[i - 1] + 1)
    return edges


EDGES = band_edges()
BANDS = len(EDGES) - 1
LEVELS = 1 << LEVEL_BITS
PLOT_BYTES = (BANDS * LEVEL_BITS + 7) // 8
CENTERS_HZ = [math.sqrt(EDGES[b] * EDGES[b + 1]) * RATE / FFT_SIZE for b in range(BANDS)]
_tilt = [TILT_DB_PER_OCTAVE * math.log2(c / 1000) for c in CENTERS_HZ]
TILT = [t - sum(_tilt) / BANDS for t in _tilt]     # mean-centred, like a plot


@dataclass
class Spectrum:
    duration: float
    plot: list[float]                       # whole-file dB per band, relative to its mean
    segments: list[list[float] | None]      # same (floored at FLOOR_DB), per SEGMENT_SECONDS grid; None where silent
    windows: list[list[float] | None]       # same, SEGMENT_SECONDS long, starting every STEP_SECONDS


def _relative(db: list[float], floor: float | None = None) -> list[float]:
    if floor is not None:
        top = max(db)
        db = [max(x, top - floor) for x in db]
    mean = sum(db) / len(db)
    return [x - mean for x in db]


def _db(power_sum: list[float], frames: int) -> list[float]:
    return [10 * math.log10(p / frames + 1e-20) for p in power_sum]


def _window_bounds(n_samples: int) -> list[tuple[int, int]]:
    """SEGMENT_SECONDS spans starting every STEP_SECONDS. A tail shorter than half a segment is dropped
    unless it is the only window."""
    size, step = SEGMENT_SECONDS * RATE, int(STEP_SECONDS * RATE)
    return [(s, min(s + size, n_samples)) for s in range(0, n_samples, step)
            if s == 0 or n_samples - s >= size // 2]


def _band_power_numpy(np, x):
    """Per-frame mean power in each band, Hann-windowed (float64)."""
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(FFT_SIZE) / (FFT_SIZE - 1))
    widths = np.diff(EDGES).astype(np.float64)
    frames = np.lib.stride_tricks.sliding_window_view(x, FFT_SIZE)[::HOP]
    out = []
    for start in range(0, len(frames), 256):   # bounded memory: 256 frames x 8192 samples per batch
        power = np.abs(np.fft.rfft(frames[start:start + 256] * window, axis=1)) ** 2
        out.append(np.add.reduceat(power[:, :EDGES[-1]], EDGES[:-1], axis=1) / widths)
    return np.concatenate(out)


def _band_power_python(samples) -> list[list[float]]:
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (FFT_SIZE - 1)) for i in range(FFT_SIZE)]
    rows = []
    for start in range(0, len(samples) - FFT_SIZE + 1, HOP):
        power = _fft_real_power([samples[start + i] * window[i] for i in range(FFT_SIZE)])
        rows.append([sum(power[EDGES[b]:EDGES[b + 1]]) / (EDGES[b + 1] - EDGES[b]) for b in range(BANDS)])
    return rows


def analyze(samples, accel: bool = True) -> Spectrum:
    """Spectrum of mono float PCM at RATE. `accel` uses NumPy when installed (same result, float64)."""
    if len(samples) < FFT_SIZE:
        raise ValueError(f"Audio must be at least {FFT_SIZE / RATE:.3f} seconds")
    np = _numpy() if accel else None
    bounds = _window_bounds(len(samples))
    # A window uses only the frames lying wholly inside it, so a neighbouring section cannot leak in.
    n_frames = (len(samples) - FFT_SIZE) // HOP + 1
    spans = [(-(-a // HOP), max(-(-a // HOP), min((b - FFT_SIZE) // HOP + 1, n_frames))) for a, b in bounds]
    # Prefix sums over frames (band power) and samples (energy) make every window O(BANDS).
    if np is not None:
        x = np.frombuffer(samples, dtype=np.float32).astype(np.float64) if isinstance(samples, array.array) \
            else np.asarray(samples, dtype=np.float64)
        rows = np.vstack([np.zeros(BANDS), np.cumsum(_band_power_numpy(np, x), axis=0)])
        cum = np.concatenate([[0.0], np.cumsum(x * x)])
        sums = [(rows[l] - rows[f]).tolist() for f, l in spans]
        energy = [float(cum[b] - cum[a]) for a, b in bounds]
    else:
        rows, acc = [[0.0] * BANDS], [0.0] * BANDS
        for row in _band_power_python(samples):
            acc = [p + q for p, q in zip(acc, row)]
            rows.append(acc)
        cum, total = [0.0], 0.0
        for v in samples:
            total += v * v
            cum.append(total)
        sums = [[p - q for p, q in zip(rows[l], rows[f])] for f, l in spans]
        energy = [cum[b] - cum[a] for a, b in bounds]
    floor = 10 ** (SILENT_DBFS / 10)
    loud = [l > f and e / (b - a) > floor for (f, l), e, (a, b) in zip(spans, energy, bounds)]
    windows = [_relative(_db(p, l - f), FLOOR_DB) if ok else None for p, (f, l), ok in zip(sums, spans, loud)]
    per = round(SEGMENT_SECONDS / STEP_SECONDS)
    # Grid segments are every per-th window; the whole-file plot pools their frames (silent ones left out).
    grid = range(0, len(windows), per)
    if not any(loud[i] for i in grid):
        raise ValueError("Audio is silent or too quiet to plot")
    total = [sum(sums[i][k] for i in grid if loud[i]) for k in range(BANDS)]
    frames = sum(spans[i][1] - spans[i][0] for i in grid if loud[i])
    return Spectrum(len(samples) / RATE, _relative(_db(total, frames)), [windows[i] for i in grid], windows)


def encode_plot(plot: list[float]) -> str:
    """A plot quantized to LEVEL_BITS per band (STEP_DB steps around the reference TILT), base-49 encoded."""
    levels = [min(LEVELS - 1, max(0, round((x - t) / STEP_DB) + LEVELS // 2)) for x, t in zip(plot, TILT)]
    packed = 0
    for n in levels:
        packed = packed << LEVEL_BITS | n
    return encode_bytes((packed << (PLOT_BYTES * 8 - BANDS * LEVEL_BITS)).to_bytes(PLOT_BYTES, "big"))


def decode_plot(text: str) -> list[float]:
    packed = int.from_bytes(decode_bytes(text, PLOT_BYTES), "big") >> (PLOT_BYTES * 8 - BANDS * LEVEL_BITS)
    levels = [(packed >> (LEVEL_BITS * (BANDS - 1 - b))) & (LEVELS - 1) for b in range(BANDS)]
    return [(n - LEVELS // 2) * STEP_DB + t for n, t in zip(levels, TILT)]


def band_weights(vocal_weight: float = 1.0, low_hz: float = 60.0, high_hz: float = 1050.0) -> list[float]:
    """Per-band weights; bands centred inside [low_hz, high_hz] get `vocal_weight` (0 drops them)."""
    return [vocal_weight if low_hz <= c <= high_hz else 1.0 for c in CENTERS_HZ]


def distance(a: list[float], b: list[float], weights: list[float] | None = None) -> float:
    """Weighted RMS difference in dB between two plots, each re-centred on its own (weighted) mean."""
    w = weights or [1.0] * BANDS
    total = sum(w)
    if not total:
        raise ValueError("All bands have zero weight")
    ma = sum(x * k for x, k in zip(a, w)) / total
    mb = sum(x * k for x, k in zip(b, w)) / total
    return math.sqrt(sum(k * ((x - ma) - (y - mb)) ** 2 for x, y, k in zip(a, b, w)) / total)


def unmatched(a: Spectrum, b: Spectrum, max_db: float, weights: list[float] | None = None) -> list[int]:
    """Indices of a's loud grid segments with no window of b within `max_db`. Order-free, and b's windows
    slide by STEP_SECONDS, so edits, padding and offsets do not count. Empty when all of a is heard in b."""
    theirs = [s for s in b.windows if s is not None]
    return [i for i, s in enumerate(a.segments)
            if s is not None and (not theirs or min(distance(s, t, weights) for t in theirs) > max_db)]


def section_diff(a: Spectrum, b: Spectrum, max_db: float, weights: list[float] | None = None) -> dict:
    """Sections of a not heard in b ("missing" from b) and of b not heard in a ("extra"), as seconds."""
    span = lambda i, s: [i * SEGMENT_SECONDS, round(min((i + 1) * SEGMENT_SECONDS, s.duration), 1)]
    return {"missing": [span(i, a) for i in unmatched(a, b, max_db, weights)],
            "extra": [span(i, b) for i in unmatched(b, a, max_db, weights)]}


def decode(path: Path, seconds: float | None = None) -> array.array:
    """Decode a local file to mono float32 PCM at RATE through FFmpeg (read-only)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", *(["-t", str(seconds)] if seconds else []), "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "FFmpeg decode failed")
    pcm = array.array("f"); pcm.frombytes(proc.stdout[:len(proc.stdout) // 4 * 4])
    if sys.byteorder != "little": pcm.byteswap()
    return pcm
