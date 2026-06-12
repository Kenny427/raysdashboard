import csv
import datetime as dt
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dashboard_server", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def write_csv(path: Path):
    fields = [
        "First buy time", "Last sell time", "Account", "Item", "Status", "Bought", "Sold",
        "Avg. buy price", "Avg. sell price", "Tax", "Profit", "Profit ea.", "Item id"
    ]
    rows = [
        {
            "First buy time": "2026-04-05T12:00:00", "Last sell time": "-", "Account": "main",
            "Item": "Abyssal bludgeon", "Status": "SELLING", "Bought": "1", "Sold": "0",
            "Avg. buy price": "18000000", "Avg. sell price": "0", "Tax": "0", "Profit": "0",
            "Profit ea.": "0", "Item id": "13263",
        },
        {
            "First buy time": "2026-05-31T13:00:00", "Last sell time": "-", "Account": "main",
            "Item": "Dragon claws", "Status": "BOUGHT", "Bought": "2", "Sold": "0",
            "Avg. buy price": "100000000", "Avg. sell price": "0", "Tax": "0", "Profit": "0",
            "Profit ea.": "0", "Item id": "13652",
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_live_unrealized_estimate_does_not_use_stale_csv_buy_for_selling_slot(tmp_path, monkeypatch):
    copilot = tmp_path / "copilot"
    copilot.mkdir()
    csv_path = tmp_path / "flips.csv"
    write_csv(csv_path)
    monkeypatch.setattr(server, "COPILOT_DIR", copilot)
    monkeypatch.setattr(server, "load_wiki_latest_prices", lambda *a, **k: {})
    monkeypatch.setattr(server, "find_latest_csv", lambda: (csv_path, []))
    monkeypatch.setattr(server, "_item_id_to_info", {
        13263: {"name": "Abyssal bludgeon", "icon": "icon-bludgeon"},
        13652: {"name": "Dragon claws", "icon": "icon-claws"},
    })
    monkeypatch.setattr(server, "_name_to_id", {"abyssal bludgeon": 13263, "dragon claws": 13652})

    (copilot / "acc_hash_0.json").write_text(json.dumps({
        "itemId": 13263, "quantitySold": 0, "totalQuantity": 1, "price": 20000000,
        "spent": 0, "state": "SELLING", "copilotPriceUsed": True, "wasCopilotSuggestion": True,
    }), encoding="utf-8")
    (copilot / "acc_hash_1.json").write_text(json.dumps({
        "itemId": 13652, "quantitySold": 0, "totalQuantity": 2, "price": 100000000,
        "spent": 100000000, "state": "BOUGHT", "copilotPriceUsed": True, "wasCopilotSuggestion": True,
    }), encoding="utf-8")

    result = server.build_live_unrealized_estimate(server.load_rows(csv_path))

    assert result["available"] is True
    assert result["slot_count"] == 2
    assert result["selling_count"] == 1
    assert result["bought_count"] == 1
    bludgeon = next(s for s in result["slots"] if s["item"] == "Abyssal bludgeon")
    assert bludgeon["estimate_method"] == "unknown_missing_buy_or_sell"
    assert bludgeon["estimated_profit"] is None
    assert result["estimated_unrealized_profit"] == 0
    assert result["active_sell_value"] == 19_600_000
    assert result["unknown_profit_slots"] >= 1
    assert result["read_only"] is True


def test_live_unrealized_estimate_uses_slot_spent_when_available(tmp_path, monkeypatch):
    copilot = tmp_path / "copilot"
    copilot.mkdir()
    monkeypatch.setattr(server, "COPILOT_DIR", copilot)
    monkeypatch.setattr(server, "load_wiki_latest_prices", lambda *a, **k: {})
    monkeypatch.setattr(server, "_item_id_to_info", {
        13263: {"name": "Abyssal bludgeon", "icon": "icon-bludgeon"},
    })
    monkeypatch.setattr(server, "_name_to_id", {"abyssal bludgeon": 13263})
    (copilot / "acc_hash_0.json").write_text(json.dumps({
        "itemId": 13263, "quantitySold": 0, "totalQuantity": 1, "price": 20000000,
        "spent": 18000000, "state": "SELLING", "copilotPriceUsed": True, "wasCopilotSuggestion": True,
    }), encoding="utf-8")

    result = server.build_live_unrealized_estimate([])

    bludgeon = next(s for s in result["slots"] if s["item"] == "Abyssal bludgeon")
    assert bludgeon["estimate_method"] == "slot_spent_avg_buy"
    assert bludgeon["estimated_profit"] == 1_600_000  # 20m less 2% tax minus 18m buy
    assert result["estimated_unrealized_profit"] == 1_600_000


def test_live_unrealized_estimate_uses_recent_api_csv_open_buy(tmp_path, monkeypatch):
    copilot = tmp_path / "copilot"
    copilot.mkdir()
    csv_path = tmp_path / "flips.csv"
    recent = dt.datetime.now().replace(microsecond=0).isoformat()
    fields = [
        "First buy time", "Last sell time", "Account", "Item", "Status", "Bought", "Sold",
        "Avg. buy price", "Avg. sell price", "Tax", "Profit", "Profit ea.", "Item id"
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "First buy time": recent, "Last sell time": "", "Account": "main",
            "Item": "Abyssal bludgeon", "Status": "BUYING", "Bought": "1", "Sold": "0",
            "Avg. buy price": "18000000", "Avg. sell price": "0", "Tax": "0", "Profit": "0",
            "Profit ea.": "0", "Item id": "13263",
        })
    monkeypatch.setattr(server, "COPILOT_DIR", copilot)
    monkeypatch.setattr(server, "load_wiki_latest_prices", lambda *a, **k: {})
    monkeypatch.setattr(server, "_item_id_to_info", {13263: {"name": "Abyssal bludgeon", "icon": "icon-bludgeon"}})
    monkeypatch.setattr(server, "_name_to_id", {"abyssal bludgeon": 13263})
    (copilot / "acc_hash_0.json").write_text(json.dumps({
        "itemId": 13263, "quantitySold": 0, "totalQuantity": 1, "price": 20000000,
        "spent": 0, "state": "SELLING", "copilotPriceUsed": True, "wasCopilotSuggestion": True,
    }), encoding="utf-8")

    result = server.build_live_unrealized_estimate(server.load_rows(csv_path))

    bludgeon = next(s for s in result["slots"] if s["item"] == "Abyssal bludgeon")
    assert bludgeon["estimate_method"] == "recent_api_csv_avg_buy"
    assert bludgeon["estimated_profit"] == 1_600_000
    assert result["estimated_unrealized_profit"] == 1_600_000

def test_selling_slot_values_at_ask_like_fc_with_above_market_note(tmp_path, monkeypatch):
    """FC parity: the ask is Copilot's suggested sell price, so profit is predicted
    at the ask even if currently above instant-buy - with an informational note."""
    copilot = tmp_path / "copilot"
    copilot.mkdir()
    csv_path = tmp_path / "flips.csv"
    write_csv(csv_path)
    monkeypatch.setattr(server, "COPILOT_DIR", copilot)
    monkeypatch.setattr(server, "find_latest_csv", lambda: (csv_path, []))
    monkeypatch.setattr(server, "_item_id_to_info", {13263: {"name": "Abyssal bludgeon", "icon": "i"}})
    monkeypatch.setattr(server, "_name_to_id", {"abyssal bludgeon": 13263})
    # ask 20m, but instant-buy is only 19.9m -> value at 19.9m
    monkeypatch.setattr(server, "load_wiki_latest_prices", lambda *a, **k: {13263: {"high": 19_900_000, "low": 19_000_000}})

    (copilot / "acc_hash_0.json").write_text(json.dumps({
        "itemId": 13263, "quantitySold": 0, "totalQuantity": 1, "price": 20_000_000,
        "spent": 18_000_000, "state": "SELLING",
    }), encoding="utf-8")

    result = server.build_live_unrealized_estimate(server.load_rows(csv_path))
    slot = result["slots"][0]
    gross = 20_000_000
    expected = gross - min(5_000_000, int(gross * 0.02)) - 18_000_000
    assert slot["estimated_profit"] == expected
    assert slot["estimate_method"].endswith("_ask_above_market")
    assert result["active_sell_value"] == gross - min(5_000_000, int(gross * 0.02))
