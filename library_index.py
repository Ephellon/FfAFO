"""FAFO (Fast-Fourier Audio Fingerprinting Offline): cache of library fingerprints and comparison results.

Stored in the FAFO_DIR folder (environment or `.env`; default `FAFO/` beside this code), never in the
scanned library. Library files are only stat()ed and decoded read-only; an entry is reused while the
file's (size, mtime) is unchanged.
"""
from __future__ import annotations
import array, json, os, sqlite3, sys, unicodedata
from datetime import datetime, timezone
from pathlib import Path
import spectrum_sig
from audio_sig import ALGORITHM_V2, ALGORITHM_VERSION, analyze
from envfile import load_env
from scan_folder import decode_audio

load_env()
INDEX_PATH = Path(os.environ.get("FAFO_DIR") or Path(__file__).with_name("FAFO")) / "library-index.sqlite3"
ALGORITHM = f"{ALGORITHM_VERSION}+{ALGORITHM_V2}"   # bump either version and every entry is recomputed

SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, algorithm TEXT NOT NULL,
    duration REAL NOT NULL, v1 BLOB NOT NULL, subfps BLOB NOT NULL, indexed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS results (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, options TEXT NOT NULL,
    record TEXT NOT NULL, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spectra (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, recipe TEXT NOT NULL,
    duration REAL NOT NULL, plot BLOB NOT NULL, windows BLOB NOT NULL, grid BLOB NOT NULL,
    trim_lead REAL NOT NULL, trim_tail REAL NOT NULL, indexed_at TEXT NOT NULL);
"""


def _pack(subfps: list[int]) -> bytes:
    data = array.array("I", subfps)
    if sys.byteorder != "little": data.byteswap()
    return data.tobytes()


def _unpack(blob: bytes) -> list[int]:
    data = array.array("I"); data.frombytes(blob)
    if sys.byteorder != "little": data.byteswap()
    return data.tolist()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LibraryIndex:
    """Lazily opened: no file is created until something is cached or looked up."""

    def __init__(self, db_path: Path = INDEX_PATH, accel: bool = True):
        self.db_path, self.accel, self._db = Path(db_path), accel, None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.db_path)
            self._db.executescript(SCHEMA)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    @staticmethod
    def _stat(path: str) -> tuple[int, int]:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns

    def cached(self, path: str) -> bool:
        size, mtime = self._stat(path)
        row = self.db.execute("SELECT 1 FROM fingerprints WHERE path=? AND size=? AND mtime_ns=? AND algorithm=?",
                              (str(path), size, mtime, ALGORITHM)).fetchone()
        return row is not None

    def entry(self, path: str) -> tuple[dict, bool]:
        """({duration, v1, subfps}, was_cached) for a library file; computed once per (size, mtime), committed at once."""
        size, mtime = self._stat(path)
        row = self.db.execute("SELECT duration, v1, subfps FROM fingerprints"
                              " WHERE path=? AND size=? AND mtime_ns=? AND algorithm=?",
                              (str(path), size, mtime, ALGORITHM)).fetchone()
        if row:
            return {"duration": row[0], "v1": bytes(row[1]), "subfps": _unpack(row[2])}, True
        samples, duration, _ = decode_audio(Path(path))
        v1, subfps = analyze(samples, accel=self.accel)
        with self.db:   # one transaction per file, so an interrupted run keeps everything finished so far
            self.db.execute("INSERT OR REPLACE INTO fingerprints VALUES (?,?,?,?,?,?,?,?)",
                            (str(path), size, mtime, ALGORITHM, duration, v1, _pack(subfps), _now()))
        return {"duration": duration, "v1": v1, "subfps": subfps}, False

    def subfingerprints(self, path: str) -> list[int]:
        """v2 sub-fingerprints of a library file (see entry)."""
        return self.entry(path)[0]["subfps"]

    def spectrum(self, path: str) -> tuple[spectrum_sig.Spectrum, bool]:
        """(xv3 spectrum, was_cached), computed once per (size, mtime) and spectrum_sig.RECIPE."""
        size, mtime = self._stat(path)
        row = self.db.execute("SELECT duration, plot, windows, grid, trim_lead, trim_tail FROM spectra"
                              " WHERE path=? AND size=? AND mtime_ns=? AND recipe=?",
                              (str(path), size, mtime, spectrum_sig.RECIPE)).fetchone()
        if row:
            return spectrum_sig.from_blobs(row[0], bytes(row[1]), bytes(row[2]), bytes(row[3]), (row[4], row[5])), True
        spectrum = spectrum_sig.analyze(spectrum_sig.decode(Path(path)), accel=self.accel)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO spectra VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (str(path), size, mtime, spectrum_sig.RECIPE, spectrum.duration,
                             *spectrum_sig.to_blobs(spectrum), *spectrum.trimmed, _now()))
        # Hand back what a later cached read returns (windows rounded to the stored centi-dB).
        return spectrum_sig.from_blobs(spectrum.duration, *spectrum_sig.to_blobs(spectrum), spectrum.trimmed), False

    def stats(self) -> dict:
        """Counts and on-disk size, for monitoring a long indexing run."""
        fingerprints, frames = self.db.execute("SELECT count(*), coalesce(sum(length(subfps)), 0) / 4 FROM fingerprints").fetchone()
        results = dict(self.db.execute("SELECT json_extract(record, '$.status'), count(*) FROM results GROUP BY 1").fetchall())
        last = self.db.execute("SELECT max(indexed_at) FROM fingerprints").fetchone()[0]
        spectra = self.db.execute("SELECT count(*) FROM spectra").fetchone()[0]
        return {"index": str(self.db_path), "fingerprints": fingerprints, "frames": frames, "spectra": spectra,
                "last_indexed": last, "results": results, "bytes": self.db_path.stat().st_size}

    def result(self, path: str, options: dict) -> dict | None:
        """A stored comparison record for this unchanged file under the same options, if any."""
        size, mtime = self._stat(path)
        row = self.db.execute("SELECT record, checked_at FROM results WHERE path=? AND size=? AND mtime_ns=? AND options=?",
                              (str(path), size, mtime, json.dumps(options, sort_keys=True))).fetchone()
        return {**json.loads(row[0]), "checked": json.loads(row[0]).get("checked", row[1])} if row else None

    def save_result(self, path: str, options: dict, record: dict) -> None:
        size, mtime = self._stat(path)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?)",
                            (str(path), size, mtime, json.dumps(options, sort_keys=True),
                             json.dumps(record, ensure_ascii=False), _now()))


_TABLES = {"fingerprints": 8, "results": 6, "spectra": 11}   # table -> column count (SCHEMA)
_UNESCAPED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@*_+-./")


def chunk_name(folder: str) -> str:
    """Folder name -> database name: NFC, then JavaScript escape() over UTF-16 code units with ';' for '%'
    (';XX' up to FF, ';uXXXX' above). Case is kept; a trailing '.' becomes ';2E' (Windows drops it)."""
    units = unicodedata.normalize("NFC", folder).encode("utf-16-le")
    out = []
    for i in range(0, len(units), 2):
        unit = int.from_bytes(units[i:i + 2], "little")
        out.append(chr(unit) if chr(unit) in _UNESCAPED else f";{unit:02X}" if unit <= 0xFF else f";u{unit:04X}")
    name = "".join(out)
    return name[:-1] + ";2E" if name.endswith(".") else name


def _folder_chunk(path: str) -> str:
    return chunk_name(Path(path).parent.name or "(root)")


class ChunkedIndex:
    """--folder-chunk: one LibraryIndex per album folder, `<folder>/<chunk_name(parent folder)>.sqlite3`.
    Folders with the same name share a database. A chunk created for the first time copies its folder's
    rows from `source` (the single index), which is only read and stays in place."""

    def __init__(self, folder: Path, accel: bool = True, source: Path | None = INDEX_PATH):
        self.folder, self.accel, self.source = Path(folder), accel, Path(source) if source else None
        self._chunks: dict[str, LibraryIndex] = {}
        self._source_paths: dict[str, list[tuple[str, str]]] | None = None

    def chunk(self, path: str) -> LibraryIndex:
        name = _folder_chunk(path)
        index = self._chunks.get(name)
        if index is None:
            db_path = self.folder / f"{name}.sqlite3"
            fresh = not db_path.exists()
            index = self._chunks[name] = LibraryIndex(db_path, self.accel)
            if fresh:
                self._migrate(index, name)
        return index

    def _migrate(self, index: LibraryIndex, name: str) -> None:
        if self.source is None or not self.source.is_file() or self.source.resolve() == index.db_path.resolve():
            return
        src = sqlite3.connect(f"{self.source.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if self._source_paths is None:   # one pass over the single index's paths, grouped by chunk
                self._source_paths = {}
                present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in [t for t in _TABLES if t in present]:   # older indexes have no spectra table
                    for (p,) in src.execute(f"SELECT path FROM {table}"):
                        self._source_paths.setdefault(_folder_chunk(p), []).append((table, p))
            rows = {t: [] for t in _TABLES}
            for table, p in self._source_paths.get(name, []):
                rows[table].extend(src.execute(f"SELECT * FROM {table} WHERE path=?", (p,)))
        finally:
            src.close()
        with index.db:
            for table, width in _TABLES.items():
                index.db.executemany(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' * width)})", rows[table])

    def close(self) -> None:
        for index in self._chunks.values():
            index.close()
        self._chunks.clear()

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    def cached(self, path: str) -> bool: return self.chunk(path).cached(path)
    def entry(self, path: str) -> tuple[dict, bool]: return self.chunk(path).entry(path)
    def subfingerprints(self, path: str) -> list[int]: return self.chunk(path).subfingerprints(path)
    def spectrum(self, path: str): return self.chunk(path).spectrum(path)
    def result(self, path: str, options: dict) -> dict | None: return self.chunk(path).result(path, options)
    def save_result(self, path: str, options: dict, record: dict) -> None: self.chunk(path).save_result(path, options, record)

    def stats(self) -> dict:
        """Totals over every chunk database in the folder (the single index and other caches excluded)."""
        totals = {"index": str(self.folder), "chunks": 0, "fingerprints": 0, "frames": 0, "spectra": 0,
                  "last_indexed": None, "results": {}, "bytes": 0}
        skip = {self.source.resolve()} if self.source else set()
        for db_path in sorted(self.folder.glob("*.sqlite3")):
            if db_path.resolve() in skip:
                continue
            db = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                is_chunk = db.execute("SELECT 1 FROM sqlite_master WHERE name='fingerprints'").fetchone()
            finally:
                db.close()
            if not is_chunk:
                continue   # another cache, e.g. musicbrainz-cache.sqlite3
            with LibraryIndex(db_path) as index:
                s = index.stats()
            totals["chunks"] += 1
            for k in ("fingerprints", "frames", "spectra", "bytes"):
                totals[k] += s[k]
            for status, n in s["results"].items():
                totals["results"][status] = totals["results"].get(status, 0) + n
            if s["last_indexed"] and (totals["last_indexed"] or "") < s["last_indexed"]:
                totals["last_indexed"] = s["last_indexed"]
        return totals
