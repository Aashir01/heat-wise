"""Heat-safe outdoor event planner: a tool-calling loop over the FortyGuard MCP.

Guardrails enforced in code (not only in the prompt):
- max LLM steps and max tool calls; per-call timeout and retries live in McpToolbox
- ambiguous US city names are never picked silently (ask the user, or state the assumption)
- env_params' required reference temperature must come from the user or a heatmap result
  (the MCP tool description says never to invent it)
- a verdict other than cannot_determine requires at least one successful data tool call
- credit cap and per-call confirmation (in McpToolbox)
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import structlog
from pydantic import ValidationError

from .geo import GeoError, resolve
from .llm import LLMError, LLMProvider, LLMReply, ToolCall
from .mcp_tools import McpToolbox, ToolResult
from .models import AgentResult, Clarification, FinalAnswer, ToolCallRecord
from .spend import SpendCapExceeded

log = structlog.get_logger("agent")

EventFn = Callable[[dict[str, Any]], None]
AskFn = Callable[[Clarification], Awaitable[str | None]]
# Heuristic: the live shape of rate-limit/quota errors over MCP has not been observed yet.
_STOP_ERRORS = re.compile(
    r"(?i)rate.?limit|too many requests|quota|insufficient credit|out of credits"
    r"|credits? (?:exhausted|remaining)|\b429\b"
)
READ_TOOLS = {"heatmap_read", "temperature_routing_read"}
DATA_TOOLS = {
    "env_params",
    "heatmap",
    "heatmap_read",
    "temperature_routing",
    "temperature_routing_read",
}
HOURLY_KEYS = (
    "apparent_temperature_celsius",
    "wet_bulb_temperature_celsius",
    "relative_humidity_percent",
    "heat_index_celsius",
    "air_quality:idx",
    "aqi_us",
    "precipitation_mm",
    "cloud_cover_octas",
)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve $ref/$defs so the schema is self-contained (simpler for tool-calling servers)."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(schema)  # type: ignore[no-any-return]


def local_tool_schemas() -> list[dict[str, Any]]:
    def fn(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters},
        }

    return [
        fn(
            "resolve_city",
            "Look up a US city by name. Returns every match. If 'ambiguous' is true you must ask "
            "the user which one (ask_user) or state your assumption in the final answer.",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ),
        fn(
            "make_polygon",
            "Build a small square GeoJSON Polygon around a point, for the heatmap tool.",
            {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "half_side_m": {"type": "number", "description": "default 500"},
                },
                "required": ["latitude", "longitude"],
            },
        ),
        fn(
            "ask_user",
            "Ask the user one clarifying question (ambiguous city, missing reference temperature, "
            "missing date or time). Use sparingly.",
            {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        ),
        fn(
            "final_answer",
            "Finish with the structured answer. Call this exactly once, when you are done.",
            _inline_refs(FinalAnswer.model_json_schema()),
        ),
    ]


def build_system_prompt(today: date, interactive: bool) -> str:
    who = (
        "A user is available: use ask_user when the city is ambiguous or a needed value is missing."
        if interactive
        else "No user is available to answer questions during this run. If a city is ambiguous you "
        "may either call ask_user (this ends the run with your question) or proceed with ONE "
        "explicit assumption that you list in final_answer.assumptions."
    )
    return f"""You are heatwise, a heat-safety planner for outdoor events in the United States.
Today is {today.isoformat()} (UTC). {who}

Data rules (from the FortyGuard tools):
- Data covers past or present hours only, from 2021-01-01. Future dates are rejected. You cannot
  forecast. If asked about the future (e.g. "tomorrow"), say so, and only offer a clearly labelled
  historical reference if it helps. Do not present historical data as a forecast.
- Times passed to tools are local to the location. Report local times with their UTC offset.
- Only US locations are supported. Always call resolve_city first. Never guess a coordinate.
- env_params needs a reference temperature in Celsius. Never invent, assume or substitute one.
  Use a value the user gave you, or the mean temperature from a heatmap result for that place and
  hour, or ask the user.
- Paid tools cost credits. Make the fewest calls that answer the question.
  Do not repeat identical calls.
- If tools fail or data is missing, say what is missing. Do not fill gaps with general knowledge
  presented as data.

Finish by calling final_answer. Use verdict "cannot_determine" when you lack data. Always say in
caveats that this is not official safety or medical advice. List every assumption."""


# Words that make "4C" a label (room 4C, gate 12F), not a temperature.
_LABEL_WORDS = {
    "room",
    "gate",
    "apt",
    "unit",
    "seat",
    "row",
    "section",
    "hall",
    "building",
    "bldg",
    "suite",
    "table",
    "booth",
    "lot",
    "terminal",
    "floor",
    "level",
    "bay",
}
_TEMP_RE = re.compile(
    r"(?P<pre>\b\w+\s+)?(?P<val>-?\d+(?:\.\d+)?)\s*(?:°|º|deg(?:rees)?)?\s*"
    r"(?P<unit>celsius|fahrenheit|C|F)\b",
    re.IGNORECASE,
)


def extract_user_temperatures(text: str) -> list[float]:
    """Temperatures the user wrote in the question, converted to Celsius."""
    out: list[float] = []
    for m in _TEMP_RE.finditer(text):
        if m.group("pre") and m.group("pre").strip().lower() in _LABEL_WORDS:
            continue
        v = float(m.group("val"))
        out.append(round((v - 32) * 5 / 9, 2) if m.group("unit").upper().startswith("F") else v)
    return out


# Keys seen in real heatmap_read values: mean_temperature_celsius and
# stats.temperature_stats.mean_celsius. REST heatmap stats use "mean".
_MEAN_KEYS = {"mean", "mean_c", "mean_celsius", "mean_temperature", "mean_temperature_celsius"}


def find_means(obj: Any) -> list[float]:
    """Collect numeric `mean` values (e.g. temperature_stats.mean) from a tool result."""
    found: list[float] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _MEAN_KEYS and isinstance(v, int | float):
                if float(v) not in found:
                    found.append(float(v))
            else:
                found.extend(x for x in find_means(v) if x not in found)
    elif isinstance(obj, list):
        for x in obj:
            found.extend(find_means(x))
    return found


def _window(
    vals: list[float | None], stamps: list[str], n: int, pick: Callable[..., Any]
) -> dict[str, Any] | None:
    best: tuple[float, int] | None = None
    for i in range(len(vals) - n + 1):
        chunk = vals[i : i + n]
        if any(v is None for v in chunk):
            continue
        mean = sum(v for v in chunk if v is not None) / n
        if best is None or pick(mean, best[0]) == mean and mean != best[0]:
            best = (mean, i)
    if best is None:
        return None
    mean, i = best
    return {"start": stamps[i], "end": stamps[i + n - 1], "mean_apparent_c": round(mean, 1)}


def adapt_env_value(value: Any) -> Any:
    """Make an env_params MCP value safe and compact for the model.

    The MCP puts only the FIRST hour in value.parameters for multi-hour requests (see
    FRICTION_LOG F-17) and the full series under value.raw. This adds the hourly series,
    the coolest and hottest 3-hour windows, and an explicit note. It is an agent-side adapter.
    """
    if not isinstance(value, dict) or "raw" not in value:
        return value
    try:
        raw = value["raw"]
        stamps: list[str] = raw["metadata"]["timestamps"]
        params: dict[str, list[Any]] = raw["locations"][0]["parameters"]
    except (KeyError, IndexError, TypeError):
        return value
    hourly = {k: params[k] for k in HOURLY_KEYS if k in params}
    out: dict[str, Any] = {
        "timezone": raw["metadata"].get("timezone"),
        "timestamps": stamps,
        "hourly": hourly,
    }
    snap = value.get("parameters_metadata") or {}
    if len(stamps) > 1:
        out["agent_note"] = (
            "This request covers several hours. 'parameters_first_hour_only' is only the first "
            "hour; use 'hourly' for the whole range."
        )
        out["parameters_first_hour_only"] = value.get("parameters")
        app = params.get("apparent_temperature_celsius")
        if app and len(stamps) >= 3:
            out["coolest_3h_by_apparent_temperature"] = _window(app, stamps, 3, min)
            out["hottest_3h_by_apparent_temperature"] = _window(app, stamps, 3, max)
    else:
        out["parameters"] = value.get("parameters")
    out["parameters_basis"] = snap.get("basis")
    return out


class PlannerAgent:
    def __init__(
        self,
        llm: LLMProvider,
        toolbox: McpToolbox,
        *,
        today: date,
        max_steps: int = 12,
        max_tool_calls: int = 15,
        interactive: bool = False,
        ask_user: AskFn | None = None,
        on_event: EventFn | None = None,
        user_temperatures: list[float] | None = None,
        read_poll_interval: float = 5.0,
        read_poll_max: int = 24,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.llm = llm
        self.toolbox = toolbox
        self.today = today
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.interactive = interactive and ask_user is not None
        self._ask = ask_user
        self._emit = on_event or (lambda _e: None)
        self.user_temps = list(user_temperatures or [])
        self.read_poll_interval = read_poll_interval
        self.read_poll_max = read_poll_max
        self._sleep = sleep
        self._derived_temps: list[float] = []
        self._pending_ambiguous: dict[str, list[str]] = {}
        self._data_ok = False
        self._records: list[ToolCallRecord] = []
        self._credits = 0
        self._final_rejections = 0
        self.messages: list[dict[str, Any]] = []  # full conversation, for audit traces

    # ---- helpers -------------------------------------------------------------------------

    def _record(
        self,
        name: str,
        args: dict[str, Any],
        ok: bool,
        *,
        error: str | None = None,
        cost: int | None = None,
        local: bool = False,
    ) -> None:
        self._records.append(
            ToolCallRecord(
                name=name, arguments=args, ok=ok, error=error, cost_credits=cost, local=local
            )
        )
        self._emit({"type": "tool_result", "name": name, "ok": ok, "error": error, "cost": cost})

    def _result(self, status: str, **kw: Any) -> AgentResult:
        return AgentResult(
            status=status,
            steps=kw.pop("steps", 0),
            tool_calls=list(self._records),
            credits_spent=self._credits,
            model=self.llm.model,
            **kw,
        )

    def _temperature_allowed(self, value: Any) -> bool:
        try:
            t = float(value)
        except (TypeError, ValueError):
            return False
        return any(abs(t - u) <= 0.05 for u in self.user_temps) or any(
            abs(t - d) <= 0.5 for d in self._derived_temps
        )

    # ---- local tools ---------------------------------------------------------------------

    def _resolve_city(self, args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        try:
            matches = resolve(query)
        except GeoError as exc:
            self._record("resolve_city", args, False, error=type(exc).__name__, local=True)
            body: dict[str, Any] = {"error": exc.user_message}
            if hasattr(exc, "suggestions"):
                body["suggestions"] = exc.suggestions
            return json.dumps(body)
        names = {m.name.lower() for m in matches}
        ambiguous = len(matches) > 1
        for n in names:
            if ambiguous:
                self._pending_ambiguous[n] = [m.label for m in matches]
            else:
                self._pending_ambiguous.pop(n, None)
        self._record("resolve_city", args, True, local=True)
        return json.dumps(
            {
                "ambiguous": ambiguous,
                "matches": [
                    {"label": m.label, "latitude": m.lat, "longitude": m.lon, "timezone": m.tz}
                    for m in matches
                ],
            }
        )

    def _make_polygon(self, args: dict[str, Any]) -> str:
        try:
            lat, lon = float(args["latitude"]), float(args["longitude"])
            half = float(args.get("half_side_m", 500))
        except (KeyError, TypeError, ValueError):
            self._record("make_polygon", args, False, error="bad_arguments", local=True)
            return json.dumps({"error": "latitude and longitude (numbers) are required"})
        dlat = half / 111_320
        dlon = half / (111_320 * max(math.cos(math.radians(lat)), 0.01))
        ring = [
            [lon - dlon, lat - dlat],
            [lon + dlon, lat - dlat],
            [lon + dlon, lat + dlat],
            [lon - dlon, lat + dlat],
            [lon - dlon, lat - dlat],
        ]
        self._record("make_polygon", args, True, local=True)
        return json.dumps(
            {"type": "Polygon", "coordinates": [[[round(x, 6) for x in p] for p in ring]]}
        )

    # ---- main loop -----------------------------------------------------------------------

    async def run(self, question: str) -> AgentResult:
        if not self.user_temps:
            self.user_temps = extract_user_temperatures(question)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(self.today, self.interactive)},
            {"role": "user", "content": question},
        ]
        self.messages = messages
        tools = self.toolbox.openai_tools() + local_tool_schemas()
        nudges = 0
        for step in range(1, self.max_steps + 1):
            self._emit({"type": "step", "n": step})
            try:
                reply = await self.llm.chat(messages, tools)
            except LLMError as exc:
                return self._result("error", steps=step, error=str(exc))
            messages.append(reply.as_message())
            if not reply.tool_calls:
                nudges += 1
                if nudges > 2:
                    return self._result(
                        "error", steps=step, error="The model did not produce a final answer."
                    )
                messages.append(
                    {"role": "user", "content": "Finish by calling the final_answer tool."}
                )
                continue
            for call in reply.tool_calls:
                outcome = await self._handle(call, messages)
                if outcome is not None:
                    outcome.steps = step
                    return outcome
                if len([r for r in self._records if not r.local]) > self.max_tool_calls:
                    return self._result(
                        "step_limit",
                        steps=step,
                        error=f"Stopped after {self.max_tool_calls} tool calls.",
                    )
        return self._result(
            "step_limit",
            steps=self.max_steps,
            error=f"Stopped after {self.max_steps} steps without a final answer.",
        )

    async def _handle(self, call: ToolCall, messages: list[dict[str, Any]]) -> AgentResult | None:
        def reply_tool(content: str) -> None:
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

        self._emit({"type": "tool_call", "name": call.name, "arguments": call.arguments})
        if call.arguments_error:
            self._record(
                call.name, {}, False, error="bad_arguments", local=call.name not in DATA_TOOLS
            )
            reply_tool(
                json.dumps({"error": f"Your arguments were not valid: {call.arguments_error}"})
            )
            return None
        name, args = call.name, call.arguments

        if name == "resolve_city":
            reply_tool(self._resolve_city(args))
            return None
        if name == "make_polygon":
            reply_tool(self._make_polygon(args))
            return None
        if name == "ask_user":
            return await self._handle_ask(args, reply_tool)
        if name == "final_answer":
            return self._handle_final(args, reply_tool)

        if name == "env_params" and not self._temperature_allowed(args.get("temperature")):
            self._record(name, args, False, error="reference_temperature_not_sourced")
            reply_tool(
                json.dumps(
                    {
                        "error": (
                            "Blocked before calling: the reference temperature must come "
                            "from the user or from a heatmap result for this place and hour. "
                            "Do not invent one. Ask the user (ask_user) or run the heatmap "
                            "tool first."
                        ),
                        "policy": "reference_temperature",
                    }
                )
            )
            return None
        try:
            result = await self.toolbox.call(name, args)
            if name in READ_TOOLS:
                result = await self._poll_read(name, args, result)
        except SpendCapExceeded as exc:
            self._record(name, args, False, error="spend_cap")
            return self._result("error", error=exc.user_message)
        return self._after_tool(name, args, result, reply_tool)

    async def _poll_read(self, name: str, args: dict[str, Any], result: ToolResult) -> ToolResult:
        """Read tools are free. While the activity is still processing, wait and read again
        (bounded) instead of handing 'processing' back to the model, which tends to give up."""
        polls = 0
        while (
            result.ok
            and isinstance(result.value, dict)
            and str(result.value.get("status", "")).lower() == "processing"
            and polls < self.read_poll_max
        ):
            polls += 1
            self._emit({"type": "waiting", "name": name, "poll": polls})
            await self._sleep(self.read_poll_interval)
            result = await self.toolbox.call(name, args)
        return result

    def _after_tool(
        self, name: str, args: dict[str, Any], result: ToolResult, reply_tool: Callable[[str], None]
    ) -> AgentResult | None:
        if result.ok:
            if name in DATA_TOOLS:
                self._data_ok = True
            if result.cost_credits:
                self._credits += result.cost_credits
            if name == "heatmap_read":
                self._derived_temps.extend(find_means(result.value))
            if name == "env_params":
                result = result.model_copy(update={"value": adapt_env_value(result.value)})
        err = (
            f"{result.error_code or result.error_category}: {result.error_message}"
            if not result.ok
            else None
        )
        self._record(name, args, result.ok, error=err, cost=result.cost_credits)
        if not result.ok and result.error_category == "ambiguous_failure":
            return self._result(
                "error",
                error=f"{result.error_message} Check your balance (credit_balance) first.",
            )
        if not result.ok and _STOP_ERRORS.search(
            f"{result.error_category} {result.error_code} {result.error_message}"
        ):
            return self._result(
                "error",
                error="FortyGuard reported a rate-limit or quota problem "
                f"({result.error_message}). "
                "Stopping without retrying.",
            )
        reply_tool(result.for_llm())
        return None

    async def _handle_ask(
        self, args: dict[str, Any], reply_tool: Callable[[str], None]
    ) -> AgentResult | None:
        clar = Clarification(
            question=str(args.get("question", "")),
            options=[str(o) for o in args.get("options") or []],
        )
        self._record("ask_user", args, True, local=True)
        if self._ask is None:
            return self._result("needs_clarification", clarification=clar)
        answer = await self._ask(clar)
        if not answer:
            return self._result("needs_clarification", clarification=clar)
        self.user_temps.extend(extract_user_temperatures(answer))
        reply_tool(json.dumps({"user_answer": answer}))
        return None

    def _handle_final(
        self, args: dict[str, Any], reply_tool: Callable[[str], None]
    ) -> AgentResult | None:
        problems: list[str] = []
        answer: FinalAnswer | None = None
        try:
            answer = FinalAnswer.model_validate(args)
        except ValidationError as exc:
            problems = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        if answer is not None:
            for city, labels in self._pending_ambiguous.items():
                if not any(city in a.lower() for a in answer.assumptions):
                    problems.append(
                        f"'{city.title()}' is ambiguous ({', '.join(labels)}). "
                        "Either ask the user, or "
                        "state which one you assumed in assumptions."
                    )
            if answer.verdict != "cannot_determine" and not self._data_ok:
                problems.append(
                    "No data tool succeeded. Use verdict 'cannot_determine' or fetch data."
                )
        if problems:
            self._final_rejections += 1
            self._record("final_answer", args, False, error="; ".join(problems)[:300], local=True)
            if self._final_rejections > 2:
                return self._result(
                    "error",
                    error="The model could not produce a valid final answer: "
                    + "; ".join(problems),
                )
            reply_tool(json.dumps({"rejected": problems}))
            return None
        self._record("final_answer", args, True, local=True)
        return self._result("answered", answer=answer)


__all__ = ["PlannerAgent", "LLMReply", "build_system_prompt", "extract_user_temperatures"]
