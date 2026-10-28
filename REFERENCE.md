# Audio Signature Library — Developer Quick Reference

A file-by-file index of the public methods, important internals, configuration, and operational gotchas. Use this alongside `README.md`: the main README explains setup and behavior; this document is the code navigation guide.

## At a glance

| Component | Entry point | Responsibility |
|---|---|---|
| DSP / encoding | `audio_sig.py` | v1 21-byte perceptual hash, v2 sub-fingerprints + excerpt alignment, base-49 encoding, Hamming distance |
| Folder scanner | `scan_folder.py` | Discover files, decode through FFmpeg, fingerprint, call registry |
| Shared registry | `registry.py` | Persist fingerprints and roots in SQLite; resolve matches and aliases over HTTP |
| Browser module | `web/audio-signature.js` | Decode an uploaded `File`, fingerprint it, call registry |
| FAFO CLI | `fafo.py` | Entry point: `fingerprint`, `compare` and `stats` services |
| Version check | `compare.py` | Read-only library vs streaming-service comparison; reports mismatches as JSONL |
| Apple adapter | `apple.py` | iTunes Search/Lookup client: album editions, song fallback, throttling |
| Spotify adapter | `spotify.py` | Web API client (client credentials): ISRC/album/song search, metadata reference |
| Settings | `envfile.py` | Dependency-free `.env` loader (`FAFO_DIR`, Spotify credentials, MusicBrainz contact) |
| MusicBrainz | `musicbrainz.py` | Cached, throttled client: artist aliases, reference releases for album checks |
| Artist matching | `artists.py` | Whole-word artist matching with an optional alias source |
| FAFO cache | `library_index.py` | SQLite cache of library fingerprints and `--resume` results (`FAFO_DIR`) |
| Tests | `tests/` | `test_audio_sig.py`, `test_compare.py`, `test_apple.py`, `test_library_index.py` |

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
| `ALGORITHM_V2` | `asfp-v2`; per-frame sub-fingerprints |
| `SUB_BITS` | 28 bits per v2 sub-fingerprint (one per adjacent band pair) |
| `COARSE_STEP` / `REFINE_CANDIDATES` | `best_alignment` search: coarse pass on every 8th frame, then rescore the best 5 offsets (±2) on all frames |
| `EXCERPT_PHASES` | `(0, 128, 256, 384)` sample shifts tried by `align_excerpt` to land on the 512-sample hop grid |

### Public functions

- **`encode_bytes(data: bytes) -> str`** — encode bytes as fixed-width base-49 text. Leading zero bytes are preserved by fixed width.
- **`decode_bytes(text: str, size: int) -> bytes`** — validate and decode base-49 text to exactly `size` bytes. Rejects invalid characters, lengths, and values that overflow the requested byte count.
- **`encode_signature(perceptual: bytes, root: bytes) -> str`** — format the two encoded fields as `30-character-perceptual_29-character-root` (separator `SIGNATURE_SEPARATOR` = `_`, also exported by the JS module).
- **`fingerprint(samples: list[float], sample_rate=TARGET_RATE) -> bytes`** — compute the 21-byte perceptual fingerprint from mono float PCM already resampled to 11,025 Hz. Rejects too-short, silent, or too-quiet input.
- **`hamming_distance(a: bytes, b: bytes) -> int`** — count differing bits; inputs must be equal length. For fingerprints, the range is 0–168 and lower is more similar.
- **`subfingerprints(samples, sample_rate=TARGET_RATE, accel=False) -> list[int]`** — asfp-v2: one 28-bit integer per frame after the first; MSB = lowest band pair.
- **`analyze(samples, sample_rate=TARGET_RATE, accel=False) -> (bytes, list[int])`** — one FFT pass returning both the v1 hash and the v2 sequence (used by the FAFO cache).
- **`best_alignment(query, reference) -> (ber, offset)`** — slide `query` along `reference`; lowest bit error rate and the frame offset into `reference`. If `query` is longer the roles swap and the offset is negative. Raises `ValueError` on empty input.
- **`align_excerpt(samples, reference, sample_rate=TARGET_RATE, phases=EXCERPT_PHASES, accel=False) -> (ber, offset)`** — analyse excerpt PCM at each sub-hop phase and return the best `best_alignment`. Offset × `HOP_SIZE` / `TARGET_RATE` = seconds into the reference.

`accel=True` uses NumPy when it is installed (otherwise falls back silently). Output is parity-tested against pure Python (BER < 0.01; measured bit-identical); pure Python remains the reference.

### Internal helpers

- **`_fft_real_power(frame)`** — radix-2 FFT and power spectrum. Kept explicit so the JS implementation can mirror its operations.
- **`_band_edges(sample_rate)`** — calculate log-spaced FFT-bin boundaries.
- **`_band_energies(samples, sample_rate, accel)`** — validate PCM and return per-frame `log1p` band energies; shared by v1 and v2. `_band_energies_numpy` is the vectorised twin (batches of 1,024 frames).
- **`_v1_from_energies` / `_subfingerprints_from_energies`** — the two hash stages over shared energies. `fingerprint()` output is pinned by a regression test.

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

## 5. `fafo.py` — service command line

- **`SERVICES`** — `("fingerprint", "compare", "stats")`; `main(argv)` dispatches on the first argument (exit `2` for an unknown or missing service).
- **`fingerprint_record(path, index, accel, with_subfps) -> dict`** — `{file, status: indexed|cached|computed, duration, asfp-v1, frames[, asfp-v2]}`.
- **`fingerprint_main(argv, prog)`** / **`stats_main(argv, prog)`** — the `fingerprint` and `stats` services; `compare` is `compare.main(argv, prog="fafo.py compare")`.

Options and output formats are in `README.md` ("Command line: `fafo.py`").

## 6. `compare.py` — library version check

CLI options, output records, mismatch codes and exit codes are documented in `README.md` ("Output contract"). Calling programs should parse stdout JSONL and the stderr summary line.

### Constants

| Name | Value | Meaning |
|---|---|---|
| `DURATION_TOLERANCE` | 2.0 s | `DURATION` mismatch threshold |
| `CONFIDENT_DURATION` | 3.0 s | Song-search fallback: max duration gap for a confident match |
| `AUDIO_MAX_BER` | 0.40 | `AUDIO` mismatch above this (calibrated: same ≤ 0.36, different ≥ 0.47) |
| `PREVIEW_SECONDS` | 31 | Max seconds read from any remote stream |
| `PREVIEW_HOSTS` | `.apple.com`, `.mzstatic.com` | Only hosts a preview URL may point to |
| `VERSION_WORDS` | remaster, live, remix, … | Title qualifiers compared by `TITLE_VERSION` |

### Types

- **`LibraryTrack`** — `path, title, artist, album_artist, album, track, disc, duration, isrc, explicit` (frozen dataclass).
- **`ServiceTrack`** — `service, id, url, title, artists, album, duration, isrc, explicit, preview_url, track, disc, album_match`. `album_match=True` means it was found inside an edition of the library track's album.
- **`Adapter`** (Protocol) — `name: str`, `find(track) -> list[ServiceTrack]` (best first).

### Functions

- **`default_adapters(country="US")`** — services in precedence order (currently `[AppleAdapter]`).
- **`probe(path) -> LibraryTrack`** / **`parse_probe(path, data)`** — ffprobe JSON to `LibraryTrack`; tag keys case-folded; container tags win over stream tags.
- **`normalize_title(title) -> (base, frozenset qualifiers)`** — drops feat./with credits, collects version words from brackets and ` - ` suffixes; symbol-only titles keep their raw casefold.
- **`is_confident(track, candidate)`** / **`pick_reference(track, adapters)`** — confidence rule and first-confident-service precedence.
- **`check_track(track, ref, duration_tolerance)`** — metadata mismatches (`DURATION`, `ISRC`, `EXPLICIT`, `TITLE_VERSION`); unknown values never count.
- **`decode_url(url, seconds=PREVIEW_SECONDS)`** — host-checked, protocol-whitelisted FFmpeg stream into memory.
- **`library_subfingerprints(path, index=None, accel=True)`** — v2 sequence of a library file, from the FAFO index when given.
- **`audio_check(track, ref, index=None, accel=True) -> (info, mismatch|None)`** — preview vs library alignment.
- **`compare(track, adapters, audio=True, index=None, accel=True) -> dict`** — full record for one track. Audio failures go to `record["audio"]["error"]` and never hide metadata mismatches.
- **`collect_files(paths) -> (files, roots)`** — sorted, de-duplicated audio files under any mix of folders and files, plus the folders that must stay read-only (a file protects its parent). Raises `ValueError` for missing paths or non-audio files.
- **`build_index(files, index, progress)`** — `--index-only` loop.
- **`main(argv=None, adapters=None, prog=None) -> int`** — CLI (also `fafo.py compare`); `adapters` can be injected (tests use fakes).

## 7. `apple.py` — Apple Music adapter

- **`AppleAdapter(country="US", fetch=http_get_json, sleep, clock)`** — `fetch`/`sleep`/`clock` are injectable for tests.
  - **`album_tracks(track)`** — searches album editions by artist and normalized album name, prefers editions whose qualifiers match (deluxe etc.), then explicit-first (clean-first if the library file is clean); looks up at most `MAX_COLLECTIONS` (3) editions; cached per album.
  - **`find(track)`** — same-title songs from the album editions (same disc/track position first), else a song search filtered by artist.
- **`http_get_json(url)`** — GET with one retry after 60 s on HTTP 403/429.
- `MIN_INTERVAL = 3.1` s between requests (Apple allows about 20 per minute). `trackExplicitness`: `explicit` → True, `cleaned` → False, `notExplicit` → unknown.

### `spotify.py` — Spotify adapter

- **`SpotifyClient(client_id, client_secret, sleep, clock).get(url)`** — client-credentials token (POST to `TOKEN_URL`, refreshed before expiry and once on 401), bearer GET, `Retry-After` back-off on 429 (max 120 s). HTTP 403 and token failures raise **`SpotifyUnavailable`**.
- **`SpotifyAdapter(fetch, market="US", sleep, clock)`** — `fetch` is injectable for tests. **`from_env(market)`** returns `None` without credentials. **`check()`** makes one cheap call and raises if the API is unusable.
  - **`find(track)`** — ISRC search first when the file has an ISRC; else same-title songs from album editions (`album_match=True`, same position first); else a track search filtered by artist.
  - **`album_tracks(track)`** — album search with artist, falling back to album-only (rescues symbol-only artists such as `¥$`), editions sorted by qualifier match then full albums before singles; at most `MAX_ALBUMS` (3); ISRCs filled from `GET /tracks` in batches of 50; cached per album.
  - Mapping: `explicit: true` → True, `false` → unknown; no `preview_url`.
- `compare.default_adapters()` loads `.env`, runs `check()`, and on failure prints `{"warning": "Spotify skipped: ..."}` and continues with Apple only. `compare.audio_reference()` picks the preview for a reference without one.

### `musicbrainz.py` and `artists.py` — aliases and album checks

- **`MusicBrainz(cache_path=None, fetch, sleep, clock, now)`** — GETs `https://musicbrainz.org/ws/2/...` at most once per 1.1 s with User-Agent `AudioSignatureFFT/1.0 ( MUSICBRAINZ_CONTACT )`; retries 503 with back-off; caches responses in memory and, with `cache_path`, in SQLite table `responses(url PK, body JSON, fetched_at unix time)` for 30 days.
  - **`artist_names(name) -> frozenset[str]`** — casefolded names and aliases of artists whose name or alias is exactly `name` (search hits like tribute bands are ignored).
  - **`release_group_id(track)`** — from `mb_release_group_id`, else via `mb_album_id`, else a release search (title, edition qualifiers, alias-aware artist credit).
  - **`reference_release(track)`** — latest Official worldwide (XW/XE) release in the group, with recordings.
- **`artists.artist_match(library, service)`** — whole-word containment either way, then alias equality against each credited name (credits split on `,` `&` `feat.` `x` …). **`set_alias_source(fn)`** installs the lookup; `compare.default_adapters(country, musicbrainz)` does so. Alias failures count as no match.
- **`compare.tracklist_check(tracks, release)`** / **`album_check(tracks, musicbrainz)`** / **`_album_pass`** — album records (`TRACKLIST`, `RELEASE_WITHDRAWN`); groups are folder + album artist + album. `LibraryTrack` carries `mb_album_id`, `mb_release_group_id`, `mb_album_status` from Picard-style tags.

## 8. `library_index.py` — FAFO cache

- **`INDEX_PATH`** — `%FAFO_DIR%\library-index.sqlite3`; `FAFO_DIR` comes from the environment or `.env` (via `envfile.load_env`), default `FAFO/` beside the code.
- **`ALGORITHM`** — `asfp-v1+asfp-v2`; bumping either version invalidates every cached fingerprint.
- **`LibraryIndex(db_path=INDEX_PATH, accel=True)`** — lazily opened (no file or folder until first use; the folder is then created). Context manager.
  - **`entry(path) -> ({duration, v1, subfps}, was_cached)`** — cached entry, or decode + `analyze` + store (one transaction per file).
  - **`subfingerprints(path)`** — `entry(path)[0]["subfps"]`.
  - **`cached(path)`** — whether an up-to-date entry exists.
  - **`stats()`** — `{index, fingerprints, frames, last_indexed, results: {status: count}, bytes}`.
  - **`result(path, options)`** / **`save_result(path, options, record)`** — `--resume` records keyed by file state and `options` (JSON, sorted keys).

Tables:

| Table | Columns |
|---|---|
| `fingerprints` | `path` (PK), `size`, `mtime_ns`, `algorithm`, `duration`, `v1` (21-byte BLOB), `subfps` (uint32 little-endian array BLOB), `indexed_at` (ISO-8601 UTC) |
| `results` | `path` (PK), `size`, `mtime_ns`, `options` (JSON), `record` (JSON), `checked_at` (ISO-8601 UTC) |

## 9. `tests/` — coverage

Run with:

```sh
python -m unittest discover -s tests -v
```

- `test_audio_sig.py` — encoding round trips, v1 regression pin, v2 shape/alignment/half-hop recovery, NumPy parity (skipped without NumPy).
- `test_compare.py` — ffprobe parsing, title normalization, mismatch rules, precedence, audio check (mocked), preview URL guard, CLI (temp library left untouched, `--report`/`--index` refused inside the folder, `--resume`, `--index-only`).
- `test_apple.py` — adapter against recorded-style fake API responses (editions, artist filter, throttling).
- `test_library_index.py` — cache hits, invalidation on change, results keyed by options, lazy creation.
- `test_fafo.py` — `fingerprint` (indexed then cached, single file, `--subfingerprints`, `--quiet`, `--no-index`, errors, refusals), `compare` delegation, `stats`, help and unknown services.

No test uses the network or the real library. These are unit tests, **not** proof of perceptual matching quality or full Python/JavaScript equivalence; calibration data is in `PLAN.md` §2.2.

## Format note: 42 bytes vs 60 characters

The raw binary layout is 21 perceptual bytes + one ASCII `_` separator byte + 20 root bytes = **42 bytes**. The readable base-49 representation is **30 characters + `_` + 29 characters = 60 ASCII characters**. Use the binary layout when a fixed 42-byte wire payload is required; use the readable form for logs and copy/paste.

## Maintainer cautions

1. **Keep the DSP mirrored.** Any change to FFT, resampling, band edges, region boundaries, bit order, or quantization may require a new `ALGORITHM_VERSION` and new test fixtures.
2. **Resampling differs by platform.** Native FFmpeg and browser Web Audio/linear resampling may yield different samples. Test actual cross-encoded fixtures before raising automatic merge confidence.
3. **Thresholds are heuristics.** The default distance and ambiguity margin are starting values, not measured guarantees. Tune against labeled same-recording and different-recording pairs.
4. **Roots require the shared registry.** They are random registry-assigned identifiers. A client cannot independently recreate a root from audio alone.
5. **Deployment hardening is required for shared use.** Put the service behind TLS and an authenticated proxy, set a strict CORS origin, restrict network access, back up the database, and add request/rate limits before public deployment.
6. **Fingerprint v1 is sensitive to edits.** Long silence padding, excerpts, or edits can change the fingerprint substantially because it divides the entire file into six equal-duration regions. Use v2 (`align_excerpt`) for excerpts.
7. **The output contract is an API.** Other programs (a PyLy controller) parse `compare.py`'s JSONL records, mismatch codes, summary line and exit codes. Change them only additively, and update `README.md` when you do.
8. **`compare.py` must stay read-only towards the library.** Keep every write (report, cache) outside the scanned folder, and keep the preview host and protocol guards in `decode_url`.
