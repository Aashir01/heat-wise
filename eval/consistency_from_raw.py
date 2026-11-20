"""Consistency and sanity checks (b) computed only from already-saved raw responses. No API calls.

Checks:
1. REST env_params, same hour, reference temperature 35 vs 25: which parameters differ.
2. Same hour (2026-11-17 14:00 Phoenix) from REST single-hour vs MCP whole-day hourly series.
3. MCP env_params result vs the same activity re-read over REST /status.
4. Same place and hour across products: env_params, heatmap (tcm), routing.
5. Timestamp sanity: offsets match the timezone, array lengths match metadata.count.
6. Unit/range sanity on every env_params value.
Output: results/raw/consistency/consistency_<timestamp>.json (never overwritten).
"""

from __future__ import annotations

import glob
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "raw"

RANGES = {  # inclusive plausible ranges by unit, not quality judgements
    "relative_humidity_percent": (0, 100),
    "cloud_cover_octas": (0, 8),
    "precipitation_mm": (0, 500),
    "apparent_temperature_celsius": (-60, 60),
    "wet_bulb_temperature_celsius": (-60, 40),
    "heat_index_celsius": (-60, 80),
    "methane_ppb": (1000, 3000),
    "co2_ppm": (300, 600),
}


def latest(pattern: str) -> Path:
    files = sorted(glob.glob(str(RAW / pattern)))
    if not files:
        raise FileNotFoundError(pattern)
    return Path(files[-1])


def load(pattern: str) -> Any:
    return json.loads(latest(pattern).read_text(encoding="utf-8"))


def rest_result(pattern: str) -> dict[str, Any]:
    return load(pattern)["response"]["body"]["data"]["result"]  # type: ignore[no-any-return]


def main() -> None:
    out: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sources": {},
        "checks": {},
    }

    # 1. reference temperature dependence
    a_src, b_src = (
        "env_params_phoenix_past_status_last.json",
        "env_params_phoenix_past_ref25_status_last.json",
    )
    a = rest_result(a_src)["locations"][0]
    b = rest_result(b_src)["locations"][0]
    diff = {
        k: [a["parameters"][k], b["parameters"].get(k)]
        for k in a["parameters"]
        if a["parameters"][k] != b["parameters"].get(k)
    }
    out["checks"]["reference_temperature_dependence"] = {
        "inputs": [a["temperature"], b["temperature"]],
        "differing_parameters": diff,
        "identical_parameters": sum(
            a["parameters"][k] == b["parameters"].get(k) for k in a["parameters"]
        ),
        "total_parameters": len(a["parameters"]),
    }
    out["sources"]["reference_temperature_dependence"] = [a_src, b_src]

    # 2. REST single hour vs MCP whole-day series at the same hour
    mcp_src = latest("mcp_env_params_phoenix_day_2*.json").name
    mcp_env = json.loads(
        json.loads((RAW / mcp_src).read_text(encoding="utf-8"))["result"]["content"][0]["text"]
    )
    raw = mcp_env["result"]["value"]["raw"]
    stamps = raw["metadata"]["timestamps"]
    idx = stamps.index("2026-11-17T14:00:00-07:00")
    mcp_params = raw["locations"][0]["parameters"]
    same_hour = {}
    for k, v in a["parameters"].items():
        if k in mcp_params:
            same_hour[k] = {
                "rest_single_hour": v[0],
                "mcp_day_series": mcp_params[k][idx],
                "equal": v[0] == mcp_params[k][idx],
            }
    out["checks"]["same_hour_rest_vs_mcp"] = {
        "hour": stamps[idx],
        "compared": len(same_hour),
        "equal": sum(x["equal"] for x in same_hour.values()),
        "values": same_hour,
        "first_hour_snapshot_apparent_c": mcp_env["result"]["value"]["parameters"].get(
            "apparent_temperature_celsius"
        ),
        "hour_14_apparent_c": mcp_params["apparent_temperature_celsius"][idx],
    }
    out["sources"]["same_hour_rest_vs_mcp"] = [a_src, mcp_src]

    # 3. MCP result vs REST re-read of the same activity
    reread_src = latest("retrieval_rest_status_of_mcp_env_2*.json").name
    reread = json.loads((RAW / reread_src).read_text(encoding="utf-8"))["response"]["body"]["data"][
        "result"
    ]
    out["checks"]["mcp_vs_rest_reread"] = {
        "identical": reread == raw,
        "parameters_identical": reread["locations"][0]["parameters"] == mcp_params,
    }
    out["sources"]["mcp_vs_rest_reread"] = [mcp_src, reread_src]

    # 4. same place and hour across products
    heat_src = "heatmap_status_last.json"
    hm = rest_result(heat_src)["stats_data"]["temperature_stats"]
    route_src = latest("routing_past_1400local_status_poll05_2*.json").name
    route = json.loads((RAW / route_src).read_text(encoding="utf-8"))["response"]["body"]["data"][
        "result"
    ]
    thermal = route["result_routing"]["features"][0]["properties"]["thermal"]
    q02_src = latest("q02_heatmap_read_after_2*.json").name
    q02 = json.loads((RAW / q02_src).read_text(encoding="utf-8"))["envelope"]["result"]["value"]
    out["checks"]["cross_product_same_hour"] = {
        "place_hour": "Phoenix, 2026-11-17 14:00 local (America/Phoenix, UTC-7)",
        "env_params_apparent_c": a["parameters"]["apparent_temperature_celsius"][0],
        "env_params_wet_bulb_c": a["parameters"]["wet_bulb_temperature_celsius"][0],
        "heatmap_tcm_min_c": hm["minimum"],
        "heatmap_tcm_max_c": hm["maximum"],
        "heatmap_tcm_mean_c": hm["mean"],
        "routing_mean_c": thermal["mean_c"],
        "routing_peak_c": thermal["peak_c"],
        "routing_minus_heatmap_max_c": round(thermal["mean_c"] - hm["maximum"], 2),
        "heatmap_15h_mcp_mean_c": q02.get("mean_temperature_celsius"),
    }
    out["sources"]["cross_product_same_hour"] = [a_src, heat_src, route_src, q02_src]

    # 5 and 6. timestamp and range sanity over every saved env_params result
    sanity = []
    env_sources = [
        ("rest", a_src),
        ("rest", b_src),
        ("mcp_day", mcp_src),
        ("rest_reread", reread_src),
    ]
    results = [rest_result(a_src), rest_result(b_src), raw, reread]
    for (kind, src), res in zip(env_sources, results, strict=True):
        md = res["metadata"]
        offset = f"{int(md['timezone_offset_hours']):+03d}:00"
        ts_ok = all(s.endswith(offset) for s in md["timestamps"])
        loc = res["locations"][0]
        lengths_ok = all(len(v) == md["time_range"]["count"] for v in loc["parameters"].values())
        out_of_range = [
            (k, x)
            for k, vals in loc["parameters"].items()
            if k in RANGES
            for x in vals
            if x is not None and not RANGES[k][0] <= x <= RANGES[k][1]
        ]
        nulls = sum(x is None for vals in loc["parameters"].values() for x in vals)
        legacy = sum(x == -999 for vals in loc["parameters"].values() for x in vals)
        sanity.append(
            {
                "source": src,
                "kind": kind,
                "timezone": md["timezone"],
                "offset": offset,
                "timestamps_match_offset": ts_ok,
                "array_lengths_match_count": lengths_ok,
                "n_values": sum(len(v) for v in loc["parameters"].values()),
                "out_of_range": out_of_range,
                "nulls": nulls,
                "legacy_minus_999": legacy,
            }
        )
    out["checks"]["sanity"] = sanity
    out["range_rules"] = RANGES

    outdir = RAW / "consistency"
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    path = outdir / f"consistency_{stamp}.json"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(out, indent=2))
    c = out["checks"]
    print(
        "ref temp: differing", list(c["reference_temperature_dependence"]["differing_parameters"])
    )
    print(
        "same hour REST vs MCP: equal",
        c["same_hour_rest_vs_mcp"]["equal"],
        "of",
        c["same_hour_rest_vs_mcp"]["compared"],
    )
    print("MCP vs REST reread identical:", c["mcp_vs_rest_reread"])
    print("cross product:", c["cross_product_same_hour"])
    for s in sanity:
        print(
            "sanity",
            s["source"][:40],
            s["timestamps_match_offset"],
            s["array_lengths_match_count"],
            "out_of_range",
            s["out_of_range"],
            "nulls",
            s["nulls"],
            "-999",
            s["legacy_minus_999"],
        )
    print("saved", path.name)


if __name__ == "__main__":
    main()
