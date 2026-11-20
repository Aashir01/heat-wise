"""Command line interface: heatwise ask "<question>"."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from .agent import PlannerAgent
from .llm import NebiusProvider
from .mcp_tools import McpConnectionError, McpToolbox, connect, load_blocked
from .models import AgentResult, Clarification
from .secrets import redact
from .settings import Settings
from .spend import SpendCapExceeded, SpendLedger

EXIT_OK, EXIT_ERROR, EXIT_CLARIFY = 0, 1, 2


def write_trace(trace_dir: Path, question: str, result: AgentResult, messages: list[Any]) -> Path:
    """Save question, result and the full tool conversation to a unique file (never overwrites)."""
    trace_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    # A random suffix keeps names unique when two traces share a microsecond (seen in tests).
    path = trace_dir / f"trace_{stamp}_{uuid.uuid4().hex[:8]}.json"
    body = {"question": question, "result": result.model_dump(mode="json"), "messages": messages}
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(redact(body), indent=2, ensure_ascii=False, default=str))
    return path


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def configure_logging(verbose: bool) -> None:
    """JSON logs on stderr. Quiet by default so streamed steps stay readable."""
    level = logging.INFO if verbose else logging.WARNING
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def format_event(event: dict[str, Any]) -> str | None:
    t = event["type"]
    if t == "step":
        return f"[step {event['n']}] thinking..."
    if t == "tool_call":
        args = str(event["arguments"])
        return f"  -> {event['name']}({args[:140]}{'...' if len(args) > 140 else ''})"
    if t == "waiting":
        return f"     (still processing, waiting... poll {event['poll']})"
    if t == "tool_result":
        cost = f", {event['cost']} credits" if event.get("cost") else ""
        status = "ok" if event["ok"] else f"FAILED: {event.get('error')}"
        return f"  <- {event['name']}: {status}{cost}"
    return None


def print_event(event: dict[str, Any]) -> None:
    line = format_event(event)
    if line:
        out(line)


def render_result(result: AgentResult) -> str:
    lines: list[str] = []
    if result.status == "answered" and result.answer:
        a = result.answer
        lines += [
            "",
            f"Verdict: {a.verdict.upper()} (confidence: {a.confidence.value})",
            a.recommendation,
        ]
        if a.window:
            lines.append(
                f"Window: {a.window.start} to {a.window.end}"
                + (f" ({a.window.timezone})" if a.window.timezone else "")
            )
        if a.data_timestamp:
            lines.append(f"Data timestamp: {a.data_timestamp}")
        if a.locations:
            lines.append("Locations: " + "; ".join(a.locations))
        if a.assumptions:
            lines += ["Assumptions:"] + [f"  - {x}" for x in a.assumptions]
        lines += ["Caveats:"] + [f"  - {x}" for x in a.caveats]
    elif result.status == "needs_clarification" and result.clarification:
        lines += [
            "",
            "I need one detail before I can answer:",
            f"  {result.clarification.question}",
        ]
        lines += [f"  - {o}" for o in result.clarification.options]
    else:
        lines += ["", f"Could not finish ({result.status}): {result.error}"]
    lines += [
        "",
        f"Tool calls ({len(result.tool_calls)}), model {result.model}, "
        f"{result.steps} steps, {result.credits_spent} credits:",
    ]
    for tc in result.tool_calls:
        mark = "ok " if tc.ok else "ERR"
        kind = "local" if tc.local else "mcp  "
        extra = f" credits={tc.cost_credits}" if tc.cost_credits else ""
        err = f" [{tc.error}]" if tc.error else ""
        lines.append(f"  {mark} {kind} {tc.name}{extra}{err}")
    return "\n".join(lines)


async def _prompt(text: str) -> str:
    return (await asyncio.to_thread(input, text)).strip()


async def ask_command(args: argparse.Namespace) -> int:
    try:
        settings = Settings.from_env()
    except KeyError as exc:
        out(f"Missing configuration: {exc.args[0]}")
        return EXIT_ERROR

    interactive = sys.stdin.isatty() and not args.non_interactive
    ledger = SpendLedger(settings.ledger_path, settings.credit_cap)

    async def confirm(tool: str, arguments: dict[str, Any], cost: int) -> bool:
        if args.yes:
            out(f"  (auto-confirmed paid call: {tool}, about {cost} credits)")
            return True
        if not interactive:
            out(f"  Paid call to {tool} (about {cost} credits) declined: pass --yes to allow.")
            return False
        answer = await _prompt(f"  Paid call: {tool}, about {cost} credits. Proceed? [y/N] ")
        return answer.lower() in {"y", "yes"}

    async def ask_user(clar: Clarification) -> str | None:
        out(f"\n? {clar.question}")
        for o in clar.options:
            out(f"   - {o}")
        return await _prompt("> ") or None

    temps = [args.temperature] if args.temperature is not None else []
    try:
        async with connect(settings.fortyguard_key) as session:
            box = await McpToolbox.create(
                session,
                ledger=ledger,
                confirm=confirm,
                blocked=load_blocked(settings.ledger_path.parent / "access.json"),
            )
            llm = NebiusProvider(settings.nebius_key, settings.model, settings.nebius_base_url)
            agent = PlannerAgent(
                llm,
                box,
                today=datetime.now(UTC).date(),
                max_steps=args.max_steps,
                interactive=interactive,
                ask_user=ask_user if interactive else None,
                on_event=print_event,
                user_temperatures=temps,
            )
            result = await agent.run(args.question)
            if args.trace_dir:
                saved = write_trace(Path(args.trace_dir), args.question, result, agent.messages)
                out(f"(trace saved: {saved})")
    except McpConnectionError as exc:
        out(str(exc))
        return EXIT_ERROR
    except SpendCapExceeded as exc:
        out(exc.user_message)
        return EXIT_ERROR

    out(result.model_dump_json(indent=2) if args.json else render_result(result))
    if result.status == "answered":
        return EXIT_OK
    return EXIT_CLARIFY if result.status == "needs_clarification" else EXIT_ERROR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="heatwise", description="Heat-safe outdoor event planner")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser(
        "ask", help="ask a question; runs a tool-calling loop on the FortyGuard MCP"
    )
    ask.add_argument("question")
    ask.add_argument("--temperature", type=float, help="your reference air temperature in Celsius")
    ask.add_argument("--yes", action="store_true", help="auto-confirm paid tool calls")
    ask.add_argument("--max-steps", type=int, default=12)
    ask.add_argument(
        "--non-interactive", action="store_true", help="never prompt; state assumptions"
    )
    ask.add_argument("--json", action="store_true", help="print the result as JSON")
    ask.add_argument("--trace-dir", help="save the full tool conversation as JSON in this folder")
    ask.add_argument("--verbose", action="store_true", help="JSON logs on stderr")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        code = asyncio.run(ask_command(args))
    except KeyboardInterrupt:
        out("Interrupted.")
        code = EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - last resort: no traceback for the user
        structlog.get_logger("cli").exception("unexpected_error")
        out(f"Unexpected error: {type(exc).__name__}: {str(exc)[:200]}. Use --verbose.")
        code = EXIT_ERROR
    if argv is None:
        sys.exit(code)
    return code


if __name__ == "__main__":
    main()
