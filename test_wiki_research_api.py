import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dashboard_server_wiki", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def test_search_wiki_items_filters_mapping_and_limits(monkeypatch):
    sample = [
        {"id": 22486, "name": "Scythe of vitur (uncharged)", "examine": "A powerful scythe.", "members": True, "lowalch": 1, "highalch": 2, "limit": 8, "icon": "scythe.png"},
        {"id": 4151, "name": "Abyssal whip", "examine": "A weapon from the abyss.", "members": True, "lowalch": 72000, "highalch": 120000, "limit": 70, "icon": "whip.png"},
    ]
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda: sample)

    results = server.search_wiki_items("scythe", limit=5)

    assert len(results) == 1
    assert results[0]["id"] == 22486
    assert results[0]["slug"] == "scythe-of-vitur-uncharged"
    assert results[0]["name"] == "Scythe of vitur (uncharged)"


def test_search_wiki_items_includes_priced_items_even_without_buy_limit(monkeypatch):
    sample = [
        {"id": 31949, "name": "Bottled storm", "examine": "A storm in a bottle.", "members": True, "lowalch": 0, "highalch": 0, "limit": None, "icon": "bottled_storm.png"},
        {"id": 31961, "name": "Broken dragon hook", "examine": "A broken hook.", "members": True, "lowalch": 0, "highalch": 0, "limit": None, "icon": "broken_dragon_hook.png"},
    ]
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda: sample)
    monkeypatch.setattr(server, "wiki_get_json", lambda path, params=None: {
        "data": {
            "31949": {"high": 10_200_000, "low": 10_000_000},
            "31961": {"high": 14_000_000, "low": 14_000_000},
        }
    })

    bottled = server.search_wiki_items("bottled storm", limit=5)
    hook = server.search_wiki_items("broken dragon hook", limit=5)

    assert bottled and bottled[0]["id"] == 31949
    assert hook and hook[0]["id"] == 31961


def test_fetch_wiki_item_detail_supports_chart_day_ranges(monkeypatch):
    sample = [{"id": 22486, "name": "Scythe of vitur (uncharged)", "examine": "A powerful scythe.", "members": True, "lowalch": 1, "highalch": 2, "limit": 8, "icon": "scythe.png"}]
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda: sample)
    chart = [
        {"timestamp": 1700000000 + i * 3600, "avgHighPrice": 1_000 + i, "avgLowPrice": 900 + i, "highPriceVolume": 1, "lowPriceVolume": 1}
        for i in range(5000)
    ]
    monkeypatch.setattr(server, "wiki_get_json", lambda path, params=None: {
        "/latest": {"data": {"22486": {"high": 1_300_000, "low": 1_280_000}}},
        "/timeseries": {"data": chart},
    }[path])

    one_day = server.fetch_wiki_item_detail("22486", timestep="1h", chart_days=1)
    thirty_days = server.fetch_wiki_item_detail("22486", timestep="6h", chart_days=30)
    half_year = server.fetch_wiki_item_detail("22486", timestep="24h", chart_days=180)

    assert one_day["chart_range"] == "1d"
    assert one_day["chart_timestep"] == "1h"
    assert len(one_day["chart"]) == 24
    assert len(thirty_days["chart"]) == 120
    assert thirty_days["chart_timestep"] == "6h"
    assert len(half_year["chart"]) == 180
    assert half_year["chart_timestep"] == "24h"


def test_fetch_wiki_item_detail_combines_mapping_latest_and_timeseries(monkeypatch):
    sample = [{"id": 22486, "name": "Scythe of vitur (uncharged)", "examine": "A powerful scythe.", "members": True, "lowalch": 1, "highalch": 2, "limit": 8, "icon": "scythe.png"}]
    monkeypatch.setattr(server, "fetch_wiki_mapping", lambda: sample)
    monkeypatch.setattr(server, "wiki_get_json", lambda path, params=None: {
        "/latest": {"data": {"22486": {"high": 1_300_000_000, "highTime": 1700000000, "low": 1_280_000_000, "lowTime": 1700000100}}},
        "/timeseries": {"data": [{"timestamp": 1700000000, "avgHighPrice": 1_300_000_000, "avgLowPrice": 1_280_000_000, "highPriceVolume": 3, "lowPriceVolume": 2}]},
    }[path])

    detail = server.fetch_wiki_item_detail("22486")

    assert detail["item"]["slug"] == "scythe-of-vitur-uncharged"
    assert detail["latest"]["high"] == 1_300_000_000
    assert detail["spread"] == 20_000_000
    assert detail["chart"][0]["avgHighPrice"] == 1_300_000_000
