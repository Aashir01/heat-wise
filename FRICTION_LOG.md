# Friction log

Note for readers of the published repo: references to `docs/source/` point to my local copy of FortyGuard's documentation, which is not included here. Files marked "not published" (the report PDF and LLM session traces) were kept local.

Real observations only. Times are UTC, from 2026-11-20. Raw evidence is under `results/raw/`.

## F-01 Credits endpoint has no documented path (REST)
- 10:18 UTC. Docs `task-management.md` / `monitoring-alerting.md` show "Check API Credits Usage" as `GET` with a key-entry widget but no URL.
- Repro: `GET https://api.fortyguard.com/v1/credits` with the `api-key` header returns 404 `{"error":true,"status_code":404,"details":{"message":"Endpoint not found"}}`. This was my single guess at the path, so it says nothing about the real path.
- Evidence: `results/raw/credits_rest_404.json` (reconstructed from console output after the original capture was overwritten).

## F-02 MCP exposes 11 tools; docs list 9
- 10:20 UTC. `list_tools` on `https://tos-mcp.app.fortyguard.com/mcp` returns `credit_balance` and `monitoring` in addition to the 9 documented tools (server `FortyGuard` 4.0.11, protocol 2025-11-25).
- `credit_balance` is the only documented-nowhere way I found to read credits. It is described as free and read-only.
- Repro: `python eval/probe.py mcp`. Evidence: `results/raw/mcp_tools.json`.

## F-03 Credit balance via MCP took ~10 s for a free read
- 10:21 and 10:2x UTC. `credit_balance` measured 10.5 s and 11.2 s client-side, while the server trace reports `latency_ms: 1883`. This client time includes session setup, so it is not a clean call latency. It needs a proper cold/warm measurement before it becomes a finding.
- Evidence: `results/raw/credits_start.json`, `results/raw/credits_after_heatmap.json`.

## F-04 Plan reported by the server differs from the dashboard
- 10:21 UTC. The dashboard shows a free key. The MCP `credit_balance` trace says `plan_type: "premium"`, and the balance was 1,000,000 credits.
- Billing matched Premium prices: 989,980 left after 2 `env_params` (2,900 each) and 1 `heatmap` (4,220), a drop of 10,020. At the free/basic prices in the docs the drop would be 7,720.
- Evidence: `results/raw/credits_start.json`, `results/raw/credits_after_heatmap.json`.
- Dashboard check (see F-13): the user's dashboard showed no credit information, so the plan label cannot be cross-checked there.

## F-05 `env_params` has no observed air-temperature field; the required `temperature` is echoed back
- 10:22 UTC. The `temperature` input is required (REST docs: "Temperature value in degrees Celsius for the location"). The MCP description says to ask the user for it and never invent it.
- I passed `35` (the docs example value), then `25`, for the same Phoenix hour (2026-11-17 14:00 local).
- Exact paths in the completed status response (`data.result...`):
  - `data.result.locations[0].temperature` is the input echoed back (35.0, then 25.0).
  - `data.result.locations[0].parameters` has exactly these keys: `heat_index_celsius`, `apparent_temperature_celsius`, `relative_humidity_percent`, `precipitation_mm`, `cloud_cover_octas`, `wet_bulb_temperature_celsius`, `air_quality:idx`, `air_quality_pm2p5:idx`, `air_quality_pm10:idx`, `air_quality_no2:idx`, `aqi_us_co`, `air_quality_o3:idx`, `air_quality_so2:idx`, `methane_ppb`, `co2_ppm`. None of them is an air temperature.
  - Other fields under `data.result.locations[0]`: `lat`, `lon`, `elevation`, `solar_irradiance`. `data.result.metadata` has `timezone`, `timezone_offset_hours`, `time_range`, `timestamps`.
  - Caveat: this is the default parameter set. The `analysis` list has no air-temperature option either (enum in `results/raw/mcp_tools.json`).
- Only `heat_index_celsius` changed between the two calls (38.7 vs 25.7). The other 14 parameters were identical.
- Consequence: a caller who does not already know the air temperature cannot get a meaningful heat index. The same call returned `apparent_temperature_celsius` 17.9 and `heat_index_celsius` 38.7 for one hour.
- Repro: `python eval/probe.py env`, then the same with a different `REF_TEMP_C`. Evidence: `results/raw/env_params_phoenix_past_status_last.json`, `results/raw/env_params_phoenix_past_ref25_status_last.json`.

## F-06 `env_params` analysis names differ between REST docs and MCP schema
- 10:20 UTC. REST docs list `air_quality:idx`, `air_quality_pm2p5:idx`, `elevation`, etc. The MCP input schema enum uses `aqi_us`, `aqi_us_pm25`, `elevation_meters`, etc. The REST response keys use the REST style (`air_quality:idx`, `air_quality_pm2p5:idx`).
- Evidence: `results/raw/mcp_tools.json`, `results/raw/env_params_phoenix_past_status_last.json`.

## F-07 Earliest supported date: REST heatmap docs say 2019, live `env_params` enforces 2021
- Saved docs copy (`docs/source/endpoints.md:33`, heatmap): "default earliest date 2019-01-01". Live MCP descriptions for `env_params`, `heatmap` and `report` say 2021-01-01.
- Live check, 10:45 UTC: `POST /v1/env_params` with `start_date` 2020-06-15 returned HTTP 400 `"... is before the earliest supported date (2021-01-01). This request date is not accepted; available data may start later or vary by source."`, code `NO_COVERAGE`, free. So `env_params` live matches the MCP text (2021).
- Not checked live: REST `heatmap` with a 2019/2020 date (it would cost 4,220 credits if accepted). The docs' 2019 claim for heatmap is therefore unverified against the live API. Also not verified against the live docs page (it is not fetchable by me; compared against the saved copy only).
- Evidence: `results/raw/env_params_datebound_2020_20261120T104511_704297Z.json`.

## F-08 Report temperature unit: docs contradict each other; the live API uses degrees Fahrenheit (verified)
- Saved docs copy: `docs/source/available-tools.md:131` says "temperature - Reference temperature in degrees Celsius." for `report`. `docs/source/endpoints.md:392` says "Temperature value in degrees Fahrenheit" and `:397` (the `date` field) says "Date for the supplied Celsius temperature". The live MCP `report` description says degrees Fahrenheit.
- Live check, 10:47 UTC: `POST /v1/heat_intelligence` with `temperature` 95, Phoenix, `date` 2026-11-17, `analysis` `["environmental"]`. The generated PDF (14 pages) states on page 1 "35.0 C (95.0 F)" and the text throughout treats 95 F as an anomalously warm November reading. So the value is read as Fahrenheit.
- Contrast: `env_params` takes Celsius (F-05) and `report` takes Fahrenheit, with no unit in the field name in either.
- Cost: 8,600 credits (balance 986,480 to 974,380 after the routing probe in F-14). Took 23 status polls (about 2.5 min).
- Not verified against the live docs page (not fetchable by me; saved copy only).
- Evidence: `results/raw/report_submit_*.json` (request parameters: `temperature` 95, lat 33.4484, lon -112.074, `date` 2026-11-17, `analysis` ["environmental"]) and `results/raw/report_status_poll*.json` (Completed). The generated PDF and its extracted text are not published; the quoted line "35.0 C (95.0 F)" from page 1 is the only content cited.

## F-09 Future dates are rejected by `env_params` (verified)
- 10:33 UTC. The MCP description says future dates are unsupported, and the homepage and docs describe forecasting. For a heat-safety planner, "tomorrow" questions cannot be answered from `env_params`.
- Repro: `python eval/probe.py forecast` (POST `/v1/env_params`, Phoenix, `start_date` 2026-11-21, `filter_type` 1).
- Result: HTTP 400, body `{"error":true,"status_code":400,"message":"Field 'date_time.start_date' (2026-11-21) is in the future. Requests must be for a past or present date.","field":"date_time.start_date","code":"NO_COVERAGE"}`. Credits were not charged (balance 989,980 before and 986,480 after, and the 3,500 difference is the routing call).
- Good: the message names the field and the rule, and the response has a machine-readable `code`. Rough: `NO_COVERAGE` is a misleading code for a date-rule violation, and the status code is 400 while the docs' status table lists "400 / 422".
- Evidence: `results/raw/env_params_phoenix_forecast_20261120T103335_067352Z.json`.

## F-10 Rate-limit headers exist but are undocumented
- 10:22 UTC. Responses carry `x-ratelimit-limit`, `x-ratelimit-remaining` and `x-ratelimit-reset`. `POST /env_params` showed limit 300, `GET /status/{id}` showed limit 1000. The reset value is an epoch number (e.g. 1795170180). The docs give no numbers.
- Inference from 12 saved samples (not documented): `x-ratelimit-reset` is the epoch of the next minute boundary, so the windows are per minute (300 for submits, 1000 for status reads, tracked in separate buckets; `remaining` went 299, 298 on consecutive submits).
- Evidence: headers in `results/raw/env_params_phoenix_past.json` and `..._status_last.json`.

## F-11 MCP tool descriptions require confirmation before paid calls
- `env_params`, `heatmap`, `report` and others say to show exact parameters and cost and ask for confirmation. A non-interactive agent has no one to ask. Design consequence for heatwise, not a defect.

## F-12 Heatmap request accepted a bare Polygon
- 10:2x UTC. `POST /v1/heatmap` with `polygon_aoi` as a bare `Polygon` (the endpoint page style) was accepted (HTTP 200, completed). The quickstart uses a `FeatureCollection`. Both are probably accepted (the MCP schema says Polygon, Feature or FeatureCollection). Only the Polygon form was tested.
- Evidence: `results/raw/heatmap_submit.json`.

## F-13 Dashboard shows no credit balance or history; usage counter did not move
- Reported by the user after the heatmap call (10:23 UTC); the exact time of the check was not recorded. The user will re-check later.
- Observed on the dashboard: "Heatmap Usage Today" stayed at 0/30 after one REST `POST /v1/heatmap` that completed. The dashboard shows no credit balance and no credit history anywhere.
- Meanwhile `credit_balance` (MCP) dropped by 10,020 credits across the three completed calls. So credits are only visible through the undocumented MCP tool `credit_balance` (F-02), and the REST credits path is undocumented (F-01).
- Open question for support: does the dashboard counter cover only dashboard-created heatmaps and not API-created ones? Not tested, so not a finding yet.

## F-14 Routing temperatures disagree with heatmap and env_params for the same hour (verified; cause unknown)
- Request check: the routing request contains only `origin`, `destination` and `departure_time`. The REST docs (`endpoints.md:640-650`) and the live MCP schema (`temperature_routing`, properties `origin`, `destination`, `departure_time`) have no reference-temperature input. So the temperatures come from the service, not from my input.
- First call, 10:32 UTC: Phoenix (33.4484,-112.074) to (33.4255,-111.94), `departure_time` `2026-11-20T11:00:00Z` (04:00 local, America/Phoenix is UTC-7). Result: 16.185 km, 12.43 min, `thermal.mean_c` 34.614, `peak_c` 34.88, `threshold_c` 32.0.
- Same-hour cross-check, 10:44 UTC: the same route with `departure_time` `2026-11-17T21:00:00Z` (14:00 local on 2026-11-17, the same hour as my earlier `env_params` and heatmap calls). Result: `thermal.mean_c` 41.814, `peak_c` 41.96, `segment_temp_c` min 41.66 / max 41.96 across 86 segments, `degree_minutes_above_threshold` 122.1.
- For that same hour and area: heatmap `tcm` `stats_data.temperature_stats` min 19.0663 / max 19.3131 / mean 19.1854 (polygon about 1 km around the route origin), and `env_params` `apparent_temperature_celsius` 17.9 / `wet_bulb_temperature_celsius` 13.7.
- So routing reports about 22 C above the heatmap maximum for the same hour, and a 04:00 local reading of 34.6 C in November. I have no ground truth. The routing result gives no temperature definition (air, apparent, or surface) and no timestamp or timezone for the temperatures. The cause is unknown: it may be a different quantity (for example surface temperature), a different time reference, or an error.
- Not claimed: that routing is wrong. Claimed: the three products give very different numbers for one hour and one place, and routing documents neither what its `_c` values mean nor their time reference.
- Evidence: `results/raw/routing_submit_20261120T103238_038422Z.json`, `results/raw/routing_status_poll05_20261120T103307_054792Z.json`, `results/raw/routing_past_1400local_status_poll05_*.json`, `results/raw/heatmap_status_last.json`, `results/raw/env_params_phoenix_past_status_last.json`.

## F-15 Async polling takes several polls even for tiny jobs
- `env_params` completed on the 2nd status poll, `heatmap` (about 1 km²) on the 4th, `routing` (16 km) on the 5th, polling about 5-7 s apart. `env_params` is documented as DIRECT in the MCP docs but is async over REST (submit returns only an `activity_id`).
- Evidence: `results/raw/routing_status_poll0*_*.json` (timestamped polls); earlier probes saved only the last poll per call.

## F-16 `wait_for_completion` is documented for three MCP tools but absent from their live schemas
- Saved docs copy (`docs/source/available-tools.md:89`, `:134`, `:221`) lists `wait_for_completion` as an optional input for `heatmap`, `report` and `temperature_routing`.
- Live check, 10:40 UTC: `input_schema.properties` of those three tools in `results/raw/mcp_tools.json` has no `wait_for_completion`. `heatmap` has `idempotency_key` and `notify_by_email`, and `report` has `notify_by_email`, which the docs do not mention.
- Not verified against the live docs page (not fetchable by me; saved copy only).
- Repro: `python eval/probe.py mcp`, then inspect `tools[*].input_schema.properties`.
- Evidence: `results/raw/mcp_tools.json`.

## F-17 MCP `env_params` for a whole day returns only the first hour in `value.parameters`
- 10:55 UTC. MCP `env_params` for Phoenix, `date_time` `{"start_date":"2026-11-17","filter_type":3}`, `temperature` 35. The call returned synchronously (no `activity_id` to poll; server `latency_ms` 7705, client 13.9 s, `cost_credits` 2900).
- `result.value.parameters` holds scalars such as `apparent_temperature_celsius` 18.6 and `relative_humidity_percent` 65.6, and `result.value.parameters_metadata` says `"basis": "first_hour_snapshot"`, `"timestamp": "2026-11-17T00:00:00-07:00"`. So these are the 00:00 values, not a daily summary.
- The full 24 hourly values are only under `result.value.raw.locations[0].parameters` (arrays of length 24, `metadata.time_range.count` 24).
- Risk: a model that reads the friendly `value.parameters` for a day-level question reports midnight conditions. The tool description does not mention this.
- Also: the default `analysis` set on MCP (`evidence[0].detail.fields`) is `apparent_temperature_celsius`, `relative_humidity_percent`, `precipitation_mm`, `cloud_cover_octas`, `wet_bulb_temperature_celsius`, `elevation_meters`, `aqi_us`. It excludes `heat_index_celsius`, the only field that depends on the required `temperature` input, so the required input does not affect the default MCP output.
- Evidence: `results/raw/mcp_env_params_phoenix_day_20261120T105524_027750Z.json`.

## F-18 MCP and REST wrap the same data differently
- MCP `env_params` returns `{"ok","result":{"tool","value","activity_id","evidence","warnings"},"trace":{...}}` as JSON text in `content[0].text` and the same object in `structured_content`; REST returns `{"error","status_code","message","data":{...}}` and requires polling. The REST `env_params` result is under `data.result`; the MCP one is under `result.value.raw`. Integrators have to parse two shapes for the same data.
- The MCP `trace` includes `plan_type`, `decision`, `cost_credits` and `latency_ms`, which is useful, and has no REST equivalent.

## F-19 MCP error for a future date is less specific than the REST error for the same mistake
- 10:56 UTC. MCP `env_params` with `start_date` 2026-11-21 returned `is_error: true` and `{"ok":false,"error":{"category":"no_coverage","message":"Check date_time.start_date: use a YYYY-MM-DD date supported by this tool and the required local time window.","retriable":false,"code":"NO_COVERAGE","field":"date_time.start_date"},"trace":{...}}`. Free (not charged).
- The same mistake over REST returned "... is in the future. Requests must be for a past or present date." (F-09). The MCP text does not say the date is in the future, so a model has to guess why the date was rejected. Good: the MCP body has `retriable` and `field`.
- Evidence: `results/raw/mcp_env_params_future_date_20261120T105648_481009Z.json`, `results/raw/env_params_phoenix_forecast_20261120T103335_067352Z.json`.

## F-20 MCP connection with an invalid key fails with an opaque error
- 10:57 UTC. Connecting with `Authorization: Bearer invalid_key_for_test` fails during `initialize`. With the official Python SDK 2.3.0 the leaf exception is `MCPError(code=-32603, message='Server returned an error response', data=None)`, wrapped in two nested `ExceptionGroup`s. No HTTP status or `WWW-Authenticate` is exposed to the caller. This may be the SDK flattening an HTTP 401, so it is not attributed to the server alone.
- Repro: connect with a bad bearer token (see `results/raw/mcp_invalid_key_unwrapped_*.json`).

## F-21 `structured_content` differs between MCP tools
- 10:55 UTC. For `env_params` the MCP `structured_content` equals the full parsed text envelope (`{"ok","result","trace"}`). For `credit_balance` it holds only the value (`{"available_credits": 971480}`), while the text content has the full envelope. A client that reads `structured_content` uniformly gets two different shapes.
- Evidence: `results/raw/credits_after_mcp_env_20261120T105547_578269Z.json`, `results/raw/mcp_env_params_phoenix_day_20261120T105524_027750Z.json`.

## F-22 A paid MCP call was charged although the client got `SSE stream ended without a response`
- 11:14 UTC. Agent run (`heatwise ask ... --yes`), MCP `env_params`, Phoenix, 2026-11-17 14:00, `temperature` 19. The call raised `mcp.shared.exceptions.MCPError: SSE stream ended without a response` (SDK 2.3.0) and no result was returned.
- Credits: `credit_balance` read 971,480 before two agent runs and 965,680 after, a drop of 5,800 = 2 x 2,900. Run 1 returned a result (its 2,900 is in the ledger). So run 2 was charged 2,900 and delivered nothing.
- Impact: a client cannot tell from the error whether it was charged and must not retry blindly. A retry would likely charge again. There is no idempotency key on `env_params` (the `heatmap` schema has an `idempotency_key`, `env_params` does not).
- Second occurrence, 11:20 UTC: identical agent run, same `MCPError`, charged again (965,680 -> 962,780). So 2 of 5 paid MCP calls so far failed in transit and both were charged.
- Third occurrence, 11:54:28 UTC (eval question q04): MCP `env_params` Phoenix, 2026-11-17 14:00, `temperature` 22, with an `analysis` list. Same `MCPError`, charged 2,900 (958,560 -> 955,660). A free network check right after passed 3/3, so the eval stopped as agreed.
- All paid MCP calls so far (6): failed in transit = 3, all `env_params` with `filter_type` 1 (single hour); succeeded = `env_params` `filter_type` 3 (10:55), `env_params` `filter_type` 1 (about 11:13, arguments truncated in the log, so whether it had an `analysis` list is unknown), `heatmap` (11:49). The two failures with known arguments both had an `analysis` list. This is an observation from 6 calls, not a tested cause.
- Diagnostics (free, `credit_balance` only):
  - 11:23 UTC: 3 sessions failed, two with `getaddrinfo failed` (local DNS) and one with `MCPError: Connection closed`. This machine's network was unstable around that time, so the SSE failures may be local.
  - 11:27 UTC: rerun with idle gaps of 0, 15 and 45 s between `initialize` and the call; all 3 succeeded. So an idle timeout between connect and call is unlikely to be the cause.
- Not known: whether the cause is the server, the SDK transport or my network. Not claimed as a server defect. The verified part is the billing: a call whose delivery failed was charged both times.
- Evidence: `results/raw/mcp_idle_session_test_20261120T112349_786435Z.json`, `results/raw/mcp_idle_session_test_20261120T112704_268967Z.json`, `results/raw/credits_after_sse_failure2_20261120T112050_300025Z.json`, `results/sessions/trace_20261120T112021_055247Z.json` (not published).
- Response in heatwise: paid calls are never retried after an ambiguous transport failure, the ledger records the call as charged (conservative for the cap), and the agent stops the run.
- Evidence: `results/sessions/phoenix_demo_traced_*.txt` (traceback; not published), `results/raw/credits_after_mcp_env_20261120T105547_578269Z.json`, `results/raw/credits_after_sse_failure_20261120T111522_597714Z.json`, `results/spend_ledger.jsonl`.

## F-23 Results of dropped MCP calls are recoverable only with an activity ID the client never received
- 11:37 UTC, all read-only, balance 962,780 before and after (free).
- REST `GET /v1/status/b3ff08c0-06f6-42c7-af7b-73773381aa4b` (an activity created through MCP `env_params` at 10:55) returned HTTP 200, `Completed`, with the full 24-hour `result`. So MCP-created activities can be read back over REST.
- MCP `heatmap_read` with the REST-created heatmap `0be78e2c-aaac-438f-bd55-d0dc1fc3ba9d` returned ok, `cost_credits` 0, with `stats` and `mean_temperature_celsius`. With the `env_params` activity ID it returned `invalid_input` "The request input is invalid." (no hint that the ID belongs to another tool).
- For the two F-22 calls the client got no response at all, so no `activity_id`. I found no documented way to list recent activities (REST or MCP), so those results could not be retrieved. Not tested: whether such a listing exists undocumented; I did not guess paths.
- Network check, 11:30-11:36 UTC: 20/20 fresh sessions of connect + `list_tools` + `credit_balance` succeeded, so the network was stable during this check.
- Evidence: `results/raw/retrieval_check_20261120T113750_354021Z.json`, `results/raw/retrieval_rest_status_of_mcp_env_20261120T113724_707134Z.json`, `results/raw/network_check_20261120T113638_264314Z.json`.

## F-24 Some invalid REST inputs return HTTP 500 "Something went wrong on our side"
- 11:41 UTC, `POST /v1/env_params`, all free (balance unchanged at 962,780):
  - `start_date` "17/11/2026" (wrong format) -> 500.
  - `filter_type` 1 without `start_time` (the docs say it is required) -> 500.
  - `filter_type` 4 from 2021-01-01 to 2026-11-01 (docs: range of days <= 1 month) -> 500.
- Body: `{"error": true, "message": "Something went wrong on our side. Please try again in a moment, or contact support@fortyguard.com if it keeps failing."}` (no `status_code`, `field` or `code`).
- Impact: these are input mistakes, but a 500 tells clients to retry. Standard retry logic (including heatwise's client) retries a deterministic error with backoff. Other mistakes on the same endpoint get a precise 422 (e.g. "Field 'temperature' is required.").
- Evidence: `results/raw/errors/errors_20261120T114134_768360Z.json` (cases `rest_bad_date_format`, `rest_missing_start_time_for_single_hour`, `rest_huge_range_days`).

## F-25 MCP validation errors use one generic message; the field is sometimes given separately
- 11:41 UTC. Missing `temperature`, latitude 200, `filter_type` 7, `start_date` "17/11/2026" and routing with origin = destination all returned `"message": "The request input is invalid."` with `category` `invalid_input`. Missing temperature and latitude 200 include `"field"` (`temperature`, `latitude`). Routing with origin = destination has no field and no reason.
- The same mistakes over REST name the field and the rule (e.g. "Input should be less than or equal to 90", "Input should be 1, 2, 3 or 4").
- Good on MCP: before-2021 ("start_date must be on or after 2021-01-01") and non-US (the full US-only explanation) have specific messages. Unknown tool: "Unknown tool: 'not_a_tool'". Unknown heatmap ID: "Activity not found or unavailable to this account. Check the activity ID and try again."
- Evidence: same file, cases `mcp_*`.

## F-26 REST error bodies have five different shapes
- Same run. Observed shapes: `{error, status_code, message, field, code}` (400), `{error, status_code, message, field, detail[]}` (422), `{error, message}` (500), `{detail}` (405, "Method Not Allowed"), and `{error, status_code, details{message}}` (404 "Endpoint not found"). `GET /v1/status/{unknown or non-UUID id}` returns 404 "No activity found for the provided activity_id." with no distinction between malformed and unknown IDs.
- Malformed JSON body returns 422 "Field '18' is invalid: JSON decode error" with `"field": "18"`. The "field" is a character offset, not a field name.
- Positive: non-US point (London) is rejected with a clear US-only message on both REST and MCP, and an over-size heatmap returns "Heatmap size exceeds current model limit of 50 mi2 /129.5 km2". Missing and invalid keys get clear 401 messages ("Missing required 'api-key' header. Send your key in the 'api-key' request header.").
- Evidence: same file.

## F-27 The model invented the docs example temperature (35 C) despite the tool description
- 11:49 UTC, eval question q02 ("What were heat-stress conditions in Phoenix at 3pm on 2026-11-17?", no temperature given), model `Qwen/Qwen3-235B-A22B-Instruct-2507`.
- The model's first `env_params` call passed `"temperature": 35`. The user gave no temperature. 35 C is the value used in the FortyGuard docs example. The `env_params` description says "never invent, assume, or silently substitute a temperature". heatwise's policy blocked the call before it was sent (no charge).
- It then switched to `heatmap` (4,220 credits) to obtain a temperature. That is a legitimate path, but it costs more than `env_params`, and it exists only because `env_params` requires a temperature it does not provide.
- One model, one question; not yet a pattern. Candidate for the second-model run.
- Own bug found in the same run (heatwise, not FortyGuard): the agent passed `heatmap_read`'s `"status": "processing"` straight back to the model, which polled three times with no delay and gave up with `cannot_determine`. A later free `heatmap_read` showed the heatmap had completed (mean 19.97 C). Fixed: read tools are now polled every 5 s (bounded) inside the agent. The heatmap-derived-temperature path also missed the real key names (`mean_celsius`, `mean_temperature_celsius`); fixed with a fixture from the real response.
- Evidence: `results/raw/mcp_questions/*_q02_missing_temperature.json`, `results/raw/q02_heatmap_read_after_20261120T115143_665631Z.json`.

## F-28 Tool descriptions list several plan prices; a model quoted the wrong one
- 11:59 UTC, eval q08 with `openai/gpt-oss-120b` (paid calls disabled). The model asked the user to approve "a paid heatmap request (3,500 credits) and a subsequent env_params request (2,110 credits)". Those are the basic/startup prices in the descriptions. This key is billed the premium prices (4,220 and 2,900, verified by `credit_balance`). The server knows the plan (`trace.plan_type: "premium"`), but the descriptions list all tiers, so a model cannot tell which applies.
- Evidence: `results/raw/mcp_questions/*_q08_adversarial_invent.json` (gpt-oss-120b run).

## F-29 Docs check: the Quickstart, run as written, fails at step 1
- 12:28 UTC. Ran both Python blocks of `docs/source/quickstart.md` unchanged except for the key placeholder (`eval/quickstart_verbatim.py`). Not charged (balance 955,660 before and after).
- **Wrong: the example request is rejected.** `POST /v1/heatmap` with the Quickstart polygon (lower Manhattan, New York, lon -74.0170 to -74.0030, lat 40.7050 to 40.7180), `start_date` 2024-07-15 14:00, `filter_type` 1, granularity 100 returned HTTP 400 in 2.3 s: `"FortyGuard currently provides data only for supported regions within the United States. The requested location is outside supported U.S. coverage..."`, `"field": "polygon_aoi"`, `"code": "OUT_OF_COVERAGE"`. The area is in the United States, so the message is misleading; it does not say whether the place or the date lacks coverage. The same area is the example in `authentication.md` and on the heatmap endpoint page, and New York is the example location for `env_params`, `streetview`, `heat_intelligence` and `routing` (not tested; each would be charged if accepted).
- **Wrong: the sample code crashes on any error.** Step 1 reads `response.json()["data"]["activity_id"]` without checking the status, so the 400 surfaced as `KeyError: 'data'` and the actual error message was never printed. Step 2 was never reached.
- **Missing:** no install step for `requests`; no `timeout` on any request; no code to read the result after `Completed` (the text says the status response contains it); no note that coverage is limited to some US areas or which ones. The polling loop accepts `succeeded` and `error`, which the status page does not list (already noted under the Quickstart status table).
- **Slow:** not applicable; the request was rejected in 2.3 s.
- Evidence: `results/raw/quickstart/quickstart_20261120T122822_357392Z.json`, `results/raw/credits_after_quickstart_20261120T122904_346624Z.json`.
- **Follow-up, 12:36 UTC (one approved paid call).** REST `POST /v1/env_params` at 40.7128, -74.0060 (the docs' `env_params` example point, inside the Quickstart box), `start_date` 2026-11-17, `start_time` 14:00, `filter_type` 1, `temperature` 35 (docs example value). Accepted (HTTP 200), `Completed` on the first status poll, charged 2,900 (955,660 -> 952,760). Result: `timezone` America/New_York, timestamp `2026-11-17T14:00:00-05:00`, 15 parameters, no nulls (e.g. `apparent_temperature_celsius` 10.8, `relative_humidity_percent` 50.9, `heat_index_celsius` 41.0, the last computed from the supplied 35 C, see F-05).
- **Likely cause of the Quickstart rejection:**
  - My key's coverage: unlikely to be the cause in general. The same key got `env_params` data for this Manhattan point, and it ran heatmaps in Phoenix. Whether the key's *heatmap* entitlement covers New York specifically: unknown.
  - Place: Manhattan is covered for `env_params` (weather-provider data). Whether it is covered for heatmaps (the temperature catalog): unknown.
  - Date: the Quickstart uses 2024-07-15; the follow-up used 2026-11-17. Whether 2024-07-15 is the problem: unknown. The two calls differ in tool, date and geometry form at the same time, so they cannot separate place from date for heatmaps. That needs a heatmap for this box on a recent date, or the Quickstart date in Phoenix (each 4,220 if accepted; not run).
  - Geometry form (`FeatureCollection` in the Quickstart): unknown but less likely, because the error named coverage rather than a parse problem, and the MCP schema says FeatureCollection is accepted.
- Evidence: `results/raw/quickstart/env_params_manhattan_submit_20261120T123620_927524Z.json`, `results/raw/quickstart/env_params_manhattan_status_poll01_20261120T123631_104443Z.json`, `results/raw/credits_after_manhattan_env_20261120T123710_327793Z.json`.

## Changes made for the published copy
These edits were made only to the copy published on GitHub, on 2026-11-20, to remove private or third-party content. The findings do not depend on the removed values.
- Redacted: in `report_status_poll23_20261120T104827_550027Z.json`, the S3 path segment `accountid=acc#...` (an account identifier) was replaced with `accountid=[REDACTED]`. The signed-URL query string was already redacted at capture time.
- Redacted: in `results/raw/quickstart/quickstart_20261120T122822_357392Z.json`, my local folder path in the Python traceback was replaced with `<repo>\`.
- Removed: `report_pdf_text_20261120T104831_013684Z.json` (full text of the FortyGuard-generated report) and the report PDF itself. F-08 cites only the quoted line.
- Removed: the Quickstart code copied into `eval/quickstart_verbatim.py`; the script now reads the two steps from a local copy of FortyGuard's Quickstart (see README).
- Not published: `docs/source/` (FortyGuard's documentation), `results/sessions/` (LLM session traces), and private notes.

## Claims checked and not logged
- "Six POST endpoints" (quickstart): correct. `endpoints.md` documents six: heatmap, satellite, streetview, heat_intelligence, env_params, routing.
- MCP troubleshooting page duplicating Client Setup: the saved file `docs/source/mcp-trouble-shooting.md` has that content, but I cannot tell whether the live page does or my saved copy is wrong. Unverified, so not logged.
- Heatmap `polygon_aoi` as `FeatureCollection` (quickstart) vs `Polygon` (endpoint page): both appear in the saved copies. The live MCP schema says Polygon, Feature or FeatureCollection. Only the Polygon form was tested live (see F-12).
