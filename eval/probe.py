"""Task 1 probe: one subcommand per live call. Every request/response is saved redacted.

Usage: python eval/probe.py <credits|mcp|nebius|env|heatmap|routing|forecast> [args]
Blocked calls (403/plan errors) are recorded in results/access.json and never retried.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from heatwise.secrets import get_secret, load_env, redact, save_json

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "raw"
ACCESS = ROOT / "results" / "access.json"
BASE = "https://api.fortyguard.com/v1"
MCP_URL = "https://tos-mcp.app.fortyguard.com/mcp"
NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1"

PHOENIX = (33.4484, -112.074)
REF_TEMP_C = 35  # value from the docs example; see FRICTION_LOG.md


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


LEDGER = ROOT / "results" / "spend_ledger.jsonl"
CAP_CREDITS = 150_000  # total evaluation cap, including the 10,020 already spent


def write_raw(name: str, obj: Any) -> Path:
    """Save redacted JSON to a unique timestamped file. Never overwrites (exclusive create)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    path = RAW / f"{name}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(redact(obj), indent=2, ensure_ascii=False))
    return path


def ledger_total() -> int:
    if not LEDGER.exists():
        return 0
    return sum(json.loads(line)["credits"] for line in LEDGER.read_text().splitlines() if line)


def ledger_add(tool: str, credits: int, note: str) -> None:
    """Append-only record of credits spent (or estimated before a call)."""
    with LEDGER.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({"at": now_iso(), "tool": tool, "credits": credits, "note": note}) + "\n"
        )


def guard_spend(estimate: int) -> None:
    total = ledger_total()
    if total + estimate > CAP_CREDITS:
        raise SystemExit(
            f"Spend cap: {total} used + {estimate} would exceed {CAP_CREDITS}. Stopping."
        )


def update_access(tool: str, **fields: Any) -> None:
    data: dict[str, Any] = json.loads(ACCESS.read_text()) if ACCESS.exists() else {}
    data[tool] = {**fields, "checked_at": now_iso()}
    save_json(ACCESS, data)


def is_blocked(tool: str) -> bool:
    if not ACCESS.exists():
        return False
    return json.loads(ACCESS.read_text()).get(tool, {}).get("status") == "blocked"


def rest(method: str, path: str, name: str, body: dict[str, Any] | None = None) -> httpx.Response:
    """Single REST call, saved redacted to results/raw/<name>.json. No retries."""
    headers = {"api-key": get_secret("TEMP_API_KEY"), "Content-Type": "application/json"}
    started = time.perf_counter()
    resp = httpx.request(method, BASE + path, headers=headers, json=body, timeout=60)
    elapsed = time.perf_counter() - started
    try:
        parsed: Any = resp.json()
    except ValueError:
        parsed = resp.text[:2000]
    saved = write_raw(
        name,
        {
            "captured_at": now_iso(),
            "request": {"method": method, "url": BASE + path, "headers": headers, "body": body},
            "response": {
                "status_code": resp.status_code,
                "elapsed_seconds": round(elapsed, 3),
                "headers": dict(resp.headers),
                "body": parsed,
            },
        },
    )
    print(f"{name}: HTTP {resp.status_code} in {elapsed:.2f}s -> results/raw/{saved.name}")
    return resp


def poll_status(activity_id: str, name: str, max_polls: int = 40, every: float = 5.0) -> Any:
    last: Any = None
    for i in range(max_polls):
        resp = rest("GET", f"/status/{activity_id}", f"{name}_status_poll{i + 1:02d}")
        if resp.status_code == 404 and i < 3:
            time.sleep(every)
            continue
        if resp.status_code != 200:
            return resp
        last = resp.json()
        status = str((last.get("data") or {}).get("status", "")).lower()
        if status in {"completed", "failed", "succeeded", "error"}:
            return resp
        time.sleep(every)
    print("polling bound reached")
    return last


def recent_date(days_back: int = 3) -> str:
    return (datetime.now(UTC) - timedelta(days=days_back)).strftime("%Y-%m-%d")


def cmd_credits(path: str) -> None:
    resp = rest("GET", path, "credits_start")
    update_access(
        "credits",
        status="ok" if resp.status_code == 200 else "error",
        status_code=resp.status_code,
        path_tried=path,
    )


def cmd_env(date: str | None, hour: str, name: str) -> None:
    if is_blocked("env_params"):
        print("env_params is blocked; not retrying")
        return
    body = {
        "latitude": PHOENIX[0],
        "longitude": PHOENIX[1],
        "temperature": REF_TEMP_C,
        "date_time": {"start_date": date or recent_date(), "start_time": hour, "filter_type": 1},
    }
    guard_spend(2900)
    resp = rest("POST", "/env_params", name, body)
    is_forecast = "forecast" in name or "datebound" in name
    if resp.status_code in (401, 403):
        update_access("env_params", status="blocked", status_code=resp.status_code)
        return
    if resp.status_code >= 400:
        if not is_forecast:
            update_access("env_params", status="error", status_code=resp.status_code)
        return
    data = resp.json().get("data") or {}
    if not is_forecast:
        update_access("env_params", status="ok", status_code=resp.status_code)
    if data.get("activity_id"):
        final = poll_status(data["activity_id"], name)
        print("final poll status:", getattr(final, "status_code", final))


def cmd_heatmap() -> None:
    if is_blocked("heatmap"):
        print("heatmap is blocked; not retrying")
        return
    lat, lon, d = PHOENIX[0], PHOENIX[1], 0.0045  # ~0.5 km half-side => ~1 km2
    ring = [
        [lon - d, lat - d],
        [lon + d, lat - d],
        [lon + d, lat + d],
        [lon - d, lat + d],
        [lon - d, lat - d],
    ]
    body = {
        "polygon_aoi": {"type": "Polygon", "coordinates": [ring]},
        "date_time": {"start_date": recent_date(), "start_time": "14:00", "filter_type": 1},
        "granularity": 100,
    }
    resp = rest("POST", "/heatmap", "heatmap_submit", body)
    if resp.status_code in (401, 403):
        update_access("heatmap", status="blocked", status_code=resp.status_code)
        return
    if resp.status_code >= 400:
        update_access("heatmap", status="error", status_code=resp.status_code)
        return
    update_access("heatmap", status="ok", status_code=resp.status_code)
    activity_id = (resp.json().get("data") or {}).get("activity_id")
    if activity_id:
        final = poll_status(activity_id, "heatmap", max_polls=30)
        print("final poll status:", getattr(final, "status_code", final))


def cmd_routing(departure: str | None = None, name: str = "routing_submit") -> None:
    if is_blocked("routing"):
        print("routing is blocked; not retrying")
        return
    body = {
        "origin": {"lat": 33.4484, "lon": -112.074},
        "destination": {"lat": 33.4255, "lon": -111.94},
        "departure_time": departure
        or (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z"),
    }
    guard_spend(3500)
    resp = rest("POST", "/routing", name, body)
    if resp.status_code in (401, 403):
        update_access("routing", status="blocked", status_code=resp.status_code)
    elif resp.status_code >= 400:
        update_access("routing", status="error", status_code=resp.status_code)
    else:
        update_access("routing", status="ok", status_code=resp.status_code)
        activity_id = (resp.json().get("data") or {}).get("activity_id")
        if activity_id:
            poll_status(activity_id, name, max_polls=30)


async def cmd_mcp() -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http_client:
        async with streamable_http_client(MCP_URL, http_client=http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                write_raw(
                    "mcp_tools",
                    {
                        "captured_at": now_iso(),
                        "server": init.server_info.model_dump(),
                        "protocol_version": init.protocol_version,
                        "tools": [t.model_dump() for t in tools.tools],
                    },
                )
                print("MCP tools:", [t.name for t in tools.tools])


FREE_MCP_TOOLS = {"credit_balance"}  # described as free and read-only by the server


async def cmd_mcp_call(tool: str, name: str) -> None:
    """Call a FREE MCP tool with no arguments. Paid tools are refused here on purpose."""
    if tool not in FREE_MCP_TOOLS:
        raise SystemExit(f"{tool} is not whitelisted as free; refusing")
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as http_client:
        async with streamable_http_client(MCP_URL, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                started = time.perf_counter()
                result = await session.call_tool(tool, {})
                elapsed = time.perf_counter() - started
                payload = result.model_dump()
                saved = write_raw(
                    name,
                    {
                        "captured_at": now_iso(),
                        "tool": tool,
                        "elapsed_seconds": round(elapsed, 3),
                        "result": payload,
                    },
                )
                print(
                    f"{tool}: isError={result.is_error} in {elapsed:.2f}s -> results/raw/{saved.name}"
                )


def cmd_report(temperature: float) -> None:
    """One heat_intelligence report (8,600 credits). Downloads the PDF in memory, extracts text."""
    if is_blocked("heat_intelligence"):
        print("heat_intelligence is blocked; not retrying")
        return
    guard_spend(8600)
    body = {
        "latitude": PHOENIX[0],
        "longitude": PHOENIX[1],
        "temperature": temperature,
        "date": "2026-11-17",
        "analysis": ["environmental"],
    }
    resp = rest("POST", "/heat_intelligence", "report_submit", body)
    if resp.status_code in (401, 403):
        update_access("heat_intelligence", status="blocked", status_code=resp.status_code)
        return
    if resp.status_code >= 400:
        update_access("heat_intelligence", status="error", status_code=resp.status_code)
        return
    update_access("heat_intelligence", status="ok", status_code=resp.status_code)
    activity_id = (resp.json().get("data") or {}).get("activity_id")
    final = poll_status(activity_id, "report", max_polls=90, every=5.0)
    data = (final.json().get("data") or {}) if hasattr(final, "json") else {}
    link = (data.get("result") or {}).get("download_link")
    print("final status:", data.get("status"), "| download_link present:", bool(link))
    if not link:
        return
    pdf = httpx.get(link, timeout=120)
    print("pdf download:", pdf.status_code, len(pdf.content), "bytes")
    if pdf.status_code != 200:
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    pdf_path = RAW / f"report_environmental_{stamp}.pdf"
    with pdf_path.open("xb") as fh:
        fh.write(pdf.content)
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages = [(pg.extract_text() or "") for pg in reader.pages]
    write_raw("report_pdf_text", {"pdf": pdf_path.name, "n_pages": len(pages), "pages": pages})
    print("saved", pdf_path.name, "pages:", len(pages))


PAID_MCP_COSTS = {"env_params": 2900, "heatmap": 4220, "temperature_routing": 3500}


async def cmd_mcp_paid(tool: str, name: str, args_json: str) -> None:
    """One paid MCP call (cost from the observed premium prices), guarded by the spend cap."""
    if tool not in PAID_MCP_COSTS:
        raise SystemExit(f"{tool} is not whitelisted for paid probing")
    if args_json.startswith("@"):
        args_json = Path(args_json[1:]).read_text(encoding="utf-8")
    json.loads(args_json)  # fail before connecting if the arguments are not valid JSON
    guard_spend(PAID_MCP_COSTS[tool])
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    async with httpx2.AsyncClient(headers=headers, timeout=120) as http_client:
        async with streamable_http_client(MCP_URL, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                started = time.perf_counter()
                result = await session.call_tool(tool, json.loads(args_json))
                elapsed = time.perf_counter() - started
                saved = write_raw(
                    name,
                    {
                        "captured_at": now_iso(),
                        "tool": tool,
                        "arguments": json.loads(args_json),
                        "elapsed_seconds": round(elapsed, 3),
                        "result": result.model_dump(),
                    },
                )
                print(
                    f"{tool}: isError={result.is_error} in {elapsed:.2f}s -> results/raw/{saved.name}"
                )


def cmd_nebius() -> None:
    resp = httpx.get(
        NEBIUS_URL + "/models",
        headers={"Authorization": f"Bearer {get_secret('NEBIUS_API_KEY')}"},
        timeout=60,
    )
    print("nebius /models:", resp.status_code)
    body: Any = resp.json() if resp.status_code == 200 else resp.text[:500]
    write_raw(
        "nebius_models", {"captured_at": now_iso(), "status_code": resp.status_code, "body": body}
    )


def main() -> None:
    load_env(ROOT / ".env")
    cmd, *args = sys.argv[1:] or ["help"]
    if cmd == "credits":
        cmd_credits(args[0])
    elif cmd == "mcp":
        asyncio.run(cmd_mcp())
    elif cmd == "nebius":
        cmd_nebius()
    elif cmd == "mcp-free":
        asyncio.run(cmd_mcp_call(args[0], args[1]))
    elif cmd == "mcp-paid":
        asyncio.run(cmd_mcp_paid(args[0], args[1], args[2]))
    elif cmd == "env":
        cmd_env(None, "14:00", "env_params_phoenix_past")
    elif cmd == "forecast":
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        cmd_env(tomorrow, "14:00", "env_params_phoenix_forecast")
    elif cmd == "env-at":
        cmd_env(args[0], "14:00", args[1])
    elif cmd == "report":
        cmd_report(float(args[0]))
    elif cmd == "heatmap":
        cmd_heatmap()
    elif cmd == "routing":
        cmd_routing(args[0] if args else None, args[1] if len(args) > 1 else "routing_submit")
    else:
        print(__doc__)
    _ = redact  # keep import explicit


if __name__ == "__main__":
    main()
