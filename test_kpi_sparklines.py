import datetime as dt
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dashboard_server", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def _finished(start: dt.datetime, specs):
    """specs: list of (hours_after_start, profit)."""
    rows = []
    for h, profit in specs:
        rows.append({"_sell": start + dt.timedelta(hours=h), "_profit": profit})
    return rows


def test_sparklines_endpoints_match_period_totals():
    start = dt.datetime(2026, 6, 5, 0, 0, 0)
    end = start + dt.timedelta(hours=24)
    specs = [(1, 100_000), (6, -40_000), (12, 250_000), (18, 50_000), (23, -10_000)]
    finished = _finished(start, specs)

    sl = server.build_sparklines(finished, (start, end), end)

    total = sum(p for _, p in specs)
    n = len(specs)
    wins = sum(1 for _, p in specs if p > 0)

    # every series spans the period at fixed resolution
    for key in ("profit", "avg_flip", "win_rate", "per_hour", "times"):
        assert len(sl[key]) == 32

    # bucket timestamps are chronological and end at the period end (for hover labels)
    assert sl["times"] == sorted(sl["times"])
    assert sl["times"][-1] == server.iso(end)

    # endpoints are 100% accurate vs the period aggregates the KPIs show
    assert sl["profit"][-1] == total
    assert sl["avg_flip"][-1] == round(total / n)
    assert sl["win_rate"][-1] == round(wins / n * 100, 1)
    assert sl["per_hour"][-1] == round(total / 24)  # 24h elapsed


def test_sparklines_profit_curve_is_cumulative_over_time():
    start = dt.datetime(2026, 6, 5, 0, 0, 0)
    end = start + dt.timedelta(hours=10)
    # one +1M flip near the very end: early buckets stay flat, last jumps
    finished = _finished(start, [(9.9, 1_000_000)])

    sl = server.build_sparklines(finished, (start, end), end)
    profit = sl["profit"]

    assert profit[0] == 0            # nothing booked early
    assert profit[-1] == 1_000_000   # full amount by the end
    # cumulative profit never decreases for an all-win series
    assert all(b >= a for a, b in zip(profit, profit[1:]))


def test_sparklines_empty_when_no_finished_flips():
    start = dt.datetime(2026, 6, 5, 0, 0, 0)
    end = start + dt.timedelta(hours=24)
    assert server.build_sparklines([], (start, end), end) == {}
