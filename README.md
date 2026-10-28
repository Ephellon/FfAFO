# Audio Signature Library

Two tools that share one DSP core (`audio_sig.py`):

1. **Registry** (prototype v1): a Python folder scanner, a browser JavaScript module and a SQLite registry that give each recording a perceptual fingerprint and a shared identity.
2. **Library version check** (`compare.py`): compares a local music library with streaming-service catalogues and **reports mismatches only**. It flags local songs that are a different version (length, ISRC, explicit/clean, title qualifiers, or different audio) from the streaming reference. It is read-only towards the library and is meant to be driven by other programs (for example a PyLy controller) as well as by hand.

Native decoding requires FFmpeg on `PATH`. NumPy is optional: when it is installed, `compare.py` uses it for fingerprinting (about 30× faster). The pure-Python DSP stays the reference implementation, mirrored in `web/audio-signature.js` for v1.

## Requirements

- Python 3.10+
- FFmpeg (`ffmpeg` and `ffprobe`) on `PATH`
- Optional: NumPy (used by `compare.py` unless `--no-numpy` is given)
- For the browser module: a modern browser with `AudioContext.decodeAudioData` and ES modules

Setup: copy `.env.example` to `.env` and fill in what you use (`FAFO_DIR`, Spotify credentials). `.env`, databases, caches, audio files and reports are git-ignored; nothing else needs installing.

---

## Library version check (`compare.py`)

### What it does

For each audio file under a folder:

1. Reads tags and duration with `ffprobe` (title, artist, album artist, album, track/disc, duration, ISRC, explicit advisory).
2. Finds the same song on the streaming services, in precedence order: **Spotify** (Web API; used when `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` are set and the API accepts them), then **Apple Music** (iTunes Search/Lookup API, no account needed). YouTube Music and SoundCloud are planned (see `PLAN.md` §8). Spotify is checked once at start-up; if it refuses (bad credentials, or Spotify's rule that the app owner needs Premium), a `{"warning": "Spotify skipped: ..."}` line goes to stderr and Apple becomes the reference.
3. Picks a **reference** track only when the match is confident: same ISRC, or the same normalized title inside a matching edition of the same album, or (song-search fallback) the same title and a duration within 3 s.
4. Runs metadata checks against the reference, then an audio check: Apple's 30 s preview is streamed into memory (never saved), fingerprinted, and aligned against the library file's fingerprint. Spotify has no previews, so for a Spotify reference the audio comes from Apple's confident match (the one with the same ISRC when there is one).
5. Emits a record only when something is wrong, unless asked otherwise.

### Guarantees

- **No writes to the library.** Library files are only `stat()`ed and read by `ffprobe`/`ffmpeg`. `--report` and `--index` are refused if they point inside the scanned folder.
- **No downloads.** Previews are streamed over HTTPS straight into memory, capped at 31 s. Only `https` URLs on `*.apple.com` / `*.mzstatic.com` are fetched, with FFmpeg's protocol whitelist set to `https,tls,tcp`.
- **No registry writes.** `compare.py` never calls the registry.
- **GET-only network access**, throttled to one Apple request every 3.1 s and one Spotify request every 0.25 s. The only POST is Spotify's client-credentials token request; the secret is sent nowhere else and never logged.

### Command line: `fafo.py`

`fafo.py` is the single entry point, with one subcommand per service:

| Service | Does | Network |
|---|---|---|
| `fingerprint PATH...` | Fingerprints audio files/folders into the FAFO index and prints each file's fingerprint | none |
| `compare PATH...` | Compares files/folders with the streaming services and reports mismatches | Apple Music |
| `stats` | Prints what the FAFO index holds (counts, size, last indexed, stored results by status) | none |

`PATH` can be any mix of folders (scanned recursively for `.wav .mp3 .m4a .aac .flac .ogg .opus .wma .aiff .aif .mp4 .webm`) and single audio files. `python fafo.py <service> --help` lists options. `python compare.py ...` is the same as `python fafo.py compare ...`.

```sh
python fafo.py fingerprint "R:\Local" --quiet --progress
python fafo.py compare "R:\Local" --resume --progress --report "R:\FAFO\mismatches.jsonl"
python fafo.py compare "R:\Local\Artist\Album\04 Song.mp3"
python fafo.py stats
```

Both `fingerprint` and `compare` can be interrupted and re-run: finished files are kept in the index. Check one song, a list of songs, or a whole folder by passing the paths.

#### `fingerprint` options and output

| Option | Effect |
|---|---|
| `--index PATH` | FAFO index (default `%FAFO_DIR%\library-index.sqlite3`; must be outside the scanned folders) |
| `--no-index` | Compute only; do not read or write the index |
| `--no-numpy` | Methodical mode: pure-Python FFT (same output, about 30× slower) |
| `--subfingerprints` | Include the full `asfp-v2` sequence (list of 28-bit integers) in each record |
| `--quiet` | No per-file records on stdout; errors and the summary only |
| `--progress` | Print `[n/total] path` per file to stderr |

One JSON line per file on stdout:

```json
{"file": "R:\\Local\\¥$\\VULTURES 2\\01 SLIDE.mp3", "status": "cached", "duration": 198.06,
 "asfp-v1": "GEwb94yMXKQqkPmAJzxMc6gpXinxcG", "frames": 4260}
```

`status` is `indexed` (computed and stored now), `cached` (already in the index), or `computed` (with `--no-index`). `asfp-v1` is the 30-character whole-track hash (the same value the registry uses); `frames` is the length of the `asfp-v2` sequence. Failed files go to stderr as `{"file": ..., "status": "error", "error": "message"}`. The stderr summary is `{"summary": {"files": n, "indexed": n, "cached": n, "computed": n, "error": n}}`. Exit code: `0` all files fingerprinted, `1` at least one failed, `2` invalid arguments.

#### `compare` options

| Option | Effect |
|---|---|
| `path...` | Library folder(s) and/or audio file(s). A single file's parent folder is treated as read-only too |
| `--report PATH` | Also write the emitted records to this JSONL file (overwritten; must be outside the folder) |
| `--verbose` | Also emit tracks that matched cleanly (`status: "ok"`) |
| `--show-unresolved` | Also emit tracks no service could confidently match |
| `--country CC` | Apple storefront, default `US`. Editions differ by storefront (e.g. the US store may only carry a clean edition) |
| `--no-audio` | Metadata checks only; no preview streaming |
| `--index PATH` | FAFO fingerprint cache, default `%FAFO_DIR%\library-index.sqlite3` (see below) |
| `--no-index` | Do not use or create the cache (each run re-fingerprints) |
| `--index-only` | Only fingerprint library files into the cache; no service lookups (older form of `fafo.py fingerprint --quiet`) |
| `--resume` | Store each file's result in the cache; later `--resume` runs reuse it for unchanged files under the same options, with no network access. Omit it to force a fresh check |
| `--no-numpy` | "Methodical" mode: pure-Python FFT for new fingerprints (same output, about 30× slower) |
| `--progress` | Print `[n/total] path` per file to stderr |

### FAFO cache (Fast-Fourier Audio Fingerprinting Offline)

`library-index.sqlite3` in the `FAFO_DIR` folder (set in `.env` or the environment; default `FAFO/` beside the code; this install uses `R:\FAFO`) holds:

- `fingerprints`: v1 hash and v2 sub-fingerprints for each library file, keyed by path, size, modification time and algorithm version. A changed file is recomputed automatically.
- `results`: stored comparison records for `--resume`, keyed by file state and options (`country`, `audio`, services). Errors and failed preview fetches are never stored, so they are retried.

Every file commits on its own, so an interrupted run loses at most one file. The cache can be deleted at any time and rebuilt. Fingerprints are reused whichever engine (NumPy or pure Python) made them; the two are parity-tested and measured bit-identical. Run one writer at a time against a cache file.

Cost measured on one album (17 files): fingerprinting about 1 s per track with NumPy; the comparison is limited by Apple's request rate (about 26 s for the album); a `--resume` rerun takes under a second.

### `compare` output contract (for calling programs)

- **stdout**: one JSON object per line (UTF-8) for each emitted track: mismatches always, `ok` with `--verbose`, `unresolved` with `--show-unresolved`.
- **stderr**: per-file `error` records (same shape), `{"warning": ...}` lines (e.g. Spotify skipped), optional `--progress` lines, and a final summary line:
  `{"summary": {"files": 17, "ok": 7, "mismatch": 8, "unresolved": 2, "error": 0}}` (plus `"reused": n` with `--resume`).
  With `--index-only` the summary is `{"summary": {"files": n, "indexed": n, "cached": n, "error": n}}`.
- **Exit code**: `0` no mismatches; `1` at least one mismatch (with `--index-only`: at least one file failed); `2` invalid arguments. Per-file errors do not change the exit code of a comparison run, so read the summary's `error` count.

Record shape (`status` is `mismatch`, `ok`, `unresolved` or `error`):

```json
{"file": "R:\\Local\\Artist\\Album\\04 Song.mp3",
 "reference": {"service": "apple", "id": "1761882999", "url": "https://music.apple.com/us/album/..."},
 "audio": {"service": "apple", "ber": 0.354, "preview_at": 45.0},
 "status": "mismatch",
 "mismatches": [{"code": "DURATION", "library": 233.1, "service": 220.2, "delta": -12.9}]}
```

- `unresolved` records are just `{"file": ..., "status": "unresolved"}`; `error` records are `{"file": ..., "status": "error", "error": "message"}`.
- `audio` is present only when a preview was checked. `ber` is the bit error rate (0 = identical, about 0.5 = unrelated); `preview_at` is where the preview starts in the library file, in seconds. If the preview could not be checked, `audio` is `{"service": ..., "error": "message"}` and the metadata mismatches are still reported.

| `code` | Meaning | Extra fields |
|---|---|---|
| `DURATION` | Length differs by more than 2.0 s | `library`, `service` (seconds), `delta` = service − library |
| `ISRC` | Both have an ISRC and they differ | `library`, `service` |
| `EXPLICIT` | Explicit/clean flag differs (only when both are known) | `library`, `service` (booleans) |
| `TITLE_VERSION` | Title version qualifiers differ (remaster, live, remix, edit, clean, deluxe, …) | `library`, `service` (sorted lists) |
| `AUDIO` | The preview does not match the library audio (BER above 0.40) | `service`, `ber`, `threshold` |

How to read them: `AUDIO` means a different recording or the wrong song. `DURATION` without `AUDIO` means the same recording edited differently (an extended, cut or alternate version), because the preview only covers about 30 s. Unknown values (no ISRC tag, no explicit flag) are never reported as mismatches.

Use as a library from Python: `compare.probe(path)` returns a `LibraryTrack`, and `compare.compare(track, compare.default_adapters("US"), index=LibraryIndex())` returns the same record dict; `fafo.fingerprint_record(path, LibraryIndex(), True, False)` returns a fingerprint record. See `REFERENCE.md`.

`stats` prints one JSON object, for example `{"index": "R:\\FAFO\\library-index.sqlite3", "fingerprints": 304, "frames": 1438552, "last_indexed": "2026-10-28T08:49:13+00:00", "results": {"mismatch": 8, "ok": 7, "unresolved": 2}, "bytes": 6090752}`, and exits `1` if the index does not exist.

---

## Registry (prototype v1)

### Signature format

- Perceptual fingerprint: 21 raw bytes (168 bits), encoded as 30 characters in the supplied 49-character alphabet.
- Canonical root: 20 raw bytes, encoded as 29 characters.
- Readable signature: `30-char-perceptual_29-char-root` (60 ASCII characters total). The separator is an underscore, which is not in the alphabet and is safe in URLs and filenames. (Before 2026-10-28 it was `/`; the registry stores the two parts separately, so only the displayed form changed.)
- Binary wire representation, if needed, is 21 bytes + one separator byte + 20 bytes = 42 bytes. The human-readable alphabet encoding is 60 characters, not 42 bytes.

Alphabet: `346789ABCDEFGHJKLMNPQRTUVWXYabcdefghijkmnpqrtwxyz`

### Algorithm v1 (`asfp-v1`, whole-track hash)

1. Decode to mono float PCM at 11,025 Hz.
2. Use 2,048-sample Hann-windowed frames with a 512-sample hop.
3. Sum FFT power into 29 logarithmic bands spanning approximately 80–5,000 Hz, and average log energy in six equal-duration regions.
4. Compare adjacent band energies in each region: 6 × 28 = 168 bits.

This is a perceptual-hash prototype, not a production-validated music-identification algorithm. It is designed for similarity matching, not exact identity. Very short or silent audio is rejected. Different decoder/resampler implementations can produce slightly different features; use shared test fixtures and tune the threshold on your corpus before relying on automatic merges. v1 cannot match excerpts (a 30 s clip of a track scores like a different song), which is why `compare.py` uses v2.

### Algorithm v2 (`asfp-v2`, sub-fingerprints for excerpts)

Same front end as v1, but instead of averaging into six regions it emits one 28-bit sub-fingerprint per frame (Haitsma–Kalker style): bit *m* is the sign of the frame-to-frame change in the energy difference between bands *m* and *m*+1. An excerpt is matched by sliding its sequence along the full track and taking the lowest bit error rate (BER); the offset says where the excerpt sits. Measured on real data: same recording 0.00–0.36, different songs 0.47 or higher; threshold 0.40. v2 is Python-only (not mirrored in the browser module) and is not used by the registry.

### Start the registry

```sh
python registry.py --host 127.0.0.1 --port 8765 --db audio-signatures.sqlite3
```

For a non-local bind, set `AUDIO_SIG_API_TOKEN` before starting the service. The server checks `Authorization: Bearer <token>`. **Do not embed a server token in browser JavaScript.** For web use, put the registry behind a same-origin authenticated backend/proxy. Configure `AUDIO_SIG_CORS_ORIGIN` to the exact web origin; its default is `http://localhost`.

The default matching threshold is 24 differing bits out of 168 (14.3%); `AUDIO_SIG_MAX_DISTANCE` changes it. If two distinct roots are within the threshold and within five bits of the best match, the API returns `ambiguous` rather than silently merging. Tune using known same-recording and different-recording pairs.

### Scan a folder

```sh
python scan_folder.py "D:\\Music" --registry http://127.0.0.1:8765 --jsonl scan-results.jsonl
```

Optional bearer token: set `AUDIO_SIG_API_TOKEN` in the environment or pass `--token`. The scanner recursively considers common audio extensions and emits one JSON record per file. Failed files are reported without stopping the scan. Unlike `compare.py`, the scanner registers fingerprints in the registry.

### Browser usage

```js
import { resolveFile } from "./audio-signature.js";
const result = await resolveFile(file, "/api/audio-registry");
console.log(result.status, result.signature);
```

Use an authenticated same-origin proxy for `/api/audio-registry`; don't put a privileged registry token in client code. The module accepts `fetchImpl` and request `headers` options if your app needs a custom authenticated fetch wrapper.

### Registry API

- `POST /fingerprints/resolve` — body: `{ "perceptual": "<30 chars>", "algorithm": "asfp-v1", "duration": 12.3, "rms": 0.1, "source": "optional label" }`. Returns `created`, `matched`, `existing`, or `ambiguous`.
- `GET /fingerprints/{perceptual}` — retrieve a registered fingerprint.
- `POST /fingerprints/aliases` — explicitly attach a new perceptual fingerprint to an existing root: `{ "perceptual": "<30 chars>", "root": "<29 chars>", "source": "reviewed alias" }`.

The registry is intended to be trusted behind a proxy or restricted to localhost. `source` is metadata only and should not be treated as a filesystem path that the service may open.

---

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests use fake services and temporary folders; they make no network requests and do not touch your library or the FAFO folder.

## Known limits

- Services: Spotify and Apple Music. YouTube Music and SoundCloud (31 s audio windows) are planned (`PLAN.md` §8).
- Spotify only works when the developer app's owner account has Premium (Spotify policy); otherwise it is skipped automatically. Spotify's `explicit: false` is treated as unknown, because it does not distinguish a clean edition from a song without explicit lyrics.
- Apple previews cover about 30 s (usually starting around 45 s in), so edits outside that window show up only as `DURATION`.
- Clean and explicit versions sound almost identical to the audio check; rely on `EXPLICIT` and `ISRC` for those.
- A renamed track on the service (for example MAYBE in the library vs FOREVER on Apple) comes back `unresolved`, not as a mismatch.
- `--resume` reuses stored results without re-asking the service, so catalogue changes since the stored run are not seen. Run without `--resume` for a fresh check.
- Registry: the root is a random registry-issued identity, not a content-derived hash; all clients must consult the same registry. Threshold 24 is an initial heuristic. The standard-library HTTP server is suitable for local development only.
