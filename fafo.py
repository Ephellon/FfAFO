"""FAFO (Fast-Fourier Audio Fingerprinting Offline) command line.

  python fafo.py fingerprint PATH...   fingerprint audio files/folders into the FAFO index (no network)
  python fafo.py compare PATH...       compare against streaming services; reports mismatches only
  python fafo.py stats                 show what the FAFO index holds
  python fafo.py xv3 PATH...           EXPERIMENTAL spectrum-plot signatures (--pair A B compares two files)

Every service reads the library only. Output is JSON Lines on stdout; errors, progress and a
summary line go to stderr. Run `python fafo.py <service> --help` for options.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import compare, spectrum_sig
from audio_sig import ALGORITHM_V2, ALGORITHM_VERSION, analyze, encode_bytes
from library_index import INDEX_PATH, LibraryIndex
from scan_folder import decode_audio

SERVICES = ("fingerprint", "compare", "stats", "xv3")


def _utf8_streams() -> None:
    # JSONL is UTF-8 regardless of console code page (piped output on Windows otherwise mangles e.g. "¥").
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")


def fingerprint_record(path: Path, index: LibraryIndex | None, accel: bool, with_subfps: bool) -> dict:
    """One file's fingerprint record; from the index when given (computed and stored if missing)."""
    if index is not None:
        entry, cached = index.entry(str(path))
    else:
        samples, duration, _ = decode_audio(path)
        v1, subfps = analyze(samples, accel=accel)
        entry, cached = {"duration": duration, "v1": v1, "subfps": subfps}, False
    record = {"file": str(path), "status": "cached" if cached else "indexed" if index is not None else "computed",
              "duration": round(entry["duration"], 3), ALGORITHM_VERSION: encode_bytes(entry["v1"]),
              "frames": len(entry["subfps"])}
    if with_subfps:
        record[ALGORITHM_V2] = entry["subfps"]
    return record


def fingerprint_main(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Fingerprint audio files/folders into the FAFO index. No network.")
    ap.add_argument("paths", nargs="+", type=Path, metavar="path", help="Library folder(s) or audio file(s)")
    ap.add_argument("--index", type=Path, default=INDEX_PATH, help=f"FAFO index, SQLite (default {INDEX_PATH})")
    ap.add_argument("--no-index", action="store_true", help="Compute only; do not read or write the index")
    ap.add_argument("--no-numpy", action="store_true", help="Methodical mode: pure-Python FFT (about 30x slower, same output)")
    ap.add_argument("--subfingerprints", action="store_true", help=f"Include the {ALGORITHM_V2} sequence (28-bit ints) per file")
    ap.add_argument("--quiet", action="store_true", help="No per-file records on stdout; summary and errors only")
    ap.add_argument("--progress", action="store_true", help="Print [n/total] per file to stderr")
    args = ap.parse_args(argv)
    _utf8_streams()
    try:
        files, roots = compare.collect_files(args.paths)
    except ValueError as exc:
        ap.error(str(exc))
    if not args.no_index and any(compare._inside(args.index, r) for r in roots):
        ap.error("--index must be outside the scanned folder (the library is read-only)")
    counts = {"indexed": 0, "cached": 0, "computed": 0, "error": 0}
    index = None if args.no_index else LibraryIndex(args.index, accel=not args.no_numpy)
    try:
        for n, path in enumerate(files, 1):
            if args.progress: print(f"[{n}/{len(files)}] {path}", file=sys.stderr, flush=True)
            try:
                record = fingerprint_record(path, index, not args.no_numpy, args.subfingerprints)
            except Exception as exc:
                record = {"file": str(path), "status": "error", "error": str(exc)}
            counts[record["status"]] += 1
            if record["status"] == "error":
                print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
            elif not args.quiet:
                print(json.dumps(record, ensure_ascii=False), flush=True)
    finally:
        if index: index.close()
    print(json.dumps({"summary": {"files": len(files), **counts}}), file=sys.stderr)
    return 1 if counts["error"] else 0


def stats_main(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Show what the FAFO index holds (read-only).")
    ap.add_argument("--index", type=Path, default=INDEX_PATH, help=f"FAFO index, SQLite (default {INDEX_PATH})")
    args = ap.parse_args(argv)
    _utf8_streams()
    if not args.index.is_file():
        print(json.dumps({"index": str(args.index), "error": "index does not exist"}), file=sys.stderr)
        return 1
    with LibraryIndex(args.index) as index:
        print(json.dumps(index.stats(), ensure_ascii=False))
    return 0


def xv3_record(path: Path, accel: bool, with_bands: bool) -> tuple[dict, spectrum_sig.Spectrum]:
    spectrum = spectrum_sig.analyze(spectrum_sig.decode(path), accel=accel)
    record = {"file": str(path), "duration": round(spectrum.duration, 3),
              spectrum_sig.ALGORITHM: spectrum_sig.encode_plot(spectrum.plot),
              "segments": sum(seg is not None for seg in spectrum.segments)}
    if with_bands:
        record["bands"] = [round(x, 2) for x in spectrum.plot]
    return record, spectrum


def xv3_main(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="EXPERIMENTAL spectrum-plot signatures (xv3). Reads audio only: "
                                 "no index, no network. Thresholds are provisional (PLAN.md §9.3).")
    ap.add_argument("paths", nargs="+", type=Path, metavar="path", help="Audio file(s)/folder(s); exactly two files with --pair")
    ap.add_argument("--pair", action="store_true", help="Compare two files: plot distance and sections heard in only one")
    ap.add_argument("--plot-db", type=float, default=spectrum_sig.PLOT_MAX_DB,
                    help=f"--pair: largest plot distance still called the same song (default {spectrum_sig.PLOT_MAX_DB})")
    ap.add_argument("--section-db", type=float, default=spectrum_sig.SECTION_MAX_DB,
                    help=f"--pair: largest segment distance still called the same section (default {spectrum_sig.SECTION_MAX_DB})")
    ap.add_argument("--vocal-weight", type=float, default=1.0, help="Weight of bands centred in 60-1050 Hz (0 ignores them)")
    ap.add_argument("--bands", action="store_true", help="Include the unquantized plot (dB per band, relative to its mean)")
    ap.add_argument("--no-numpy", action="store_true", help="Methodical mode: pure-Python FFT (much slower, same output)")
    ap.add_argument("--progress", action="store_true", help="Print [n/total] per file to stderr")
    args = ap.parse_args(argv)
    _utf8_streams()
    weights = spectrum_sig.band_weights(args.vocal_weight)
    if args.pair:
        if len(args.paths) != 2 or not all(p.is_file() for p in args.paths):
            ap.error("--pair takes exactly two audio files")
        (ra, a), (rb, b) = (xv3_record(p, not args.no_numpy, args.bands) for p in args.paths)
        plot_db = spectrum_sig.distance(a.plot, b.plot, weights)
        sections = spectrum_sig.section_diff(a, b, args.section_db, weights)
        record = {"a": ra, "b": rb, "plot_db": round(plot_db, 2), **sections,
                  "match": plot_db <= args.plot_db and not sections["missing"] and not sections["extra"]}
        print(json.dumps(record, ensure_ascii=False))
        return 0 if record["match"] else 1
    try:
        files, _ = compare.collect_files(args.paths)
    except ValueError as exc:
        ap.error(str(exc))
    errors = 0
    for n, path in enumerate(files, 1):
        if args.progress: print(f"[{n}/{len(files)}] {path}", file=sys.stderr, flush=True)
        try:
            print(json.dumps(xv3_record(path, not args.no_numpy, args.bands)[0], ensure_ascii=False), flush=True)
        except Exception as exc:
            errors += 1
            print(json.dumps({"file": str(path), "status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
    print(json.dumps({"summary": {"files": len(files), "error": errors}}), file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__.strip())
        return 0
    if not argv or argv[0] not in SERVICES:
        print(__doc__.strip(), file=sys.stderr)
        if argv: print(f"\nUnknown service {argv[0]!r}; choose from {', '.join(SERVICES)}", file=sys.stderr)
        return 2
    service, rest, prog = argv[0], argv[1:], f"fafo.py {argv[0]}"
    if service == "fingerprint":
        return fingerprint_main(rest, prog)
    if service == "compare":
        return compare.main(rest, prog=prog)
    if service == "xv3":
        return xv3_main(rest, prog)
    return stats_main(rest, prog)


if __name__ == "__main__": sys.exit(main())
