"""The committed report must equal a fresh regeneration from the committed raw files."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import make_report  # noqa: E402


def test_report_is_deterministic() -> None:
    assert make_report.build(chart=False) == make_report.build(chart=False)


def test_committed_report_matches_raw_data() -> None:
    committed = (ROOT / "results" / "REPORT.md").read_text(encoding="utf-8")
    assert make_report.build(chart=False) == committed, (
        "results/REPORT.md is stale: run python eval/make_report.py"
    )


def test_report_numbers_match_ledger_and_balance() -> None:
    text = make_report.build(chart=False)
    total = sum(int(e["credits"]) for e in make_report.ledger())
    lowest = min(b[1] for b in make_report.balances())
    assert f"Ledger total {total:,}" in text
    assert f"so {1_000_000 - lowest:,} used" in text
    assert total == 1_000_000 - lowest  # ledger reconciles with credit_balance
