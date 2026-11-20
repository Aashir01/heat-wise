# Discovery: FortyGuard REST API and MCP

Captured 2026-11-20 (UTC). Everything below comes from my local copy of FortyGuard's docs (`docs/source/*.md`, not included in this repo) or from a saved raw response in `results/raw/`. Single calls are samples, not statistics.

## Summary

| Item | Observed |
|---|---|
| REST base URL | `https://api.fortyguard.com/v1` |
| REST auth | Header `api-key: <key>` |
| MCP endpoint | `https://tos-mcp.app.fortyguard.com/mcp`, Streamable HTTP, protocol `2025-11-25` |
| MCP auth | Header `Authorization: Bearer <key>` (differs from REST) |
| MCP server | `FortyGuard` version `4.0.11` |
| MCP tools | 11 (docs list 9; extra: `credit_balance`, `monitoring`) |
| Plan reported by server | `premium` (trace of `credit_balance`); dashboard shows a free key (F-04, F-13) |
| Credits at start | 1,000,000 (`credit_balance`) |
| Rate-limit headers | `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset` (undocumented). Limit 300 on `POST` submits, 1000 on `GET /status/{id}` |
| Timestamps | ISO 8601 with offset, local to the location, e.g. `2026-11-17T14:00:00-07:00`, plus `timezone` (`America/Phoenix`) and `timezone_offset_hours` in `env_params` metadata |
| Geohash fields | None seen in any response |
| Units | `env_params`: °C, %, mm, octas, AQI index, ppb, ppm, W/m². Heatmap `tcm`: °C. Routing: `_c` suffix, `distance_km`, `duration_min` |
| Missing values | Documented as JSON `null` (legacy `-999`). Not observed in the one location tested |
| Geography | US only. Docs say requests outside supported US coverage are not allowed (not yet tested) |
| Forecast | `env_params` rejects future dates (HTTP 400, free). `routing` accepted a departure one hour ahead |
| LLM | Nebius, OpenAI-compatible, 24 models listed (see "Model shortlist") |

## Access and cost per tool (this key)

From `results/access.json`, `results/spend_ledger.jsonl` and `credit_balance` readings.

| Tool / endpoint | Result | Observed cost |
|---|---|---|
| `credit_balance` (MCP) | OK, free | 0 |
| MCP `list_tools` | OK, free | 0 |
| REST `GET /v1/credits` | 404 (my one path guess) | 0 |
| REST `POST /v1/env_params` | OK | 2,900 per completed call |
| REST `POST /v1/heatmap` | OK | 4,220 |
| REST `POST /v1/routing` | OK | 3,500 |
| `env_params` with future date | HTTP 400, rejected | 0 |
| `satellite`, `streetview`, `report`, `monitoring` | Not called | docs: 14,400 / 8,600 / 8,600 / 3,500 |

Balance history: 1,000,000 -> 989,980 (2 `env_params`, 1 `heatmap`) -> 986,480 (routing). Evaluation spend at the end of Task 1: 13,520 of the 150,000 cap (final totals: `results/REPORT.md`).

## REST flow

1. `POST /v1/<endpoint>` returns `{"error":false,"status_code":200,"message":"... Submitted Successfully","data":{"activity_id":"<uuid>"}}`.
2. `GET /v1/status/<activity_id>` returns `data.status` of `Processing` or `Completed` (or `Failed`), and `data.result` when complete.
3. Observed polls to completion (5-7 s apart): `env_params` 2, `heatmap` 4, `routing` 5.
4. Observed single-call times: POST 2.8-5.1 s, each status poll 1.7-2.4 s. Not statistically meaningful yet.

### Example: `env_params` (REST)

Request body (`results/raw/env_params_phoenix_past.json`):

```json
{"latitude": 33.4484, "longitude": -112.074, "temperature": 35,
 "date_time": {"start_date": "2026-11-17", "start_time": "14:00", "filter_type": 1}}
```

Completed result, abridged (`results/raw/env_params_phoenix_past_status_last.json`):

```json
{"metadata": {"timezone": "America/Phoenix", "timezone_offset_hours": -7,
              "time_range": {"start": "2026-11-17T14:00:00-07:00", "interval": "1h", "count": 1}},
 "locations": [{"lat": 33.4484, "lon": -112.074, "elevation": 333.0, "temperature": 35.0,
   "parameters": {"heat_index_celsius": [38.7], "apparent_temperature_celsius": [17.9],
                  "relative_humidity_percent": [44.7], "wet_bulb_temperature_celsius": [13.7],
                  "precipitation_mm": [0.0], "cloud_cover_octas": [5], "air_quality:idx": [52.1]},
   "solar_irradiance": {"clear_sky": {"ghi": 517.86, "dni": 860.34, "dhi": 67.02}}}]}
```

The `temperature` field is the caller's own input echoed back. Only `heat_index_celsius` depends on it (F-05).

### Example: `heatmap` (REST)

About 1 km² polygon around Phoenix, `filter_type` 1, granularity 100. Completed result has `map_data` (GeoJSON FeatureCollection, 72 features) and `stats_data` with `temperature_stats` min 19.0663, max 19.3131, mean 19.1854, standard deviation 0.0779 (°C). Evidence: `results/raw/heatmap_status_last.json`.

### Example: `routing` (REST)

Request: origin (33.4484, -112.074), destination (33.4255, -111.94), `departure_time` `2026-11-20T11:00:00Z`. Result: one route, 16.185 km, 12.43 min, `thermal.mean_c` 34.614 (see F-14 for why this needs a cross-check). Evidence: `results/raw/routing_status_poll05_20261120T103307_054792Z.json`.

## MCP tools

Full descriptions and input schemas are in `results/raw/mcp_tools.json` (saved from `list_tools`). Every paid tool's description tells the client to show the exact parameters and cost and ask the user to confirm before calling (F-11).

| Tool | Purpose (from the server description) | Required inputs | Paid |
|---|---|---|---|
| `credit_balance` | Read available credits; free, read-only | none | no |
| `env_params` | Point-level environmental values for one coordinate and date | `latitude`, `longitude`, `temperature` (°C, caller-supplied; the description says never invent it), `date_time` | 2,110 basic/startup, 2,900 premium |
| `heatmap` | Submit a heatmap for a polygon and local date window; returns `activity_id` | `polygon_aoi`, `date_time` | 3,500 free/basic/startup, 4,220 premium |
| `heatmap_read` | Read stats of an existing heatmap | `activity_id` or `heatmap_id` | no |
| `report` | Heat-intelligence report; temperature in °F | `latitude`, `longitude`, `temperature`, `date`, `analysis` (1-5, premium max 2) | 6,300 free, 8,600 premium |
| `report_read` | Read a completed report | `analytics_id` | no |
| `satellite` | Land-cover segmentation from satellite imagery | `date_time` (+ `sat` point) | 14,400 premium |
| `streetview` | Segmentation of street-view imagery | `latitude`, `longitude`, `vertical_angle`, `horizontal_angle`, `back_view` | 8,600 |
| `temperature_routing` | Temperature-aware route generation, contiguous US only | `origin`, `destination`, `departure_time` | 3,500 |
| `temperature_routing_read` | Read an existing routing activity | `activity_id` | no |
| `monitoring` | Recurring monitors of a heatmap: 14 operations; `list_monitors` and `list_alerts` are free | `operation` | 3,500 for most operations |

Real MCP example call and response: `credit_balance` only (`results/raw/credits_start.json`): `{"ok":true,"result":{"tool":"credit_balance","value":{"available_credits":1000000},...},"trace":{"plan_type":"premium","decision":"allow","status":"ok","latency_ms":1883,"cost_credits":0}}`. No paid tool was called over MCP yet. REST examples above stand in for them until the approved eval runs.

Schema mismatches against the REST docs (F-06, F-07, F-08): `analysis` enum names, the earliest supported date, and the `report` temperature unit.

## Model shortlist (Nebius)

`GET /v1/models` returns 24 models with only `id`, `created`, `object`, `owned_by`. It does not say which support tool calling (`results/raw/nebius_models_*.json`). Shortlist from known capabilities; each must pass a tool-calling smoke test (Nebius tokens only, no FortyGuard credits) before it is fixed for the evaluation:

1. `Qwen/Qwen3-235B-A22B-Instruct-2507`: proposed primary. Large instruct model with function calling, no reasoning mode to add noise.
2. `openai/gpt-oss-120b`: proposed second model for the comparison run.
3. `Qwen/Qwen3-30B-A3B-Instruct-2507`: cheaper model for development and offline-adjacent tests.

I have no verified knowledge of the newer entries (Kimi-K3, GLM-5.x, DeepSeek-V4, MiniMax-M3 and others), so I did not shortlist them.

## Design consequences for heatwise

- There is no observed air temperature in `env_params` (F-05). A "will it be safe at 2pm" planner needs a real temperature from the user or another source, and the response's `apparent_temperature_celsius` and `wet_bulb_temperature_celsius` are the only provider-sourced heat-stress values.
- No forecast from `env_params` (F-09). "Tomorrow" questions must be answered honestly as unsupported, or routed to `temperature_routing` (accepts future departure; F-14 caveat).
- No city input anywhere. heatwise needs its own gazetteer.
- Credits are charged per completed call and re-runs are charged again, so the agent and eval need a spend guard and a cache.
