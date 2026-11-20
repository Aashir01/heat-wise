"""Load API keys from .env and make sure they never leak into logs or saved files."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

SECRET_ENV_NAMES = ("TEMP_API_KEY", "NEBIUS_API_KEY")
REDACTED = "[REDACTED]"
_SENSITIVE_HEADERS = {"api-key", "authorization", "x-api-key", "proxy-authorization"}
_SIGNED_URL_PARAMS = re.compile(
    r"(?i)([?&](?:X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token|X-Goog-Signature|X-Goog-Credential|Signature|sig|token)=)[^&\s\"']+"
)


def load_env(path: Path | None = None) -> None:
    """Load .env into os.environ (does not override variables already set)."""
    load_dotenv(path, override=False)


def get_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise KeyError(f"{name} is not set. Add it to .env (see .env.example).")
    return value


def _secret_values() -> list[str]:
    return [v for n in SECRET_ENV_NAMES if (v := os.environ.get(n, "").strip())]


def redact_text(text: str) -> str:
    """Remove known secret values, bearer tokens and signed-URL signatures from text."""
    for value in _secret_values():
        text = text.replace(value, REDACTED)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", rf"\1{REDACTED}", text)
    return _SIGNED_URL_PARAMS.sub(rf"\1{REDACTED}", text)


_URL_KEYS = {"download_link", "map_download_url", "stats_download_url", "report_url", "signed_url"}


def strip_query(url: str) -> str:
    """Drop the query string of a (possibly signed) URL."""
    return url.split("?", 1)[0] + "?" + REDACTED if "?" in url else url


def redact(obj: Any) -> Any:
    """Recursively redact secrets from a JSON-like structure."""
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in _SENSITIVE_HEADERS:
                out[k] = REDACTED
            elif str(k).lower() in _URL_KEYS and isinstance(v, str):
                out[k] = strip_query(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def save_json(path: Path, obj: Any) -> None:
    """Write redacted JSON. Parent directories are created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(obj), indent=2, ensure_ascii=False), encoding="utf-8")
