"""Recursively fingerprint audio files and resolve them in a registry."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, urllib.error, urllib.request
from pathlib import Path
from audio_sig import TARGET_RATE, fingerprint, encode_bytes

EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".mp4", ".webm"}


def decode_audio(path: Path) -> tuple[list[float], float, float]:
    """Use FFmpeg for broad format support; output mono float PCM at the target rate."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1", "-ar", str(TARGET_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or "FFmpeg decode failed")
    if len(proc.stdout) < 4 * 2048 or len(proc.stdout) % 4:
        raise RuntimeError("Decoded audio is empty, too short, or malformed")
    import array
    pcm = array.array("f"); pcm.frombytes(proc.stdout)
    if sys.byteorder != "little": pcm.byteswap()
    samples = list(pcm)
    rms = (sum(x*x for x in samples) / len(samples)) ** 0.5
    return samples, len(samples) / TARGET_RATE, rms


def post_json(url: str, payload: dict, token: str = "") -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type":"application/json"}
    if token: headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--registry", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default=os.environ.get("AUDIO_SIG_API_TOKEN", ""))
    ap.add_argument("--jsonl", type=Path, help="Write one JSON result per file")
    args = ap.parse_args()
    if not args.folder.is_dir(): ap.error(f"Not a folder: {args.folder}")
    files = sorted(p for p in args.folder.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS)
    out = args.jsonl.open("w", encoding="utf-8") if args.jsonl else None
    try:
        for path in files:
            try:
                samples, duration, rms = decode_audio(path)
                fp = fingerprint(samples)
                result = post_json(args.registry.rstrip("/") + "/fingerprints/resolve", {
                    "perceptual": encode_bytes(fp), "algorithm":"asfp-v1", "duration":round(duration, 3),
                    "rms":round(rms, 8), "source":str(path.resolve())
                }, args.token)
                record = {"file":str(path), **result}
            except Exception as exc:
                record = {"file":str(path), "status":"error", "error":str(exc)}
            line = json.dumps(record, ensure_ascii=False)
            print(line)
            if out: out.write(line + "\n")
    finally:
        if out: out.close()

if __name__ == "__main__": main()
