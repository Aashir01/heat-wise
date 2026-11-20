"""Latency eval (a), free calls only. Raw samples are saved; percentiles are computed in the report.

Series (N per series from argv, default 30):
- rest_status_completed: GET /v1/status/{completed activity}, one HTTP client (first call = cold).
- rest_rejected_422: POST /v1/env_params without temperature (rejected, free).
- mcp_credit_balance_warm / mcp_list_tools_warm: one MCP session, repeated calls.
Cold MCP connect times come from eval/network_check.py (fresh session per round).
Paid-call latency is not measured here (cost); observed paid samples are listed in the report.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from probe import BASE, MCP_URL, RAW, ROOT, get_secret, load_env, now_iso
from stats import summary

COMPLETED_ACTIVITY = "fc573deb-f593-41b8-8625-bbb3cb3d24a5"  # REST env_params, completed


def rest_series(n: int) -> dict[str, list[dict[str, Any]]]:
    key = get_secret("TEMP_API_KEY")
    out: dict[str, list[dict[str, Any]]] = {"rest_status_completed": [], "rest_rejected_422": []}
    with httpx.Client(base_url=BASE, headers={"api-key": key}, timeout=60) as client:
        for i in range(n):
            t = time.perf_counter()
            r = client.get(f"/status/{COMPLETED_ACTIVITY}")
            out["rest_status_completed"].append(
                {
                    "i": i,
                    "ms": round((time.perf_counter() - t) * 1000, 1),
                    "status": r.status_code,
                    "remaining": r.headers.get("x-ratelimit-remaining"),
                }
            )
        for i in range(n):
            t = time.perf_counter()
            r = client.post(
                "/env_params",
                json={
                    "latitude": 33.4484,
                    "longitude": -112.074,
                    "date_time": {
                        "start_date": "2026-11-17",
                        "start_time": "14:00",
                        "filter_type": 1,
                    },
                },
            )
            out["rest_rejected_422"].append(
                {
                    "i": i,
                    "ms": round((time.perf_counter() - t) * 1000, 1),
                    "status": r.status_code,
                    "remaining": r.headers.get("x-ratelimit-remaining"),
                }
            )
    return out


async def mcp_series(n: int) -> dict[str, list[dict[str, Any]]]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    out: dict[str, list[dict[str, Any]]] = {
        "mcp_credit_balance_warm": [],
        "mcp_list_tools_warm": [],
    }
    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=120) as http_client,
        streamable_http_client(MCP_URL, http_client=http_client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        for i in range(n):
            t = time.perf_counter()
            res = await session.call_tool("credit_balance", {})
            ms = round((time.perf_counter() - t) * 1000, 1)
            trace = json.loads(res.content[0].text).get("trace", {}) if res.content else {}
            out["mcp_credit_balance_warm"].append(
                {
                    "i": i,
                    "ms": ms,
                    "is_error": res.is_error,
                    "server_latency_ms": trace.get("latency_ms"),
                }
            )
        for i in range(n):
            t = time.perf_counter()
            tools = await session.list_tools()
            out["mcp_list_tools_warm"].append(
                {
                    "i": i,
                    "ms": round((time.perf_counter() - t) * 1000, 1),
                    "n_tools": len(tools.tools),
                }
            )
    return out


def main(n: int) -> None:
    load_env(ROOT / ".env")
    series = rest_series(n)
    series.update(asyncio.run(mcp_series(n)))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    outdir = RAW / "latency"
    outdir.mkdir(parents=True, exist_ok=True)
    payload = {"captured_at": now_iso(), "n_per_series": n, "series": series}
    with (outdir / f"latency_{stamp}.json").open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2))
    with (outdir / f"latency_{stamp}.csv").open("x", encoding="utf-8") as fh:
        fh.write("series,i,ms\n")
        for name, rows in series.items():
            for r in rows:
                fh.write(f"{name},{r['i']},{r['ms']}\n")
    for name, rows in series.items():
        print(name, summary([r["ms"] for r in rows]))
    print("saved", f"latency_{stamp}.json/.csv")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
