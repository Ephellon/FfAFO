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
import compare, flowerbed, spectrum_sig
from audio_sig import ALGORITHM_V2, ALGORITHM_VERSION, analyze, encode_bytes, encode_signature
from library_index import INDEX_PATH, ChunkedIndex, LibraryIndex
from scan_folder import decode_audio

SERVICES = ("fingerprint", "compare", "stats", "xv3")


def _utf8_streams() -> None:
    # JSONL is UTF-8 regardless of console code page (piped output on Windows otherwise mangles e.g. "¥").
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")


def fingerprint_record(path: Path, index: LibraryIndex | None, accel: bool, with_subfps: bool,
                       with_flowerbed: bool = False) -> dict:
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
    if with_flowerbed:
        key = compare.probe(path).flowerbed
        record["flowerbed"] = key
        if key:
            record["root"] = flowerbed.root_text(key)
            record["signature"] = encode_signature(entry["v1"], flowerbed.root(key))
    return record


def fingerprint_main(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="Fingerprint audio files/folders into the FAFO index. No network.")
    ap.add_argument("paths", nargs="+", type=Path, metavar="path", help="Library folder(s) or audio file(s)")
    ap.add_argument("--index", type=Path, default=INDEX_PATH, help=f"FAFO index, SQLite (default {INDEX_PATH})")
    ap.add_argument("--no-index", action="store_true", help="Compute only; do not read or write the index")
    ap.add_argument("--folder-chunk", action="store_true",
                    help="One index per album folder, beside --index; the first use copies entries from --index")
    ap.add_argument("--no-numpy", action="store_true", help="Methodical mode: pure-Python FFT (about 30x slower, same output)")
    ap.add_argument("--subfingerprints", action="store_true", help=f"Include the {ALGORITHM_V2} sequence (28-bit ints) per file")
    ap.add_argument("--flowerbed", action="store_true",
                    help="Add the tag-derived root: flowerbed key, root and full signature (null key if tags are incomplete)")
    ap.add_argument("--granular", action="store_true", help="Add each file's size and mtime (ISO 8601 UTC) as FAFO saw them")
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
    index = None if args.no_index else compare.open_index(args.index, not args.no_numpy, args.folder_chunk)
    try:
        for n, path in enumerate(files, 1):
            if args.progress: print(f"[{n}/{len(files)}] {path}", file=sys.stderr, flush=True)
            try:
                record = fingerprint_record(path, index, not args.no_numpy, args.subfingerprints, args.flowerbed)
                if args.granular: record.update(compare._file_state(path))
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
    ap.add_argument("--folder-chunk", action="store_true", help="Totals over the per-album indexes beside --index")
    args = ap.parse_args(argv)
    _utf8_streams()
    if args.folder_chunk:
        print(json.dumps(ChunkedIndex(args.index.parent, source=args.index).stats(), ensure_ascii=False))
        return 0
    if not args.index.is_file():
        print(json.dumps({"index": str(args.index), "error": "index does not exist"}), file=sys.stderr)
        return 1
    with LibraryIndex(args.index) as index:
        print(json.dumps(index.stats(), ensure_ascii=False))
    return 0


def xv3_record(path: Path, accel: bool, with_bands: bool, index=None,
               plot_signed: bool = False) -> tuple[dict, spectrum_sig.Spectrum]:
    if index is not None:
        spectrum, cached = index.spectrum(str(path))
        status = "cached" if cached else "indexed"
    else:
        spectrum, status = spectrum_sig.analyze(spectrum_sig.decode(path), accel=accel), "computed"
    record = {"file": str(path), "status": status, "duration": round(spectrum.duration, 3),
              spectrum_sig.ALGORITHM: spectrum_sig.encode_plot(spectrum.plot),
              "segments": sum(seg is not None for seg in spectrum.segments), "trimmed": list(spectrum.trimmed)}
    if plot_signed:   # forces flowerbed: the root comes from the file's tags
        key = compare.probe(path).flowerbed
        record["flowerbed"] = key
        if key:
            record["root"] = flowerbed.root_text(key)
            record["plot_signed"] = spectrum_sig.plot_signature(spectrum.plot, flowerbed.root(key))
    if with_bands:
        record["bands"] = [round(x, 2) for x in spectrum.plot]
    return record, spectrum


def xv3_main(argv: list[str], prog: str) -> int:
    ap = argparse.ArgumentParser(prog=prog, description="EXPERIMENTAL spectrum-plot signatures (xv3). Reads audio only, "
                                 "no network; spectra are cached in the FAFO index. Thresholds are provisional (PLAN.md §9.3).")
    ap.add_argument("paths", nargs="+", type=Path, metavar="path", help="Audio file(s)/folder(s); exactly two files with --pair")
    ap.add_argument("--pair", action="store_true", help="Compare two files: plot distance and sections heard in only one")
    ap.add_argument("--plot-db", type=float, default=spectrum_sig.PLOT_MAX_DB,
                    help=f"--pair: largest plot distance still called the same song (default {spectrum_sig.PLOT_MAX_DB})")
    ap.add_argument("--section-db", type=float, default=spectrum_sig.SECTION_MAX_DB,
                    help=f"--pair: largest segment distance still called the same section (default {spectrum_sig.SECTION_MAX_DB})")
    ap.add_argument("--vocal-weight", type=float, default=1.0, help="Weight of bands centred in 60-1050 Hz (0 ignores them)")
    ap.add_argument("--bands", action="store_true", help="Include the unquantized plot (dB per band, relative to its mean)")
    ap.add_argument("--plot-signed", action="store_true",
                    help=f"Add the plot joined to the flowerbed root by '{spectrum_sig.PLOT_JOIN}' (forces flowerbed)")
    ap.add_argument("--granular", action="store_true", help="Add each file's size and mtime (ISO 8601 UTC) as FAFO saw them")
    ap.add_argument("--index", type=Path, default=INDEX_PATH, help=f"FAFO index, SQLite (default {INDEX_PATH})")
    ap.add_argument("--no-index", action="store_true", help="Compute only; do not read or write the index")
    ap.add_argument("--folder-chunk", action="store_true", help="One index per album folder, beside --index")
    ap.add_argument("--no-numpy", action="store_true", help="Methodical mode: pure-Python FFT (much slower, same output)")
    ap.add_argument("--progress", action="store_true", help="Print [n/total] per file to stderr")
    args = ap.parse_args(argv)
    _utf8_streams()
    weights = spectrum_sig.band_weights(args.vocal_weight)
    if args.pair and (len(args.paths) != 2 or not all(p.is_file() for p in args.paths)):
        ap.error("--pair takes exactly two audio files")
    try:
        files, roots = compare.collect_files(args.paths)
    except ValueError as exc:
        ap.error(str(exc))
    if not args.no_index and any(compare._inside(args.index, r) for r in roots):
        ap.error("--index must be outside the scanned folder (the library is read-only)")
    index = None if args.no_index else compare.open_index(args.index, not args.no_numpy, args.folder_chunk)
    def record_of(p: Path) -> tuple[dict, spectrum_sig.Spectrum]:
        record, spectrum = xv3_record(p, not args.no_numpy, args.bands, index, args.plot_signed)
        if args.granular: record.update(compare._file_state(p))
        return record, spectrum
    try:
        if args.pair:
            (ra, a), (rb, b) = (record_of(p) for p in args.paths)
            plot_db = spectrum_sig.distance(a.plot, b.plot, weights)
            sections = spectrum_sig.section_diff(a, b, args.section_db, weights)
            record = {"a": ra, "b": rb, "plot_db": round(plot_db, 2), **sections,
                      "match": plot_db <= args.plot_db and not sections["missing"] and not sections["extra"]}
            print(json.dumps(record, ensure_ascii=False))
            return 0 if record["match"] else 1
        errors = 0
        for n, path in enumerate(files, 1):
            if args.progress: print(f"[{n}/{len(files)}] {path}", file=sys.stderr, flush=True)
            try:
                print(json.dumps(record_of(path)[0], ensure_ascii=False), flush=True)
            except Exception as exc:
                errors += 1
                print(json.dumps({"file": str(path), "status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        print(json.dumps({"summary": {"files": len(files), "error": errors}}), file=sys.stderr)
        return 1 if errors else 0
    finally:
        if index: index.close()


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
