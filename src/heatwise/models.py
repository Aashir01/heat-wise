"""Structured output models for the agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TimeWindow(BaseModel):
    start: str = Field(
        description="Local start, ISO 8601 with offset, e.g. 2026-11-17T14:00:00-07:00"
    )
    end: str = Field(description="Local end, ISO 8601 with offset")
    timezone: str | None = Field(default=None, description="IANA time zone, e.g. America/Phoenix")


class FinalAnswer(BaseModel):
    """The only way the agent ends a turn with an answer."""

    recommendation: str = Field(description="Plain-language answer for the user, 1-4 sentences")
    verdict: Literal["safe", "caution", "unsafe", "cannot_determine"]
    window: TimeWindow | None = Field(
        default=None, description="The time window the recommendation is about, if any"
    )
    confidence: Confidence
    caveats: list[str] = Field(
        description="Limits of the data and advice. Always include that this is not "
        "official safety "
        "or medical advice."
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Every assumption made, e.g. which city was meant when the name was ambiguous",
    )
    data_timestamp: str | None = Field(
        default=None, description="Timestamp (from the tool data) of the observation used"
    )
    locations: list[str] = Field(
        default_factory=list, description="City labels used, e.g. 'Austin, TX'"
    )


class Clarification(BaseModel):
    question: str
    options: list[str] = []


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any]
    ok: bool
    error: str | None = None
    cost_credits: int | None = None
    local: bool = False


class AgentResult(BaseModel):
    status: Literal["answered", "needs_clarification", "error", "step_limit"]
    answer: FinalAnswer | None = None
    clarification: Clarification | None = None
    error: str | None = None
    steps: int = 0
    tool_calls: list[ToolCallRecord] = []
    credits_spent: int = 0
    model: str = ""
