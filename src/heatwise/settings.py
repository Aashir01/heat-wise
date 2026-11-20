"""Runtime configuration. Secrets come only from the environment (.env), never from code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .secrets import get_secret, load_env

# Fixed for the whole evaluation: first shortlisted model that passed eval/model_smoke.py.
DEFAULT_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"
NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
MCP_URL = "https://tos-mcp.app.fortyguard.com/mcp"
DEFAULT_CREDIT_CAP = 150_000


@dataclass(frozen=True)
class Settings:
    fortyguard_key: str
    nebius_key: str
    model: str
    nebius_base_url: str
    ledger_path: Path
    credit_cap: int

    @classmethod
    def from_env(cls, *, require_llm: bool = True) -> Settings:
        """Raises KeyError (with a clear message) when a required key is missing."""
        load_env(Path.cwd() / ".env")
        return cls(
            fortyguard_key=get_secret("TEMP_API_KEY"),
            nebius_key=get_secret("NEBIUS_API_KEY")
            if require_llm
            else os.environ.get("NEBIUS_API_KEY", ""),
            model=os.environ.get("NEBIUS_MODEL", "").strip() or DEFAULT_MODEL,
            nebius_base_url=os.environ.get("NEBIUS_BASE_URL", "").strip() or NEBIUS_BASE_URL,
            ledger_path=Path(os.environ.get("HEATWISE_LEDGER", "results/spend_ledger.jsonl")),
            credit_cap=int(os.environ.get("HEATWISE_CREDIT_CAP", DEFAULT_CREDIT_CAP)),
        )
