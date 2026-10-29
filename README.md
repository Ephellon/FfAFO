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
5. With `--album-checks`, compares each album folder with **MusicBrainz**: the reference is the latest Official worldwide release in the album's release group (found from the files' MusicBrainz tags, else by search).
6. Emits a record only when something is wrong, unless asked otherwise.

Artist names match on whole words ("Ye" does not match inside "Kanye"); when that fails, MusicBrainz artist names and aliases are tried, so `Kanye West` matches a service credit of `Ye`. MusicBrainz responses are cached for 30 days in `FAFO_DIR\musicbrainz-cache.sqlite3` (1 request per second, as MusicBrainz requires). `--no-musicbrainz` turns all of this off.

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
| `xv3 PATH...` | **Experimental.** Spectrum-plot signatures, cached in the FAFO index; `--pair A B` compares two files (see below) | none |

`PATH` can be any mix of folders (scanned recursively for `.wav .mp3 .m4a .aac .flac .ogg .opus .wma .aiff .aif .mp4 .webm`) and single audio files. `python fafo.py <service> --help` lists options. `python compare.py ...` is the same as `python fafo.py compare ...`.

```sh
python fafo.py fingerprint "R:\Local" --quiet --progress
python fafo.py compare "R:\Local" --resume --album-checks --granular --progress --report "R:\FAFO\mismatches.jsonl" --run-status "R:\FAFO\last-run.json"
python fafo.py compare "R:\Local\Artist\Album\04 Song.mp3"
python fafo.py stats
```

Both `fingerprint` and `compare` can be interrupted and re-run: finished files are kept in the index. Check one song, a list of songs, or a whole folder by passing the paths.

#### `fingerprint` options and output

| Option | Effect |
|---|---|
| `--index PATH` | FAFO index (default `%FAFO_DIR%\library-index.sqlite3`; must be outside the scanned folders) |
| `--no-index` | Compute only; do not read or write the index |
| `--folder-chunk` | One index per album folder instead of one file (see [FAFO cache](#fafo-cache-fast-fourier-audio-fingerprinting-offline)) |
| `--flowerbed` | Add the tag-derived identity: `flowerbed` key, `root` and full `signature` (see [Flowerbed roots](#flowerbed-roots)) |
| `--no-numpy` | Methodical mode: pure-Python FFT (same output, about 30× slower) |
| `--subfingerprints` | Include the full `asfp-v2` sequence (list of 28-bit integers) in each record |
| `--granular` | Add each file's `size` (bytes) and `mtime` (epoch seconds, as `os.stat`) |
| `--quiet` | No per-file records on stdout; errors and the summary only |
| `--progress` | Print `[n/total] path` per file to stderr |

One JSON line per file on stdout (here with `--flowerbed`):

```json
{"file": "R:\\Local\\¥$\\VULTURES 2\\01 SLIDE.mp3", "status": "cached", "duration": 198.06,
 "asfp-v1": "GEwb94yMXKQqkPmAJzxMc6gpXinxcG", "frames": 4260,
 "flowerbed": "¥$/SLIDE/2024.08", "root": "6CjQWV6XVnKDjAjk9yMUiipqAemEk",
 "signature": "GEwb94yMXKQqkPmAJzxMc6gpXinxcG_6CjQWV6XVnKDjAjk9yMUiipqAemEk"}
```

`status` is `indexed` (computed and stored now), `cached` (already in the index), or `computed` (with `--no-index`). `asfp-v1` is the 30-character whole-track hash (the same value the registry uses); `frames` is the length of the `asfp-v2` sequence. With `--flowerbed`, `flowerbed` is `null` (and `root`/`signature` are left out) when the file lacks an artist, title or dated year tag. Failed files go to stderr as `{"file": ..., "status": "error", "error": "message"}`. The stderr summary is `{"summary": {"files": n, "indexed": n, "cached": n, "computed": n, "error": n}}`. Exit code: `0` all files fingerprinted, `1` at least one failed, `2` invalid arguments.

#### `compare` options

| Option | Effect |
|---|---|
| `path...` | Library folder(s) and/or audio file(s). A single file's parent folder is treated as read-only too |
| `--report PATH` | Also write the emitted records to this JSONL file (must be outside the folder). Written as `PATH.tmp` and renamed over `PATH` only when the run completes, so a reader never sees half a report and an interrupted run leaves the previous one |
| `--run-status PATH` | Write `{"started", "finished", "exit", "records"}` (ISO 8601 UTC times) to this JSON file: at the start with `finished`/`exit` `null`, again when the run completes. `records` is the number of emitted records |
| `--granular` | Add `size` (bytes) and `mtime` (epoch seconds, as `os.stat`) of each file as FAFO saw it, so a reader can drop records for files changed since |
| `--verbose` | Also emit tracks that matched cleanly (`status: "ok"`) |
| `--show-unresolved` | Also emit tracks no service could confidently match |
| `--country CC` | Apple storefront, default `US`. Editions differ by storefront (e.g. the US store may only carry a clean edition) |
| `--no-audio` | Metadata checks only; no preview streaming |
| `--index PATH` | FAFO fingerprint cache, default `%FAFO_DIR%\library-index.sqlite3` (see below) |
| `--no-index` | Do not use or create the cache (each run re-fingerprints) |
| `--folder-chunk` | One cache per album folder beside `--index`; the first use copies entries over from `--index` |
| `--index-only` | Only fingerprint library files into the cache; no service lookups (older form of `fafo.py fingerprint --quiet`) |
| `--resume` | Store each file's result in the cache; later `--resume` runs reuse it for unchanged files under the same options, with no network access. Omit it to force a fresh check |
| `--no-numpy` | "Methodical" mode: pure-Python FFT for new fingerprints (same output, about 30× slower) |
| `--progress` | Print `[n/total] path` per file to stderr |
| `--album-checks` | After the per-file pass, emit one record per album folder (TRACKLIST, RELEASE_WITHDRAWN) |
| `--no-musicbrainz` | No MusicBrainz requests: no artist aliases and no album checks |

#### `xv3` (experimental): spectrum-plot signatures

Not used by `compare`, and its thresholds are provisional until real-world testing is done (`PLAN.md` §9.3). Each file is decoded once, mono at 32 kHz. The whole file is averaged into a power spectrum, like an audio spectrum plot: 52 bands of 1/6 octave from 40 Hz to 15 kHz, in dB relative to the file's own mean, so loudness does not count. A spectrum is also kept for each 10-second segment, so a section added or missing in one file shows up even when the whole-file average barely moves. Segments are compared with 10-second windows of the other file that slide in 1.25-second steps, so a time offset between the two files does not count. Bands more than 40 dB below a segment's loudest band count as empty, so encoder noise in quiet intros does not count either.

**Quiet edges are trimmed.** A quiet intro or outro (count-ins, fades, room noise, or a preview's fade) is left out of the segments when it sits more than 20 dB under the file's loud level (the 90th percentile of its 1.25-second block levels). At most 10 seconds is trimmed from each end. Trimmed audio still counts as something the *other* file can match against. `trimmed` in each record gives the seconds left out at the start and end. On 60 library files this halved false section flags (12 → 6 of 180 same-song comparisons) with no loss in catching spliced-in sections. Against 35 real Apple previews, 34 were fully heard in their own track at the default 1.5 dB (35 at 2.0 dB), and 35 of 36 were rejected by another track from the same album (`PLAN.md` §9.3).

**Cached.** Spectra are stored in the FAFO index (`spectra` table), keyed by path, size, modification time and the full xv3 recipe, so a repeat lookup reads the index instead of decoding. Any change to the recipe recomputes everything.

| Option | Effect |
|---|---|
| `--pair A B` | Compare exactly two files instead of listing signatures |
| `--plot-db N` | `--pair`: largest whole-file plot distance (RMS dB) still called the same song |
| `--section-db N` | `--pair`: largest segment distance still called the same section |
| `--vocal-weight W` | Weight of bands centred in 60–1050 Hz (`0` ignores them; default `1`) |
| `--bands` | Include the unquantized plot (`bands`: 52 dB values) |
| `--plot-signed` | Add `plot_signed`: the plot code and the [flowerbed root](#flowerbed-roots) joined by `-` (forces flowerbed; also adds `flowerbed` and `root`) |
| `--index PATH` / `--no-index` / `--folder-chunk` / `--granular` | Same as for `fingerprint` |
| `--no-numpy` | Methodical mode: pure-Python FFT (same output, much slower) |
| `--progress` | Print `[n/total] path` per file to stderr |

Without `--pair`, there is one line per file. The `xv3` value is the plot stored as its difference from a typical music slope (−5 dB per octave), in 1 dB steps within ±16 dB. That is 5 bits per band: 33 bytes, or 48 characters in the signature alphabet, with about 0.3 dB of rounding error.

```json
{"file": "R:\\Local\\Doechii\\Alligator Bites Never Heal\\16 SLIDE.mp3", "status": "cached", "duration": 174.289,
 "xv3": "3ckNaxd47ERAR6LnHfAMEt6mygYEjKke4MaJJC7RppjEn4bi", "segments": 17, "trimmed": [0.0, 0.0]}
```

`status` is `indexed`, `cached` or `computed`, as for `fingerprint`. With `--plot-signed`, `plot_signed` is 78 characters, `<48-char plot>-<29-char root>`; `-` is not in the signature alphabet, so it always splits cleanly and needs no shell quoting.

With `--pair`, one line: `{"a": {...}, "b": {...}, "plot_db": 0.84, "missing": [[start, end], ...], "extra": [[start, end], ...], "match": true}`. `missing` lists sections of `A` (in seconds) heard nowhere in `B`; `extra` lists sections of `B` heard nowhere in `A`. A section starts where its segment does, so after a trimmed intro the spans start at the trim (for example `[5.0, 15.0]`). Exit code: `0` match, `1` no match or a failed file, `2` invalid arguments. Segments are matched without regard to order, so padding and edits do not count as differences; silent segments are skipped.

### FAFO cache (Fast-Fourier Audio Fingerprinting Offline)

`library-index.sqlite3` in the `FAFO_DIR` folder (set in `.env` or the environment; default `FAFO/` beside the code; this install uses `R:\FAFO`) holds:

- `fingerprints`: v1 hash and v2 sub-fingerprints for each library file, keyed by path, size, modification time and algorithm version. A changed file is recomputed automatically.
- `results`: stored comparison records for `--resume`, keyed by file state and options (`country`, `audio`, services). Errors and failed preview fetches are never stored, so they are retried.
- `spectra`: xv3 spectra (whole-file plot, every 10-second window in hundredths of a dB, about 20 KB per 4-minute track), keyed by file state and the xv3 recipe.

**`--folder-chunk`** (for `fingerprint`, `compare`, `xv3` and `stats`) uses one database per album folder instead, beside `--index`: `%FAFO_DIR%\<album folder name>.sqlite3`. The folder name is Unicode-normalized (NFC) and then encoded like JavaScript's `escape()` over UTF-16 code units, with `;` in place of `%`: `Post Malone` → `Post;20Malone`, `¥$` → `;A5;24`, `カニエ` → `;u30AB;u30CB;u30A8`, `a;b` → `a;3Bb`. Case is kept, and a trailing `.` becomes `;2E` because Windows drops it. Folders with the same name (the same album title by different artists) share a database. A file directly inside an artist folder uses the artist folder's name. The first time a chunk database is created, its folder's entries are copied over from `library-index.sqlite3`, so nothing is decoded again; the single index is opened read-only and left in place. Without `--folder-chunk`, everything stays in the single index. `stats --folder-chunk` totals every chunk database.

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
 "mismatches": [{"code": "DURATION", "library": 233.1, "service": 220.2, "delta": -12.9}],
 "checked": "2026-10-29T05:12:40+00:00"}
```

- `unresolved` records are just `{"file": ..., "status": "unresolved", "checked": ...}`; `error` records are `{"file": ..., "status": "error", "error": "message", "checked": ...}`.
- `checked` (every record, file and album, added 2026-10-28 for PyLy) is when the file was really compared, ISO 8601 UTC. A record reused by `--resume` keeps its original time, so a caller can skip stale records. `file` and `folder` are absolute paths spelled as on disk.
- With `--granular`, file records also carry `"size": 8123456, "mtime": 1793232903.123456`: the file as FAFO saw it. `mtime` is `os.stat(path).st_mtime` (epoch seconds; a file replaced by an older-dated copy still differs). Album records have neither (writing a `.lrc` changes a folder's mtime).
- A file whose size or mtime changes while it is being checked is checked once more; if it changes again it is skipped as an `error` record (`"file changed while it was checked"`), which is not stored, so the next run retries it. This holds with or without `--granular`.
- `audio` is present only when a preview was checked. `ber` is the bit error rate (0 = identical, about 0.5 = unrelated); `preview_at` is where the preview starts in the library file, in seconds. If the preview could not be checked, `audio` is `{"service": ..., "error": "message"}` and the metadata mismatches are still reported.

| `code` | Meaning | Extra fields |
|---|---|---|
| `DURATION` | Length differs by more than 2.0 s | `library`, `service` (seconds), `delta` = service − library |
| `ISRC` | Both have an ISRC and they differ | `library`, `service` |
| `EXPLICIT` | Explicit/clean flag differs (only when both are known) | `library`, `service` (booleans) |
| `TITLE_VERSION` | Title version qualifiers differ (remaster, live, remix, edit, clean, deluxe, …) | `library`, `service` (sorted lists) |
| `AUDIO` | The preview does not match the library audio (BER above 0.40) | `service`, `ber`, `threshold` |

**Album records** (`--album-checks`) have `album`, `artist`, `folder` instead of `file`, a `musicbrainz` reference with `status` and `date`, and the same `status` values. `TRACKLIST` `missing` entries are named like the library files: `NN Title`, or `NN Title (CD DD)` on a multi-disc release; `moved` positions stay `disc-track`. The summary gains `"albums": {"ok", "mismatch", "unresolved", "error"}`, and an album mismatch also makes the exit code `1`.

```json
{"album": "VULTURES 2", "artist": "¥$", "folder": "R:\\Local\\¥$\\VULTURES 2",
 "reference": {"service": "musicbrainz", "id": "efda4d75-…", "url": "https://musicbrainz.org/release/efda4d75-…", "status": "Official", "date": "2024-11-10"},
 "status": "mismatch",
 "mismatches": [{"code": "RELEASE_WITHDRAWN", "library": "3cc48778-…", "service": "efda4d75-…"},
                {"code": "TRACKLIST", "library_count": 17, "service_count": 16, "missing": ["12 FOREVER"],
                 "extra": ["09 MAYBE.mp3", "09 MAYBE.mp4"], "duplicates": ["MAYBE"],
                 "moved": [{"title": "530", "library": "1-12", "service": "1-7"}]}]}
```

| Album `code` | Meaning | Extra fields |
|---|---|---|
| `RELEASE_WITHDRAWN` | The files are tagged with a MusicBrainz release marked withdrawn, and an Official release replaces it | `library`, `service` (release ids) |
| `TRACKLIST` | Track count, titles or order differ from the reference release | `library_count`, `service_count`, `missing` (`disc-track title`), `extra` (file names), `duplicates` (titles), `moved` (`title`, `library`, `service` positions) |

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

### Flowerbed roots

By default a root is random, issued by the registry. A **flowerbed** root is derived from the file's own tags instead, so the same song gets the same root on every machine without asking a registry:

```
key  = <ARTIST>/<TITLE>/<YYYY.MM>
root = SHA-1("flowerbed-v1\n" + key)        20 bytes, the size of any root
```

- **Artist**: the track artist tag; multiple artists are trimmed and joined by `;`. **Title**: the title tag. **Date**: the date tag (`year` or `original date` if there is none) as `YYYY.MM`; a year alone means January (`2019` → `2019.01`). Values come only from the file's metadata, never from folder or file names or a service; a file missing any of the three has no flowerbed key.
- **Normalization**: Unicode NFC, UPPERCASE, whitespace runs reduced to one space, and no spaces touching punctuation (Unicode `P*` characters; symbols such as `$` keep their spaces): `Ty Dolla $ign , Ye` → `TY DOLLA $IGN,YE`, `Song ( Remix )` → `SONG(REMIX)`.
- The album is left out on purpose: an artist rarely releases two songs with the same title in the same month. Two versions of a song (a remix, a radio edit) only get different roots if their titles differ.
- The `flowerbed-v1` prefix versions the recipe. Changing anything about it means a new prefix, so old and new roots never collide by accident.

**In the registry** (`flowerbed` field on `/fingerprints/resolve`, or `scan_folder.py --flowerbed`), each root records its scheme (`random` or `flowerbed-v1`), and audio and tags are reconciled without ever merging two existing identities:

| Situation | Result |
|---|---|
| No audio match; tag root unknown | `created` with the tag root |
| No audio match; tag root already known | `rooted`: joins it (the same tags are the same song) |
| Audio matches a **random** root; tag root unknown | `matched`/`existing`: keeps the random root and records the tag root as its alias |
| Audio matches the root the tags give (or its alias) | `matched`/`existing` |
| Audio matches one root, tags point to another | `ambiguous` with `"reason": "tags"` and both candidates (`via: "audio"`, `via: "tags"`); nothing is written |

### Scan a folder

```sh
python scan_folder.py "D:\\Music" --registry http://127.0.0.1:8765 --jsonl scan-results.jsonl
```

Optional bearer token: set `AUDIO_SIG_API_TOKEN` in the environment or pass `--token`. The scanner recursively considers common audio extensions and emits one JSON record per file. Failed files are reported without stopping the scan. Unlike `compare.py`, the scanner registers fingerprints in the registry. `--flowerbed` sends each file's [flowerbed root](#flowerbed-roots) and adds its `flowerbed` key to the record.

### Browser usage

```js
import { resolveFile } from "./audio-signature.js";
const result = await resolveFile(file, "/api/audio-registry");
console.log(result.status, result.signature);
```

Use an authenticated same-origin proxy for `/api/audio-registry`; don't put a privileged registry token in client code. The module accepts `fetchImpl` and request `headers` options if your app needs a custom authenticated fetch wrapper.

### Registry API

- `POST /fingerprints/resolve` — body: `{ "perceptual": "<30 chars>", "algorithm": "asfp-v1", "duration": 12.3, "rms": 0.1, "source": "optional label", "flowerbed": "<29-char root, optional>" }`. Returns `created`, `matched`, `existing`, `rooted` (flowerbed only), or `ambiguous`.
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
- A renamed track on the service (for example MAYBE in the library vs FOREVER on Apple) comes back `unresolved` per file; with `--album-checks` it shows up in the album's `TRACKLIST` as missing + extra.
- Album checks compare with MusicBrainz's newest Official worldwide release; if MusicBrainz lacks the album or its newest edition, the album is `unresolved` or compared with an older edition.
- `--resume` reuses stored results without re-asking the service, so catalogue changes since the stored run are not seen. Run without `--resume` for a fresh check.
- Registry: by default the root is a random registry-issued identity, so all clients must consult the same registry; flowerbed roots (from tags) avoid that but are only as good as the tags. The browser module does not compute flowerbed roots yet. Threshold 24 is an initial heuristic. The standard-library HTTP server is suitable for local development only.
