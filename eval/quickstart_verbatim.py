"""Docs check (e): run the Quickstart's two Python blocks as written.

The code is read from a local copy of FortyGuard's Quickstart (docs/source/quickstart.md, not
included in this repo); see load_steps().

The only changes to the docs code: the placeholder "YOUR_API_KEY" is replaced in memory by the key
from .env, and print() output is captured. Every HTTP request/response is recorded by a transport
hook (redacted), so the docs code itself is untouched. Spend: one heatmap (4,220 credits observed).
Output: results/raw/quickstart/quickstart_<timestamp>.json (never overwritten).
"""

from __future__ import annotations

import contextlib
import io
import json
import time
import traceback
from datetime import UTC, datetime
from typing import Any

import requests

from probe import RAW, ROOT, get_secret, guard_spend, load_env, now_iso, redact

QUICKSTART = ROOT / "docs" / "source" / "quickstart.md"


def load_steps() -> tuple[str, str]:
    """Read the Quickstart's two Python blocks from a local copy of FortyGuard's docs.

    FortyGuard's documentation is not redistributed in this repo. To re-run this check, copy the
    Quickstart page text (including "Step 1 - Submit your request" and "Step 2 - Check task
    status" with their code) into docs/source/quickstart.md.
    """
    if not QUICKSTART.exists():
        raise SystemExit(
            "docs/source/quickstart.md not found. Copy FortyGuard's Quickstart page text there "
            "(see README, 'Docs check')."
        )
    text = QUICKSTART.read_text(encoding="utf-8")
    after_1 = text.split("Step 1 \u2014 Submit your request", 1)[1]
    step_1 = after_1.split("\nCopy\n", 1)[0].strip("\n") + "\n"
    after_2 = text.split("Step 2 \u2014 Check task status", 1)[1]
    step_2 = after_2.strip("\n") + "\n"
    return step_1, step_2


calls: list[dict[str, Any]] = []
_orig_send = requests.Session.send


def _recording_send(self: requests.Session, request: requests.PreparedRequest, **kw: Any) -> Any:
    started = time.perf_counter()
    resp = _orig_send(self, request, **kw)
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:500]
    if isinstance(body, dict) and isinstance(body.get("data"), dict) and "result" in body["data"]:
        result = body["data"]["result"] or {}
        md = result.get("map_data") if isinstance(result, dict) else None
        if isinstance(md, dict) and isinstance(md.get("features"), list):
            result["map_data"] = {
                "type": md.get("type"),
                "n_features": len(md["features"]),
                "first_feature": md["features"][0] if md["features"] else None,
                "_note": "features truncated by the recorder",
            }
    calls.append(
        {
            "at": now_iso(),
            "method": request.method,
            "url": request.url,
            "request_headers": dict(request.headers),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "status_code": resp.status_code,
            "ratelimit_remaining": resp.headers.get("x-ratelimit-remaining"),
            "body": body,
        }
    )
    return resp


def run_block(name: str, code: str, env: dict[str, Any]) -> dict[str, Any]:
    out = io.StringIO()
    started = time.perf_counter()
    rec: dict[str, Any] = {"block": name}
    try:
        with contextlib.redirect_stdout(out):
            exec(
                compile(code.replace("YOUR_API_KEY", get_secret("TEMP_API_KEY")), name, "exec"), env
            )
        rec["outcome"] = "ok"
    except Exception as exc:  # noqa: BLE001 - the failure is the finding
        rec["outcome"] = "exception"
        rec["exception"] = f"{type(exc).__name__}: {exc}"
        rec["traceback"] = traceback.format_exc(limit=3)
    rec["wall_seconds"] = round(time.perf_counter() - started, 1)
    rec["stdout"] = out.getvalue()
    return rec


def main() -> None:
    load_env(ROOT / ".env")
    guard_spend(4220)
    requests.Session.send = _recording_send  # type: ignore[method-assign]
    step_1, step_2 = load_steps()
    env: dict[str, Any] = {}
    blocks = [run_block("quickstart_step1", step_1, env)]
    if blocks[0]["outcome"] == "ok":
        blocks.append(run_block("quickstart_step2", step_2, env))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    outdir = RAW / "quickstart"
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"quickstart_{stamp}.json"
    payload = {
        "captured_at": now_iso(),
        "source": "docs/source/quickstart.md",
        "blocks": blocks,
        "http_calls": calls,
        "activity_id": env.get("activity_id"),
    }
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(redact(payload), indent=2, ensure_ascii=False))
    for b in blocks:
        print(b["block"], b["outcome"], b["wall_seconds"], "s", b.get("exception", ""))
        print("  stdout:", b["stdout"].strip().replace("\n", " | ")[:300])
    print("http calls:", len(calls), "| statuses:", [c["status_code"] for c in calls])
    print("saved", path.name)


if __name__ == "__main__":
    main()
