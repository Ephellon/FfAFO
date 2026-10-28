# Audio Signature Library (prototype v1)

Python folder scanner + browser JavaScript module + SQLite registry. The DSP is intentionally mirrored in both languages and uses no NumPy dependency. Native decoding requires FFmpeg; browsers use Web Audio's supported decoders.

## Signature format

- Perceptual fingerprint: 21 raw bytes (168 bits), encoded as 30 characters in the supplied 49-character alphabet.
- Canonical root: 20 raw bytes, encoded as 29 characters.
- Readable signature: `30-char-perceptual/29-char-root` (60 ASCII characters total).
- Binary wire representation, if needed, is 21 bytes + one slash byte + 20 bytes = 42 bytes. The human-readable alphabet encoding is 60 characters, not 42 bytes.

Alphabet: `346789ABCDEFGHJKLMNPQRTUVWXYabcdefghijkmnpqrtwxyz`

## Algorithm v1

1. Decode to mono float PCM at 11,025 Hz.
2. Use 2,048-sample Hann-windowed frames with a 512-sample hop.
3. Sum FFT power into 29 logarithmic bands spanning approximately 80–5,000 Hz, and average log energy in six equal-duration regions.
4. Compare adjacent band energies in each region: 6 × 28 = 168 bits.

This is a perceptual-hash prototype, not a production-validated music-identification algorithm. It is designed for similarity matching, not exact identity. Very short or silent audio is rejected. Different decoder/resampler implementations can produce slightly different features; use shared test fixtures and tune the threshold on your corpus before relying on automatic merges.

## Requirements

- Python 3.10+
- FFmpeg on `PATH` for native decoding
- A modern browser with `AudioContext.decodeAudioData` and ES modules

## Start the registry

```sh
python registry.py --host 127.0.0.1 --port 8765 --db audio-signatures.sqlite3
```

For a non-local bind, set `AUDIO_SIG_API_TOKEN` before starting the service. The server checks `Authorization: Bearer <token>`. **Do not embed a server token in browser JavaScript.** For web use, put the registry behind a same-origin authenticated backend/proxy. Configure `AUDIO_SIG_CORS_ORIGIN` to the exact web origin; its default is `http://localhost`.

The default matching threshold is 24 differing bits out of 168 (14.3%); `AUDIO_SIG_MAX_DISTANCE` changes it. If two distinct roots are within the threshold and within five bits of the best match, the API returns `ambiguous` rather than silently merging. Tune using known same-recording and different-recording pairs.

## Scan a folder

```sh
python scan_folder.py "D:\\Music" --registry http://127.0.0.1:8765 --jsonl scan-results.jsonl
```

Optional bearer token: set `AUDIO_SIG_API_TOKEN` in the environment or pass `--token`. The scanner recursively considers common audio extensions and emits one JSON record per file. Failed files are reported without stopping the scan.

## Browser usage

```js
import { resolveFile } from "./audio-signature.js";
const result = await resolveFile(file, "/api/audio-registry");
console.log(result.status, result.signature);
```

Use an authenticated same-origin proxy for `/api/audio-registry`; don't put a privileged registry token in client code. The module accepts `fetchImpl` and request `headers` options if your app needs a custom authenticated fetch wrapper.

## Registry API

- `POST /fingerprints/resolve` — body: `{ "perceptual": "<30 chars>", "algorithm": "asfp-v1", "duration": 12.3, "rms": 0.1, "source": "optional label" }`. Returns `created`, `matched`, `existing`, or `ambiguous`.
- `GET /fingerprints/{perceptual}` — retrieve a registered fingerprint.
- `POST /fingerprints/aliases` — explicitly attach a new perceptual fingerprint to an existing root: `{ "perceptual": "<30 chars>", "root": "<29 chars>", "source": "reviewed alias" }`.

The registry is intended to be trusted behind a proxy or restricted to localhost. `source` is metadata only and should not be treated as a filesystem path that the service may open.

## Tests

```sh
python -m unittest discover -s tests -v
```

## Known prototype limits

- The root is a random registry-issued identity, not a content-derived hash; all clients must consult the same registry.
- The current fingerprint uses equal-duration regions, so edits, silence padding, and excerpts may change it substantially.
- Threshold 24 is an initial heuristic, not a measured guarantee. Collect labelled test pairs before production use.
- The standard-library HTTP server is suitable for local development. Use TLS, authentication, request limits, backups, and a production WSGI/ASGI server or reverse proxy for a shared deployment.
