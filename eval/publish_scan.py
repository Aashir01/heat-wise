"""Pre-publish scan. Prints findings only; never prints a key value.

Usage:
  python eval/publish_scan.py history            # every blob in every commit of this repo
  python eval/publish_scan.py tree <dir>         # every file under <dir> (e.g. a clean copy)
Checks: literal values of TEMP_API_KEY / NEBIUS_API_KEY, plus privacy/secret patterns.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
PATTERNS = {
    "Bearer <token>": re.compile(
        rb"Bearer\s+(?!\[REDACTED\]|\$|<|\{|\"|'|YOUR|your)[A-Za-z0-9._\-]{12,}"
    ),
    "api-key header with value": re.compile(
        rb"api-key[\"']?\s*[:=]\s*[\"'](?!\[REDACTED\]|YOUR|your)[A-Za-z0-9._\-]{12,}"
    ),
    "sk- style key": re.compile(rb"\bsk-[A-Za-z0-9]{16,}"),
    "token= value": re.compile(rb"(?i)\btoken[\"']?\s*[:=]\s*[\"'][A-Za-z0-9._\-]{16,}"),
    "email address": re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "C:\\Users path": re.compile(rb"(?i)c:[\\/]+users"),
    "HP\\ path segment": re.compile(rb"\\HP\\|\\\\HP\\\\|/HP/"),
    "long random string (40+)": re.compile(
        rb"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9_\-]{40,}(?![A-Za-z0-9+/=_-])"
    ),
}
ALLOWED_EMAILS = {b"heatwise@example.invalid", b"support@fortyguard.com"}


def keys() -> dict[str, bytes]:
    env = dotenv_values(ROOT / ".env")
    return {k: v.encode() for k, v in env.items() if k in ("TEMP_API_KEY", "NEBIUS_API_KEY") and v}


def scan_bytes(
    label: str, data: bytes, secret: dict[str, bytes], hits: dict[str, set[str]]
) -> None:
    for name, value in secret.items():
        if value in data:
            hits[f"KEY VALUE {name}"].add(label)
    if label.lower().endswith((".png", ".pdf")):
        return
    for name, rx in PATTERNS.items():
        for m in rx.finditer(data):
            s = m.group(0)
            if name == "email address" and s in ALLOWED_EMAILS:
                continue
            if name.startswith("long random") and (
                s.isalpha() or b"_" in s and s.replace(b"_", b"").isalpha()
            ):
                continue
            hits[name].add(label)
            break


def scan_history(secret: dict[str, bytes]) -> dict[str, set[str]]:
    hits: dict[str, set[str]] = defaultdict(set)
    commits = subprocess.run(
        ["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    seen: set[str] = set()
    for c in commits:
        tree = subprocess.run(
            ["git", "ls-tree", "-r", c], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.splitlines()
        for line in tree:
            meta, path = line.split("\t", 1)
            blob = meta.split()[2]
            if blob in seen:
                continue
            seen.add(blob)
            data = subprocess.run(
                ["git", "cat-file", "-p", blob], cwd=ROOT, capture_output=True, check=True
            ).stdout
            scan_bytes(f"{path} (commit {c[:7]})", data, secret, hits)
    msgs = subprocess.run(
        ["git", "log", "--all", "--format=%an <%ae> %cn <%ce> %s"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    scan_bytes("commit metadata", msgs, secret, hits)
    print(f"history: {len(commits)} commits, {len(seen)} distinct blobs scanned")
    return hits


def scan_tree(base: Path, secret: dict[str, bytes]) -> dict[str, set[str]]:
    hits: dict[str, set[str]] = defaultdict(set)
    n = 0
    for p in base.rglob("*"):
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(base).parts) and p.name != ".env":
            n += 1
            scan_bytes(p.relative_to(base).as_posix(), p.read_bytes(), secret, hits)
    print(f"tree: {n} files scanned under {base.name}")
    return hits


def main() -> int:
    secret = keys()
    print(f"key values loaded for scanning: {len(secret)} (values not shown)")
    hits = (
        scan_history(secret) if sys.argv[1] == "history" else scan_tree(Path(sys.argv[2]), secret)
    )
    for name in sorted(hits):
        files = sorted(hits[name])
        print(f"[{name}] {len(files)} file(s)")
        for f in files[:40]:
            print("   ", f)
        if len(files) > 40:
            print(f"    ... {len(files) - 40} more")
    key_hits = [n for n in hits if n.startswith("KEY VALUE")]
    print("KEY VALUES:", "FOUND" if key_hits else "not found")
    return 1 if key_hits else 0


if __name__ == "__main__":
    sys.exit(main())
