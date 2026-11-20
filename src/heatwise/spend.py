"""Credit spend ledger and guard. Append-only JSONL; the cap covers the whole evaluation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

# Observed premium prices (see docs/DISCOVERY.md). Only completed calls are charged.
PAID_TOOL_COSTS: dict[str, int] = {
    "env_params": 2900,
    "heatmap": 4220,
    "temperature_routing": 3500,
}


class SpendCapExceeded(Exception):
    def __init__(self, used: int, estimate: int, cap: int) -> None:
        super().__init__(
            f"Credit cap reached: {used} used + {estimate} for this call would exceed {cap}."
        )
        self.used, self.estimate, self.cap = used, estimate, cap

    @property
    def user_message(self) -> str:
        return f"{self} No call was made."


class SpendLedger:
    def __init__(self, path: Path, cap: int) -> None:
        self.path = path
        self.cap = cap

    def total(self) -> int:
        if not self.path.exists():
            return 0
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return sum(int(json.loads(line)["credits"]) for line in lines if line.strip())

    def guard(self, estimate: int) -> None:
        used = self.total()
        if used + estimate > self.cap:
            raise SpendCapExceeded(used, estimate, self.cap)

    def record(self, tool: str, credits: int, note: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "tool": tool,
            "credits": credits,
            "note": note,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
