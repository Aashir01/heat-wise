"""Tool-calling smoke test for shortlisted Nebius models (costs LLM tokens only, no FortyGuard credits).

Pass = (1) the model calls the tool with valid JSON args containing numeric lat/lon, and
(2) after receiving a tool result it answers in plain text. Stops at the first model that passes.
Raw request/response pairs are saved to results/raw/ with unique timestamped names.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from heatwise.secrets import get_secret, load_env, redact

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "raw"
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
SHORTLIST = [
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "openai/gpt-oss-120b",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
]
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_conditions",
            "description": "Get environmental conditions for a coordinate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                },
                "required": ["latitude", "longitude"],
            },
        },
    }
]


def save(name: str, obj: Any) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    path = RAW / f"{name}_{stamp}.json"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(redact(obj), indent=2, ensure_ascii=False))
    return path.name


def test_model(client: OpenAI, model: str) -> tuple[bool, str]:
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": "What are conditions at latitude 30.27, longitude -97.74 (Austin)? Use the tool.",
        }
    ]
    try:
        first = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, temperature=0
        )  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - record any API failure as a failed smoke test
        save("nebius_smoke_error", {"model": model, "step": 1, "error": repr(exc)})
        return False, f"step 1 error: {type(exc).__name__}: {str(exc)[:200]}"
    msg = first.choices[0].message
    save("nebius_smoke_step1", {"model": model, "response": first.model_dump()})
    if not msg.tool_calls:
        return False, "no tool call returned"
    call = msg.tool_calls[0]
    try:
        args = json.loads(call.function.arguments)
        float(args["latitude"]), float(args["longitude"])
    except (ValueError, KeyError, TypeError) as exc:
        return False, f"bad tool args: {call.function.arguments!r} ({exc})"
    if call.function.name != "get_conditions":
        return False, f"wrong tool name {call.function.name!r}"
    messages.append(msg.model_dump(exclude_none=True))
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps({"apparent_temperature_c": 27.5}),
        }
    )
    try:
        second = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, temperature=0
        )  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        save("nebius_smoke_error", {"model": model, "step": 2, "error": repr(exc)})
        return False, f"step 2 error: {type(exc).__name__}: {str(exc)[:200]}"
    save("nebius_smoke_step2", {"model": model, "response": second.model_dump()})
    text = second.choices[0].message.content or ""
    if "27" not in text:
        return False, f"final answer does not use the tool result: {text[:120]!r}"
    return True, "ok"


def main() -> None:
    load_env(ROOT / ".env")
    client = OpenAI(base_url=BASE_URL, api_key=get_secret("NEBIUS_API_KEY"), timeout=90)
    for model in SHORTLIST:
        ok, detail = test_model(client, model)
        print(f"{model}: {'PASS' if ok else 'FAIL'} - {detail}")
        if ok:
            print(f"FIXED MODEL: {model}")
            return
    print("no shortlisted model passed", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
