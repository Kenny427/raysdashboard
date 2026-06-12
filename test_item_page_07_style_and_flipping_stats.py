import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
spec = importlib.util.spec_from_file_location("dashboard_server_item_page", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def test_wiki_item_detail_includes_dashboard_flipping_stats(monkeypatch):
    sample = [{"id": 22486, "name": "Scythe of vitur (uncharged)", "examine": "A powerful scythe.", "members": True, "lowalch": 1, "highalch": 2, "limit": 8, "icon": "scythe.png"}]
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda: sample)
    monkeypatch.setattr(server, "wiki_get_json", lambda path, params=None: {
        "/latest": {"data": {"22486": {"high": 1_300_000_000, "highTime": 1700000000, "low": 1_280_000_000, "lowTime": 1700000100}}},
        "/timeseries": {"data": [{"timestamp": 1700000000, "avgHighPrice": 1_300_000_000, "avgLowPrice": 1_280_000_000, "highPriceVolume": 3, "lowPriceVolume": 2}]},
    }[path])
    monkeypatch.setattr(server, "get_item_detail", lambda item_name, period_name="all_time", **kwargs: {
        "item": item_name,
        "period": period_name,
        "n": 12,
        "profit": 345_678,
        "avg_profit": 28_806,
        "win_rate": 75.0,
        "med_dur_h": 2.5,
        "best_hour": 19,
        "flips": [{"profit": 50_000}],
    })

    detail = server.fetch_wiki_item_detail("scythe-of-vitur-uncharged")

    assert detail["flipping_stats"]["n"] == 12
    assert detail["flipping_stats"]["profit"] == 345_678
    assert detail["flipping_stats"]["scope_label"] == "all_time · active accounts"


def test_wiki_item_detail_handles_items_with_no_flipping_history(monkeypatch):
    sample = [{"id": 4151, "name": "Abyssal whip", "examine": "A weapon.", "members": True, "lowalch": 1, "highalch": 2, "limit": 70, "icon": "whip.png"}]
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda: sample)
    monkeypatch.setattr(server, "wiki_get_json", lambda path, params=None: {"data": {"4151": {"high": 100, "low": 90}}} if path == "/latest" else {"data": []})
    monkeypatch.setattr(server, "get_item_detail", lambda *args, **kwargs: {"error": "No finished flips found"})

    detail = server.fetch_wiki_item_detail("abyssal-whip")

    assert detail["flipping_stats"]["available"] is False
    assert detail["flipping_stats"]["n"] == 0


def test_item_page_uses_previous_card_layout_not_07gg_redesign():
    assert "itemPage07" not in INDEX
    assert "geBreadcrumb" not in INDEX
    assert "marketHero" not in INDEX
    assert "priceChart07" not in INDEX
    assert "itemHero" in INDEX
    assert "itemStatsGrid" in INDEX
    assert "Market overview" in INDEX
    assert "performanceStats" in INDEX


def test_item_page_renders_personal_flipping_stats_panel():
    assert "function renderFlippingStats" in INDEX
    assert "Your flip record" in INDEX
    assert "flipping_stats" in INDEX
    assert "Recent flips" in INDEX
    assert "flipRows" in INDEX
