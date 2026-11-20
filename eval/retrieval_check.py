"""Can activity results be read back through read-only calls, across REST and MCP?

Uses only activity IDs captured earlier. Reads: REST GET /status/{id}; MCP heatmap_read.
Balance is read before and after, so any charge would be visible.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from probe import MCP_URL, ROOT, get_secret, load_env, now_iso, rest, write_raw

MCP_ENV_ACTIVITY = "b3ff08c0-06f6-42c7-af7b-73773381aa4b"  # MCP env_params, 10:55 UTC (completed)
REST_HEATMAP_ACTIVITY = "0be78e2c-aaac-438f-bd55-d0dc1fc3ba9d"  # REST heatmap, 10:23 UTC


async def mcp_calls(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    out = []
    async with (
        httpx2.AsyncClient(headers=headers, timeout=120) as http_client,
        streamable_http_client(MCP_URL, http_client=http_client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        for name, args in calls:
            t = time.perf_counter()
            res = await session.call_tool(name, args)
            text = res.content[0].text if res.content else None
            out.append(
                {
                    "tool": name,
                    "arguments": args,
                    "is_error": res.is_error,
                    "elapsed_s": round(time.perf_counter() - t, 2),
                    "envelope": json.loads(text) if text else res.structured_content,
                }
            )
    return out


def balance(rows: list[dict[str, Any]]) -> int | None:
    for r in rows:
        if r["tool"] == "credit_balance":
            return int(
                (r["envelope"].get("result") or {}).get("value", {}).get("available_credits")
            )
    return None


async def main() -> None:
    before = await mcp_calls([("credit_balance", {})])
    r1 = rest("GET", f"/status/{MCP_ENV_ACTIVITY}", "retrieval_rest_status_of_mcp_env")
    reads = await mcp_calls(
        [
            ("heatmap_read", {"activity_id": REST_HEATMAP_ACTIVITY}),
            ("heatmap_read", {"activity_id": MCP_ENV_ACTIVITY}),
        ]
    )
    after = await mcp_calls([("credit_balance", {})])
    summary = {
        "captured_at": now_iso(),
        "balance_before": balance(before),
        "balance_after": balance(after),
        "rest_status_of_mcp_env_activity": {
            "http_status": r1.status_code,
            "status": ((r1.json() or {}).get("data") or {}).get("status")
            if r1.status_code == 200
            else None,
        },
        "mcp_reads": reads,
    }
    saved = write_raw("retrieval_check", summary)
    print("balance", summary["balance_before"], "->", summary["balance_after"])
    print("REST /status of MCP env activity:", summary["rest_status_of_mcp_env_activity"])
    for r in reads:
        env = r["envelope"] or {}
        print(
            r["tool"],
            r["arguments"],
            "is_error",
            r["is_error"],
            "ok",
            env.get("ok"),
            "| error:",
            (env.get("error") or {}).get("code"),
            (env.get("error") or {}).get("message"),
            "| value keys:",
            list(((env.get("result") or {}).get("value") or {}).keys())[:8],
            "| cost:",
            (env.get("trace") or {}).get("cost_credits"),
        )
    print("saved", saved.name)


if __name__ == "__main__":
    load_env(ROOT / ".env")
    asyncio.run(main())
