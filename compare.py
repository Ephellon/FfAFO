"""Read-only comparison of local audio files against streaming-service catalogues.

Reports mismatches only. Never writes to the scanned folder or the registry and never saves audio.
"""
from __future__ import annotations
import argparse, array, json, re, subprocess, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol
from urllib.parse import urlparse
from audio_sig import HOP_SIZE, TARGET_RATE, align_excerpt, subfingerprints
from envfile import load_env
from library_index import INDEX_PATH, LibraryIndex
from scan_folder import EXTENSIONS, decode_audio

DURATION_TOLERANCE = 2.0     # seconds; recalibrate against real service data (PLAN.md §7.5)
CONFIDENT_DURATION = 3.0     # seconds; metadata-only candidate acceptance window
AUDIO_MAX_BER = 0.40         # same recording measured ≤0.363 (noisy copy), different songs ≥0.484 (PLAN.md §2.2)
PREVIEW_SECONDS = 31         # never read more than this from a remote stream
PREVIEW_HOSTS = (".apple.com", ".mzstatic.com")   # preview URLs come from API data; only these hosts are fetched

VERSION_WORDS = ("remaster", "live", "remix", "mix", "edit", "version", "clean", "explicit",
                 "acoustic", "demo", "mono", "stereo", "instrumental", "extended", "radio", "deluxe", "sped up", "slowed")
_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.I)
_BRACKET = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
_DASH_SUFFIX = re.compile(r"\s+-\s+(.*)$")


@dataclass(frozen=True)
class LibraryTrack:
    path: str
    title: str
    artist: str
    album_artist: str
    album: str
    track: int | None
    disc: int | None
    duration: float
    isrc: str | None = None
    explicit: bool | None = None


@dataclass(frozen=True)
class ServiceTrack:
    service: str
    id: str
    url: str
    title: str
    artists: tuple[str, ...]
    album: str
    duration: float | None
    isrc: str | None = None
    explicit: bool | None = None
    preview_url: str | None = None
    track: int | None = None
    disc: int | None = None
    album_match: bool = False    # found inside a catalogue edition of the library track's album


class Adapter(Protocol):
    name: str
    def find(self, track: LibraryTrack) -> list[ServiceTrack]: ...


def default_adapters(country: str = "US") -> list[Adapter]:
    """Services in precedence order (PLAN.md §4): Spotify when configured and usable, then Apple Music.

    Spotify is checked once up front; if it is unusable a JSON warning goes to stderr and the run continues
    without it, so stored --resume results only ever name services that really took part.
    """
    from apple import AppleAdapter   # imported here: the adapters import this module's types
    from spotify import SpotifyAdapter
    load_env()
    adapters: list[Adapter] = []
    spotify = SpotifyAdapter.from_env(market=country)
    if spotify is not None:
        try:
            spotify.check()
            adapters.append(spotify)
        except Exception as exc:
            print(json.dumps({"warning": f"Spotify skipped: {exc}"}, ensure_ascii=False), file=sys.stderr)
    return adapters + [AppleAdapter(country=country)]


class _Memo:
    """Per-track memo so precedence and audio-source selection never query a service twice."""

    def __init__(self, adapter: Adapter):
        self.adapter, self.name, self._found = adapter, adapter.name, None

    def find(self, track: LibraryTrack) -> list[ServiceTrack]:
        if self._found is None:
            self._found = self.adapter.find(track)
        return self._found


def _first(tags: dict, *keys: str) -> str | None:
    for key in keys:
        value = tags.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _number(text: str | None) -> int | None:
    m = re.match(r"\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


def _explicit(tags: dict) -> bool | None:
    """iTunes advisory/rtng: 1 or 4 = explicit, 2 = clean, 0 = none (unknown). Plain EXPLICIT tag: truthy text."""
    advisory = _first(tags, "itunesadvisory", "rtng")
    if advisory is not None:
        return {"1": True, "4": True, "2": False}.get(advisory)
    flag = _first(tags, "explicit")
    if flag is not None:
        return flag.lower() in ("1", "true", "yes", "explicit")
    return None


def parse_probe(path: str, data: dict) -> LibraryTrack:
    """Build a LibraryTrack from `ffprobe -show_format -show_streams -of json` output."""
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("No audio stream")
    fmt = data.get("format", {})
    # Container tags win over stream tags; keys are case-folded because MP3, FLAC and MP4 disagree on case.
    tags = {k.lower(): v for k, v in audio.get("tags", {}).items()}
    tags.update({k.lower(): v for k, v in fmt.get("tags", {}).items()})
    duration = float(audio.get("duration") or fmt.get("duration") or 0)
    if duration <= 0:
        raise ValueError("Unknown duration")
    isrc = _first(tags, "isrc", "tsrc")
    return LibraryTrack(
        path=path,
        title=_first(tags, "title") or Path(path).stem,
        artist=_first(tags, "artist") or "",
        album_artist=_first(tags, "album_artist", "albumartist", "album artist") or _first(tags, "artist") or "",
        album=_first(tags, "album") or "",
        track=_number(_first(tags, "track", "tracknumber")),
        disc=_number(_first(tags, "disc", "discnumber")),
        duration=round(duration, 3),
        isrc=isrc.upper().replace("-", "") if isrc else None,
        explicit=_explicit(tags),
    )


def probe(path: Path) -> LibraryTrack:
    """Read tags and duration with ffprobe; the file is only opened for reading."""
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "ffprobe failed")
    return parse_probe(str(path), json.loads(proc.stdout.decode("utf-8", "replace")))


def normalize_title(title: str) -> tuple[str, frozenset[str]]:
    """Return (comparable base title, version qualifiers). Featured-artist credits are dropped, not qualifiers."""
    text = _FEAT.sub("", title)
    qualifiers = set()
    def keep(fragment: str) -> None:
        low = fragment.casefold().replace("remastered", "remaster")
        qualifiers.update(w for w in VERSION_WORDS if re.search(rf"\b{w}\b", low))
    for m in _BRACKET.finditer(text):
        keep(m.group(1))
    text = _BRACKET.sub("", text)
    m = _DASH_SUFFIX.search(text)
    if m and any(re.search(rf"\b{w}\b", m.group(1).casefold().replace("remastered", "remaster")) for w in VERSION_WORDS):
        keep(m.group(1))
        text = text[:m.start()]
    base = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.casefold())).strip()
    # Symbol-only titles/names (e.g. "¥$") would otherwise all collapse to "".
    return base or re.sub(r"\s+", " ", text.casefold()).strip(), frozenset(qualifiers)


def is_confident(track: LibraryTrack, candidate: ServiceTrack) -> bool:
    if track.isrc and candidate.isrc:
        return track.isrc == candidate.isrc
    if normalize_title(track.title)[0] != normalize_title(candidate.title)[0]:
        return False
    if candidate.album_match:
        # Same title on the same album is the same song even when the length differs; that difference is the finding.
        return True
    return candidate.duration is not None and abs(track.duration - candidate.duration) <= CONFIDENT_DURATION


def pick_reference(track: LibraryTrack, adapters: Iterable[Adapter]) -> ServiceTrack | None:
    """First confident candidate from the highest-precedence service that has one."""
    for adapter in adapters:
        for candidate in adapter.find(track):
            if is_confident(track, candidate):
                return candidate
    return None


def check_track(track: LibraryTrack, ref: ServiceTrack, duration_tolerance: float = DURATION_TOLERANCE) -> list[dict]:
    """Metadata checks, cheapest first. Only failures are returned; unknown values never count as mismatches."""
    out = []
    if ref.duration is not None and abs(track.duration - ref.duration) > duration_tolerance:
        out.append({"code": "DURATION", "library": track.duration, "service": ref.duration,
                    "delta": round(ref.duration - track.duration, 3)})
    if track.isrc and ref.isrc and track.isrc != ref.isrc:
        out.append({"code": "ISRC", "library": track.isrc, "service": ref.isrc})
    if track.explicit is not None and ref.explicit is not None and track.explicit != ref.explicit:
        out.append({"code": "EXPLICIT", "library": track.explicit, "service": ref.explicit})
    lib_q, ref_q = normalize_title(track.title)[1], normalize_title(ref.title)[1]
    if lib_q != ref_q:
        out.append({"code": "TITLE_VERSION", "library": sorted(lib_q), "service": sorted(ref_q)})
    return out


def decode_url(url: str, seconds: int = PREVIEW_SECONDS) -> list[float]:
    """Stream at most `seconds` of a remote preview through FFmpeg into memory; nothing touches disk."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(PREVIEW_HOSTS):
        raise ValueError(f"Refusing preview URL outside allowed hosts: {url}")
    # The whitelist stops a crafted URL from making FFmpeg open local files or other protocols.
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "https,tls,tcp", "-t", str(seconds), "-i", url,
           "-vn", "-ac", "1", "-ar", str(TARGET_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "FFmpeg preview decode failed")
    pcm = array.array("f"); pcm.frombytes(proc.stdout[:len(proc.stdout) // 4 * 4])
    if sys.byteorder != "little": pcm.byteswap()
    return list(pcm)


def library_subfingerprints(path: str, index: LibraryIndex | None = None, accel: bool = True) -> list[int]:
    """v2 sub-fingerprints of a library file (decoded read-only), from the FAFO index when given."""
    if index is not None:
        return index.subfingerprints(path)
    return subfingerprints(decode_audio(Path(path))[0], accel=accel)


def audio_check(track: LibraryTrack, ref: ServiceTrack, index: LibraryIndex | None = None,
                accel: bool = True) -> tuple[dict, dict | None]:
    """Align the service preview against the library file. Returns (audio info, AUDIO mismatch or None)."""
    ber, offset = align_excerpt(decode_url(ref.preview_url), library_subfingerprints(track.path, index, accel), accel=accel)
    info = {"service": ref.service, "ber": round(ber, 3), "preview_at": round(offset * HOP_SIZE / TARGET_RATE, 2)}
    if ber > AUDIO_MAX_BER:
        return info, {"code": "AUDIO", "service": ref.service, "ber": info["ber"], "threshold": AUDIO_MAX_BER}
    return info, None


def audio_reference(track: LibraryTrack, ref: ServiceTrack, adapters: Iterable[Adapter]) -> ServiceTrack | None:
    """Where the audio check gets its clip: the reference itself, else (e.g. Spotify, which has no previews) the
    first other service with a confident candidate that has one, preferring the reference's ISRC."""
    if ref.preview_url:
        return ref
    for adapter in adapters:
        if adapter.name == ref.service:
            continue
        found = [c for c in adapter.find(track) if c.preview_url and is_confident(track, c)]
        if found:
            return next((c for c in found if ref.isrc and c.isrc == ref.isrc), found[0])
    return None


def compare(track: LibraryTrack, adapters: Iterable[Adapter], audio: bool = True, index: LibraryIndex | None = None,
            accel: bool = True) -> dict:
    adapters = [_Memo(a) for a in adapters]
    ref = pick_reference(track, adapters)
    if ref is None:
        return {"file": track.path, "status": "unresolved"}
    mismatches = check_track(track, ref)
    record = {"file": track.path, "reference": {"service": ref.service, "id": ref.id, "url": ref.url}}
    if audio:
        try:
            source = audio_reference(track, ref, adapters)
            if source is not None:
                record["audio"], mismatch = audio_check(track, source, index, accel)
                if mismatch: mismatches.append(mismatch)
        except Exception as exc:   # an audio failure must not hide metadata mismatches
            record["audio"] = {"service": ref.service if ref.preview_url else "unknown", "error": str(exc)}
    return {**record, "status": "mismatch" if mismatches else "ok", "mismatches": mismatches}


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def collect_files(paths: Iterable[Path]) -> tuple[list[Path], list[Path]]:
    """Audio files under the given files/folders, plus the folders that must stay read-only.

    A single file protects its parent folder. Raises ValueError for a missing path or a non-audio file.
    """
    files, roots = set(), []
    for path in paths:
        if path.is_dir():
            roots.append(path)
            files.update(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
        elif path.is_file():
            if path.suffix.lower() not in EXTENSIONS:
                raise ValueError(f"Not a supported audio file: {path}")
            roots.append(path.parent)
            files.add(path)
        else:
            raise ValueError(f"Not a file or folder: {path}")
    return sorted(files), roots


def main(argv: list[str] | None = None, adapters: list[Adapter] | None = None, prog: str | None = None) -> int:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__)
    ap.add_argument("paths", nargs="+", type=Path, metavar="path", help="Library folder(s) or audio file(s)")
    ap.add_argument("--report", type=Path, help="Also write output records to this JSONL file (must be outside the folder)")
    ap.add_argument("--verbose", action="store_true", help="Also emit tracks that matched cleanly")
    ap.add_argument("--show-unresolved", action="store_true", help="Also emit tracks no service could match")
    ap.add_argument("--country", default="US", help="Storefront country code for catalogue lookups")
    ap.add_argument("--no-audio", action="store_true", help="Metadata checks only; skip preview fingerprinting")
    ap.add_argument("--index", type=Path, default=INDEX_PATH,
                    help=f"FAFO fingerprint cache, SQLite (default {INDEX_PATH}; must be outside the folder)")
    ap.add_argument("--no-numpy", action="store_true",
                    help="Methodical mode: pure-Python FFT for new fingerprints (about 30x slower, same output)")
    ap.add_argument("--no-index", action="store_true", help="Do not cache library fingerprints")
    ap.add_argument("--index-only", action="store_true", help="Only fingerprint library files into the index (no network)")
    ap.add_argument("--resume", action="store_true",
                    help="Keep each file's result in the index; later --resume runs reuse it for unchanged files")
    ap.add_argument("--progress", action="store_true", help="Print [n/total] per file to stderr")
    args = ap.parse_args(argv)
    # JSONL is UTF-8 regardless of console code page (piped output on Windows otherwise mangles e.g. "¥").
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")
    try:
        files, roots = collect_files(args.paths)
    except ValueError as exc:
        ap.error(str(exc))
    if args.report and any(_inside(args.report, r) for r in roots):
        ap.error("--report must be outside the scanned folder (the library is read-only)")
    if args.no_index and (args.resume or args.index_only):
        ap.error("--resume and --index-only need the index")
    if not args.no_index and any(_inside(args.index, r) for r in roots):
        ap.error("--index must be outside the scanned folder (the library is read-only)")
    index = None if args.no_index else LibraryIndex(args.index, accel=not args.no_numpy)
    try:
        if args.index_only:
            return build_index(files, index, args.progress)
        adapters = default_adapters(args.country) if adapters is None else adapters
        return _run(args, files, adapters, index)
    finally:
        if index: index.close()


def build_index(files: list[Path], index: LibraryIndex, progress: bool = False) -> int:
    """Fingerprint every file not already indexed. Each file commits on its own, so an interrupted run resumes."""
    counts = {"indexed": 0, "cached": 0, "error": 0}
    for n, path in enumerate(files, 1):
        if progress: print(f"[{n}/{len(files)}] {path}", file=sys.stderr, flush=True)
        try:
            if index.cached(str(path)):
                counts["cached"] += 1
                continue
            index.subfingerprints(str(path))
            counts["indexed"] += 1
        except Exception as exc:
            counts["error"] += 1
            print(json.dumps({"file": str(path), "status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
    print(json.dumps({"summary": {"files": len(files), **counts}}), file=sys.stderr)
    return 1 if counts["error"] else 0


def _run(args, files: list[Path], adapters: list[Adapter], index: LibraryIndex | None) -> int:
    counts = {"ok": 0, "mismatch": 0, "unresolved": 0, "error": 0}
    # Stored results are only reused under identical options; changing services or flags rechecks everything.
    options = {"country": args.country, "audio": not args.no_audio, "services": [a.name for a in adapters]}
    reused = 0
    out = args.report.open("w", encoding="utf-8") if args.report else None
    try:
        for n, path in enumerate(files, 1):
            if args.progress: print(f"[{n}/{len(files)}] {path}", file=sys.stderr, flush=True)
            record = index.result(str(path), options) if args.resume else None
            if record is not None:
                reused += 1
            else:
                try:
                    record = compare(probe(path), adapters, audio=not args.no_audio, index=index, accel=not args.no_numpy)
                except Exception as exc:
                    record = {"file": str(path), "status": "error", "error": str(exc)}
                # Errors (including a failed preview fetch) are not stored, so the next run retries them.
                if args.resume and record["status"] != "error" and "error" not in record.get("audio", {}):
                    index.save_result(str(path), options, record)
            counts[record["status"]] += 1
            show = (record["status"] in ("mismatch", "error")
                    or (record["status"] == "unresolved" and args.show_unresolved)
                    or (record["status"] == "ok" and args.verbose))
            if not show:
                continue
            line = json.dumps(record, ensure_ascii=False)
            print(line, file=sys.stderr if record["status"] == "error" else sys.stdout)
            if out: out.write(line + "\n")
    finally:
        if out: out.close()
    summary = {"files": len(files), **counts, **({"reused": reused} if args.resume else {})}
    print(json.dumps({"summary": summary}), file=sys.stderr)
    return 1 if counts["mismatch"] else 0

if __name__ == "__main__": sys.exit(main())
