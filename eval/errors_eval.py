"""Error-handling eval (c). Sends requests that should be rejected, records how they are rejected.

Every case is expected to be free (rejected requests are not charged). Cases marked `may_charge`
could be accepted; their worst-case cost is reserved in the ledger first, and the real cost is
checked by comparing credit_balance before and after the run.

Output: results/raw/errors/errors_<timestamp>.json and .csv (never overwritten).
"Actionable" is a fixed rule, applied by code: the message names the offending field or states the
accepted values/limit. The raw message is kept next to the label for human review.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from probe import BASE, MCP_URL, RAW, ROOT, get_secret, guard_spend, ledger_add, load_env, now_iso
from probe import redact as redact_obj

PHX = {"latitude": 33.4484, "longitude": -112.074}
GOOD_DT = {"start_date": "2026-11-17", "start_time": "14:00", "filter_type": 1}
FIELD_WORDS = re.compile(
    r"(?i)latitude|longitude|temperature|date_time|start_date|end_date|filter_type|start_time|"
    r"polygon|api.?key|authori[sz]ation|field|must|between|format|YYYY|range|limit|supported|"
    r"earliest|future|required|missing|not allowed|outside|United States"
)

REST_CASES: list[dict[str, Any]] = [
    {
        "id": "rest_missing_key",
        "method": "POST",
        "path": "/env_params",
        "key": None,
        "json": {**PHX, "temperature": 20, "date_time": GOOD_DT},
    },
    {
        "id": "rest_invalid_key",
        "method": "POST",
        "path": "/env_params",
        "key": "invalid_key_for_test",
        "json": {**PHX, "temperature": 20, "date_time": GOOD_DT},
    },
    {
        "id": "rest_malformed_json",
        "method": "POST",
        "path": "/env_params",
        "raw": '{"latitude": 33.4,',
    },
    {
        "id": "rest_missing_temperature",
        "method": "POST",
        "path": "/env_params",
        "json": {**PHX, "date_time": GOOD_DT},
    },
    {
        "id": "rest_latitude_out_of_range",
        "method": "POST",
        "path": "/env_params",
        "json": {"latitude": 200, "longitude": -112.074, "temperature": 20, "date_time": GOOD_DT},
    },
    {
        "id": "rest_temperature_as_string",
        "method": "POST",
        "path": "/env_params",
        "json": {**PHX, "temperature": "hot", "date_time": GOOD_DT},
    },
    {
        "id": "rest_bad_filter_type",
        "method": "POST",
        "path": "/env_params",
        "json": {**PHX, "temperature": 20, "date_time": {**GOOD_DT, "filter_type": 7}},
    },
    {
        "id": "rest_bad_date_format",
        "method": "POST",
        "path": "/env_params",
        "json": {**PHX, "temperature": 20, "date_time": {**GOOD_DT, "start_date": "17/11/2026"}},
    },
    {
        "id": "rest_missing_start_time_for_single_hour",
        "method": "POST",
        "path": "/env_params",
        "json": {
            **PHX,
            "temperature": 20,
            "date_time": {"start_date": "2026-11-17", "filter_type": 1},
        },
    },
    {
        "id": "rest_future_date",
        "method": "POST",
        "path": "/env_params",
        "json": {**PHX, "temperature": 20, "date_time": {**GOOD_DT, "start_date": "2027-01-01"}},
    },
    {
        "id": "rest_non_us_london",
        "method": "POST",
        "path": "/env_params",
        "may_charge": 2900,
        "json": {
            "latitude": 51.5074,
            "longitude": -0.1278,
            "temperature": 20,
            "date_time": GOOD_DT,
        },
    },
    {
        "id": "rest_huge_range_days",
        "method": "POST",
        "path": "/env_params",
        "may_charge": 2900,
        "json": {
            **PHX,
            "temperature": 20,
            "date_time": {"start_date": "2021-01-01", "end_date": "2026-11-01", "filter_type": 4},
        },
    },
    {
        "id": "rest_heatmap_huge_polygon",
        "method": "POST",
        "path": "/heatmap",
        "may_charge": 4220,
        "json": {
            "polygon_aoi": {
                "type": "Polygon",
                "coordinates": [
                    [[-113.0, 33.0], [-111.0, 33.0], [-111.0, 34.0], [-113.0, 34.0], [-113.0, 33.0]]
                ],
            },
            "date_time": GOOD_DT,
            "granularity": 100,
        },
    },
    {"id": "rest_unknown_endpoint", "method": "POST", "path": "/not_an_endpoint", "json": {}},
    {"id": "rest_wrong_method", "method": "GET", "path": "/env_params"},
    {
        "id": "rest_status_unknown_activity",
        "method": "GET",
        "path": "/status/00000000-0000-0000-0000-000000000000",
    },
    {"id": "rest_status_garbage_id", "method": "GET", "path": "/status/not-a-uuid"},
]

MCP_CASES: list[dict[str, Any]] = [
    {"id": "mcp_missing_temperature", "tool": "env_params", "args": {**PHX, "date_time": GOOD_DT}},
    {
        "id": "mcp_latitude_out_of_range",
        "tool": "env_params",
        "args": {"latitude": 200, "longitude": -112.074, "temperature": 20, "date_time": GOOD_DT},
    },
    {
        "id": "mcp_bad_filter_type",
        "tool": "env_params",
        "args": {**PHX, "temperature": 20, "date_time": {**GOOD_DT, "filter_type": 7}},
    },
    {
        "id": "mcp_bad_date_format",
        "tool": "env_params",
        "args": {**PHX, "temperature": 20, "date_time": {**GOOD_DT, "start_date": "17/11/2026"}},
    },
    {
        "id": "mcp_before_2021",
        "tool": "env_params",
        "args": {**PHX, "temperature": 20, "date_time": {**GOOD_DT, "start_date": "2020-06-15"}},
    },
    {
        "id": "mcp_non_us_london",
        "tool": "env_params",
        "may_charge": 2900,
        "args": {
            "latitude": 51.5074,
            "longitude": -0.1278,
            "temperature": 20,
            "date_time": GOOD_DT,
        },
    },
    {"id": "mcp_unknown_tool", "tool": "not_a_tool", "args": {}},
    {
        "id": "mcp_heatmap_read_unknown_id",
        "tool": "heatmap_read",
        "args": {"activity_id": "00000000-0000-0000-0000-000000000000"},
    },
    {
        "id": "mcp_routing_same_origin_destination",
        "tool": "temperature_routing",
        "may_charge": 3500,
        "args": {
            "origin": {"lat": 33.4484, "lon": -112.074},
            "destination": {"lat": 33.4484, "lon": -112.074},
            "departure_time": "2026-11-17T21:00:00Z",
        },
    },
]


def actionable(message: str | None) -> bool:
    return bool(message and FIELD_WORDS.search(message))


def body_shape(obj: Any) -> str:
    if isinstance(obj, dict):
        return ",".join(sorted(obj.keys()))
    return type(obj).__name__


def message_of(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return str(obj)[:300] if obj else None
    for key in ("message", "detail", "error"):
        v = obj.get(key)
        if isinstance(v, str):
            return v
        if isinstance(v, dict) and isinstance(v.get("message"), str):
            return str(v["message"])
        if isinstance(v, list) and v:
            return json.dumps(v)[:300]
    details = obj.get("details")
    if isinstance(details, dict):
        return str(details.get("message"))
    return None


def run_rest(case: dict[str, Any]) -> dict[str, Any]:
    if case.get("may_charge"):
        guard_spend(case["may_charge"])
    headers = {"Content-Type": "application/json"}
    key = case.get("key", "REAL")
    if key == "REAL":
        headers["api-key"] = get_secret("TEMP_API_KEY")
    elif key:
        headers["api-key"] = key
    started = time.perf_counter()
    content = case.get("raw")
    resp = httpx.request(
        case["method"],
        BASE + case["path"],
        headers=headers,
        json=case.get("json") if content is None else None,
        content=content,
        timeout=60,
    )
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:500]
    msg = message_of(body)
    accepted = (
        resp.status_code == 200 and isinstance(body, dict) and "activity_id" in json.dumps(body)
    )
    return {
        "id": case["id"],
        "surface": "rest",
        "request": {
            "method": case["method"],
            "path": case["path"],
            "body": case.get("json", case.get("raw")),
        },
        "status_code": resp.status_code,
        "elapsed_ms": elapsed,
        "body_shape": body_shape(body),
        "code": body.get("code") if isinstance(body, dict) else None,
        "field": body.get("field") if isinstance(body, dict) else None,
        "message": msg,
        "actionable": actionable(msg),
        "accepted": accepted,
        "may_charge": case.get("may_charge", 0),
        "body": body,
    }


async def run_mcp(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    rows = []
    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=120) as http_client,
        streamable_http_client(MCP_URL, http_client=http_client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        for case in cases:
            if case.get("may_charge"):
                guard_spend(case["may_charge"])
            started = time.perf_counter()
            row: dict[str, Any] = {
                "id": case["id"],
                "surface": "mcp",
                "request": {"tool": case["tool"], "arguments": case["args"]},
                "may_charge": case.get("may_charge", 0),
            }
            try:
                res = await session.call_tool(case["tool"], case["args"])
                text = res.content[0].text if res.content else None
                try:
                    env: Any = json.loads(text) if text else res.structured_content
                except json.JSONDecodeError:
                    env = text
                err = (env.get("error") or {}) if isinstance(env, dict) else {}
                msg = err.get("message") if isinstance(err, dict) and err else message_of(env)
                row.update(
                    is_error=res.is_error,
                    body_shape=body_shape(env),
                    code=err.get("code") if isinstance(err, dict) else None,
                    field=err.get("field") if isinstance(err, dict) else None,
                    category=err.get("category") if isinstance(err, dict) else None,
                    retriable=err.get("retriable") if isinstance(err, dict) else None,
                    message=msg,
                    actionable=actionable(msg),
                    accepted=isinstance(env, dict) and env.get("ok") is True,
                    cost_credits=(
                        (env.get("trace") or {}).get("cost_credits")
                        if isinstance(env, dict)
                        else None
                    ),
                    body=env,
                )
            except Exception as exc:  # noqa: BLE001 - the protocol-level error is the result
                row.update(
                    is_error=True,
                    body_shape="exception",
                    exception=type(exc).__name__,
                    message=str(exc)[:300],
                    actionable=actionable(str(exc)),
                    accepted=False,
                )
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            rows.append(row)
    return rows


async def balance() -> int:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {get_secret('TEMP_API_KEY')}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=120) as http_client,
        streamable_http_client(MCP_URL, http_client=http_client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        res = await session.call_tool("credit_balance", {})
        return int(json.loads(res.content[0].text)["result"]["value"]["available_credits"])


def main() -> None:
    load_env(ROOT / ".env")
    before = asyncio.run(balance())
    rows = [run_rest(c) for c in REST_CASES]
    rows += asyncio.run(run_mcp(MCP_CASES))
    time.sleep(60)  # give any accepted activity time to complete before reading the balance
    after = asyncio.run(balance())
    charged = before - after
    if charged:
        ledger_add("errors_eval", charged, "charge observed during error eval (accepted cases)")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    outdir = RAW / "errors"
    outdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": now_iso(),
        "balance_before": before,
        "balance_after": after,
        "credits_charged": charged,
        "actionable_rule": FIELD_WORDS.pattern,
        "cases": rows,
    }
    with (outdir / f"errors_{stamp}.json").open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(redact_obj(payload), indent=2, ensure_ascii=False))
    cols = [
        "id",
        "surface",
        "status_code",
        "is_error",
        "accepted",
        "code",
        "field",
        "category",
        "retriable",
        "actionable",
        "elapsed_ms",
        "body_shape",
        "message",
    ]
    with (outdir / f"errors_{stamp}.csv").open("x", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    for r in rows:
        print(
            f"{r['id']:<40} {str(r.get('status_code', 'mcp')):<4} accepted={r['accepted']!s:<5} "
            f"actionable={r['actionable']!s:<5} {(r.get('message') or '')[:90]}"
        )
    print(f"balance {before} -> {after} (charged {charged}); saved errors_{stamp}.json/.csv")


if __name__ == "__main__":
    main()
