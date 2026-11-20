"""Free network/MCP stability check: N rounds of (connect, list_tools, credit_balance).

Each round opens a fresh session, so connect time is a cold measurement. All calls are free.
Usage: python eval/network_check.py 20
Saves one timestamped JSON with every round to results/raw/.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import time
from typing import Any

from probe import MCP_URL, ROOT, get_secret, load_env, now_iso, write_raw


def leaves(exc: BaseException) -> list[BaseException]:
    subs = getattr(exc, "exceptions", None)
    return [x for s in subs for x in leaves(s)] if subs else [exc]


async def one_round(i: int) -> dict[str, Any]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    row: dict[str, Any] = {"round": i, "at": now_iso()}
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo("tos-mcp.app.fortyguard.com", 443)
        row["dns_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    except OSError as exc:
        row.update(outcome="failed", stage="dns", errors=[f"{type(exc).__name__}: {exc}"])
        return row
    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    stage = "connect"
    try:
        t = time.perf_counter()
        async with (
            httpx2.AsyncClient(headers=headers, timeout=60) as http_client,
            streamable_http_client(MCP_URL, http_client=http_client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            row["connect_ms"] = round((time.perf_counter() - t) * 1000, 1)
            stage = "list_tools"
            t = time.perf_counter()
            tools = await session.list_tools()
            row["list_tools_ms"] = round((time.perf_counter() - t) * 1000, 1)
            row["n_tools"] = len(tools.tools)
            stage = "credit_balance"
            t = time.perf_counter()
            res = await session.call_tool("credit_balance", {})
            row["credit_balance_ms"] = round((time.perf_counter() - t) * 1000, 1)
            row["credit_balance_is_error"] = res.is_error
            row["available_credits"] = (res.structured_content or {}).get("available_credits")
            row["outcome"] = "ok"
    except BaseException as exc:  # noqa: BLE001 - record everything
        row.update(
            outcome="failed",
            stage=stage,
            errors=[f"{type(x).__name__}: {str(x)[:200]}" for x in leaves(exc)],
        )
    row["round_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return row


async def main(n: int) -> None:
    rows = []
    for i in range(1, n + 1):
        row = await one_round(i)
        print(
            row["round"],
            row["outcome"],
            {k: row.get(k) for k in ("connect_ms", "list_tools_ms", "credit_balance_ms")},
            row.get("errors", ""),
            flush=True,
        )
        rows.append(row)
    ok = sum(r["outcome"] == "ok" for r in rows)
    saved = write_raw(
        "network_check", {"captured_at": now_iso(), "rounds": rows, "ok": ok, "total": n}
    )
    print(f"{ok}/{n} rounds ok -> results/raw/{saved.name}")


if __name__ == "__main__":
    load_env(ROOT / ".env")
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 20))
