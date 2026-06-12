import datetime as dt
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dashboard_server", ROOT / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def row(account, status, profit, sell_time, sold=1):
    return {
        "Account": account,
        "Status": status,
        "Profit": str(profit),
        "_profit": profit,
        "_tax": 0,
        "_sell": sell_time,
        "_sold": sold,
    }


def test_bankroll_plan_includes_partial_selling_profit_after_baseline():
    now = dt.datetime.now()
    baseline_at = now - dt.timedelta(days=1)
    acct = "main"
    csv_data = {
        "available": True,
        "analysis_rows": [
            row(acct, "FINISHED", 1_000_000, now - dt.timedelta(hours=2)),
        ],
        "open_rows": [
            row(acct, "SELLING", 250_000, now - dt.timedelta(hours=1), sold=2),
            row(acct, "BUYING", 999_999, now - dt.timedelta(hours=1), sold=0),
            row("alt4", "SELLING", 333_333, now - dt.timedelta(hours=1), sold=1),
        ],
    }
    config = {
        "active_accounts": [acct],
        "baseline_at": baseline_at.isoformat(timespec="seconds"),
        "account_baselines": {acct: 10_000_000},
    }

    plan = server.compute_bankroll_plan(csv_data, config)
    account = plan["accounts"][acct]

    assert account["finished_profit_since_baseline"] == 1_000_000
    assert account["partial_selling_profit_since_baseline"] == 250_000
    assert account["profit_since_baseline"] == 1_250_000
    assert account["current_bankroll"] == 11_250_000
    assert plan["totals"]["total_profit_since_baseline"] == 1_250_000
    assert plan["totals"]["partial_selling_profit_since_baseline"] == 250_000


def test_default_active_accounts_are_not_hardcoded():
    # No personal account names ship with the repo; accounts come from
    # bankroll_config.json or are discovered from the Copilot CSV.
    assert server.DEFAULT_ACTIVE_ACCOUNTS == []


def test_bankroll_transfer_moves_baseline_without_changing_total():
    config = {
        "active_accounts": ["main", "alt2"],
        "baseline_at": "2026-05-29T03:25:00.000Z",
        "account_baselines": {"main": 100_000_000, "alt2": 0},
        "notes": "",
    }

    updated = server.apply_bankroll_transfer(config, "main", "alt2", 25_000_000)

    # Transfers live in the adjustments layer; baselines and realized profit stay untouched.
    assert updated["account_adjustments"]["main"] == -25_000_000
    assert updated["account_adjustments"]["alt2"] == 25_000_000
    assert sum(updated["account_adjustments"].values()) == 0
    assert updated["account_baselines"] == {"main": 100_000_000, "alt2": 0}
    assert "alt2" in updated["active_accounts"]


def test_bond_purchase_deducts_from_one_account_and_total_bankroll():
    config = {
        "active_accounts": ["main", "alt2"],
        "baseline_at": "2026-05-29T03:25:00.000Z",
        "account_baselines": {"main": 100_000_000, "alt2": 25_000_000},
        "notes": "",
    }

    updated = server.apply_bond_purchase(config, "alt2", 12_500_000)

    # Bond purchases are capital withdrawals in the adjustments layer.
    assert updated["account_adjustments"]["alt2"] == -12_500_000
    assert updated["account_baselines"]["alt2"] == 25_000_000
    assert updated["account_baselines"]["main"] == 100_000_000
    assert "alt2" in updated["active_accounts"]
