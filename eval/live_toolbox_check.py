"""Free live check: connect to the MCP, build the toolbox, call credit_balance (costs nothing)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from heatwise.mcp_tools import McpToolbox, connect, load_blocked
from heatwise.settings import Settings
from heatwise.spend import SpendLedger


async def main() -> None:
    s = Settings.from_env(require_llm=False)
    ledger = SpendLedger(s.ledger_path, s.credit_cap)
    async with connect(s.fortyguard_key) as session:
        box = await McpToolbox.create(
            session, ledger=ledger, blocked=load_blocked(Path("results/access.json"))
        )
        print("exposed:", sorted(box.specs), flush=True)
        res = await box.call("credit_balance", {})
        print("credit_balance ok:", res.ok, res.value, "| plan:", res.plan_type, flush=True)
        print("tool schema chars sent to LLM:", len(json.dumps(box.openai_tools())), flush=True)
    print("ledger total:", ledger.total(), "cap:", ledger.cap, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
