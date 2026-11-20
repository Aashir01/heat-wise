"""Fail if the values of TEMP_API_KEY or NEBIUS_API_KEY appear in any file outside .env/.venv/.git."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__"}


def main() -> int:
    env = dotenv_values(ROOT / ".env")
    values = [v for k, v in env.items() if k in ("TEMP_API_KEY", "NEBIUS_API_KEY") and v]
    if not values:
        print("no keys in .env; nothing to scan for")
        return 0
    hits = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or SKIP_DIRS & set(p.relative_to(ROOT).parts) or p.name == ".env":
            continue
        data = p.read_bytes()
        if any(v.encode() in data for v in values):
            hits.append(str(p.relative_to(ROOT)))
    print(f"scanned for {len(values)} key values; files containing a key: {hits or 'none'}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
