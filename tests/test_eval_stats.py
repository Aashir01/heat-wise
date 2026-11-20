import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from stats import percentile, summary  # noqa: E402


def test_nearest_rank_percentiles() -> None:
    data = list(range(1, 101))  # 1..100
    assert percentile(data, 50) == 50
    assert percentile(data, 95) == 95
    assert percentile(data, 99) == 99
    assert percentile(data, 100) == 100
    assert percentile(data, 0) == 1


def test_small_n_p99_is_max() -> None:
    data = [float(x) for x in range(30)]
    assert percentile(data, 99) == max(data)


def test_order_does_not_matter() -> None:
    assert percentile([5, 1, 3], 50) == percentile([1, 3, 5], 50) == 3


def test_errors() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1], 101)


def test_summary() -> None:
    s = summary([10, 20, 30, 40])
    assert s["n"] == 4 and s["p50"] == 20 and s["max"] == 40 and s["mean"] == 25
    assert summary([]) == {"n": 0}
