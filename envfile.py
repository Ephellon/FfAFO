"""Minimal `.env` loader (no dependency). Real environment variables always win over the file."""
from __future__ import annotations
import os
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")


def load_env(path: Path = ENV_PATH) -> None:
    """Set KEY=VALUE lines from `path` into os.environ unless already set. Missing file is fine."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            os.environ.setdefault(key, value)
