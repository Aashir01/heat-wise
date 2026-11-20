# Proposed evaluation plan (awaiting approval)

Cap: 150,000 credits total, including the 13,520 already spent (see `results/spend_ledger.jsonl`). The remaining 136,480 are left for your own testing except what is listed here. Costs are the observed Premium prices: `env_params` 2,900, `heatmap` 4,220, `routing` 3,500.

Rules enforced in code: a spend guard reads the ledger before every paid call and aborts at the cap, raw files are never overwritten, a 403/429/quota response stops the run, and a blocked tool is never retried.

## Spend by evaluation

| Eval | Paid calls | Credits (expected) | Credits (worst case) |
|---|---|---|---|
| a) Latency | Reuse the paid calls from b); everything else uses free calls: `credit_balance`, MCP `list_tools`, `/status` polls, rejected requests, N=30 each, cold vs warm | 0 | 0 |
| b) Consistency | 3 identical Phoenix `env_params` (same inputs); Tempe and Mesa vs Phoenix, same hour (adjacent points); Austin, Houston, Denver, same hour; 1 `routing` at 14:00 local to cross-check F-14 | 8 x 2,900 + 3,500 = 26,700 | 26,700 |
| c) Error handling | Invalid coordinates, non-US point, missing key, invalid key, malformed body, over-long date range, future date (done). Expected to be rejected for free | 0 | 10,000 (if a case is accepted and processed; guard stops there) |
| d) MCP quality | 16 natural-language questions through the agent: clear (4), vague (3), multi-city (3), ambiguous city (2), out-of-scope (2), adversarial (2). About 8 should reach one paid `env_params` call | 8 x 2,900 = 23,200 | 45,000 (hard section cap) |
| e) Docs check | Quickstart run verbatim: one `heatmap` (the Manhattan polygon) plus status loop | 4,220 | 4,220 |
| Second model run of d) | Same 16 questions on a second model | 23,200 | 30,000 (hard section cap) |
| **Total** | | **about 77,300** | **about 116,000** |

Added to the 13,520 already spent, that gives about 90,800 expected and 129,500 worst case, under the 150,000 cap, with at least 20,000 credits of slack.

## What was cut from the original brief, and why

- N=100s of calls per endpoint. Paid calls are capped at 3 identical repeats, which is too few for a meaningful p95/p99. Percentiles are reported only for the free calls (N=30). Paid-call latency is reported as raw values with N stated.
- Cold vs warm split on paid endpoints. Done only on free calls.
- Heatmap latency and consistency beyond the 2 calls made, and the one docs-check call.
- `satellite`, `streetview`, `report` and `monitoring` are not called. They cost 8,600 to 14,400 credits each and are not needed for the planner use case. They can be added as single calls (about 31,600 credits for satellite, streetview and report together) if you want coverage of them.
- Heat-intelligence reports and the n8n workflow (Task 8, optional).

## Dependencies and open points

- Paid MCP tool descriptions say "ask the user to confirm before executing". In the eval, the agent runs with an auto-confirm callback that logs whether the model asked for confirmation, and the spend guard still applies.
- `env_params` needs a caller-supplied temperature (F-05). The 16 questions that need one say the temperature in the text, or the agent must ask. Which behavior the model shows is part of the result.
- The model smoke test: a tiny tool-calling request to each shortlisted Nebius model (tokens only). The first model that passes is fixed for the whole eval and named in the report.
- Task order stays 2, 3, 5, 4, 6, then report to you, then 9, 10, 11.
