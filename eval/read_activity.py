"""Free read of an existing activity via an MCP *_read tool. Saves a timestamped raw file.

Usage: python eval/read_activity.py heatmap_read <activity_id> <name>
"""

from __future__ import annotations

import asyncio
import json
import sys

from heatwise.mcp_tools import connect
from heatwise.settings import Settings
from probe import ROOT, load_env, now_iso, write_raw

READ_TOOLS = {"heatmap_read", "temperature_routing_read"}


async def main(tool: str, activity_id: str, name: str) -> None:
    if tool not in READ_TOOLS:
        raise SystemExit(f"{tool} is not a read tool")
    s = Settings.from_env(require_llm=False)
    async with connect(s.fortyguard_key) as session:
        res = await session.call_tool(tool, {"activity_id": activity_id})
        env = json.loads(res.content[0].text)
    saved = write_raw(
        name, {"captured_at": now_iso(), "tool": tool, "activity_id": activity_id, "envelope": env}
    )
    value = (env.get("result") or {}).get("value") or {}
    print(
        "ok",
        env.get("ok"),
        "| status",
        value.get("status"),
        "| mean C",
        value.get("mean_temperature_celsius"),
        "| cost",
        (env.get("trace") or {}).get("cost_credits"),
        "|",
        saved.name,
        flush=True,
    )


if __name__ == "__main__":
    load_env(ROOT / ".env")
    asyncio.run(main(*sys.argv[1:4]))
