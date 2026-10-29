"""SQLite-backed shared registry API for audio perceptual fingerprints."""
from __future__ import annotations
import argparse, json, os, secrets, sqlite3, time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote
import flowerbed as flowerbed_mod
from audio_sig import (ALGORITHM_VERSION, ALPHABET, PERCEPTUAL_BYTES, ROOT_BYTES,
                       decode_bytes, encode_bytes, encode_signature, hamming_distance)

DEFAULT_DB = os.environ.get("AUDIO_SIG_DB", "audio-signatures.sqlite3")
MAX_MATCH_DISTANCE = int(os.environ.get("AUDIO_SIG_MAX_DISTANCE", "24"))
AMBIGUITY_MARGIN = int(os.environ.get("AUDIO_SIG_AMBIGUITY_MARGIN", "5"))
API_TOKEN = os.environ.get("AUDIO_SIG_API_TOKEN", "")


def connect(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS roots (
        root TEXT PRIMARY KEY, created_at REAL NOT NULL, note TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS fingerprints (
        perceptual TEXT PRIMARY KEY, root TEXT NOT NULL REFERENCES roots(root),
        algorithm TEXT NOT NULL, created_at REAL NOT NULL,
        duration REAL, rms REAL, source TEXT NOT NULL DEFAULT ''
      );
      CREATE INDEX IF NOT EXISTS fingerprints_root_idx ON fingerprints(root);
      CREATE TABLE IF NOT EXISTS root_aliases (
        alias TEXT PRIMARY KEY, root TEXT NOT NULL REFERENCES roots(root),
        scheme TEXT NOT NULL, created_at REAL NOT NULL
      );
    """)
    if "scheme" not in {row["name"] for row in db.execute("PRAGMA table_info(roots)")}:
        db.execute(f"ALTER TABLE roots ADD COLUMN scheme TEXT NOT NULL DEFAULT '{flowerbed_mod.RANDOM}'")
        db.commit()
    return db


def canonical_root(db: sqlite3.Connection, root_text: str) -> str | None:
    """The root itself if registered, the root it is an alias of, or None."""
    if db.execute("SELECT 1 FROM roots WHERE root=?", (root_text,)).fetchone():
        return root_text
    row = db.execute("SELECT root FROM root_aliases WHERE alias=?", (root_text,)).fetchone()
    return row["root"] if row else None


def _tags_agree(db: sqlite3.Connection, audio_root: str, tag_root: str | None) -> bool:
    """Flowerbed rule (PLAN.md §9.2): may audio-matched `audio_root` take the tags' root? Records an alias when
    it is new and `audio_root` was issued at random; never merges two existing identities."""
    if tag_root is None:
        return True
    known = canonical_root(db, tag_root)
    if known is not None:
        return known == audio_root
    scheme = db.execute("SELECT scheme FROM roots WHERE root=?", (audio_root,)).fetchone()["scheme"]
    if scheme != flowerbed_mod.RANDOM:
        return False    # same audio already rooted under different tags
    db.execute("INSERT INTO root_aliases(alias,root,scheme,created_at) VALUES(?,?,?,?)",
               (tag_root, audio_root, flowerbed_mod.SCHEME, time.time()))
    return True


def _tag_conflict(db: sqlite3.Connection, perceptual_text: str, audio_root: str, distance: int, tag_root: str) -> dict:
    return {"status": "ambiguous", "reason": "tags", "perceptual": perceptual_text, "algorithm": ALGORITHM_VERSION,
            "candidates": [{"root": audio_root, "via": "audio", "distance": distance},
                           {"root": canonical_root(db, tag_root) or tag_root, "via": "tags"}]}


def new_root() -> tuple[bytes, str]:
    raw = secrets.token_bytes(ROOT_BYTES)
    return raw, encode_bytes(raw)


def resolve(db: sqlite3.Connection, perceptual_text: str, duration=None, rms=None, source="", flowerbed=None) -> dict:
    """`flowerbed`: the root text the file's tags give (flowerbed.root_text), or None for random roots."""
    try:
        return _resolve(db, perceptual_text, duration, rms, source, flowerbed)
    finally:
        if db.in_transaction:   # ambiguous and error paths write nothing
            db.rollback()


def _resolve(db: sqlite3.Connection, perceptual_text: str, duration, rms, source, flowerbed) -> dict:
    if flowerbed is not None and encode_bytes(decode_bytes(flowerbed, ROOT_BYTES)) != flowerbed:
        raise ValueError("Flowerbed root encoding is not canonical")
    # Serialize matching and root creation to avoid duplicate identities under concurrency.
    db.execute("BEGIN IMMEDIATE")
    fp = decode_bytes(perceptual_text, PERCEPTUAL_BYTES)
    if encode_bytes(fp) != perceptual_text:
        raise ValueError("Fingerprint encoding is not canonical")
    existing = db.execute("SELECT * FROM fingerprints WHERE perceptual=?", (perceptual_text,)).fetchone()
    if existing:
        if not _tags_agree(db, existing["root"], flowerbed):
            return _tag_conflict(db, perceptual_text, existing["root"], 0, flowerbed)
        db.commit()   # keeps an alias _tags_agree may have recorded
        root_raw = decode_bytes(existing["root"], ROOT_BYTES)
        return {"status": "existing", "perceptual": perceptual_text, "root": existing["root"],
                "signature": encode_signature(fp, root_raw), "distance": 0, "algorithm": existing["algorithm"]}

    rows = db.execute("SELECT perceptual, root, algorithm FROM fingerprints WHERE algorithm=?", (ALGORITHM_VERSION,)).fetchall()
    best_by_root = {}
    for row in rows:
        candidate = decode_bytes(row["perceptual"], PERCEPTUAL_BYTES)
        distance = hamming_distance(fp, candidate)
        root = row["root"]
        if root not in best_by_root or distance < best_by_root[root][0]:
            best_by_root[root] = (distance, row["perceptual"])
    ranked = sorted((dist, root, candidate) for root, (dist, candidate) in best_by_root.items())
    if ranked and ranked[0][0] <= MAX_MATCH_DISTANCE:
        best_dist, best_root, matched_fp = ranked[0]
        competing = [x for x in ranked[1:] if x[0] <= MAX_MATCH_DISTANCE and x[0] <= best_dist + AMBIGUITY_MARGIN]
        if competing:
            return {"status": "ambiguous", "perceptual": perceptual_text, "algorithm": ALGORITHM_VERSION,
                    "candidates": [{"root": r, "distance": d, "matched_perceptual": p}
                                   for d, r, p in ranked[:5] if d <= MAX_MATCH_DISTANCE]}
        if not _tags_agree(db, best_root, flowerbed):
            return _tag_conflict(db, perceptual_text, best_root, best_dist, flowerbed)
        db.execute("INSERT INTO fingerprints(perceptual,root,algorithm,created_at,duration,rms,source) VALUES(?,?,?,?,?,?,?)",
                   (perceptual_text, best_root, ALGORITHM_VERSION, time.time(), duration, rms, source))
        db.commit()
        root_raw = decode_bytes(best_root, ROOT_BYTES)
        return {"status": "matched", "perceptual": perceptual_text, "root": best_root,
                "signature": encode_signature(fp, root_raw), "distance": best_dist,
                "matched_perceptual": matched_fp, "algorithm": ALGORITHM_VERSION}

    # No audio match. Flowerbed tags already known: same artist/title/month is the same song, so join it.
    status, root_text = "created", flowerbed and canonical_root(db, flowerbed)
    if root_text:
        status = "rooted"
    elif flowerbed:
        root_text = flowerbed
        db.execute("INSERT INTO roots(root,created_at,scheme) VALUES(?,?,?)", (root_text, time.time(), flowerbed_mod.SCHEME))
    else:
        _, root_text = new_root()
        db.execute("INSERT INTO roots(root,created_at,scheme) VALUES(?,?,?)", (root_text, time.time(), flowerbed_mod.RANDOM))
    db.execute("INSERT INTO fingerprints(perceptual,root,algorithm,created_at,duration,rms,source) VALUES(?,?,?,?,?,?,?)",
               (perceptual_text, root_text, ALGORITHM_VERSION, time.time(), duration, rms, source))
    db.commit()
    root_raw = decode_bytes(root_text, ROOT_BYTES)
    return {"status": status, "perceptual": perceptual_text, "root": root_text,
            "signature": encode_signature(fp, root_raw), "algorithm": ALGORITHM_VERSION}


class Handler(BaseHTTPRequestHandler):
    server_version = "AudioSignatureRegistry/1.0"
    def _headers(self, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", os.environ.get("AUDIO_SIG_CORS_ORIGIN", "http://localhost"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
    def _json(self, status, obj):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self._headers(status); self.wfile.write(body)
    def _authorized(self):
        if not API_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {API_TOKEN}"
    def do_OPTIONS(self): self._headers(204)
    def do_GET(self):
        if not self._authorized(): return self._json(401, {"error":"unauthorized"})
        path = urlparse(self.path).path.strip("/").split("/")
        if len(path) != 2 or path[0] != "fingerprints": return self._json(404, {"error":"not found"})
        try: decode_bytes(unquote(path[1]), PERCEPTUAL_BYTES)
        except ValueError as e: return self._json(400, {"error":str(e)})
        with closing(connect(self.server.db_path)) as db:
            row = db.execute("SELECT * FROM fingerprints WHERE perceptual=?", (unquote(path[1]),)).fetchone()
            if not row: return self._json(404, {"error":"fingerprint not found"})
            root_raw = decode_bytes(row["root"], ROOT_BYTES)
            return self._json(200, {"perceptual":row["perceptual"], "root":row["root"],
                "signature":encode_signature(decode_bytes(row["perceptual"], PERCEPTUAL_BYTES),root_raw),
                "algorithm":row["algorithm"], "created_at":row["created_at"], "duration":row["duration"], "source":row["source"]})
    def do_POST(self):
        if not self._authorized(): return self._json(401, {"error":"unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536: return self._json(413, {"error":"request too large"})
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path.rstrip("/").split("/")
            with closing(connect(self.server.db_path)) as db:
                if path == ["", "fingerprints", "resolve"]:
                    if payload.get("algorithm", ALGORITHM_VERSION) != ALGORITHM_VERSION:
                        return self._json(409, {"error":"unsupported algorithm version"})
                    result = resolve(db, payload["perceptual"], payload.get("duration"), payload.get("rms"),
                                     payload.get("source", ""), payload.get("flowerbed"))
                    return self._json(200, result)
                if path == ["", "fingerprints", "aliases"]:
                    fp_text, root_text = payload["perceptual"], payload["root"]
                    decode_bytes(fp_text, PERCEPTUAL_BYTES); decode_bytes(root_text, ROOT_BYTES)
                    if not db.execute("SELECT 1 FROM roots WHERE root=?", (root_text,)).fetchone():
                        return self._json(404, {"error":"canonical root not found"})
                    if db.execute("SELECT 1 FROM fingerprints WHERE perceptual=?", (fp_text,)).fetchone():
                        return self._json(409, {"error":"fingerprint already registered; explicit reassignment is not allowed"})
                    db.execute("INSERT INTO fingerprints(perceptual,root,algorithm,created_at,duration,rms,source) VALUES(?,?,?,?,?,?,?)",
                        (fp_text, root_text, ALGORITHM_VERSION, time.time(), payload.get("duration"), payload.get("rms"), payload.get("source", "manual-alias")))
                    db.commit()
                    return self._json(201, {"status":"aliased", "perceptual":fp_text, "root":root_text,
                        "signature":encode_signature(decode_bytes(fp_text, PERCEPTUAL_BYTES), decode_bytes(root_text, ROOT_BYTES))})
            return self._json(404, {"error":"not found"})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            return self._json(400, {"error":str(e)})
        except sqlite3.IntegrityError as e:
            return self._json(409, {"error":"registry conflict", "detail":str(e)})
    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=DEFAULT_DB)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not API_TOKEN:
        parser.error("Set AUDIO_SIG_API_TOKEN before binding to a non-local interface")
    connect(args.db).close()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.db_path = args.db
    print(f"Registry listening on http://{args.host}:{args.port} (database: {args.db})")
    server.serve_forever()

if __name__ == "__main__": main()
