"""Swappable LLM provider interface.

Messages and tool schemas use the OpenAI chat format, which Nebius speaks. Another provider only
has to implement `LLMProvider.chat`. `MockProvider` drives the offline tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    InternalServerError,
    RateLimitError,
)

log = structlog.get_logger("llm")


class LLMError(Exception):
    """Provider failure with a message that is safe to show to the user."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    arguments_error: str | None = None  # set when the model produced invalid JSON arguments


@dataclass
class LLMReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        return msg


class LLMProvider(Protocol):
    model: str

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMReply: ...


def parse_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    if not raw or not raw.strip():
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON arguments: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, "tool arguments must be a JSON object"
    return parsed, None


class NebiusProvider:
    """OpenAI-compatible chat completions on Nebius Token Factory."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        timeout: float = 90.0,
        max_retries: int = 3,
        http_client: Any | None = None,
    ) -> None:
        self.model = model
        # The OpenAI SDK retries connection errors, 408/409/429 and 5xx with exponential backoff.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools or None,  # type: ignore[arg-type]
                temperature=0,
            )
        except AuthenticationError as exc:
            raise LLMError("The LLM API key (NEBIUS_API_KEY) is missing or invalid.") from exc
        except RateLimitError as exc:
            raise LLMError("The LLM provider rate limit was hit. Try again shortly.") from exc
        except (APIConnectionError, APITimeoutError, InternalServerError) as exc:
            raise LLMError(
                "Could not reach the LLM provider (network, timeout or server error)."
            ) from exc
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            if tc.type != "function":
                continue
            args, err = parse_arguments(tc.function.arguments)
            calls.append(ToolCall(tc.id, tc.function.name, args, err))
        log.info("llm_reply", model=self.model, tool_calls=[c.name for c in calls])
        return LLMReply(content=msg.content, tool_calls=calls)


class AnthropicProvider:
    """Placeholder showing where a second provider plugs in. Not implemented or tested."""

    model = "anthropic-not-implemented"

    def __init__(self, *_: Any, **__: Any) -> None:
        raise NotImplementedError(
            "AnthropicProvider is a stub. Implement LLMProvider.chat with the Anthropic SDK."
        )

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        raise NotImplementedError


class MockProvider:
    """Scripted replies for offline tests: each step is an LLMReply or fn(messages)."""

    model = "mock"

    def __init__(self, script: list[LLMReply | Callable[[list[dict[str, Any]]], LLMReply]]) -> None:
        self._script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        self.calls.append([dict(m) for m in messages])
        if not self._script:
            raise AssertionError("MockProvider script exhausted")
        step = self._script.pop(0)
        return step(messages) if callable(step) else step
