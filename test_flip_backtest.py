"""Unit tests for the flip-finder backtest replay math (pure, no network)."""
import server

CFG = {"label": "test 1h", "hours_per_bucket": 1, "horizon": 24, "win": 168}


def test_clear_margin_round_trips_complete():
    # Flat market with a steady 100/110 spread: every signal should be able to
    # buy at the bid and sell at the ask within the horizon.
    series = [{"ts": i * 3600, "high": 110, "low": 100, "vbuy": 50, "vsell": 50} for i in range(50)]
    recs = server._bt_replay_item(series, CFG, limit=1000)
    assert recs, "a clear positive margin should produce signals"
    assert all(r["realized_unit"] >= 0 for r in recs), "no fantasy fire-sale losses"
    s = server._bt_summarize(recs, CFG, items_used=1)
    # Only the final bucket (one forward bucket) can be stuck, so completion is near-total.
    assert s["completion_rate"] >= 95.0
    # Predicted and realized use the same capture, so a fully-captured flat market calibrates ~1:1.
    assert s["realized_pct_of_predicted"] is not None and s["realized_pct_of_predicted"] >= 90.0


def test_no_margin_yields_no_signals():
    # high == low -> margin is negative after tax -> nothing to recommend.
    series = [{"ts": i * 3600, "high": 100, "low": 100, "vbuy": 50, "vsell": 50} for i in range(30)]
    assert server._bt_replay_item(series, CFG, limit=1000) == []


def test_empty_summary_is_safe():
    s = server._bt_summarize([], CFG, items_used=0)
    assert s["signals"] == 0
