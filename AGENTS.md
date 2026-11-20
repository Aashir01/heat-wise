# AGENTS.md: working in the heatwise repo

heatwise is a heat-safe outdoor event planner built on the FortyGuard Temperature MCP and REST API. US locations only.

## Layout
- `src/heatwise/fortyguard_client/`: typed async REST client (httpx, Pydantic, retries, TTL cache, rate-limit awareness).
- `src/heatwise/mcp_tools.py`: MCP connection (Streamable HTTP, `Authorization: Bearer`), tool exposure, spend guard, safe retries.
- `src/heatwise/agent.py`: tool-calling loop, guardrails, structured `FinalAnswer`.
- `src/heatwise/llm.py`: `LLMProvider` protocol, `NebiusProvider` (OpenAI-compatible), `MockProvider`.
- `src/heatwise/geo.py` and `gazetteer_data.py`: US city resolution that never picks an ambiguous name silently.
- `src/heatwise/cli.py`: `heatwise ask "<question>"`.
- `eval/`: live probes and evaluation scripts (they spend credits).
- `results/raw/`: raw captures. Never edit or overwrite; new files get a timestamped name.
- `docs/DISCOVERY.md`, `FRICTION_LOG.md`: verified findings only.

## Commands
- Tests (offline, no keys needed): `.venv/Scripts/python -m pytest -q`
- Lint and types: `python -m ruff check src tests eval`, `python -m ruff format --check src tests eval`, `python -m mypy`
- Setup: `uv venv --python 3.12 .venv` then `uv pip install --python .venv/Scripts/python.exe -e ".[dev]"`

## Rules
1. Never print, log, commit or save `TEMP_API_KEY` or `NEBIUS_API_KEY`. Use `heatwise.secrets.redact` / `save_json`.
2. Do not invent FortyGuard endpoints, tool names, parameters or response shapes. Use `docs/DISCOVERY.md`, `results/raw/mcp_tools*.json` and the fixtures in `tests/fixtures/` (built from real captures by `tests/fixtures/_build.py`). Mark any hand-written body as synthetic.
3. Every paid call costs credits (env_params 2,900, heatmap 4,220, routing 3,500 on this key). Check the ledger (`results/spend_ledger.jsonl`) and the 150,000 cap before any live run. Never retry a paid call after an ambiguous failure.
4. A 403/plan error, a 429 or a quota error stops the run. Do not work around it.
5. Tests must stay offline: use respx, `MockProvider` and fake MCP sessions.
6. Never put a number in a report unless it comes from a file in `results/`.
