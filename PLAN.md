# Plan: Library vs. Streaming-Service Version Check

**Goal:** For each track in `R:\Local`, find the matching track on Spotify, Apple Music, YouTube Music and SoundCloud (checked in that order), and **report only mismatches**. The tool only reads: it downloads nothing, writes nothing to the library or to the services, and adds no new identities to the registry.

**Status:** Build steps 1–5 done (2026-10-28); steps 6–8 pending.

---

## 0. Ground rules (enforced in code, not just by convention)

| Rule | Mechanism |
|---|---|
| No writes to `R:\Local` | Library files are opened read-only through `ffmpeg`/`ffprobe` only. No tag writing, renaming or moving. |
| No registry writes | `compare.py` never calls `POST /fingerprints/resolve`. It opens SQLite with `file:...?mode=ro`, or uses a new read-only `GET /fingerprints/nearest`. |
| No downloads | Previews are streamed over HTTPS straight into `ffmpeg -i <url> ... pipe:1`, and the PCM stays in memory. No temporary audio files. Full-track sources are capped at a 31 s window (`-ss <offset> -t 31`). |
| Network is GET-only | Service adapters use GET only. The one exception is Spotify's client-credentials token `POST`, which is authentication, not a content write. |
| Output | Mismatch records go to stdout. `--report <path>` is optional and the user chooses the path, which must be outside `R:\Local`. |

The one deliberate local write is the **fingerprint cache** (§2.3). It lives in the project folder, never in the library. It is a decision point (see §7).

---

## 1. Why v1 isn't enough (measured 2026-10-27)

| Test | Distance (bits of 168) | Verdict |
|---|---|---|
| YouTube SLIDE (noisy, full length) vs. library SLIDE | 13 | ✅ match |
| Closest pair of different tracks on VULTURES 2 | 50 | separation is fine |
| 30 s clip of SLIDE at 0 / 30 / 60 s vs. full SLIDE | 59 / 47 / 45 | ❌ fails; looks like a different song |

v1 averages six equal-length regions of the **whole file**, so it can never match an excerpt. The v1 fingerprint stays useful for comparing full tracks. Previews need a new algorithm version.

---

## 2. Fingerprint `asfp-v2`: windowed sub-fingerprints

### 2.1 Algorithm
Keep v1's front end (11,025 Hz mono, 2,048-sample Hann window, 512-sample hop, 29 log bands from 80 to 5,000 Hz). Instead of averaging into 6 regions, emit **one 28-bit sub-fingerprint per frame** in the Haitsma–Kalker style:

```
bit[n, m] = 1  if (E[n,m] − E[n,m+1]) − (E[n−1,m] − E[n−1,m+1]) > 0
```

The time-differential term makes the bits robust to EQ, loudness and codec differences. One frame is about 46 ms, so a 30 s preview gives about 640 sub-fingerprints and a 4-minute track about 5,200.

### 2.2 Matching an excerpt
- Slide the preview's block of sub-fingerprints along the library track's sequence and compute the **bit error rate (BER)** at each offset.
- Score = lowest BER. Keep the offset too, since it tells you where the preview sits in the track.
- Starting threshold: BER ≤ 0.35 means same recording (the Haitsma–Kalker literature value). Tune it per §6.
- **Measured 2026-10-27 (SLIDE, 30 s clips at 5 offsets):**

  | Clip source | BER vs. SLIDE | BER vs. other tracks |
  |---|---|---|
  | Library file | 0.000–0.055 | 0.484–0.488 |
  | Noisy YouTube copy | 0.189–0.363 | 0.484–0.488 |

  Every clip was located at the correct offset. Unaligned clips were fixed by `align_excerpt`: it re-analyses the clip at 4 sub-hop shifts, which brought one library clip from 0.162 to 0.037. The noisy intro clip came in at 0.363, so **0.40 is the safer starting threshold**. Re-tune it on clean Apple previews in step 4.
- **Step 4 calibration (2026-10-27):** every Apple VULTURES 2 preview (US store, *cleaned* edition) was aligned against every library track, 256 pairs in all.

  | Pairs | Count | BER |
  |---|---|---|
  | Same song | 16 | 0.038–0.359 (FRIED 0.354, MAYBE↔FOREVER 0.359) |
  | Different songs | 240 | ≥ 0.470 (mean 0.485) |
  | Exception: Apple HUSBAND preview vs. library LIFESTYLE | 1 | 0.361 |

  The exception is real overlap: the preview lands at 235 s in the library's 331 s LIFESTYLE, past the end of Apple's 203 s version. **The 0.40 threshold holds.** Apple previews score higher than library clips, probably because this storefront only has the censored edition. Most Apple previews start about 45 s into the track.
- Cost: about 4,500 offsets × 640 frames, using XOR and `int.bit_count()`. Comparing one preview against **one** candidate track (picked by metadata) is fine in pure Python. We never search the whole library for a match.

### 2.3 Library index (cache)
- One FFT pass per library file produces **both** the v1 hash and the v2 sub-fingerprint sequence, keyed by `(path, size, mtime)` so changed files are recomputed.
- Stored in `library-index.sqlite3` in the project folder, about 20 KB per 4-minute track.
- Measured pure-Python cost: about 21 s to decode and analyse a 198 s track (4,260 frames), and about 12 s for a 30 s clip with 4-phase alignment. That is roughly 25 s per track, so a 10,000-track index takes about 70 hours. The optional NumPy path is effectively required for the first full index. It can be resumed and processed folder by folder (see §7 on NumPy).
- **Built in step 5 (2026-10-28):** `library_index.py`, tables `fingerprints` (v1 + v2 per file, keyed by path/size/mtime_ns/algorithm) and `results` (stored records for `--resume`, keyed by file state + options; errors are never stored, so they retry). Each file commits on its own, so an interrupted run loses at most one file.
- **NumPy measured (SLIDE, 198 s):** analyse 20.5 s → 0.7 s; 4-phase preview alignment 12.9 s → 0.3 s. Output was bit-identical to pure Python (v1 equal, 0 of 119,280 v2 bits differ). Because of that, accel is also used for preview analysis, not only indexing; the parity test guards both.
- **VULTURES 2 (17 files):** `--index-only` 16 s (about 1 s per track, mostly ffmpeg decode); full compare 26 s (Apple throttle dominates); `--resume` rerun 0.5 s with identical output. Index ≈ 16 KB per track. Projection for 10,000 tracks: about 3 h to index; the compare is network-bound.

### 2.4 Code changes
- `audio_sig.py`: factor the frame/band loop into `_band_energies(samples)`. Add `subfingerprints(samples) -> list[int]` and `best_alignment(query, reference) -> (ber, offset)`. Leave v1 `fingerprint()` byte-identical so existing signatures stay valid.
- Mirroring v2 in `web/audio-signature.js` is out of scope (not needed for this tool).

---

## 3. Library metadata

`ffprobe -show_format -show_streams -of json` per file gives: artist, album artist, album, title, track number, duration, `ISRC` (if tagged), and the explicit flag (`ITUNESADVISORY` / `rtng` for M4A, `EXPLICIT`/custom for FLAC and MP3, often missing). It needs no new dependency, since FFmpeg is already required.

Build the search key from album artist + album, then title. Normalize titles by lowercasing and stripping `(feat. …)`, `[Explicit]`, `- Remastered` and similar. Keep the stripped suffixes, because they are version evidence.

---

## 4. Service adapters (in precedence order)

Each adapter implements:
```python
find(track: LibraryTrack) -> list[ServiceTrack]     # candidates, best first
ServiceTrack: service, id, url, title, artists, album, duration_s,
              isrc | None, explicit | None, preview_url | None
```

| # | Service | Lookup | Audio for comparison | Auth / notes |
|---|---|---|---|---|
| 1 | **Spotify** | Web API search (`isrc:` first, then `artist:` `album:` `track:`) | ❌ `preview_url` was removed for new apps (Nov 2024), so metadata only | Client-credentials (you supply client ID and secret in env vars) |
| 2 | **Apple Music** | iTunes Search/Lookup API (`entity=song`, album lookup by `collectionId`) | ✅ 30 s AAC `previewUrl`, streamed | No auth. Rate limit about 20 req/min, so throttle. |
| 3 | **YouTube Music** | `ytmusicapi` (unofficial) search and album browse; duration comes from the search result | ✅ 31 s window: `yt-dlp -g -f bestaudio` (URL only) → `ffmpeg -ss <offset> -t 31 -i <url>` (HTTP range reads, stops at 31 s) | Unofficial and may break. `--no-stream` turns it off. |
| 4 | **SoundCloud** | API v2 search (unofficial `client_id`) | ✅ 31 s window from the HLS stream (`-t 31` pulls only the first few segments) | Unofficial and fragile. Lowest priority. |

**Window placement:** the default offset is 40% of the reference duration (skips intros). `--windows 2` takes two clips at 25% and 75% for better coverage. Each window is aligned with v2 exactly like an Apple preview.

**Precedence rule:** the reference version is the first service, in order, that returns a confident candidate: ISRC equal; or the same normalized title **inside a matched edition of the same album** (any duration, because that difference is what we want to report); or, for song-search fallback, the same normalized title + artist + duration within 3 s. Lower-priority services are checked only when `--all-services` is set, or when a higher one returns nothing.

Apple supplies the audio check, and it runs even when Spotify is the reference. The Spotify ↔ Apple pairing is confirmed by ISRC when both have one.

---

## 5. Mismatch rules

Run the checks cheapest first. **Emit a record only if at least one fails.**

| Code | Check | Default trigger |
|---|---|---|
| `DURATION` | \|library − reference\| | > 2.0 s |
| `ISRC` | both present and different | any difference |
| `EXPLICIT` | library flag vs. reference flag, both known | any difference |
| `AUDIO` | v2 BER of the Apple preview or the 31 s YouTube/SoundCloud window vs. library track | best BER > threshold |
| `TRACKLIST` | album-level: track count/order vs. reference album | any difference (`--album-checks`) |

**Not mismatches** (hidden by default, shown with `--verbose`): not found on any service, matched cleanly, or ambiguous candidates. `--show-unresolved` lists the tracks that could not be checked so gaps stay visible.

### Output record
```json
{"file": "R:\\Local\\¥$\\VULTURES 2\\01 SLIDE.mp3",
 "reference": {"service": "spotify", "id": "...", "url": "..."},
 "mismatches": [{"code": "DURATION", "library": 219.4, "service": 223.1, "delta": 3.7},
                {"code": "AUDIO", "service": "apple", "ber": 0.44, "threshold": 0.35}]}
```

### Known blind spot
Clean and explicit versions differ only in muted words, so the BER difference will be small. Rely on the `EXPLICIT`/`ISRC` checks for those. The audio check catches different mixes, edits, re-recordings and wrong tracks.

---

## 6. Calibration and tests

1. **Labelled pairs.** Same recording: the library track vs. its own Apple preview, plus the noisy YouTube SLIDE. Different recording: other tracks on the same album. Known version pairs: a clean/explicit pair and a remaster you own. VULTURES 2 is a good real-world case, because streaming versions were revised after release.
2. **Excerpt robustness.** In-memory 30 s clips of library tracks at random offsets should give BER < threshold and the correct offset ± 1 frame.
3. **Unit tests** (`tests/test_audio_sig.py` and the new `tests/test_compare.py`):
   - v1 `fingerprint()` output is unchanged (regression fixture).
   - v2 is deterministic, and a synthetic excerpt aligns at its known offset.
   - Adapters are tested against recorded JSON fixtures, with no live network in tests.
   - Mismatch rules: table-driven pass/fail cases.
   - Read-only guard: running compare leaves the registry DB file's mtime and hash unchanged.
4. Set the BER threshold and duration tolerance from the measured pair distributions, then record them in README.

---

## 7. Decisions (2026-10-27)

1. **Cache:** ✅ `R:\FAFO\library-index.sqlite3` (FAFO = Fast-Fourier Audio Fingerprinting Offline; changed from the project folder 2026-10-28; `FAFO_DIR` env var or `--index` override). It never touches `R:\Local` and can be rebuilt.
2. **NumPy:** ✅ Default for indexing and preview analysis when installed; `--no-numpy` is "methodical" pure-Python mode. The pure-Python version stays the reference. A parity test requires BER < 0.01 between the two paths on fixtures. Cached fingerprints are reused whichever engine made them (measured bit-identical).
3. **YouTube/SoundCloud audio:** ✅ On, but capped at a 31 s window per clip (§4); `--no-stream` disables it. The duration check runs first, from metadata alone.
4. **Spotify:** ✅ Credentials come from `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`. Spotify is the reference when present; Apple is the fallback reference.
5. **Duration tolerance:** 2 s for now. Recalibrate on VULTURES 2 vs. Apple during build step 2.

---

## 8. Build order

1. Read-only `compare.py` skeleton with the ffprobe metadata reader and the mismatch report format (no audio yet). Check that it makes no writes.
2. Apple adapter, then duration/explicit checks on VULTURES 2.
3. Refactor `asfp-v2` + `best_alignment`, plus the v1 regression test.
4. Audio check against Apple previews, then calibrate on labelled pairs.
5. Library index cache, then a resumable full-library run. ✅ 2026-10-28: `--index-only`, `--resume`, `--index`, `--no-index`, `--progress`, `--no-numpy`; cache moved to `R:\FAFO`. Added `fafo.py` CLI (`fingerprint`, `compare`, `stats` services; files or folders) for callers such as the PyLy controller.
6. Spotify adapter and the precedence logic. ✅ 2026-10-28 (fixture-tested). Live API currently answers 403 "Active premium subscription required for the owner of the app", so the start-up check skips Spotify with a warning until the app owner has Premium.
7. YouTube Music and SoundCloud adapters with 31 s windowed streaming.
8. Album-level `TRACKLIST` check. ✅ 2026-10-28 via MusicBrainz (`--album-checks`), plus `RELEASE_WITHDRAWN` and MusicBrainz artist aliases (Kanye West / Ye) for all services. VULTURES 2: files tagged with the withdrawn 2024-08-03 release; reference is the Official 2024-11-10 release (FOREVER missing, MAYBE extra and duplicated, 5 tracks moved).
