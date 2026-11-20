# Needs GNU make (on Windows: e.g. `choco install make`, or run the commands under each target
# directly in PowerShell; the README lists them).
ifeq ($(OS),Windows_NT)
  PY := .venv/Scripts/python
else
  PY := .venv/bin/python
endif

.PHONY: setup test lint typecheck check eval eval-mcp report secrets-scan demo clean

setup:            ## create venv and install
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e ".[dev]"

test:             ## offline unit tests (no keys needed)
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests eval
	$(PY) -m ruff format --check src tests eval

typecheck:
	$(PY) -m mypy

check: lint typecheck test

# Free evals (no credits): network check, error handling, latency, consistency from raw data.
eval:
	$(PY) eval/network_check.py 20
	$(PY) eval/errors_eval.py
	$(PY) eval/latency_eval.py 30
	$(PY) eval/consistency_from_raw.py

# Paid MCP questions (spend credits; 30,000 section cap). Example: make eval-mcp GROUP=0
eval-mcp:
	$(PY) eval/mcp_questions.py $(GROUP)

report:
	$(PY) eval/make_report.py --chart

secrets-scan:
	$(PY) eval/secret_scan.py

# Free demo (no paid call): ambiguous city, then out-of-scope.
demo:
	$(PY) -m heatwise ask "Is it safe to hold a 2pm outdoor event in Springfield on 2026-11-17? It was 20C." --non-interactive
	$(PY) -m heatwise ask "Is it safe to hold a 2pm outdoor event in London tomorrow?" --non-interactive

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
