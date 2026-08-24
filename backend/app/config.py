"""Env-driven settings with Windows-safe defaults.

Container deployment overrides via env vars (MEDIA_ROOT=/media etc.);
unset, everything resolves relative to the repo root.
"""

import os
from pathlib import Path

# backend/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _path_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


MEDIA_ROOT = _path_env("MEDIA_ROOT", REPO_ROOT / "media")
CONFIG_DIR = _path_env("CONFIG_DIR", REPO_ROOT / "config")
DATA_DIR = _path_env("DATA_DIR", REPO_ROOT / "data")

POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

for _d in (MEDIA_ROOT, CONFIG_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)
