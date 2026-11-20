"""Offline tests for the MCP layer and LLM providers (fake MCP session, respx for Nebius)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from heatwise.llm import LLMError, LLMReply, MockProvider, NebiusProvider, ToolCall, parse_arguments
from heatwise.mcp_tools import McpToolbox, ToolResult, load_blocked, parse_tool_result
from heatwise.spend import SpendCapExceeded, SpendLedger

FIX = Path(__file__).parent / "fixtures"


def fx(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def raw_result(payload: dict[str, Any], is_error: bool = False) -> Any:
    """Mimics the SDK result: the full envelope is the text content."""
    return SimpleNamespace(
        structured_content=payload,
        content=[SimpleNamespace(text=json.dumps(payload))],
        is_error=is_error,
    )


@dataclass
class FakeSession:
    responses: list[Any]
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def list_tools(self) -> Any:
        names = [
            "credit_balance",
            "env_params",
            "heatmap",
            "heatmap_read",
            "report",
            "satellite",
            "streetview",
            "temperature_routing",
            "temperature_routing_read",
            "monitoring",
        ]
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=n,
                    description=f"{n} desc",
                    input_schema={"type": "object", "properties": {}},
                )
                for n in names
            ]
        )

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, s: float) -> None:
        self.delays.append(s)


async def toolbox(
    session: FakeSession, tmp_path: Path, cap: int = 150_000, **kw: Any
) -> McpToolbox:
    ledger = SpendLedger(tmp_path / "ledger.jsonl", cap)
    return await McpToolbox.create(session, ledger=ledger, sleep=Sleeps(), rng=lambda: 1.0, **kw)


# ---- parsing real shapes ----------------------------------------------------------------------


def test_parse_real_env_day_success() -> None:
    res = parse_tool_result("env_params", fx("mcp_env_day.json"), False)
    assert res.ok and res.cost_credits == 2900 and res.plan_type == "premium"
    assert res.value["parameters_metadata"]["basis"] == "first_hour_snapshot"
    assert len(res.value["raw"]["metadata"]["timestamps"]) == 24


def test_parse_real_error_future_date() -> None:
    res = parse_tool_result("env_params", fx("mcp_error_future_date.json"), True)
    assert (
        not res.ok and res.error_code == "NO_COVERAGE" and res.error_field == "date_time.start_date"
    )
    assert res.retriable is False


def test_credit_balance_structured_content_is_value_only_but_text_is_envelope() -> None:
    res = parse_tool_result("credit_balance", json.dumps(fx("mcp_credit_balance.json")), False)
    assert res.ok and res.value == {"available_credits": 971480}


def test_unparseable_payload_is_an_error_not_a_crash() -> None:
    res = parse_tool_result("env_params", "<html>gateway</html>", False)
    assert not res.ok and res.error_category == "unparseable"


def test_for_llm_truncates_with_marker() -> None:
    res = ToolResult(tool="x", ok=True, value="a" * 20000)
    assert "[TRUNCATED" in res.for_llm(max_chars=500)


# ---- toolbox ----------------------------------------------------------------------------------


async def test_only_planner_tools_are_exposed_and_blocked_are_hidden(tmp_path: Path) -> None:
    box = await toolbox(FakeSession([]), tmp_path, blocked={"heatmap"})
    names = set(box.specs)
    assert "report" not in names and "satellite" not in names and "monitoring" not in names
    assert "heatmap" not in names and "env_params" in names
    schema = box.openai_tools()[0]
    assert schema["type"] == "function" and "parameters" in schema["function"]


def test_load_blocked_maps_rest_names_to_mcp_names(tmp_path: Path) -> None:
    p = tmp_path / "access.json"
    p.write_text(json.dumps({"routing": {"status": "blocked"}, "env_params": {"status": "ok"}}))
    assert load_blocked(p) == {"temperature_routing"}
    assert load_blocked(tmp_path / "missing.json") == set()


async def test_paid_call_is_recorded_in_ledger(tmp_path: Path) -> None:
    session = FakeSession([raw_result(fx("mcp_env_day.json"))])
    box = await toolbox(session, tmp_path)
    res = await box.call("env_params", {"latitude": 1})
    assert res.ok
    assert box.ledger is not None and box.ledger.total() == 2900


async def test_free_tool_is_not_charged(tmp_path: Path) -> None:
    box = await toolbox(FakeSession([raw_result(fx("mcp_credit_balance.json"))]), tmp_path)
    assert (await box.call("credit_balance", {})).ok
    assert box.ledger is not None and box.ledger.total() == 0


async def test_cap_blocks_call_before_any_network(tmp_path: Path) -> None:
    session = FakeSession([])
    box = await toolbox(session, tmp_path, cap=2000)
    with pytest.raises(SpendCapExceeded):
        await box.call("env_params", {})
    assert session.calls == []


async def test_declined_confirmation_makes_no_call(tmp_path: Path) -> None:
    async def deny(tool: str, args: dict[str, Any], cost: int) -> bool:
        return False

    session = FakeSession([])
    box = await toolbox(session, tmp_path, confirm=deny)
    res = await box.call("heatmap", {})
    assert not res.ok and res.error_category == "declined" and session.calls == []


async def test_unknown_tool_is_reported(tmp_path: Path) -> None:
    res = await (await toolbox(FakeSession([]), tmp_path)).call("satellite", {})
    assert not res.ok and res.error_category == "unknown_tool"


async def test_non_retriable_error_is_not_retried(tmp_path: Path) -> None:
    session = FakeSession([raw_result(fx("mcp_error_future_date.json"), True)])
    res = await (await toolbox(session, tmp_path)).call("env_params", {})
    assert not res.ok and len(session.calls) == 1


async def test_retriable_error_is_retried_with_backoff(tmp_path: Path) -> None:
    flaky = {
        "ok": False,
        "error": {"category": "upstream", "message": "busy", "retriable": True, "code": "X"},
    }
    session = FakeSession(
        [raw_result(flaky, True), raw_result(flaky, True), raw_result(fx("mcp_env_day.json"))]
    )
    box = await toolbox(session, tmp_path)
    res = await box.call("env_params", {})
    assert res.ok and len(session.calls) == 3


async def test_paid_timeout_is_not_retried(tmp_path: Path) -> None:
    session = FakeSession([TimeoutError()])
    box = await toolbox(session, tmp_path)
    res = await box.call("env_params", {})
    assert not res.ok and res.error_category == "ambiguous_failure" and len(session.calls) == 1
    # A failed delivery can still be charged (F-22): counted against the cap, conservatively.
    assert box.ledger is not None and box.ledger.total() == 2900


async def test_free_timeout_is_retried_then_succeeds(tmp_path: Path) -> None:
    session = FakeSession([TimeoutError(), raw_result(fx("mcp_credit_balance.json"))])
    res = await (await toolbox(session, tmp_path)).call("credit_balance", {})
    assert res.ok and len(session.calls) == 2


async def test_per_call_timeout_fires(tmp_path: Path) -> None:
    import asyncio

    class Slow(FakeSession):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            await asyncio.sleep(5)

    box = await toolbox(Slow([]), tmp_path, timeout=0.05, max_retries=0)
    res = await box.call("credit_balance", {})
    assert not res.ok and res.error_category == "transport"


# ---- LLM --------------------------------------------------------------------------------------


def test_parse_arguments_reports_bad_json() -> None:
    assert parse_arguments('{"a": 1}') == ({"a": 1}, None)
    args, err = parse_arguments("{not json")
    assert args == {} and err is not None
    assert parse_arguments("[1]")[1] is not None
    assert parse_arguments("") == ({}, None)


def test_reply_as_message_round_trips_tool_calls() -> None:
    msg = LLMReply(None, [ToolCall("c1", "env_params", {"latitude": 1.0})]).as_message()
    assert msg["tool_calls"][0]["function"]["name"] == "env_params"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"latitude": 1.0}


async def test_mock_provider_follows_script_and_records_calls() -> None:
    mock = MockProvider([LLMReply("one"), lambda m: LLMReply(f"saw {len(m)} messages")])
    assert (await mock.chat([{"role": "user", "content": "x"}], [])).content == "one"
    assert (await mock.chat([{"role": "user", "content": "x"}] * 2, [])).content == "saw 2 messages"
    assert len(mock.calls) == 2


def completion(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }


def provider(handler: Any, max_retries: int = 0) -> NebiusProvider:
    """The OpenAI SDK 3.x uses httpx2, so respx cannot intercept it; inject a mock transport."""
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return NebiusProvider(
        "k",
        "m",
        "https://api.tokenfactory.nebius.com/v1/",
        max_retries=max_retries,
        http_client=client,
    )


async def test_nebius_provider_parses_tool_calls() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx2.Response(
            200,
            json=completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "t1",
                            "type": "function",
                            "function": {"name": "env_params", "arguments": '{"latitude": 30.27}'},
                        },
                        {
                            "id": "t2",
                            "type": "function",
                            "function": {"name": "env_params", "arguments": "{oops"},
                        },
                    ],
                }
            ),
        )

    reply = await provider(handler).chat([{"role": "user", "content": "hi"}], [])
    assert reply.tool_calls[0].arguments == {"latitude": 30.27}
    assert reply.tool_calls[1].arguments_error is not None


async def test_nebius_auth_error_becomes_clear_llm_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
        await provider(handler).chat([{"role": "user", "content": "hi"}], [])


async def test_nebius_retries_server_errors_then_succeeds() -> None:
    seen: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(1)
        if len(seen) == 1:
            return httpx2.Response(500, json={"error": {"message": "x"}})
        return httpx2.Response(200, json=completion({"role": "assistant", "content": "ok"}))

    reply = await provider(handler, max_retries=2).chat([{"role": "user", "content": "hi"}], [])
    assert reply.content == "ok" and len(seen) == 2


async def test_sdk_mcp_error_on_paid_call_is_not_retried_and_counts_as_charged(
    tmp_path: Path,
) -> None:
    """Mirrors the live failure: MCPError 'SSE stream ended without a response' (F-22)."""
    session = FakeSession([MCPError(code=-32603, message="SSE stream ended without a response")])
    box = await toolbox(session, tmp_path)
    res = await box.call("env_params", {})
    assert not res.ok and res.error_category == "ambiguous_failure"
    assert "may have been charged" in (res.error_message or "")
    assert len(session.calls) == 1
    assert box.ledger is not None and box.ledger.total() == 2900


async def test_sdk_mcp_error_on_free_call_is_retried(tmp_path: Path) -> None:
    session = FakeSession(
        [
            MCPError(code=-32603, message="SSE stream ended without a response"),
            raw_result(fx("mcp_credit_balance.json")),
        ]
    )
    res = await (await toolbox(session, tmp_path)).call("credit_balance", {})
    assert res.ok and len(session.calls) == 2
