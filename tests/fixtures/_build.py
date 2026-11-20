"""Rebuild tests/fixtures/*.json from the real, already-redacted captures in results/raw/.

Provenance: every fixture body is a real FortyGuard response body. Only two reductions are made:
heatmap `map_data.features` and routing geometry coordinates are truncated to keep fixtures small.
Run: python tests/fixtures/_build.py
"""

from __future__ import annotations

import glob
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "results" / "raw"
OUT = Path(__file__).resolve().parent


def body(pattern: str) -> Any:
    path = sorted(glob.glob(str(RAW / pattern)))[-1]
    return json.loads(Path(path).read_text(encoding="utf-8"))["response"]["body"]


def write(name: str, obj: Any) -> None:
    (OUT / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    write("env_submit.json", body("env_params_phoenix_past.json"))
    write("status_processing.json", body("routing_status_poll01_2*.json"))
    write("env_status_completed.json", body("env_params_phoenix_past_status_last.json"))

    heat = body("heatmap_status_last.json")
    heat["data"]["result"]["map_data"]["features"] = heat["data"]["result"]["map_data"]["features"][
        :2
    ]
    write("heatmap_status_completed.json", heat)

    route = body("routing_past_1400local_status_poll05_2*.json")
    for feat in route["data"]["result"]["result_routing"]["features"]:
        feat["geometry"]["coordinates"] = feat["geometry"]["coordinates"][:5]
    write("routing_status_completed.json", route)

    write("error_400_future_date.json", body("env_params_phoenix_forecast_2*.json"))
    write("error_400_before_2021.json", body("env_params_datebound_2020_2*.json"))
    write("error_404_endpoint.json", body("credits_rest_404.json"))

    def mcp(pattern: str) -> Any:
        path = sorted(glob.glob(str(RAW / pattern)))[-1]
        return json.loads(Path(path).read_text(encoding="utf-8"))["result"]["structured_content"]

    write("mcp_env_day.json", mcp("mcp_env_params_phoenix_day_2*.json"))
    write("mcp_error_future_date.json", mcp("mcp_env_params_future_date_2*.json"))
    path = sorted(glob.glob(str(RAW / "credits_after_mcp_env_2*.json")))[-1]
    text = json.loads(Path(path).read_text(encoding="utf-8"))["result"]["content"][0]["text"]
    write("mcp_credit_balance.json", json.loads(text))  # full envelope from the text content
    path = sorted(glob.glob(str(RAW / "q02_heatmap_read_after_2*.json")))[-1]
    write(
        "mcp_heatmap_read_completed.json",
        json.loads(Path(path).read_text(encoding="utf-8"))["envelope"],
    )


if __name__ == "__main__":
    main()
