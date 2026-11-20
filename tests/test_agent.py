"""Agent tests: scripted MockProvider conversations over a fake MCP session. All offline."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from heatwise.agent import (
    PlannerAgent,
    adapt_env_value,
    extract_user_temperatures,
    find_means,
)
from heatwise.llm import LLMError, LLMReply, MockProvider, ToolCall
from heatwise.mcp_tools import McpToolbox
from heatwise.models import Clarification
from heatwise.spend import SpendLedger

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 20)


def fx(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def envelope(value: Any, cost: int = 0, activity_id: str | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "result": {
            "tool": "x",
            "value": value,
            "activity_id": activity_id,
            "evidence": [],
            "warnings": [],
        },
        "trace": {"cost_credits": cost, "plan_type": "premium"},
    }


def error_env(
    category: str, message: str, code: str = "X", retriable: bool = False
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"category": category, "message": message, "code": code, "retriable": retriable},
        "trace": {},
    }


class FakeSession:
    """Returns queued envelopes per tool name."""

    def __init__(self, queues: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.queues = queues or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        names = [
            "env_params",
            "heatmap",
            "heatmap_read",
            "temperature_routing",
            "temperature_routing_read",
            "credit_balance",
        ]
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=n, description=n, input_schema={"type": "object", "properties": {}}
                )
                for n in names
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        payload = self.queues[name].pop(0)
        return SimpleNamespace(
            structured_content=payload,
            content=[SimpleNamespace(text=json.dumps(payload))],
            is_error=not payload.get("ok", False),
        )


def call(name: str, args: dict[str, Any], cid: str = "c") -> ToolCall:
    return ToolCall(cid, name, args)


def turn(*calls: ToolCall) -> LLMReply:
    return LLMReply(None, list(calls))


def final_args(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "recommendation": "Conditions look manageable.",
        "verdict": "caution",
        "confidence": "medium",
        "caveats": ["Not official safety or medical advice."],
        "assumptions": [],
        "locations": ["Austin, TX"],
    }
    base.update(over)
    return base


async def make_agent(
    script: list[Any],
    tmp_path: Path,
    queues: dict[str, list[dict[str, Any]]] | None = None,
    *,
    cap: int = 150_000,
    ask: Any = None,
    **kw: Any,
) -> tuple[PlannerAgent, FakeSession, MockProvider, list[dict[str, Any]]]:
    session = FakeSession(queues)
    ledger = SpendLedger(tmp_path / "ledger.jsonl", cap)
    box = await McpToolbox.create(session, ledger=ledger)
    llm = MockProvider(script)
    events: list[dict[str, Any]] = []
    agent = PlannerAgent(
        llm,
        box,
        today=TODAY,
        on_event=events.append,
        ask_user=ask,
        interactive=ask is not None,
        **kw,
    )
    return agent, session, llm, events


ENV_ARGS = {
    "latitude": 30.2672,
    "longitude": -97.7431,
    "temperature": 35,
    "date_time": {"start_date": "2026-11-17", "filter_type": 3},
}


# ---- happy path -------------------------------------------------------------------------------


async def test_happy_path_uses_user_temperature_and_records_calls(tmp_path: Path) -> None:
    script = [
        turn(call("resolve_city", {"query": "Austin"}, "1")),
        turn(call("env_params", ENV_ARGS, "2")),
        turn(call("final_answer", final_args(data_timestamp="2026-11-17T14:00:00-06:00"), "3")),
    ]
    queues = {"env_params": [envelope(fx("mcp_env_day.json")["result"]["value"], cost=2900)]}
    agent, session, llm, _ = await make_agent(script, tmp_path, queues)
    result = await agent.run("Is it OK to run in Austin on 2026-11-17? It was 35C.")
    assert result.status == "answered" and result.answer is not None
    assert result.credits_spent == 2900 and result.steps == 3
    assert [(t.name, t.local) for t in result.tool_calls] == [
        ("resolve_city", True),
        ("env_params", False),
        ("final_answer", True),
    ]
    assert session.calls[0][0] == "env_params"
    tool_msg = next(m for m in llm.calls[2] if m.get("tool_call_id") == "2")
    assert (
        "coolest_3h_by_apparent_temperature" in tool_msg["content"]
    )  # adapter output reached the model


async def test_user_temperature_parsed_from_question(tmp_path: Path) -> None:
    assert extract_user_temperatures("it is 95F, or 35 °C, maybe 20c") == [35.0, 35.0, 20.0]
    assert extract_user_temperatures("meet at 5 PM in room 4C, gate 12F") == []
    assert extract_user_temperatures("about 95 degrees Fahrenheit") == [35.0]
    assert extract_user_temperatures("no numbers here") == []


# ---- ambiguity --------------------------------------------------------------------------------


async def test_ambiguous_city_interactive_asks_then_proceeds(tmp_path: Path) -> None:
    asked: list[Clarification] = []

    async def ask(c: Clarification) -> str | None:
        asked.append(c)
        return "Portland, Maine. It is 20C."

    script = [
        turn(call("resolve_city", {"query": "Portland"}, "1")),
        turn(call("ask_user", {"question": "Which Portland?", "options": ["OR", "ME"]}, "2")),
        turn(call("resolve_city", {"query": "Portland, ME"}, "3")),
        turn(
            call(
                "final_answer",
                final_args(verdict="cannot_determine", locations=["Portland, ME"]),
                "4",
            )
        ),
    ]
    agent, _, _, _ = await make_agent(script, tmp_path, ask=ask)
    result = await agent.run("Is it safe in Portland?")
    assert result.status == "answered" and len(asked) == 1
    assert agent.user_temps == [20.0]


async def test_ambiguous_city_final_without_assumption_is_rejected(tmp_path: Path) -> None:
    script = [
        turn(call("resolve_city", {"query": "Portland"}, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
        turn(
            call(
                "final_answer",
                final_args(
                    verdict="cannot_determine",
                    assumptions=[
                        "Assumed Portland, OR because the name was ambiguous (also Portland, ME)."
                    ],
                ),
                "3",
            )
        ),
    ]
    agent, _, llm, _ = await make_agent(script, tmp_path)
    result = await agent.run("Is it safe in Portland?")
    assert result.status == "answered" and result.answer is not None
    assert "Portland, OR" in result.answer.assumptions[0]
    rejection = next(m for m in llm.calls[2] if m.get("tool_call_id") == "2")
    assert "ambiguous" in rejection["content"]
    assert [t.ok for t in result.tool_calls if t.name == "final_answer"] == [False, True]


async def test_ambiguous_non_interactive_ask_user_ends_with_clarification(tmp_path: Path) -> None:
    script = [
        turn(call("resolve_city", {"query": "Springfield"}, "1")),
        turn(
            call("ask_user", {"question": "Which Springfield?", "options": ["IL", "MO", "MA"]}, "2")
        ),
    ]
    agent, _, _, _ = await make_agent(script, tmp_path)
    result = await agent.run("Heat at the Springfield fair?")
    assert result.status == "needs_clarification" and result.clarification is not None
    assert result.clarification.options == ["IL", "MO", "MA"]


# ---- bad input and honesty guards -------------------------------------------------------------


@pytest.mark.parametrize("city", ["Atlantis", "London", "Housten"])
async def test_bad_city_makes_no_data_call(tmp_path: Path, city: str) -> None:
    script = [
        turn(call("resolve_city", {"query": city}, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine", locations=[]), "2")),
    ]
    agent, session, llm, _ = await make_agent(script, tmp_path)
    result = await agent.run(f"Is it safe in {city}?")
    assert result.status == "answered" and session.calls == []
    first_tool_msg = next(m for m in llm.calls[1] if m.get("tool_call_id") == "1")
    assert "error" in json.loads(first_tool_msg["content"])


async def test_verdict_without_data_is_rejected(tmp_path: Path) -> None:
    script = [
        turn(call("final_answer", final_args(verdict="safe"), "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    agent, _, _, _ = await make_agent(script, tmp_path)
    result = await agent.run("Is it safe in Austin tomorrow?")
    assert result.status == "answered" and result.answer is not None
    assert result.answer.verdict == "cannot_determine"


async def test_invented_reference_temperature_is_blocked_without_spending(tmp_path: Path) -> None:
    script = [
        turn(call("env_params", ENV_ARGS, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    agent, session, llm, _ = await make_agent(script, tmp_path)
    result = await agent.run("How hot is it in Austin on 2026-11-17?")
    assert session.calls == []
    assert result.tool_calls[0].error == "reference_temperature_not_sourced"
    assert (
        "Do not invent" in next(m for m in llm.calls[1] if m.get("tool_call_id") == "1")["content"]
    )


async def test_heatmap_mean_unlocks_reference_temperature(tmp_path: Path) -> None:
    stats = {"temperature_stats": {"mean": 19.185358, "minimum": 19.07, "maximum": 19.31}}
    queues = {
        "heatmap_read": [envelope(stats)],
        "env_params": [envelope(fx("mcp_env_day.json")["result"]["value"], cost=2900)],
    }
    script = [
        turn(call("heatmap_read", {"activity_id": "a"}, "1")),
        turn(call("env_params", {**ENV_ARGS, "temperature": 19.2}, "2")),
        turn(call("final_answer", final_args(), "3")),
    ]
    agent, session, _, _ = await make_agent(script, tmp_path, queues)
    result = await agent.run("Conditions in Austin on 2026-11-17?")
    assert result.status == "answered"
    assert [c[0] for c in session.calls] == ["heatmap_read", "env_params"]


def test_find_means_handles_nested_shapes() -> None:
    assert find_means({"a": [{"temperature_stats": {"mean": 20.5}}], "mean": 1}) == [20.5, 1.0]


# ---- limits and failures ----------------------------------------------------------------------


async def test_step_limit(tmp_path: Path) -> None:
    script = [turn(call("resolve_city", {"query": "Austin"}, str(i))) for i in range(5)]
    agent, _, _, _ = await make_agent(script, tmp_path, max_steps=3)
    result = await agent.run("loop forever")
    assert result.status == "step_limit" and result.steps == 3


async def test_tool_call_limit(tmp_path: Path) -> None:
    queues = {"credit_balance": [envelope({"available_credits": 1}) for _ in range(5)]}
    script = [turn(call("credit_balance", {}, str(i))) for i in range(5)]
    agent, session, _, _ = await make_agent(script, tmp_path, queues, max_tool_calls=2)
    result = await agent.run("balance?")
    assert result.status == "step_limit" and len(session.calls) == 3


async def test_llm_error_becomes_clear_error_result(tmp_path: Path) -> None:
    def boom(_m: list[dict[str, Any]]) -> LLMReply:
        raise LLMError("The LLM API key (NEBIUS_API_KEY) is missing or invalid.")

    agent, _, _, _ = await make_agent([boom], tmp_path)
    result = await agent.run("hi")
    assert result.status == "error" and "NEBIUS_API_KEY" in (result.error or "")


async def test_spend_cap_stops_the_run_before_any_call(tmp_path: Path) -> None:
    script = [turn(call("env_params", ENV_ARGS, "1"))]
    agent, session, _, _ = await make_agent(script, tmp_path, cap=1000)
    result = await agent.run("It is 35C. Conditions in Austin on 2026-11-17?")
    assert result.status == "error" and "cap" in (result.error or "").lower()
    assert session.calls == []


async def test_rate_limit_error_stops_the_run_without_retry(tmp_path: Path) -> None:
    queues = {
        "env_params": [error_env("rate_limited", "Too many requests, slow down", "RATE_LIMIT")]
    }
    script = [turn(call("env_params", ENV_ARGS, "1")), turn(call("final_answer", final_args()))]
    agent, session, _, _ = await make_agent(script, tmp_path, queues)
    result = await agent.run("It is 35C in Austin on 2026-11-17.")
    assert result.status == "error" and "rate-limit" in (result.error or "")
    assert len(session.calls) == 1


async def test_non_retriable_tool_error_is_shown_to_model_which_can_recover(tmp_path: Path) -> None:
    queues = {"env_params": [error_env("no_coverage", "Check date_time.start_date", "NO_COVERAGE")]}
    script = [
        turn(call("env_params", ENV_ARGS, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    agent, _, llm, _ = await make_agent(script, tmp_path, queues)
    result = await agent.run("It is 35C in Austin on 2026-11-17.")
    assert result.status == "answered"
    assert "NO_COVERAGE" in next(m for m in llm.calls[1] if m.get("tool_call_id") == "1")["content"]
    assert result.tool_calls[0].ok is False


async def test_invalid_json_arguments_are_reported_not_fatal(tmp_path: Path) -> None:
    script = [
        turn(ToolCall("1", "resolve_city", {}, "invalid JSON arguments: Expecting value")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    agent, _, _, _ = await make_agent(script, tmp_path)
    assert (await agent.run("q")).status == "answered"


async def test_model_that_never_calls_final_answer_gets_nudged_then_errors(tmp_path: Path) -> None:
    script = [LLMReply("I think it is fine."), LLMReply("Really."), LLMReply("Yes.")]
    agent, _, llm, _ = await make_agent(script, tmp_path)
    result = await agent.run("q")
    assert result.status == "error" and len(llm.calls) == 3


async def test_invalid_final_answer_schema_is_rejected_then_fixed(tmp_path: Path) -> None:
    script = [
        turn(call("final_answer", {"recommendation": "ok"}, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    agent, _, _, _ = await make_agent(script, tmp_path)
    assert (await agent.run("q")).status == "answered"


# ---- adapter and local tools ------------------------------------------------------------------


def test_adapt_env_value_day_summary_matches_the_data() -> None:
    value = fx("mcp_env_day.json")["result"]["value"]
    out = adapt_env_value(value)
    app = value["raw"]["locations"][0]["parameters"]["apparent_temperature_celsius"]
    assert len(out["timestamps"]) == 24 and out["hourly"]["apparent_temperature_celsius"] == app
    best = min(sum(app[i : i + 3]) / 3 for i in range(22))
    assert out["coolest_3h_by_apparent_temperature"]["mean_apparent_c"] == round(best, 1)
    assert "first hour" in out["agent_note"]
    assert out["parameters_first_hour_only"]["apparent_temperature_celsius"] == app[0]


def test_adapt_env_value_leaves_unknown_shapes_alone() -> None:
    assert adapt_env_value({"something": 1}) == {"something": 1}
    assert adapt_env_value("text") == "text"


async def test_make_polygon_is_closed_and_about_one_km(tmp_path: Path) -> None:
    agent, _, _, _ = await make_agent([], tmp_path)
    poly = json.loads(agent._make_polygon({"latitude": 33.4484, "longitude": -112.074}))
    ring = poly["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    lat_span_m = (ring[2][1] - ring[0][1]) * 111_320
    assert 950 < lat_span_m < 1050


async def test_events_are_streamed(tmp_path: Path) -> None:
    script = [
        turn(call("resolve_city", {"query": "Austin"}, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    agent, _, _, events = await make_agent(script, tmp_path)
    await agent.run("q")
    kinds = [e["type"] for e in events]
    assert kinds.count("step") == 2 and "tool_call" in kinds and "tool_result" in kinds


async def test_ambiguous_paid_failure_stops_the_agent_and_is_not_repeated(tmp_path: Path) -> None:
    from mcp.shared.exceptions import MCPError

    class Broken(FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            self.calls.append((name, arguments))
            raise MCPError(code=-32603, message="SSE stream ended without a response")

    session = Broken()
    ledger = SpendLedger(tmp_path / "ledger.jsonl", 150_000)
    box = await McpToolbox.create(session, ledger=ledger)
    script = [turn(call("env_params", ENV_ARGS, "1")), turn(call("env_params", ENV_ARGS, "2"))]
    agent = PlannerAgent(MockProvider(script), box, today=TODAY)
    result = await agent.run("It is 35C in Austin on 2026-11-17.")
    assert result.status == "error" and "credit_balance" in (result.error or "")
    assert len(session.calls) == 1  # never repeated automatically
    assert ledger.total() == 2900


async def test_read_tool_is_polled_until_completed_without_model_round_trips(
    tmp_path: Path,
) -> None:
    processing = envelope({"status": "processing", "heatmap_id": "h"})
    done = fx("mcp_heatmap_read_completed.json")  # real completed heatmap_read envelope
    queues = {"heatmap_read": [processing, processing, done]}
    script = [
        turn(call("heatmap_read", {"activity_id": "h"}, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    session = FakeSession(queues)
    box = await McpToolbox.create(session, ledger=SpendLedger(tmp_path / "l.jsonl", 150_000))
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)

    llm = MockProvider(script)
    agent = PlannerAgent(llm, box, today=TODAY, sleep=fake_sleep, read_poll_interval=5)
    await agent.run("q")
    assert len(session.calls) == 3 and waits == [5, 5]
    tool_msg = next(m for m in llm.calls[1] if m.get("tool_call_id") == "1")
    assert "completed" in tool_msg["content"]
    assert [round(x, 2) for x in agent._derived_temps] == [19.97]


async def test_read_polling_is_bounded(tmp_path: Path) -> None:
    processing = envelope({"status": "processing"})
    queues = {"heatmap_read": [processing] * 4}
    script = [
        turn(call("heatmap_read", {"activity_id": "h"}, "1")),
        turn(call("final_answer", final_args(verdict="cannot_determine"), "2")),
    ]
    session = FakeSession(queues)
    box = await McpToolbox.create(session)

    async def no_sleep(s: float) -> None:
        return None

    agent = PlannerAgent(MockProvider(script), box, today=TODAY, sleep=no_sleep, read_poll_max=3)
    await agent.run("q")
    assert len(session.calls) == 4


def test_find_means_on_real_heatmap_read_value() -> None:
    value = fx("mcp_heatmap_read_completed.json")["result"]["value"]
    assert [round(x, 3) for x in find_means(value)] == [19.968]
