"""Free diagnostic: does an MCP session survive being idle before a call? (credit_balance only)

Usage: python eval/idle_session_test.py 0 15 45
Each number is an idle time in seconds between initialize() and the call. A fresh session per case.
Results are saved to a unique timestamped file in results/raw/.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

from probe import MCP_URL, get_secret, load_env, now_iso, write_raw
from probe import ROOT as ROOT


async def one_case(idle_s: float) -> dict[str, Any]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    case: dict[str, Any] = {"idle_seconds": idle_s}
    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    try:
        async with (
            httpx2.AsyncClient(headers=headers, timeout=120) as http_client,
            streamable_http_client(MCP_URL, http_client=http_client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            await asyncio.sleep(idle_s)
            started = time.perf_counter()
            result = await session.call_tool("credit_balance", {})
            case["outcome"] = "ok"
            case["is_error"] = result.is_error
            case["call_seconds"] = round(time.perf_counter() - started, 2)
    except BaseException as exc:  # noqa: BLE001 - record anything, including exception groups
        leaves: list[BaseException] = []

        def walk(e: BaseException) -> None:
            subs = getattr(e, "exceptions", None)
            if subs:
                for s in subs:
                    walk(s)
            else:
                leaves.append(e)

        walk(exc)
        case["outcome"] = "failed"
        case["errors"] = [f"{type(x).__name__}: {str(x)[:200]}" for x in leaves]
    return case


async def main(idles: list[float]) -> None:
    cases = []
    for idle in idles:
        case = await one_case(idle)
        print(case, flush=True)
        cases.append(case)
    saved = write_raw("mcp_idle_session_test", {"captured_at": now_iso(), "cases": cases})
    print("saved", saved.name)


if __name__ == "__main__":
    load_env(ROOT / ".env")
    asyncio.run(main([float(x) for x in sys.argv[1:]] or [0, 15]))
