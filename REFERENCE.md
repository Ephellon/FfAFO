# Audio Signature Library — Developer Quick Reference

A file-by-file index of the public methods, important internals, configuration, and operational gotchas. Use this alongside `README.md`: the main README explains setup and behavior; this document is the code navigation guide.

## At a glance

| Component | Entry point | Responsibility |
|---|---|---|
| DSP / encoding | `audio_sig.py` | Deterministic 21-byte perceptual fingerprint, base-49 encoding, Hamming distance |
| Folder scanner | `scan_folder.py` | Discover files, decode through FFmpeg, fingerprint, call registry |
| Shared registry | `registry.py` | Persist fingerprints and roots in SQLite; resolve matches and aliases over HTTP |
| Browser module | `web/audio-signature.js` | Decode an uploaded `File`, fingerprint it, call registry |
| Tests | `tests/test_audio_sig.py` | Encoding and basic fingerprint behavior checks |

## 1. `audio_sig.py` — DSP and identifiers

### Public constants

| Name | Meaning |
|---|---|
| `ALPHABET` | Exact 49-character no-lookalike alphabet; keep identical in JavaScript |
| `BASE` | Encoding base, derived from `len(ALPHABET)` |
| `PERCEPTUAL_BYTES` | Fingerprint length: 21 bytes / 168 bits |
| `ROOT_BYTES` | Registry root length: 20 bytes |
| `ALGORITHM_VERSION` | `asfp-v1`; change when fingerprint semantics change |
| `TARGET_RATE` | Required PCM rate: 11,025 Hz |
| `FRAME_SIZE` / `HOP_SIZE` | FFT frame: 2,048 samples; advance: 512 samples |
| `SEGMENTS` / `BANDS` | Six time regions and 29 log-spaced frequency bands |

### Public functions

- **`encode_bytes(data: bytes) -> str`** — encode bytes as fixed-width base-49 text. Leading zero bytes are preserved by fixed width.
- **`decode_bytes(text: str, size: int) -> bytes`** — validate and decode base-49 text to exactly `size` bytes. Rejects invalid characters, lengths, and values that overflow the requested byte count.
- **`encode_signature(perceptual: bytes, root: bytes) -> str`** — format the two encoded fields as `30-character-perceptual/29-character-root`.
- **`fingerprint(samples: list[float], sample_rate=TARGET_RATE) -> bytes`** — compute the 21-byte perceptual fingerprint from mono float PCM already resampled to 11,025 Hz. Rejects too-short, silent, or too-quiet input.
- **`hamming_distance(a: bytes, b: bytes) -> int`** — count differing bits; inputs must be equal length. For fingerprints, the range is 0–168 and lower is more similar.

### Internal helpers

- **`_fft_real_power(frame)`** — radix-2 FFT and power spectrum. Kept explicit so the JS implementation can mirror its operations.
- **`_band_edges(sample_rate)`** — calculate log-spaced FFT-bin boundaries.

### Fingerprint recipe

Mono PCM → Hann window → 2,048-point FFT → power in 29 log bands → average log energy in six equal-duration regions → compare adjacent band energies → 168 bits. Bits are packed most-significant-bit first into 21 bytes.

## 2. `scan_folder.py` — native folder scan

- **`EXTENSIONS`** — set of filename extensions scanned recursively. Add formats here only if FFmpeg can decode them in the target environment.
- **`decode_audio(path) -> (samples, duration, rms)`** — invokes FFmpeg, strips video, converts to mono float PCM at `TARGET_RATE`, and returns samples plus metadata. FFmpeg must be on `PATH`.
- **`post_json(url, payload, token="")`** — sends a JSON POST; optional bearer token is added to the request header.
- **`main()`** — command-line entry point. Scans recursively, emits one JSON line per file, and continues after individual file errors.

Useful options:

```sh
python scan_folder.py "D:\\Music" --registry http://127.0.0.1:8765
python scan_folder.py "D:\\Music" --jsonl results.jsonl
python scan_folder.py "D:\\Music" --token "$AUDIO_SIG_API_TOKEN"
```

`--jsonl` writes the same per-file result records to a file. Failed files appear with `status: error`; they do not stop the rest of the scan.

## 3. `registry.py` — shared identity registry

### Configuration constants / environment variables

| Setting | Default | Effect |
|---|---|---|
| `AUDIO_SIG_DB` | `audio-signatures.sqlite3` | Default SQLite database path |
| `AUDIO_SIG_MAX_DISTANCE` | `24` | Maximum Hamming distance eligible for a match (out of 168 bits) |
| `AUDIO_SIG_AMBIGUITY_MARGIN` | `5` | If another root is within this distance of the best candidate, return `ambiguous` |
| `AUDIO_SIG_API_TOKEN` | empty | Optional bearer-token protection; required for non-loopback binds |
| `AUDIO_SIG_CORS_ORIGIN` | `http://localhost` | Exact allowed browser origin returned in CORS headers |

### Functions and class methods

- **`connect(db_path)`** — open SQLite, enable WAL and foreign keys, and create tables/indexes if absent.
- **`new_root()`** — generate a random 20-byte canonical root and its base-49 text. Roots are registry-issued IDs, not audio-derived hashes.
- **`resolve(db, perceptual_text, duration=None, rms=None, source="")`** — main match-and-register operation. Exact existing fingerprint → `existing`; confident nearby fingerprint → `matched`; unclear candidate set → `ambiguous`; no eligible candidate → `created`.
- **`Handler._headers(status)` / `_json(status, obj)`** — shared HTTP response formatting and JSON serialization.
- **`Handler._authorized()`** — validates bearer token when `AUDIO_SIG_API_TOKEN` is configured.
- **`Handler.do_GET()`** — retrieve one fingerprint by perceptual ID.
- **`Handler.do_POST()`** — resolve/register fingerprints or attach a reviewed alias.
- **`main()`** — parse host, port, and database options; refuse non-loopback binding if no API token is configured.

### API routes

| Method and route | Purpose |
|---|---|
| `POST /fingerprints/resolve` | Match or register a perceptual fingerprint |
| `GET /fingerprints/{perceptual}` | Retrieve an existing fingerprint record |
| `POST /fingerprints/aliases` | Explicitly attach an unregistered perceptual ID to an existing root |

Typical resolve request:

```json
{
  "perceptual": "<30 base-49 characters>",
  "algorithm": "asfp-v1",
  "duration": 12.3,
  "rms": 0.1,
  "source": "optional label"
}
```

Possible statuses: `created`, `matched`, `existing`, `ambiguous`. Review ambiguous candidates instead of auto-merging. Alias registration is explicit and does not permit reassigning an already-registered perceptual ID.

### Database tables

- **`roots`** — one row per canonical identity (`root`, creation time, optional note).
- **`fingerprints`** — one row per unique perceptual ID, linked to a root and algorithm version; stores timestamp and optional duration/RMS/source metadata.

`BEGIN IMMEDIATE` serializes resolution transactions so simultaneous registrations do not race through the match-and-create path. SQLite uniqueness constraints remain a final guard against duplicate keys.

## 4. `web/audio-signature.js` — browser API

### Exports

- **`ALPHABET`, `ALGORITHM_VERSION`, `TARGET_RATE`** — format/version constants; keep in sync with Python.
- **`encodeBytes(bytes)` / `decodeBytes(text, size)`** — base-49 encoding helpers. Use a browser/runtime with `BigInt` support.
- **`encodeSignature(perceptual, root)`** — create the readable combined form.
- **`fingerprint(samples, sampleRate=TARGET_RATE)`** — DSP implementation that mirrors `audio_sig.py`; input must be mono `Float`-like PCM already at 11,025 Hz.
- **`decodeFile(file, audioContext=null)`** — decode with Web Audio, average channels to mono, then linearly resample to 11,025 Hz. Returns `{ samples, duration, rms }`.
- **`resolveFile(file, registryUrl, { fetchImpl=fetch, headers={} }={})`** — decode and fingerprint the upload, then POST it to `/fingerprints/resolve`.

Example:

```js
import { resolveFile } from "./audio-signature.js";
const result = await resolveFile(file, "/api/audio-registry");
console.log(result.status, result.signature);
```

The `headers` option supports an app-managed auth flow; `fetchImpl` can be replaced for tests or a custom network layer. Never embed a privileged registry token in public browser code—use a same-origin authenticated backend/proxy.

## 5. `tests/test_audio_sig.py` — current coverage

Run with:

```sh
python -m unittest discover -s tests -v
```

Current tests cover alphabet size, base-49 round trips for several byte lengths, combined signature text length, deterministic output for a synthetic tone mix, 21-byte fingerprint size, and silence rejection. These are smoke/unit tests, **not** proof of perceptual matching quality or full Python/JavaScript equivalence.

## Format note: 42 bytes vs 60 characters

The raw binary layout is 21 perceptual bytes + one ASCII slash byte + 20 root bytes = **42 bytes**. The readable base-49 representation is **30 characters + slash + 29 characters = 60 ASCII characters**. Use the binary layout when a fixed 42-byte wire payload is required; use the readable form for logs and copy/paste.

## Maintainer cautions

1. **Keep the DSP mirrored.** Any change to FFT, resampling, band edges, region boundaries, bit order, or quantization may require a new `ALGORITHM_VERSION` and new test fixtures.
2. **Resampling differs by platform.** Native FFmpeg and browser Web Audio/linear resampling may yield different samples. Test actual cross-encoded fixtures before raising automatic merge confidence.
3. **Thresholds are heuristics.** The default distance and ambiguity margin are starting values, not measured guarantees. Tune against labeled same-recording and different-recording pairs.
4. **Roots require the shared registry.** They are random registry-assigned identifiers. A client cannot independently recreate a root from audio alone.
5. **Deployment hardening is required for shared use.** Put the service behind TLS and an authenticated proxy, set a strict CORS origin, restrict network access, back up the database, and add request/rate limits before public deployment.
6. **Fingerprint v1 is sensitive to edits.** Long silence padding, excerpts, or edits can change the fingerprint substantially because it divides the entire file into six equal-duration regions.
