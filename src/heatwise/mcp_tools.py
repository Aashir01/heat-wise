"""FortyGuard MCP layer: connect, expose tools to the LLM, call them safely.

Result and error shapes below were captured from the live server (see results/raw/mcp_*.json):
  success: {"ok": true, "result": {tool, value, activity_id?, evidence, warnings}, "trace": {...}}
  error:   {"ok": false, "error": {category, message, retriable, code, field}, "trace": {...}}
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog
from mcp.shared.exceptions import MCPError as SdkMcpError
from pydantic import BaseModel

from .settings import MCP_URL
from .spend import PAID_TOOL_COSTS, SpendLedger

log = structlog.get_logger("mcp")

# Tools the planner exposes to the LLM. Others (report, satellite, streetview, monitoring) cost
# 8,600+ credits each and are not needed for the use case.
PLANNER_TOOLS = (
    "env_params",
    "heatmap",
    "heatmap_read",
    "temperature_routing",
    "temperature_routing_read",
    "credit_balance",
)
# results/access.json uses REST endpoint names; map them to MCP tool names.
_ACCESS_TO_MCP = {"routing": "temperature_routing", "heat_intelligence": "report"}


class McpError(Exception):
    """MCP failure with a message that is safe to show to the user."""


class McpConnectionError(McpError):
    pass


@dataclass(frozen=True)
class McpToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolResult(BaseModel):
    """Normalised tool outcome. `value` is whatever the tool returned (kept raw on purpose)."""

    tool: str
    ok: bool
    value: Any = None
    activity_id: str | None = None
    warnings: list[str] = []
    cost_credits: int | None = None
    plan_type: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    error_field: str | None = None
    error_message: str | None = None
    retriable: bool = False

    def for_llm(self, max_chars: int = 6000) -> str:
        """Compact JSON for the model. Large payloads are truncated with an explicit marker."""
        body: dict[str, Any]
        if self.ok:
            body = {
                "ok": True,
                "value": self.value,
                "activity_id": self.activity_id,
                "warnings": self.warnings,
            }
        else:
            body = {
                "ok": False,
                "error": {
                    "category": self.error_category,
                    "code": self.error_code,
                    "field": self.error_field,
                    "message": self.error_message,
                    "retriable": self.retriable,
                },
            }
        text = json.dumps(body, default=str)
        if len(text) > max_chars:
            text = text[:max_chars] + "... [TRUNCATED: output too long]"
        return text


def parse_tool_result(tool: str, payload: Any, is_error: bool) -> ToolResult:
    """Parse the JSON envelope the server returns (structured_content or text)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return ToolResult(
                tool=tool,
                ok=False,
                error_category="unparseable",
                error_message=payload[:300] or "Empty tool response",
            )
    if not isinstance(payload, dict):
        return ToolResult(
            tool=tool,
            ok=False,
            error_category="unparseable",
            error_message="Tool response was not a JSON object",
        )
    trace = payload.get("trace") or {}
    if payload.get("ok") is True and not is_error:
        res = payload.get("result") or {}
        return ToolResult(
            tool=tool,
            ok=True,
            value=res.get("value"),
            activity_id=res.get("activity_id"),
            warnings=[str(w) for w in res.get("warnings") or []],
            cost_credits=trace.get("cost_credits"),
            plan_type=trace.get("plan_type"),
        )
    err = payload.get("error") or {}
    return ToolResult(
        tool=tool,
        ok=False,
        plan_type=trace.get("plan_type"),
        error_category=err.get("category"),
        error_code=err.get("code"),
        error_field=err.get("field"),
        error_message=err.get("message") or payload.get("message") or "Tool call failed",
        retriable=bool(err.get("retriable", False)),
    )


class McpSession(Protocol):
    async def list_tools(self) -> Any: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


ConfirmFn = Callable[[str, dict[str, Any], int], Awaitable[bool]]


def load_blocked(access_path: Path) -> set[str]:
    """Tools marked blocked in results/access.json (403 / plan errors). Never exposed or retried."""
    if not access_path.exists():
        return set()
    data = json.loads(access_path.read_text(encoding="utf-8"))
    return {
        _ACCESS_TO_MCP.get(name, name) for name, v in data.items() if v.get("status") == "blocked"
    }


class McpToolbox:
    def __init__(
        self,
        session: McpSession,
        specs: list[McpToolSpec],
        *,
        ledger: SpendLedger | None = None,
        confirm: ConfirmFn | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        backoff_base: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._session = session
        self.specs = {s.name: s for s in specs}
        self.ledger = ledger
        self.ledger_note = "agent call"
        self.confirm = confirm
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._rng = rng

    @classmethod
    async def create(
        cls,
        session: McpSession,
        *,
        allow: tuple[str, ...] = PLANNER_TOOLS,
        blocked: set[str] | None = None,
        **kw: Any,
    ) -> McpToolbox:
        listed = await session.list_tools()
        specs = [
            McpToolSpec(t.name, t.description or "", dict(t.input_schema))
            for t in listed.tools
            if t.name in allow and t.name not in (blocked or set())
        ]
        return cls(session, specs, **kw)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.input_schema,
                },
            }
            for s in self.specs.values()
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in self.specs:
            return ToolResult(
                tool=name,
                ok=False,
                error_category="unknown_tool",
                error_message=f"Unknown or unavailable tool '{name}'.",
            )
        cost = PAID_TOOL_COSTS.get(name, 0)
        if cost:
            if self.ledger is not None:
                self.ledger.guard(cost)  # raises SpendCapExceeded
            if self.confirm is not None and not await self.confirm(name, arguments, cost):
                return ToolResult(
                    tool=name,
                    ok=False,
                    error_category="declined",
                    error_message="The user declined this paid call.",
                )
        result = await self._call_with_retries(name, arguments, paid=bool(cost))
        if cost and self.ledger is not None:
            if result.ok:
                self.ledger.record(
                    name,
                    result.cost_credits if result.cost_credits is not None else cost,
                    self.ledger_note,
                )
            elif result.error_category == "ambiguous_failure":
                # Observed live (FRICTION_LOG F-22): a failed delivery can still be charged.
                # Count it against the cap, and verify with credit_balance afterwards.
                self.ledger.record(
                    name, cost, f"{self.ledger_note} UNCERTAIN: failed in transit, assumed charged"
                )
        return result

    async def _call_with_retries(
        self, name: str, arguments: dict[str, Any], *, paid: bool
    ) -> ToolResult:
        last: ToolResult | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = await asyncio.wait_for(self._session.call_tool(name, arguments), self.timeout)
            except (TimeoutError, ConnectionError, OSError, SdkMcpError) as exc:
                log.warning(
                    "mcp_transport_error", tool=name, attempt=attempt, error=type(exc).__name__
                )
                if paid:
                    # The server may have completed (and charged) the call. Do not repeat it.
                    return ToolResult(
                        tool=name,
                        ok=False,
                        error_category="ambiguous_failure",
                        error_message=f"The call to {name} failed in transit "
                        f"({type(exc).__name__}) and may have been charged. "
                        "Not retried automatically.",
                    )
                last = ToolResult(
                    tool=name,
                    ok=False,
                    error_category="transport",
                    error_message=f"Could not reach the FortyGuard MCP ({type(exc).__name__}).",
                    retriable=True,
                )
            else:
                # The text content is always the full envelope. For credit_balance the
                # structured_content holds only the value, so it is used as a fallback only.
                payload = raw.content[0].text if raw.content else raw.structured_content
                last = parse_tool_result(name, payload, bool(raw.is_error))
                log.info(
                    "mcp_call",
                    tool=name,
                    ok=last.ok,
                    attempt=attempt,
                    error=last.error_code,
                    cost=last.cost_credits,
                )
                if last.ok or not last.retriable:
                    return last
            if attempt < self.max_retries:
                await self._sleep(self._rng() * self.backoff_base * (2**attempt))
        assert last is not None
        return last


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return [exc]
    return [leaf for s in subs for leaf in _leaf_exceptions(s)]


@asynccontextmanager
async def connect(
    api_key: str, *, url: str = MCP_URL, timeout: float = 120.0
) -> AsyncIterator[Any]:
    """Open an authenticated MCP session (Streamable HTTP, Bearer auth)."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    connected = False
    try:
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
            ) as http_client,
            streamable_http_client(url, http_client=http_client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            connected = True
            yield session
    except BaseExceptionGroup as group:
        if connected:
            raise  # an error from the caller's own code, not a connection failure
        leaves = _leaf_exceptions(group)
        log.error("mcp_connect_failed", errors=[type(x).__name__ for x in leaves])
        raise McpConnectionError(
            "Could not connect to the FortyGuard MCP. Check TEMP_API_KEY and your network."
        ) from group
