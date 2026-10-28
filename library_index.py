"""FAFO (Fast-Fourier Audio Fingerprinting Offline): cache of library fingerprints and comparison results.

Stored in the FAFO_DIR folder (environment or `.env`; default `FAFO/` beside this code), never in the
scanned library. Library files are only stat()ed and decoded read-only; an entry is reused while the
file's (size, mtime) is unchanged.
"""
from __future__ import annotations
import array, json, os, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
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

    def stats(self) -> dict:
        """Counts and on-disk size, for monitoring a long indexing run."""
        fingerprints, frames = self.db.execute("SELECT count(*), coalesce(sum(length(subfps)), 0) / 4 FROM fingerprints").fetchone()
        results = dict(self.db.execute("SELECT json_extract(record, '$.status'), count(*) FROM results GROUP BY 1").fetchall())
        last = self.db.execute("SELECT max(indexed_at) FROM fingerprints").fetchone()[0]
        return {"index": str(self.db_path), "fingerprints": fingerprints, "frames": frames, "last_indexed": last,
                "results": results, "bytes": self.db_path.stat().st_size}

    def result(self, path: str, options: dict) -> dict | None:
        """A stored comparison record for this unchanged file under the same options, if any."""
        size, mtime = self._stat(path)
        row = self.db.execute("SELECT record FROM results WHERE path=? AND size=? AND mtime_ns=? AND options=?",
                              (str(path), size, mtime, json.dumps(options, sort_keys=True))).fetchone()
        return json.loads(row[0]) if row else None

    def save_result(self, path: str, options: dict, record: dict) -> None:
        size, mtime = self._stat(path)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?)",
                            (str(path), size, mtime, json.dumps(options, sort_keys=True),
                             json.dumps(record, ensure_ascii=False), _now()))
