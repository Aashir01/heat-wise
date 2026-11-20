"""MCP quality eval (d): natural-language questions through the planner agent, one group at a time.

Usage: python eval/mcp_questions.py <group index 0..3> [--model MODEL] [--only qid,qid]
- Section cap: 30,000 credits for all runs of this eval (ledger entries noted "mcp_questions").
- Paid calls are auto-confirmed (no user is present); the spend guard still applies.
- After the group, the credit balance is compared with the ledger.
- If a paid call failed in transit (ambiguous_failure), a free network check runs. If the network
  was fine, the eval stops (unexplained charged failure) and exits with code 3.
Outputs (never overwritten): results/raw/mcp_questions/<run>_<qid>.json per question and
results/raw/mcp_questions/<run>_group<k>.json with grades.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from heatwise.agent import PlannerAgent
from heatwise.llm import NebiusProvider
from heatwise.mcp_tools import McpToolbox, connect, load_blocked
from heatwise.models import AgentResult
from heatwise.secrets import redact
from heatwise.settings import Settings
from heatwise.spend import PAID_TOOL_COSTS, SpendLedger

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "raw" / "mcp_questions"
SECTION_CAP = 30_000
TOTAL_CAP = 150_000
NOTE = "mcp_questions"
TODAY = datetime(2026, 11, 20, tzinfo=UTC).date()


def write_x(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(redact(obj), indent=2, ensure_ascii=False, default=str))


def ledger_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def section_spent(path: Path) -> int:
    return sum(
        int(e["credits"]) for e in ledger_entries(path) if str(e.get("note", "")).startswith(NOTE)
    )


async def read_balance(key: str) -> int:
    async with connect(key) as session:
        res = await session.call_tool("credit_balance", {})
        return int(json.loads(res.content[0].text)["result"]["value"]["available_credits"])


async def network_ok(key: str, rounds: int = 3) -> tuple[bool, list[str]]:
    notes = []
    ok = 0
    for _ in range(rounds):
        try:
            socket.getaddrinfo("tos-mcp.app.fortyguard.com", 443)
            await read_balance(key)
            ok += 1
            notes.append("ok")
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            notes.append(f"{type(exc).__name__}: {str(exc)[:120]}")
        await asyncio.sleep(2)
    return ok == rounds, notes


def grade(q: dict[str, Any], result: AgentResult) -> dict[str, Any]:
    calls = result.tool_calls
    called = {c.name for c in calls}
    ok_paid = [c for c in calls if c.ok and c.name in PAID_TOOL_COSTS]
    blocked_temp = [c for c in calls if c.error == "reference_temperature_not_sourced"]
    rejected_final = [c for c in calls if c.name == "final_answer" and not c.ok]
    failures: list[str] = []
    if result.status not in q["ok_status"]:
        failures.append(f"status {result.status} not in {q['ok_status']}")
    lo, hi = q["paid"]
    if not lo <= len(ok_paid) <= hi:
        failures.append(f"{len(ok_paid)} successful paid calls, expected {lo}-{hi}")
    if q["must_call"] and not (called & set(q["must_call"])):
        failures.append(f"none of {q['must_call']} called")
    bad = {c.name for c in calls if c.ok} & set(q["must_not_call"])
    if bad:
        failures.append(f"called forbidden {sorted(bad)}")
    return {
        "pass": not failures,
        "failures": failures,
        "tools_called": [c.name for c in calls],
        "successful_paid_calls": len(ok_paid),
        "blocked_invented_temperature": len(blocked_temp),
        "rejected_final_answers": len(rejected_final),
        "failed_tool_calls": [f"{c.name}: {c.error}" for c in calls if not c.ok],
        "ambiguous_failures": [c.name for c in calls if c.error and "ambiguous_failure" in c.error],
        "declined_paid_calls": [
            c.name for c in calls if c.error and c.error.startswith("declined")
        ],
    }


async def run_question(
    settings: Settings, ledger: SpendLedger, model: str, q: dict[str, Any], *, no_paid: bool = False
) -> dict[str, Any]:
    async def confirm(tool: str, args: dict[str, Any], cost: int) -> bool:
        return not no_paid  # --no-paid: every paid call is declined (logged as 'declined')

    started = time.perf_counter()
    async with connect(settings.fortyguard_key) as session:
        box = await McpToolbox.create(
            session,
            ledger=ledger,
            confirm=confirm,
            blocked=load_blocked(ROOT / "results" / "access.json"),
        )
        box.ledger_note = f"{NOTE} {q['id']} {model}"
        llm = NebiusProvider(settings.nebius_key, model, settings.nebius_base_url)
        agent = PlannerAgent(
            llm, box, today=TODAY, max_steps=10, max_tool_calls=8, interactive=False
        )
        result = await agent.run(q["question"])
    return {
        "question": q,
        "model": model,
        "no_paid": no_paid,
        "elapsed_s": round(time.perf_counter() - started, 1),
        "result": result.model_dump(mode="json"),
        "grade": grade(q, result),
        "messages": agent.messages,
    }


async def main() -> int:
    import logging

    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    ap = argparse.ArgumentParser()
    ap.add_argument("group", type=int)
    ap.add_argument("--model", default=None)
    ap.add_argument("--only", default="")
    ap.add_argument("--no-paid", action="store_true", help="decline every paid call (free run)")
    args = ap.parse_args()
    settings = Settings.from_env()
    model = args.model or settings.model
    groups = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))["groups"]
    questions = groups[args.group]
    if args.only:
        wanted = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in wanted]

    ledger_path = settings.ledger_path
    base = SpendLedger(ledger_path, TOTAL_CAP)
    remaining_section = SECTION_CAP - section_spent(ledger_path)
    cap = min(TOTAL_CAP, base.total() + max(0, remaining_section))
    ledger = SpendLedger(ledger_path, cap)
    print(
        f"section spent {SECTION_CAP - remaining_section}/{SECTION_CAP}; ledger {base.total()}; "
        f"effective cap for this run {cap}",
        flush=True,
    )

    run = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    balance_before = await read_balance(settings.fortyguard_key)
    ledger_before = base.total()
    records = []
    stop_reason = None
    for q in questions:
        rec = await run_question(settings, ledger, model, q, no_paid=args.no_paid)
        write_x(OUT / f"{run}_{q['id']}.json", rec)
        g = rec["grade"]
        print(
            f"{q['id']:<30} {rec['result']['status']:<20} pass={g['pass']!s:<5} paid={g['successful_paid_calls']} "
            f"tools={g['tools_called']} {g['failures'] or ''}",
            flush=True,
        )
        records.append(
            {
                "id": q["id"],
                "status": rec["result"]["status"],
                "grade": g,
                "credits_spent": rec["result"]["credits_spent"],
                "elapsed_s": rec["elapsed_s"],
            }
        )
        if g["ambiguous_failures"]:
            net_ok, notes = await network_ok(settings.fortyguard_key)
            records[-1]["network_check"] = {"ok": net_ok, "notes": notes}
            if net_ok:
                stop_reason = f"{q['id']}: charged-or-uncertain failure with a healthy network"
                break
            print("network was failing; explained by network. Pausing 60 s.", flush=True)
            await asyncio.sleep(60)
        if (
            rec["result"]["status"] == "error"
            and "cap" in (rec["result"].get("error") or "").lower()
        ):
            stop_reason = "section credit cap reached"
            break

    await asyncio.sleep(20)  # let any in-flight activity settle before reading the balance
    balance_after = await read_balance(settings.fortyguard_key)
    ledger_after = SpendLedger(ledger_path, TOTAL_CAP).total()
    summary = {
        "run": run,
        "group": args.group,
        "model": model,
        "no_paid": args.no_paid,
        "questions": records,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "charged_by_balance": balance_before - balance_after,
        "charged_by_ledger": ledger_after - ledger_before,
        "stop_reason": stop_reason,
    }
    tag = "_nopaid" if args.no_paid else ""
    write_x(OUT / f"{run}_group{args.group}{tag}_{model.split('/')[-1]}.json", summary)
    print(
        f"balance {balance_before} -> {balance_after} (charged {balance_before - balance_after}); "
        f"ledger +{ledger_after - ledger_before}; stop: {stop_reason}",
        flush=True,
    )
    return 3 if stop_reason and "healthy network" in stop_reason else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
