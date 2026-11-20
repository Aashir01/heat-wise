import json
from pathlib import Path

import pytest

from heatwise.cli import build_parser, format_event, render_result, write_trace
from heatwise.models import AgentResult, Clarification, FinalAnswer, ToolCallRecord

KEY = "fg_cli_test_secret_key_999"


def answered() -> AgentResult:
    return AgentResult(
        status="answered",
        answer=FinalAnswer(
            recommendation="Fine.",
            verdict="safe",
            confidence="high",
            caveats=["Not official advice."],
            assumptions=["Assumed Portland, OR."],
            locations=["Portland, OR"],
        ),
        tool_calls=[
            ToolCallRecord(name="env_params", arguments={}, ok=True, cost_credits=2900),
            ToolCallRecord(name="resolve_city", arguments={}, ok=True, local=True),
        ],
        steps=3,
        credits_spent=2900,
        model="m",
    )


def test_render_answer_lists_assumptions_caveats_and_tool_calls() -> None:
    text = render_result(answered())
    assert "Verdict: SAFE (confidence: high)" in text
    assert "Assumed Portland, OR." in text and "Not official advice." in text
    assert "mcp   env_params credits=2900" in text and "2900 credits" in text


def test_render_clarification_and_error() -> None:
    clar = AgentResult(
        status="needs_clarification",
        clarification=Clarification(question="Which one?", options=["OR", "ME"]),
    )
    assert "Which one?" in render_result(clar) and "- ME" in render_result(clar)
    err = AgentResult(status="error", error="boom")
    assert "boom" in render_result(err)


def test_format_event_variants() -> None:
    assert format_event({"type": "step", "n": 2}) == "[step 2] thinking..."
    assert "FAILED: x" in (
        format_event({"type": "tool_result", "name": "t", "ok": False, "error": "x"}) or ""
    )
    assert format_event({"type": "unknown"}) is None


def test_trace_is_unique_and_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMP_API_KEY", KEY)
    msgs = [{"role": "tool", "content": f"leaked {KEY}"}]
    p1 = write_trace(tmp_path, "q", answered(), msgs)
    p2 = write_trace(tmp_path, "q", answered(), msgs)
    assert p1 != p2
    assert KEY not in p1.read_text(encoding="utf-8")
    assert json.loads(p1.read_text(encoding="utf-8"))["result"]["status"] == "answered"


def test_parser_flags() -> None:
    a = build_parser().parse_args(
        ["ask", "q", "--temperature", "21.5", "--yes", "--non-interactive"]
    )
    assert a.temperature == 21.5 and a.yes and a.non_interactive and a.max_steps == 12
