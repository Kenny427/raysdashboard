import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dashboard_server_finder", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


MAPPING = [
    {"id": 4151, "name": "Abyssal whip", "members": True, "limit": 70, "icon": "whip.png"},
    {"id": 561, "name": "Nature rune", "members": False, "limit": 18000, "icon": "nature.png"},
    {"id": 9999, "name": "Junk relic", "members": True, "limit": 5, "icon": "junk.png"},
]


def _patch_market(monkeypatch, now_ts=None):
    import time
    now_ts = now_ts or int(time.time())
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda max_age_seconds=86_400: MAPPING)
    monkeypatch.setattr(server, "load_wiki_latest_prices", lambda max_age_seconds=300: {
        # whip: healthy two-sided flip
        4151: {"high": 1_700_000, "low": 1_650_000, "highTime": now_ts - 60, "lowTime": now_ts - 120},
        # nature rune: bulk commodity, tiny roi
        561: {"high": 102, "low": 100, "highTime": now_ts - 10, "lowTime": now_ts - 20},
        # junk relic: huge margin, no volume, stale -> manipulation trap
        9999: {"high": 5_000_000, "low": 3_000_000, "highTime": now_ts - 7200, "lowTime": now_ts - 7200},
    })
    monkeypatch.setattr(server, "load_wiki_1h_market", lambda max_age_seconds=600: {"data": {
        "4151": {"avgHighPrice": 1_690_000, "avgLowPrice": 1_640_000, "highPriceVolume": 300, "lowPriceVolume": 280},
        "561": {"avgHighPrice": 101, "avgLowPrice": 100, "highPriceVolume": 500_000, "lowPriceVolume": 480_000},
        "9999": {"avgHighPrice": 4_000_000, "avgLowPrice": 3_500_000, "highPriceVolume": 1, "lowPriceVolume": 0},
    }})
    monkeypatch.setattr(server, "load_wiki_5m_market", lambda max_age_seconds=300: {"data": {
        "4151": {"avgHighPrice": 1_695_000, "avgLowPrice": 1_645_000, "highPriceVolume": 30, "lowPriceVolume": 25},
        "561": {"avgHighPrice": 101, "avgLowPrice": 100, "highPriceVolume": 40_000, "lowPriceVolume": 41_000},
    }})
    monkeypatch.setattr(server, "load_wiki_24h_market", lambda max_age_seconds=1800: {"data": {
        "4151": {"avgHighPrice": 1_680_000, "avgLowPrice": 1_630_000, "highPriceVolume": 7000, "lowPriceVolume": 6800},
        "561": {"avgHighPrice": 102, "avgLowPrice": 100, "highPriceVolume": 11_000_000, "lowPriceVolume": 10_500_000},
        "9999": {"avgHighPrice": 4_100_000, "avgLowPrice": 3_400_000, "highPriceVolume": 4, "lowPriceVolume": 3},
    }})
    monkeypatch.setattr(server, "load_wiki_daily_volumes", lambda max_age_seconds=1800: {4151: 14_000, 561: 21_000_000, 9999: 7})
    monkeypatch.setattr(server, "_current_blocked_ids", lambda: set())
    monkeypatch.setattr(server, "_personal_flip_stats_by_id", lambda: {4151: {"my_flips": 12, "my_profit": 480_000}})
    monkeypatch.setattr(server, "build_market_speed_status", lambda: {"status": "fast", "label": "Market is moving fast"})
    # isolate finder tests from the local history db / watchlist / bootstrap thread
    monkeypatch.setattr(server, "record_market_snapshot", lambda *a, **k: 0)
    monkeypatch.setattr(server, "compute_history_stats", lambda *a, **k: {})
    monkeypatch.setattr(server, "load_watchlist", lambda: set())
    monkeypatch.setattr(server, "maybe_start_history_bootstrap", lambda *a, **k: None)
    # isolate from any live calibration.json on the dev machine, and from signal logging
    monkeypatch.setattr(server, "load_calibration", lambda: {})
    monkeypatch.setattr(server, "log_finder_signals", lambda *a, **k: 0)


def test_flip_finder_metrics_and_personal_history(monkeypatch):
    _patch_market(monkeypatch)
    out = server.build_flip_finder()
    by_id = {x["item_id"]: x for x in out["items"]}

    whip = by_id[4151]
    assert whip["instant_buy"] == 1_700_000
    assert whip["instant_sell"] == 1_650_000
    assert whip["tax"] == 34_000
    assert whip["margin_after_tax"] == 1_700_000 - 34_000 - 1_650_000
    assert whip["roi_pct"] == round(whip["margin_after_tax"] / 1_650_000 * 100, 2)
    assert whip["buy_limit"] == 70
    assert whip["limit_profit"] == whip["margin_after_tax"] * 70
    assert whip["fill_hours"] == round(70 / 280, 2)
    assert whip["my_flips"] == 12 and whip["my_profit"] == 480_000
    assert whip["score"] >= 50
    assert "manip_risk" not in whip["flags"]

    assert out["stats"]["scanned"] == 3
    assert out["market_speed"]["status"] == "fast"


def test_flip_finder_flags_manipulation_and_stale(monkeypatch):
    _patch_market(monkeypatch)
    out = server.build_flip_finder()
    junk = next(x for x in out["items"] if x["item_id"] == 9999)
    assert "manip_risk" in junk["flags"]
    assert "stale" in junk["flags"]
    assert "one_sided" in junk["flags"]
    assert junk["grade"] in ("D", "E", "F")


def test_flip_finder_dump_flag(monkeypatch):
    _patch_market(monkeypatch)
    # crash the whip in the 5m bucket with heavy sell pressure
    monkeypatch.setattr(server, "load_wiki_5m_market", lambda max_age_seconds=300: {"data": {
        "4151": {"avgHighPrice": 1_590_000, "avgLowPrice": 1_540_000, "highPriceVolume": 10, "lowPriceVolume": 60},
    }})
    monkeypatch.setattr(server, "load_wiki_1h_market", lambda max_age_seconds=600: {"data": {
        "4151": {"avgHighPrice": 1_690_000, "avgLowPrice": 1_640_000, "highPriceVolume": 100, "lowPriceVolume": 250},
    }})
    out = server.build_flip_finder()
    whip = next(x for x in out["items"] if x["item_id"] == 4151)
    assert whip["trend_1h_pct"] < -3
    assert "dump" in whip["flags"]


def test_finder_v2_offer_prices_ev_and_dip_flag(monkeypatch):
    _patch_market(monkeypatch)
    monkeypatch.setattr(server, "compute_history_stats", lambda *a, **k: {
        4151: {"n_hours": 100, "mean_mid": 1_800_000.0, "std_mid": 50_000.0, "margin_share": 0.9,
               "two_sided_share": 0.95, "vol_hour_mean": 500.0, "low_7d": 1_600_000.0, "high_7d": 1_900_000.0},
    })
    out = server.build_flip_finder()
    whip = next(x for x in out["items"] if x["item_id"] == 4151)

    # suggested market-making offers: outbid the low by 1, undercut the high by 1
    assert whip["buy_at"] == 1_650_001
    assert whip["sell_at"] == 1_699_999
    expected_margin = whip["sell_at"] - min(5_000_000, int(whip["sell_at"] * 0.02)) - whip["buy_at"]
    assert whip["offer_margin"] == expected_margin

    # mid 1,675,000 vs mean 1.8m / std 50k -> 2.5 std cheap, short trend stable -> dip
    assert whip["z_score"] == -2.5
    assert "dip_buy" in whip["flags"]
    assert "falling_knife" not in whip["flags"]

    # EV/day/slot: capture min(15% of thin side x 24h, limit x 6 windows) = min(1008, 420)
    assert whip["ev_day"] == int(expected_margin * 420 * 0.9 * 0.95)
    assert whip["margin_consistency"] == 0.9
    assert whip["volatility_pct"] == round(50_000 / 1_800_000 * 100, 2)


def test_finder_v2_overheated_flag_and_score_penalty(monkeypatch):
    _patch_market(monkeypatch)

    def hist(*a, **k):
        return {4151: {"n_hours": 100, "mean_mid": 1_500_000.0, "std_mid": 50_000.0, "margin_share": 0.9,
                       "two_sided_share": 0.95, "vol_hour_mean": 500.0, "low_7d": 1_400_000.0, "high_7d": 1_700_000.0}}

    monkeypatch.setattr(server, "compute_history_stats", hist)
    out = server.build_flip_finder()
    whip = next(x for x in out["items"] if x["item_id"] == 4151)
    assert whip["z_score"] == 3.5
    assert "overheated" in whip["flags"]


def test_history_snapshot_stats_and_sparks_roundtrip(monkeypatch, tmp_path):
    import time as _t
    monkeypatch.setattr(server, "MARKET_HISTORY_DB_PATH", tmp_path / "hist.db")
    server._history_stats_cache = None
    server._last_snapshot_ts = 0.0
    base = int(_t.time()) - 20 * 3600
    for i in range(20):
        wrote = server.record_market_snapshot(
            {"4151": {"avgHighPrice": 1_000_000 + i * 1000, "avgLowPrice": 990_000 + i * 1000,
                      "highPriceVolume": 100, "lowPriceVolume": 90}},
            snapshot_ts=base + i * 3600, min_gap_s=0)
        assert wrote == 1

    stats = server.compute_history_stats(cache_s=0)
    s = stats[4151]
    assert s["n_hours"] == 20
    assert 990_000 < s["mean_mid"] < 1_010_000
    assert s["two_sided_share"] == 1.0
    # spread is 10k but 2% tax on ~1m is 20k: margin never positive in this history
    assert s["margin_share"] == 0.0

    sparks = server.fetch_history_sparks([4151])
    assert len(sparks[4151]) == 20
    assert sparks[4151][0] < sparks[4151][-1]


def test_watchlist_toggle_and_dip_alerts(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WATCHLIST_PATH", tmp_path / "watch.json")
    assert server.toggle_watchlist(4151)["watched"] is True
    assert server.load_watchlist() == {4151}
    assert server.toggle_watchlist(4151)["watched"] is False
    assert server.load_watchlist() == set()

    _patch_market(monkeypatch)
    monkeypatch.setattr(server, "load_watchlist", lambda: {4151})
    monkeypatch.setattr(server, "compute_history_stats", lambda *a, **k: {
        4151: {"n_hours": 100, "mean_mid": 1_800_000.0, "std_mid": 50_000.0, "margin_share": 0.9,
               "two_sided_share": 0.95, "vol_hour_mean": 500.0, "low_7d": 1_600_000.0, "high_7d": 1_900_000.0},
    })
    out = server.build_flip_finder()
    assert any(a["item_id"] == 4151 and a["kind"] == "dip" for a in out["alerts"])


def _patch_portfolio(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "PORTFOLIO_PATH", tmp_path / "portfolio.json")
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda max_age_seconds=86_400: MAPPING)
    monkeypatch.setattr(server, "load_wiki_latest_prices", lambda max_age_seconds=300: {
        4151: {"high": 1_700_000, "low": 1_650_000},
    })


def test_portfolio_add_sell_partial_and_close(monkeypatch, tmp_path):
    _patch_portfolio(monkeypatch, tmp_path)

    added = server.portfolio_add({"item": "abyssal whip", "qty": 10, "buy_price": 1_600_000, "target_sell": 1_750_000})
    pid = added["position"]["id"]
    assert added["position"]["item_id"] == 4151

    view = server.build_portfolio_view()
    assert view["summary"]["open_count"] == 1
    pos = view["open"][0]
    assert pos["remaining_qty"] == 10
    assert pos["cost_remaining"] == 16_000_000
    # live value: 10 * 1,650,000 minus 2% tax on the gross
    gross = 16_500_000
    assert pos["live_value_after_tax"] == gross - min(5_000_000, int(gross * 0.02))
    assert pos["unrealized_profit"] == pos["live_value_after_tax"] - 16_000_000
    assert pos["target_profit"] is not None

    # partial sell stays open
    server.portfolio_sell({"id": pid, "qty": 4, "price": 1_700_000})
    view = server.build_portfolio_view()
    pos = view["open"][0]
    assert pos["remaining_qty"] == 6
    sell_gross = 4 * 1_700_000
    expected_realized = sell_gross - min(5_000_000, int(sell_gross * 0.02)) - 4 * 1_600_000
    assert pos["realized_profit"] == expected_realized

    # selling the rest closes the position
    server.portfolio_sell({"id": pid, "qty": 6, "price": 1_700_000})
    view = server.build_portfolio_view()
    assert view["summary"]["open_count"] == 0
    assert view["summary"]["closed_count"] == 1
    assert view["closed"][0]["status"] == "closed"
    assert view["summary"]["realized_profit"] == view["closed"][0]["realized_profit"]


def test_portfolio_sell_validates_quantity(monkeypatch, tmp_path):
    _patch_portfolio(monkeypatch, tmp_path)
    pid = server.portfolio_add({"item_id": "4151", "qty": 5, "buy_price": 1_000_000})["position"]["id"]
    try:
        server.portfolio_sell({"id": pid, "qty": 6, "price": 1_100_000})
        assert False, "overselling should raise"
    except ValueError as exc:
        assert "between 1 and 5" in str(exc)


def test_portfolio_delete_and_edit(monkeypatch, tmp_path):
    _patch_portfolio(monkeypatch, tmp_path)
    pid = server.portfolio_add({"item": "Abyssal whip", "qty": 2, "buy_price": 1_500_000})["position"]["id"]

    edited = server.portfolio_edit({"id": pid, "target_sell": 1_800_000, "note": "weekend flip"})
    assert edited["position"]["target_sell"] == 1_800_000
    assert edited["position"]["note"] == "weekend flip"

    server.portfolio_delete({"id": pid})
    assert server.build_portfolio_view()["summary"]["open_count"] == 0
    try:
        server.portfolio_delete({"id": pid})
        assert False, "double delete should raise"
    except ValueError:
        pass
