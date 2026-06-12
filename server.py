#!/usr/bin/env python3
"""OSRS Flipping Copilot Dashboard V8-clean-items.

Serves dashboard on http://127.0.0.1:8791 with data from:
- Newest Flipping Copilot CSV export (Documents/Downloads/Desktop/RuneLite)
- Local item name/icon mapping cache

Current UI intentionally excludes noisy Portfolio panels, Fun scoreboards, and personal-record
noise. A compact read-only live unrealized estimate can use local Copilot slot files while
the dashboard remains focused on realized CSV stats, active accounts, range-aware item
research, and baseline + P/L bankroll tracking.

Routes:
  /                       -> index.html
  /api/summary             -> JSON dashboard data (v8-clean-items)
  /api/health              -> {"status":"ok"}
  /api/bankroll-config     -> GET/POST bankroll configuration
  /api/item/{item_name}    -> Per-item deep stats (supports period= and all_accounts=1)
  /api/export/blocklist-candidates -> JSON problem/blocklist items
  /api/export/analysis-context -> JSON analysis context for copying
  /api/copilot/export-csv -> POST simple UI-assisted Copilot CSV export

CLI: python server.py --once prints JSON and exits.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import http.server
import json
import math
import sqlite3
import threading
import time
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlparse, parse_qs, unquote, urlencode, quote

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
COPILOT_DIR = HOME / ".runelite" / "flipping-copilot"
USER_DOCUMENTS = HOME / "Documents"
DOWNLOADS = HOME / "Downloads"
DESKTOP = HOME / "Desktop"
MAPPING_CACHE_PATH = ROOT / "osrs_mapping_cache.json"
LOCAL_CONFIG_PATH = ROOT / "local_config.json"  # optional, gitignored, machine-specific
WIKI_LATEST_CACHE_PATH = ROOT / "wiki_latest_cache.json"
WIKI_1H_CACHE_PATH = ROOT / "wiki_1h_cache.json"
WIKI_5M_CACHE_PATH = ROOT / "wiki_5m_cache.json"
WIKI_VOLUMES_CACHE_PATH = ROOT / "wiki_volumes_cache.json"
WIKI_24H_CACHE_PATH = ROOT / "wiki_24h_cache.json"
PORTFOLIO_PATH = ROOT / "portfolio.json"
FLIPS_CSV_PATH = ROOT / "flips.csv"  # canonical home for the synced flip history
WATCHLIST_PATH = ROOT / "watchlist.json"
MARKET_HISTORY_DB_PATH = ROOT / "market_history.db"
MARKET_VOLUME_HISTORY_PATH = ROOT / "market_volume_history.json"
BANKROLL_CONFIG_PATH = ROOT / "bankroll_config.json"
BANKROLL_LEDGER_PATH = ROOT / "bankroll_ledger.json"
SESSION_ANCHOR_PATH = ROOT / "session_anchor.json"
BLOCKLIST_PATH = ROOT / "blocklist.json"  # legacy/local fallback only
BLOCKLIST_REVIEW_PATH = ROOT / "blocklist_review.json"
ACTIVE_PROFILE_PATH = ROOT / "active_profile.json"
WIKI_API_BASE = "https://prices.runescape.wiki/api/v1/osrs"
WIKI_USER_AGENT = "osrs-flip-dashboard/1.0 (local self-hosted dashboard)"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
VERSION = "v8-clean-items"
# Accounts are configured in bankroll_config.json (gitignored). When empty, the
# dashboard automatically uses every account found in the Copilot CSV.
DEFAULT_ACTIVE_ACCOUNTS: list[str] = []
DEFAULT_BLOCKLIST_PROFILE = "Dashboard blocklist"
DEFAULT_MONTHLY_GOAL = 1_000_000_000
DEFAULT_GOAL_DAYS = 31

_item_id_to_info: dict[int, dict] | None = None
_name_to_id: dict[str, int] | None = None
_wiki_mapping_cache: list[dict] | None = None
_local_config_cache: dict | None = None


def load_local_config() -> dict:
    """Optional machine-specific settings from local_config.json (gitignored).

    Supported keys:
      dashboard_title           shown in the top bar / page title
      blocklist_profile         default Copilot blocklist profile name
      extra_csv_dirs            extra folders to search for flips.csv exports
      copilot_ui_export_script  path to a UI-assisted Copilot export script
      copilot_api_export_script path to a read-only Copilot API export script
    """
    global _local_config_cache
    if _local_config_cache is not None:
        return _local_config_cache
    try:
        raw = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
        _local_config_cache = raw if isinstance(raw, dict) else {}
    except Exception:
        _local_config_cache = {}
    return _local_config_cache


def copilot_blocklist_profile_name() -> str:
    return str(load_local_config().get("blocklist_profile") or DEFAULT_BLOCKLIST_PROFILE)


def parse_num(x: Any) -> int:
    if x is None:
        return 0
    s = str(x).replace(",", "").replace("gp", "").strip()
    if not s or s == "-":
        return 0
    try:
        return int(float(s))
    except Exception:
        return 0


def norm_dt(d: dt.datetime | None) -> dt.datetime | None:
    if d and d.tzinfo:
        return d.astimezone().replace(tzinfo=None)
    return d


def parse_time(s: str | None) -> dt.datetime | None:
    s = (s or "").strip()
    if not s or s == "-":
        return None

    # API exporter timestamps are UTC ISO strings ending in Z. Convert those to
    # local naive datetimes once on the backend, then the frontend can display
    # them as-is with browser-local formatting. If we parse Z with strptime first,
    # recent transactions show raw UTC and appear 1-2 hours wrong.
    has_tz = s.upper().endswith("Z") or (len(s) >= 6 and s[-6] in "+-" and s[-3] == ":")
    if has_tz:
        try:
            return norm_dt(dt.datetime.fromisoformat(s.replace("Z", "+00:00")))
        except Exception:
            pass

    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            pass
    try:
        return norm_dt(dt.datetime.fromisoformat(s.replace("Z", "+00:00")))
    except Exception:
        return None


def iso(d: dt.datetime | None) -> str | None:
    return d.isoformat(timespec="seconds") if d else None


def _load_item_mapping() -> tuple[dict[int, dict], dict[str, int]]:
    global _item_id_to_info, _name_to_id
    if _item_id_to_info:
        return _item_id_to_info, _name_to_id
    data = None
    if MAPPING_CACHE_PATH.exists():
        try:
            data = json.loads(MAPPING_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("items") or data.get("data") or []
        except Exception:
            data = None
    if not data:
        # No local cache yet (e.g. fresh clone): fetch the wiki mapping, which
        # also writes MAPPING_CACHE_PATH for next time.
        try:
            data = fetch_wiki_mapping()
        except Exception:
            data = []
    ids: dict[int, dict] = {}
    names: dict[str, int] = {}
    for it in data or []:
        if isinstance(it, dict) and it.get("id") is not None and it.get("name"):
            iid = int(it["id"])
            name = str(it["name"])
            ids[iid] = {
                "name": name,
                "icon": f"https://static.runelite.net/cache/item/icon/{iid}.png",
            }
            names[name.lower()] = iid
    if ids:
        # Only memoize a non-empty mapping so a failed fetch retries next call.
        _item_id_to_info, _name_to_id = ids, names
    return ids, names


def get_item_info(item_id_or_name: int | str | None) -> dict:
    item_map, name_to_id = _load_item_mapping()
    if item_id_or_name is None:
        return {}
    try:
        iid = int(item_id_or_name)
        if iid in item_map:
            return {"itemId": iid, **item_map[iid]}
        if iid > 0:
            return {"itemId": iid, "name": f"Item {iid}", "icon": f"https://static.runelite.net/cache/item/icon/{iid}.png"}
    except Exception:
        pass
    name = str(item_id_or_name).strip().lower()
    iid = name_to_id.get(name)
    if iid is not None:
        return {"itemId": iid, **item_map.get(iid, {})}
    return {}


def load_bankroll_config() -> dict:
    default = {
        "active_accounts": accounts_from_csv(),
        "baseline_at": dt.datetime.now().isoformat(timespec="seconds"),
        "account_baselines": {},
        "account_adjustments": {},
        "notes": "",
        "monthly_goal": DEFAULT_MONTHLY_GOAL,
        "goal_days": DEFAULT_GOAL_DAYS,
        "owed": 0,
        "dashboard_title": load_local_config().get("dashboard_title") or "",
    }
    if not BANKROLL_CONFIG_PATH.exists():
        return default
    try:
        data = json.loads(BANKROLL_CONFIG_PATH.read_text(encoding="utf-8"))
        active_accounts = [str(a).strip() for a in (data.get("active_accounts") or []) if str(a).strip()]
        if not active_accounts:
            active_accounts = accounts_from_csv()
        return {
            "active_accounts": active_accounts,
            "baseline_at": data.get("baseline_at") or default["baseline_at"],
            "account_baselines": data.get("account_baselines") or {},
            "account_adjustments": data.get("account_adjustments") or {},
            "notes": data.get("notes", ""),
            "monthly_goal": int(parse_num(data.get("monthly_goal"))) or DEFAULT_MONTHLY_GOAL,
            "goal_days": int(parse_num(data.get("goal_days"))) or DEFAULT_GOAL_DAYS,
            "owed": max(0, int(parse_num(data.get("owed")))),
            "dashboard_title": load_local_config().get("dashboard_title") or "",
        }
    except Exception:
        return default


def save_bankroll_config(config: dict) -> dict:
    sanitized = {
        "active_accounts": [str(a).strip() for a in config.get("active_accounts", DEFAULT_ACTIVE_ACCOUNTS)],
        "baseline_at": config.get("baseline_at") or dt.datetime.now().isoformat(timespec="seconds"),
        "account_baselines": {str(k): int(parse_num(v)) for k, v in (config.get("account_baselines") or {}).items()},
        "account_adjustments": {str(k): int(parse_num(v)) for k, v in ((config.get("account_adjustments") if config.get("account_adjustments") is not None else (load_bankroll_config().get("account_adjustments") or {})) or {}).items()},
        "notes": str(config.get("notes", "")),
        "monthly_goal": int(parse_num(config.get("monthly_goal"))) or DEFAULT_MONTHLY_GOAL,
        "goal_days": max(1, int(parse_num(config.get("goal_days"))) or DEFAULT_GOAL_DAYS),
        "owed": max(0, int(parse_num(config.get("owed")))) if config.get("owed") is not None else max(0, int(parse_num(load_bankroll_config().get("owed")))),
    }
    try:
        BANKROLL_CONFIG_PATH.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}
    return {"saved": True, "config": sanitized}


def active_profile_name() -> str:
    """Name of the Copilot profile the dashboard currently manages."""
    try:
        n = json.loads(ACTIVE_PROFILE_PATH.read_text(encoding="utf-8")).get("name")
        return n or copilot_blocklist_profile_name()
    except Exception:
        return copilot_blocklist_profile_name()


def set_active_profile_name(name: str) -> None:
    ACTIVE_PROFILE_PATH.write_text(json.dumps({"name": str(name)}), encoding="utf-8")


def _profile_path(name: str) -> Path:
    return COPILOT_DIR / f"{name}.profile.json"


def _copilot_blocklist_profile_path() -> Path:
    """Path of the profile the dashboard currently manages (the active one)."""
    return _profile_path(active_profile_name())


def list_copilot_profiles() -> dict:
    active = active_profile_name()
    out = []
    if COPILOT_DIR.exists():
        for p in sorted(COPILOT_DIR.glob("*.profile.json")):
            name = p.name[:-len(".profile.json")]
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                bc = len([x for x in raw.get("blockedItemIds", []) if str(x).lstrip("-").isdigit() and int(x) > 0])
            except Exception:
                bc = None
            out.append({"name": name, "active": name == active, "blocked_count": bc})
    if active not in [o["name"] for o in out]:
        out.insert(0, {"name": active, "active": True, "blocked_count": None})
    return {"active": active, "profiles": out}


def create_suggested_profile(name: str, min_daily_volume: int = 100, min_price: int = 50_000, max_price: int = 2_100_000_000) -> dict:
    """Create a Copilot profile that ALLOWS mid+high tier liquid items and blocks
    everything else (cheap junk + ultra-low-volume rares)."""
    name = str(name or "").strip()
    if not name:
        return {"error": "profile name required"}
    latest = load_wiki_latest_prices(900)
    hourly = (load_wiki_1h_market(900).get("data") or {})
    mapping = fetch_wiki_mapping()
    allowed, universe = set(), []
    for m in mapping:
        if m.get("id") is None:
            continue
        iid = int(m["id"])
        universe.append(iid)
        mk = _research_market_row(iid, latest, hourly, m)
        if not mk:
            continue
        price = mk.get("price") or 0
        dv = mk.get("daily_volume_est") or 0
        if dv >= min_daily_volume and min_price <= price <= max_price:
            allowed.add(iid)
    blocked = [iid for iid in universe if iid not in allowed]
    path = _profile_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            path.with_suffix(path.suffix + ".dashboard.bak").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    path.write_text(json.dumps({"blockedItemIds": blocked}, separators=(",", ":")), encoding="utf-8")
    return {"saved": True, "name": name, "allowed_count": len(allowed), "blocked_count": len(blocked), "total": len(universe)}


def _blocklist_item_row(item_id: int) -> dict:
    info = get_item_info(item_id)
    name = info.get("name") or f"Item {item_id}"
    return {
        "id": int(item_id),
        "item_id": int(item_id),
        "name": name,
        "item": name,
        "icon_url": info.get("icon"),
    }


def load_blocklist() -> dict:
    """Load the real Copilot profile blocklist, not the legacy generated dashboard blocklist."""
    profile_path = _copilot_blocklist_profile_path()
    if not profile_path.exists():
        return {
            "error": f"Copilot blocklist profile not found: {profile_path}",
            "profile_name": active_profile_name(),
            "profile_path": str(profile_path),
            "allowed_items": [],
            "blocked_items": [],
            "stats": {"allowed_count": 0, "blocked_count": 0},
        }
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
        ids = []
        seen = set()
        for value in raw.get("blockedItemIds", []):
            try:
                iid = int(value)
            except Exception:
                continue
            if iid > 0 and iid not in seen:
                ids.append(iid)
                seen.add(iid)
        blocked_items = [_blocklist_item_row(iid) for iid in ids]
        blocked_items.sort(key=lambda x: str(x.get("name", "")).lower())
        return {
            "profile_name": active_profile_name(),
            "profile_path": str(profile_path),
            "source": "flipping-copilot-profile",
            "allowed_items": [],
            "blocked_items": blocked_items,
            "stats": {"allowed_count": 0, "blocked_count": len(blocked_items)},
        }
    except Exception as exc:
        return {
            "error": f"Failed to read Copilot blocklist profile: {exc}",
            "profile_name": active_profile_name(),
            "profile_path": str(profile_path),
            "allowed_items": [],
            "blocked_items": [],
            "stats": {"allowed_count": 0, "blocked_count": 0},
        }


def save_blocklist(data: dict) -> None:
    """Write the real Copilot profile blocklist. Keeps only blockedItemIds, matching the profile schema."""
    profile_path = _copilot_blocklist_profile_path()
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    ids = []
    seen = set()
    for value in data.get("blockedItemIds", []):
        try:
            iid = int(value)
        except Exception:
            continue
        if iid > 0 and iid not in seen:
            ids.append(iid)
            seen.add(iid)
    if profile_path.exists():
        backup = profile_path.with_suffix(profile_path.suffix + ".dashboard.bak")
        try:
            backup.write_text(profile_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
    profile_path.write_text(json.dumps({"blockedItemIds": ids}, separators=(",", ":")), encoding="utf-8")


def rebuild_blocklist() -> dict:
    """Compatibility shim: current blocklist is loaded from the Copilot profile."""
    return load_blocklist()


def get_blocklist() -> dict:
    """Return active Copilot profile blocklist plus advisory review data."""
    return build_blocklist_review(save=True)


def update_blocklist(action: str, item: str | None = None, item_id: int | None = None) -> dict:
    """Add/remove item IDs from the active Flipping Copilot profile blocklist."""
    profile_path = _copilot_blocklist_profile_path()
    if not profile_path.exists():
        return {"error": f"Copilot blocklist profile not found: {profile_path}"}
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": f"Failed to read Copilot blocklist profile: {exc}"}

    ids = []
    seen = set()
    for value in raw.get("blockedItemIds", []):
        try:
            iid = int(value)
        except Exception:
            continue
        if iid > 0 and iid not in seen:
            ids.append(iid)
            seen.add(iid)

    action = str(action or "").strip().lower()
    resolved_id: int | None = None
    if item_id is not None:
        try:
            resolved_id = int(item_id)
        except Exception:
            resolved_id = None
    if resolved_id is None and item:
        info = get_item_info(item)
        if info.get("itemId") is not None:
            resolved_id = int(info["itemId"])

    if not resolved_id or resolved_id <= 0:
        return {"error": "Could not resolve item to a tradeable item ID"}

    if action == "block":
        if resolved_id not in seen:
            ids.append(resolved_id)
    elif action in {"allow", "unblock"}:
        ids = [iid for iid in ids if iid != resolved_id]
    else:
        return {"error": f"Unknown action '{action}'"}

    save_blocklist({"blockedItemIds": ids})
    return get_blocklist()



def _market_row_for_item(item_id: int, latest: dict[int, dict], hourly: dict) -> dict:
    latest_row = latest.get(int(item_id), {}) if latest else {}
    hrow = (hourly.get("data") or {}).get(str(int(item_id)), {}) if hourly else {}
    high = int(latest_row.get("high") or 0)
    low = int(latest_row.get("low") or 0)
    price = high or low
    vol = int(hrow.get("highPriceVolume") or 0) + int(hrow.get("lowPriceVolume") or 0)
    dvol = load_wiki_daily_volumes().get(int(item_id), 0) or vol * 24
    spread_after_tax = int((high * 0.98) - low) if high and low else 0
    roi = (spread_after_tax / low * 100) if low and spread_after_tax else 0
    return {"price": price, "high": high, "low": low, "hourly_volume": vol, "daily_volume_est": dvol, "spread_after_tax": spread_after_tax, "roi": round(roi, 2)}


def _personal_item_stats(rows: list[dict]) -> dict[int, dict]:
    stats: dict[int, dict] = {}
    for r in rows:
        name = str(r.get("Item") or r.get("item") or "").strip()
        info = get_item_info(name)
        iid = info.get("itemId")
        if iid is None:
            continue
        iid = int(iid)
        st = stats.setdefault(iid, {"n": 0, "profit": 0, "wins": 0, "losses": 0})
        profit = parse_num(r.get("Profit") or r.get("profit"))
        st["n"] += 1
        st["profit"] += profit
        if profit >= 0:
            st["wins"] += 1
        else:
            st["losses"] += 1
    for st in stats.values():
        st["avg_profit"] = int(st["profit"] / st["n"]) if st["n"] else 0
        st["win_rate"] = round((st["wins"] / st["n"] * 100), 1) if st["n"] else 0
    return stats


def _blocklist_candidate_row(item_id: int, latest: dict[int, dict], hourly: dict, personal: dict[int, dict]) -> dict:
    info = get_item_info(item_id)
    market = _market_row_for_item(item_id, latest, hourly)
    pst = personal.get(int(item_id), {"n": 0, "profit": 0, "avg_profit": 0, "win_rate": 0})
    name = info.get("name") or f"Item {item_id}"
    score = 0
    price = market["price"]
    vol = market["hourly_volume"]
    spread = market["spread_after_tax"]
    if price >= 20_000_000: score += 35
    elif price >= 5_000_000: score += 20
    elif price >= 1_000_000: score += 10
    if vol >= 250: score += 25
    elif vol >= 100: score += 18
    elif vol >= 40: score += 10
    if spread >= 250_000: score += 20
    elif spread >= 75_000: score += 12
    elif spread >= 25_000: score += 6
    if pst.get("profit", 0) > 0: score += min(20, int(pst.get("profit", 0) / 1_000_000))
    if "3rd age" in name.lower() or "bond" in name.lower(): score -= 80
    reason_bits = []
    if price >= 20_000_000: reason_bits.append("high-value")
    if vol >= 100: reason_bits.append("liquid")
    if spread >= 75_000: reason_bits.append("tax-adjusted spread")
    if pst.get("n", 0): reason_bits.append(f"your history {pst.get('profit',0):,} GP")
    return {"id": int(item_id), "item_id": int(item_id), "name": name, "item": name, "icon_url": info.get("icon"), **market, "personal": pst, "score": score, "reason": " · ".join(reason_bits) or "basic market candidate"}


def build_blocklist_review(save: bool = True) -> dict:
    """Build advisory blocklist review. Does NOT auto-apply block/unblock edits."""
    base = load_blocklist()
    blocked_ids = {int(x.get("item_id") or x.get("id")) for x in base.get("blocked_items", []) if x.get("item_id") or x.get("id")}
    latest = load_wiki_latest_prices(max_age_seconds=900)
    hourly = load_wiki_1h_market(max_age_seconds=900)
    personal = _personal_item_stats(load_rows())
    item_map, _ = _load_item_mapping()
    tradeable_ids = set(latest.keys()) | {int(k) for k in (hourly.get("data") or {}).keys() if str(k).isdigit()}
    if not tradeable_ids:
        tradeable_ids = set(item_map.keys())
    universe_ids = tradeable_ids | blocked_ids
    allowed_ids = sorted(i for i in universe_ids if i not in blocked_ids and i in item_map)

    allowed_rows = [_blocklist_candidate_row(i, latest, hourly, personal) for i in allowed_ids]
    blocked_rows = [_blocklist_candidate_row(i, latest, hourly, personal) for i in sorted(blocked_ids) if i in item_map or i in tradeable_ids]

    allowed_rows.sort(key=lambda x: (x.get("score", 0), x.get("price", 0), x.get("hourly_volume", 0)), reverse=True)
    blocked_rows.sort(key=lambda x: str(x.get("name", "")).lower())

    test_unblock = [x for x in blocked_rows if x["score"] >= 45 and "3rd age" not in x["name"].lower() and "bond" not in x["name"].lower()]
    test_unblock.sort(key=lambda x: x["score"], reverse=True)

    allowed_losers = [x for x in allowed_rows if (x.get("personal", {}).get("n", 0) >= 2 and x.get("personal", {}).get("profit", 0) < 0) or (x.get("price", 0) < 100_000 and x.get("hourly_volume", 0) < 20)]
    allowed_losers.sort(key=lambda x: (x.get("personal", {}).get("profit", 0), x.get("score", 0)))

    stats = {
        "profile_name": active_profile_name(),
        "total_tradeable": len(universe_ids),
        "allowed_count": len(allowed_ids),
        "blocked_count": len(blocked_ids),
        "allowed_pct": round(len(allowed_ids) / max(len(universe_ids), 1) * 100, 1),
        "blocked_pct": round(len(blocked_ids) / max(len(universe_ids), 1) * 100, 1),
        "high_value_allowed": sum(1 for x in allowed_rows if x.get("price", 0) >= 20_000_000),
        "liquid_allowed": sum(1 for x in allowed_rows if x.get("hourly_volume", 0) >= 100),
        "personal_allowed_winners": sum(1 for x in allowed_rows if x.get("personal", {}).get("profit", 0) > 0),
        "personal_allowed_losers": sum(1 for x in allowed_rows if x.get("personal", {}).get("profit", 0) < 0),
        "reviewed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "advisory_only": True,
    }
    review = {
        **base,
        "stats": stats,
        "allowed_items": allowed_rows[:1000],
        "blocked_items": blocked_rows[:6000],
        "allowed_total": len(allowed_rows),
        "blocked_total": len(blocked_rows),
        "suggestions": {
            "test_unblock": test_unblock[:50],
            "consider_block": allowed_losers[:50],
            "best_allowed": allowed_rows[:80],
        },
    }
    if save:
        try:
            BLOCKLIST_REVIEW_PATH.write_text(json.dumps(review, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass
    return review

def load_bankroll_ledger() -> list[dict]:
    """Audit trail of bankroll adjustments (transfers, bond deductions)."""
    if not BANKROLL_LEDGER_PATH.exists():
        return []
    try:
        data = json.loads(BANKROLL_LEDGER_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_bankroll_ledger(entry: dict) -> None:
    """Append one adjustment record to the bankroll audit log."""
    try:
        ledger = load_bankroll_ledger()
        ledger.append({"ts": dt.datetime.now().isoformat(timespec="seconds"), **entry})
        BANKROLL_LEDGER_PATH.write_text(json.dumps(ledger[-500:], indent=2), encoding="utf-8")
    except Exception:
        pass


def _adjustments_dict(updated: dict) -> dict:
    return {str(k): int(parse_num(v)) for k, v in (updated.get("account_adjustments") or {}).items()}


def _ensure_account(updated: dict, *names: str) -> list[str]:
    accounts = [str(a).strip() for a in (updated.get("active_accounts") or DEFAULT_ACTIVE_ACCOUNTS) if str(a).strip()]
    for n in names:
        if n and n not in accounts:
            accounts.append(n)
    updated["active_accounts"] = accounts
    return accounts


def apply_balance_adjustment(config: dict, account: str, amount: int | float | str, kind: str = "deposit") -> dict:
    """Deposit (add) or withdraw (remove) capital from one account.

    Adjusts the account's running balance via the separate adjustments layer, leaving
    the original baseline and realized profit untouched. Deposits/withdrawals are
    capital movements, not profit.
    """
    account = str(account or "").strip()
    amount_i = int(parse_num(amount))
    kind = str(kind or "deposit").strip().lower()
    if not account:
        raise ValueError("account is required")
    if amount_i <= 0:
        raise ValueError("amount must be positive")
    sign = 1 if kind == "deposit" else -1
    updated = dict(config or {})
    _ensure_account(updated, account)
    adj = _adjustments_dict(updated)
    adj[account] = adj.get(account, 0) + sign * amount_i
    updated["account_adjustments"] = adj
    return updated


def apply_bankroll_transfer(config: dict, from_account: str, to_account: str, amount: int | float | str) -> dict:
    """Move capital between accounts (withdraw from one, deposit to the other). Net-zero to total bankroll; does not touch RuneLite/OSRS."""
    from_account = str(from_account or "").strip()
    to_account = str(to_account or "").strip()
    amount_i = int(parse_num(amount))
    if not from_account or not to_account:
        raise ValueError("from_account and to_account are required")
    if from_account == to_account:
        raise ValueError("from_account and to_account must differ")
    if amount_i <= 0:
        raise ValueError("amount must be positive")
    updated = dict(config or {})
    _ensure_account(updated, from_account, to_account)
    adj = _adjustments_dict(updated)
    adj[from_account] = adj.get(from_account, 0) - amount_i
    adj[to_account] = adj.get(to_account, 0) + amount_i
    updated["account_adjustments"] = adj
    return updated


def apply_bond_purchase(config: dict, account: str, amount: int | float | str) -> dict:
    """Deduct bond/membership GP from one account (a withdrawal of capital)."""
    return apply_balance_adjustment(config, account, amount, kind="withdraw")


def find_latest_csv() -> tuple[Path | None, list[dict]]:
    # Use real Copilot exports for dashboard data. Test exports are useful for
    # smoke tests but must not become the selected data source just because they
    # were written more recently than Documents/flips.csv.
    candidates: list[tuple[int, int, str, Path]] = []
    folders = [
        # Project folder is where the API sync writes; the rest are fallbacks for
        # manual Copilot plugin exports. Newest still wins within a priority tier.
        (0, "project", ROOT),
        (0, "user_documents", USER_DOCUMENTS),
        (1, "downloads", DOWNLOADS),
        (2, "desktop", DESKTOP),
        (3, "copilot_dir", COPILOT_DIR),
    ]
    for extra in load_local_config().get("extra_csv_dirs") or []:
        folders.insert(0, (0, "extra_csv_dir", Path(str(extra))))
    for priority, label, root in folders:
        if not root.exists():
            continue
        for pat in ("flips.csv", "*flips*.csv"):
            for p in root.glob(pat):
                if not p.is_file() or p.name.lower() == "flips_api_test.csv":
                    continue
                exact_rank = 0 if p.name.lower() == "flips.csv" else 1
                candidates.append((priority, exact_rank, label, p))
    source_list, seen = [], set()
    for priority, exact_rank, label, p in candidates:
        if str(p) in seen:
            continue
        seen.add(str(p))
        try:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        except Exception:
            mtime = None
        source_list.append({"folder": label, "priority": priority, "exact_rank": exact_rank, "path": str(p), "mtime": mtime})
    source_list.sort(key=lambda x: (x["priority"], x["exact_rank"], x.get("mtime") or ""))
    if not candidates:
        return None, source_list
    best_priority = min(c[0] for c in candidates)
    best_exact_rank = min(c[1] for c in candidates if c[0] == best_priority)
    selected = max((p for pri, rank, _, p in candidates if pri == best_priority and rank == best_exact_rank), key=lambda p: p.stat().st_mtime)
    return selected, source_list


_rows_cache: tuple[str, float, list[dict]] | None = None  # (path, mtime, rows)


def load_rows(csv_path: Path | None = None) -> list[dict]:
    global _rows_cache
    if csv_path is None:
        csv_path, _ = find_latest_csv()
    if not csv_path or not csv_path.exists():
        return []
    try:
        mtime = csv_path.stat().st_mtime
        if _rows_cache and _rows_cache[0] == str(csv_path) and _rows_cache[1] == mtime:
            return _rows_cache[2]
    except Exception:
        mtime = None
    rows: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            r = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
            r["Status"] = str(r.get("Status", "")).strip().upper()
            r["Account"] = str(r.get("Account", "")).strip()
            r["Item"] = str(r.get("Item", "")).strip()
            r["_profit"] = parse_num(r.get("Profit"))
            r["_tax"] = parse_num(r.get("Tax"))
            r["_bought"] = parse_num(r.get("Bought"))
            r["_sold"] = parse_num(r.get("Sold"))
            r["_avg_buy"] = parse_num(r.get("Avg. buy price"))
            r["_avg_sell"] = parse_num(r.get("Avg. sell price"))
            r["_profit_ea"] = parse_num(r.get("Profit ea."))
            r["_item_id"] = parse_num(r.get("Item id") or r.get("Item ID"))
            r["_buy"] = parse_time(r.get("First buy time") or r.get("Buy time"))
            r["_sell"] = parse_time(r.get("Last sell time") or r.get("Sell time"))
            r["_event"] = r.get("_sell") or r.get("_buy")
            if r.get("_buy") and r.get("_sell"):
                r["_dur_h"] = round((r["_sell"] - r["_buy"]).total_seconds() / 3600, 1)
            else:
                r["_dur_h"] = None
            info = get_item_info(r.get("_item_id") or r.get("Item"))
            r["icon_url"] = info.get("icon")
            rows.append(r)
    if mtime is not None:
        _rows_cache = (str(csv_path), mtime, rows)
    return rows


def accounts_from_csv() -> list[str]:
    """Distinct account names found in the newest Copilot CSV. Used when no
    accounts are configured so the dashboard works out of the box."""
    try:
        names = {r.get("Account") for r in load_rows()}
        return sorted(n for n in names if n)
    except Exception:
        return []


def period_bounds() -> dict[str, tuple[dt.datetime, dt.datetime]]:
    now = dt.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + dt.timedelta(days=1)
    yesterday = today - dt.timedelta(days=1)
    this_week_start = today - dt.timedelta(days=today.weekday())
    last_week_start = this_week_start - dt.timedelta(days=7)
    this_month_start = today.replace(day=1)
    last_month_end = this_month_start
    if this_month_start.month == 1:
        last_month_start = this_month_start.replace(year=this_month_start.year - 1, month=12)
    else:
        last_month_start = this_month_start.replace(month=this_month_start.month - 1)
    return {
        "today": (today, tomorrow),
        "yesterday": (yesterday, today),
        "last_week": (last_week_start, this_week_start),
        "this_week": (this_week_start, tomorrow),
        "last_month": (last_month_start, last_month_end),
        "this_month": (this_month_start, tomorrow),
        "yesterday_evening": (yesterday.replace(hour=18), today),
        "last_6h": (now - dt.timedelta(hours=6), now + dt.timedelta(seconds=1)),
        "last_12h": (now - dt.timedelta(hours=12), now + dt.timedelta(seconds=1)),
        "last_24h": (now - dt.timedelta(hours=24), now + dt.timedelta(seconds=1)),
        "last_7_days": (today - dt.timedelta(days=7), tomorrow),
        "last_30_days": (today - dt.timedelta(days=30), tomorrow),
        "all_time": (dt.datetime(2020, 1, 1), tomorrow),
    }


def custom_bounds(start_param: str | None, end_param: str | None) -> tuple[dt.datetime, dt.datetime] | None:
    s, e = parse_time(start_param), parse_time(end_param)
    if s and e and s < e:
        return s, e
    return None


def in_bounds(row: dict, bounds: tuple[dt.datetime, dt.datetime], event_field: str = "_event") -> bool:
    event = row.get(event_field) or row.get("_event")
    return bool(event and bounds[0] <= event < bounds[1])


def active_account_set(config: dict | None = None) -> set[str]:
    cfg = config or load_bankroll_config()
    return {str(a).lower() for a in (cfg.get("active_accounts") or DEFAULT_ACTIVE_ACCOUNTS)}


def rows_for_scope(rows: list[dict], bounds: tuple[dt.datetime, dt.datetime] | None, config: dict | None = None, all_accounts: bool = False, status: str | None = None) -> list[dict]:
    active = active_account_set(config)
    out = []
    for r in rows:
        if status and r.get("Status") != status:
            continue
        if not all_accounts and r.get("Account", "").lower() not in active:
            continue
        if bounds and not in_bounds(r, bounds):
            continue
        out.append(r)
    return out


def item_summary(rows: list[dict]) -> list[dict]:
    by_item: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("Item"):
            by_item[r["Item"]].append(r)
    items = []
    for name, rs in by_item.items():
        profits = [r.get("_profit", 0) for r in rs]
        durs = [r.get("_dur_h") for r in rs if r.get("_dur_h") is not None]
        wins = sum(1 for p in profits if p > 0)
        icon = next((r.get("icon_url") for r in rs if r.get("icon_url")), None)
        items.append({
            "item": name,
            "icon_url": icon,
            "n": len(rs),
            "profit": sum(profits),
            "avg_profit": round(sum(profits) / max(1, len(rs))),
            "median_profit": median(profits) if profits else 0,
            "win_rate": round(wins / max(1, len(rs)) * 100, 1),
            "med_dur_h": round(median(durs), 1) if durs else None,
            "avg_dur_h": round(sum(durs) / len(durs), 1) if durs else None,
            "first_sell": iso(min((r.get("_sell") for r in rs if r.get("_sell")), default=None)),
            "last_sell": iso(max((r.get("_sell") for r in rs if r.get("_sell")), default=None)),
        })
    return sorted(items, key=lambda x: x["profit"], reverse=True)


def problem_items_from(items: list[dict]) -> list[dict]:
    candidates = []
    for x in items:
        reasons = []
        if x.get("profit", 0) < 0:
            reasons.append("negative P/L")
        if x.get("n", 0) >= 3 and x.get("win_rate", 100) < 55:
            reasons.append("low win rate")
        if x.get("n", 0) >= 3 and x.get("avg_profit", 0) < 100_000:
            reasons.append("filler")
        if x.get("med_dur_h") and x["med_dur_h"] > 24 and x.get("avg_profit", 0) < 250_000:
            reasons.append("long hold/low return")
        if reasons:
            score = abs(min(0, x.get("profit", 0))) + (x.get("n", 0) * 50_000)
            candidates.append({**x, "reasons": reasons, "reason": ", ".join(reasons), "score": score})
    return sorted(candidates, key=lambda x: (x["score"], -x.get("profit", 0)), reverse=True)[:12]


def hourly_rate_metrics(activity_rows: list[dict], bounds: tuple[dt.datetime, dt.datetime], effective_end: dt.datetime, gap_minutes: int = 90, recent_minutes: int = 60) -> dict:
    events = []
    for r in activity_rows:
        ev = r.get("_sell") or r.get("_event")
        if ev and bounds[0] <= ev < bounds[1] and ev <= effective_end:
            events.append((ev, r.get("_profit", 0)))
    events.sort(key=lambda x: x[0])
    if not events:
        return {
            "active_session_profit": 0,
            "active_session_hours": 0,
            "active_session_profit_per_hour": 0,
            "recent_profit_per_hour": 0,
            "recent_window_minutes": recent_minutes,
            "trading_state": "idle",
        }

    gap = dt.timedelta(minutes=gap_minutes)
    blocks = []
    block_start = last_ev = events[0][0]
    block_profit = events[0][1]
    for ev, profit in events[1:]:
        if ev - last_ev > gap:
            blocks.append((block_start, last_ev, block_profit))
            block_start = ev
            block_profit = profit
        else:
            block_profit += profit
        last_ev = ev
    blocks.append((block_start, last_ev, block_profit))

    last_start, last_event, session_profit = blocks[-1]
    active_now = effective_end - last_event <= gap
    session_end = effective_end if active_now else last_event
    session_hours = max(1.0, (session_end - last_start).total_seconds() / 3600)
    recent_start = max(bounds[0], effective_end - dt.timedelta(minutes=recent_minutes))
    recent_profit = sum(profit for ev, profit in events if ev >= recent_start)
    recent_hours = max(1.0, (effective_end - recent_start).total_seconds() / 3600)
    return {
        "active_session_profit": session_profit,
        "active_session_hours": round(session_hours, 2),
        "active_session_profit_per_hour": round(session_profit / session_hours),
        "recent_profit_per_hour": round(recent_profit / recent_hours),
        "recent_window_minutes": recent_minutes,
        "trading_state": "active" if active_now else "idle",
    }


def load_session_anchor() -> dt.datetime | None:
    """Manual GP/hour session start time (set via the Reset button)."""
    try:
        raw = json.loads(SESSION_ANCHOR_PATH.read_text(encoding="utf-8"))
        return parse_time(raw.get("anchor"))
    except Exception:
        return None


def save_session_anchor(when: dt.datetime | None = None) -> dt.datetime:
    when = when or dt.datetime.now()
    try:
        SESSION_ANCHOR_PATH.write_text(json.dumps({"anchor": iso(when)}), encoding="utf-8")
    except Exception:
        pass
    return when


def compute_live_rate(rows: list[dict], config: dict | None = None, session_start: str | None = None) -> dict:
    """Real-time GP/hour over genuine rolling time windows (NOT clamped to the
    selected day/period), so the rate spans across midnight correctly.

    Also computes a time-decayed (exponentially-weighted) GP/hour: each flip's
    contribution fades smoothly with a half-life, so a single big flip no longer
    holds the rate flat for a full hour then drops off a cliff. The decayed rate
    converges to the true sustained GP/hour for a steady earning pace.
    """
    cfg = config or load_bankroll_config()
    now = dt.datetime.now()
    wide = (dt.datetime(2000, 1, 1), now + dt.timedelta(days=1))
    finished = rows_for_scope(rows, wide, cfg, all_accounts=False, status="FINISHED")
    partial = [r for r in rows_for_scope(rows, wide, cfg, all_accounts=False, status="SELLING") if r.get("_sold", 0) > 0 and r.get("_profit", 0)]
    events = finished + partial
    metrics = hourly_rate_metrics(events, (dt.datetime(2000, 1, 1), now), now)

    half_life_h = 40 / 60          # 40-minute half-life
    ln2 = 0.6931471805599453
    lam = ln2 / half_life_h        # decay constant (per hour)
    cutoff = now - dt.timedelta(hours=6)   # older flips are negligible (>8 half-lives)
    decayed = 0.0
    for r in events:
        ev = r.get("_sell") or r.get("_event")
        if not ev or ev < cutoff or ev > now:
            continue
        age_h = (now - ev).total_seconds() / 3600
        decayed += r.get("_profit", 0) * (2 ** (-age_h / half_life_h))
    metrics["smoothed_profit_per_hour"] = round(lam * decayed)
    metrics["smoothing_half_life_min"] = round(half_life_h * 60)

    # Rate-over-time series for the sparkline: the smoothed GP/hr as it would have read
    # at each point over the last few hours, so the line rises/falls with your real pace.
    series_hours = 3.0
    n_buckets = 40
    span_start = now - dt.timedelta(hours=series_hours)
    tail = now - dt.timedelta(hours=series_hours + 6)
    pts = []
    for r in events:
        ev = r.get("_sell") or r.get("_event")
        if ev and tail <= ev <= now:
            pts.append((ev, r.get("_profit", 0)))
    rate_v, rate_t = [], []
    step = (now - span_start) / n_buckets
    for i in range(1, n_buckets + 1):
        bt = span_start + step * i
        s = 0.0
        for ev, profit in pts:
            if ev <= bt:
                age_h = (bt - ev).total_seconds() / 3600
                if age_h <= 6:
                    s += profit * (2 ** (-age_h / half_life_h))
        rate_v.append(round(lam * s))
        rate_t.append(iso(bt))
    metrics["rate_series"] = {"values": rate_v, "times": rate_t, "hours": series_hours}

    # "How are we doing vs usual?" — baseline = realized GP per ACTIVE hour over the
    # last 7 days (hours with >=1 finished flip), so quiet stretches read as slow and
    # busy stretches read as busy relative to your own norm.
    wk_cut = now - dt.timedelta(days=7)
    active_buckets = set()
    total_7d = 0
    for r in finished:
        ev = r.get("_sell") or r.get("_event")
        if not ev or ev < wk_cut or ev > now:
            continue
        total_7d += r.get("_profit", 0)
        active_buckets.add((ev.year, ev.timetuple().tm_yday, ev.hour))
    active_hours = max(1, len(active_buckets))
    baseline = round(total_7d / active_hours)
    metrics["baseline_per_hour"] = baseline
    cur = metrics["smoothed_profit_per_hour"]
    ratio = (cur / baseline) if baseline > 0 else None
    metrics["pace_ratio"] = round(ratio, 2) if ratio is not None else None
    metrics["pace_state"] = None if ratio is None else ("busy" if ratio >= 1.4 else "slow" if ratio <= 0.6 else "normal")

    # Manual session timer: exact GP/hour since the user last hit "Reset".
    anchor = (parse_time(session_start) if session_start else None) or load_session_anchor()
    if anchor:
        post = []
        for r in events:
            ev = r.get("_sell") or r.get("_event")
            if ev and anchor <= ev <= now:
                post.append((ev, r.get("_profit", 0)))
        since = sum(p for _, p in post)
        elapsed_h = max((now - anchor).total_seconds() / 3600, 5 / 60)  # 5-min floor so the first minutes aren't wild
        metrics["session_reset_at"] = iso(anchor)
        metrics["session_since_profit"] = since
        metrics["session_since_hours"] = round((now - anchor).total_seconds() / 3600, 3)
        metrics["session_since_per_hour"] = round(since / elapsed_h)
        # Session sparkline: running GP/hr over time since reset (matches the headline's history).
        total_secs = (now - anchor).total_seconds()
        nb = max(4, min(40, int(total_secs // 60)))
        sv, stt = [], []
        for i in range(1, nb + 1):
            bt = anchor + dt.timedelta(seconds=total_secs * i / nb)
            prof = sum(p for ev, p in post if ev <= bt)
            eh = max((bt - anchor).total_seconds() / 3600, 5 / 60)
            sv.append(round(prof / eh))
            stt.append(iso(bt))
        metrics["session_rate_series"] = {"values": sv, "times": stt}
    return metrics


def build_sparklines(finished: list[dict], bounds: tuple[dt.datetime, dt.datetime], effective_end: dt.datetime, buckets: int = 32) -> dict:
    """Range-aware cumulative series for KPI sparklines.

    Splits the period into evenly spaced time buckets between bounds[0] and the
    effective end (now for in-progress ranges, the range end otherwise) and walks
    every finished flip in chronological order, so the curves are accurate for any
    range: today, yesterday, week, month or custom.
    """
    start = bounds[0]
    span = (effective_end - start).total_seconds()
    if span <= 0:
        return {}
    evs = []
    for r in finished:
        t = r.get("_sell") or r.get("_event")
        if t is None:
            continue
        p = r.get("_profit", 0)
        evs.append((t, p, 1 if p > 0 else 0))
    if not evs:
        return {}
    evs.sort(key=lambda x: x[0])
    profit_s: list = []
    avg_s: list = []
    win_s: list = []
    hour_s: list = []
    times_s: list = []
    cum_p = cum_n = cum_w = 0
    idx = 0
    for b in range(1, buckets + 1):
        b_end = start + dt.timedelta(seconds=span * b / buckets)
        while idx < len(evs) and evs[idx][0] <= b_end:
            cum_p += evs[idx][1]
            cum_n += 1
            cum_w += evs[idx][2]
            idx += 1
        elapsed_h = max((b_end - start).total_seconds() / 3600, 1 / 60)
        profit_s.append(round(cum_p))
        avg_s.append(round(cum_p / cum_n) if cum_n else 0)
        win_s.append(round(cum_w / cum_n * 100, 1) if cum_n else 0)
        hour_s.append(round(cum_p / elapsed_h))
        times_s.append(iso(b_end))
    return {"profit": profit_s, "avg_flip": avg_s, "win_rate": win_s, "per_hour": hour_s, "times": times_s}


def compute_period_stats(rows: list[dict], bounds: tuple[dt.datetime, dt.datetime], config: dict | None = None, all_accounts: bool = False) -> dict:
    finished = rows_for_scope(rows, bounds, config, all_accounts, "FINISHED")
    selling_partial = [r for r in rows_for_scope(rows, bounds, config, all_accounts, "SELLING") if r.get("_sold", 0) > 0 and r.get("_profit", 0)]
    profits = [r.get("_profit", 0) for r in finished]

    wins = sum(1 for p in profits if p > 0)
    losses = [p for p in profits if p < 0]
    durs = [r.get("_dur_h") for r in finished if r.get("_dur_h") is not None]
    invested = sum(r.get("_avg_buy", 0) * max(1, r.get("_bought", 0)) for r in finished)
    tax = sum(r.get("_tax", 0) for r in finished)
    items = item_summary(finished)
    accounts: dict[str, dict] = defaultdict(lambda: {"n": 0, "profit": 0, "wins": 0, "under100k": 0})
    windows: dict[str, dict] = defaultdict(lambda: {"n": 0, "profit": 0, "under100k": 0})
    buckets: dict[str, dict] = defaultdict(lambda: {"n": 0, "profit": 0})
    hours: dict[int, dict] = defaultdict(lambda: {"n": 0, "profit": 0})
    for r in finished:
        p = r.get("_profit", 0)
        acc = r.get("Account") or "?"
        accounts[acc]["n"] += 1
        accounts[acc]["profit"] += p
        accounts[acc]["wins"] += int(p > 0)
        accounts[acc]["under100k"] += int(p < 100_000)
        ev = r.get("_sell") or r.get("_event")
        if ev:
            hours[ev.hour]["n"] += 1
            hours[ev.hour]["profit"] += p
            hour = ev.hour
            if 0 <= hour < 6: key = "night"
            elif 6 <= hour < 12: key = "morning"
            elif 12 <= hour < 18: key = "afternoon"
            else: key = "evening"
            windows[key]["n"] += 1
            windows[key]["profit"] += p
            windows[key]["under100k"] += int(p < 100_000)
        if p < 0: b = "loss"
        elif p < 100_000: b = "0-100k"
        elif p < 500_000: b = "100-500k"
        elif p < 1_000_000: b = "500k-1m"
        else: b = "1m+"
        buckets[b]["n"] += 1
        buckets[b]["profit"] += p
    for d in accounts.values():
        d["win_rate"] = round(d["wins"] / max(1, d["n"]) * 100, 1)
        d["under100k_share"] = round(d["under100k"] / max(1, d["n"]) * 100, 1)
    for d in windows.values():
        d["under100k_share"] = round(d["under100k"] / max(1, d["n"]) * 100, 1)
    under100k = sum(1 for p in profits if p < 100_000)
    partial_profit = sum(r.get("_profit", 0) for r in selling_partial)
    partial_units = sum(r.get("_sold", 0) for r in selling_partial)
    total_profit = sum(profits)
    copilot_profit = total_profit + partial_profit
    now = dt.datetime.now()
    effective_end = min(bounds[1], now) if bounds[1] > now and bounds[0] <= now else bounds[1]
    period_hours = max(1.0, (effective_end - bounds[0]).total_seconds() / 3600)
    rate_metrics = hourly_rate_metrics(finished + selling_partial, bounds, effective_end)
    recent_transactions = []
    for r in sorted(finished, key=lambda x: x.get("_sell") or x.get("_event") or dt.datetime.min, reverse=True)[:100]:
        p = r.get("_profit", 0)
        recent_transactions.append({
            "item": r.get("Item", ""),
            "icon_url": r.get("icon_url"),
            "profit": p,
            "result": "win" if p >= 0 else "loss",
            "bought": r.get("Bought"),
            "sold": r.get("Sold"),
            "avg_buy": r.get("_avg_buy"),
            "avg_sell": r.get("_avg_sell"),
            "tax": r.get("_tax"),
            "profit_ea": r.get("_profit_ea"),
            "duration_h": r.get("_dur_h"),
            "last_sell": iso(r.get("_sell")),
        })
    # In-progress sales: SELLING rows with partial fills. Their _profit is the
    # realized profit on the sold portion (straight from Copilot's API), so it
    # belongs in the feed — clearly flagged with sold/total quantities.
    for r in selling_partial:
        p = r.get("_profit", 0)
        recent_transactions.append({
            "item": r.get("Item", ""),
            "icon_url": r.get("icon_url"),
            "profit": p,
            "result": "win" if p >= 0 else "loss",
            "in_progress": True,
            "sold_qty": r.get("_sold"),
            "bought_qty": r.get("_bought"),
            "bought": r.get("Bought"),
            "sold": r.get("Sold"),
            "avg_buy": r.get("_avg_buy"),
            "avg_sell": r.get("_avg_sell"),
            "tax": r.get("_tax"),
            "profit_ea": r.get("_profit_ea"),
            "duration_h": r.get("_dur_h"),
            "last_sell": iso(r.get("_sell")),
        })
    recent_transactions.sort(key=lambda x: x.get("last_sell") or "", reverse=True)
    recent_transactions = recent_transactions[:100]
    return {
        "n": len(finished),
        "profit": total_profit,
        "finished_profit": total_profit,
        "partial_selling_profit": partial_profit,
        "partial_selling_units": partial_units,
        "partial_selling_n": len(selling_partial),
        "copilot_profit": copilot_profit,
        "profit_per_hour": round(copilot_profit / period_hours),
        "period_hours": round(period_hours, 2),
        **rate_metrics,
        "win_rate": round(wins / max(1, len(finished)) * 100, 1),
        "avg_profit": round(sum(profits) / max(1, len(finished))),
        "tax": tax,
        "invested_capital": invested,
        "roi": round(sum(profits) / invested * 100, 2) if invested else None,
        "loss_count": len(losses),
        "loss_profit": sum(losses),
        "under100k": under100k,
        "under100k_share": round(under100k / max(1, len(finished)) * 100, 1),
        "median_duration_h": round(median(durs), 1) if durs else None,
        "avg_duration_h": round(sum(durs) / len(durs), 1) if durs else None,
        "top_items": items[:20],
        "worst_items": sorted(items, key=lambda x: x["profit"])[:20],
        "all_items": items,
        "problem_items": problem_items_from(items),
        "recent_transactions": recent_transactions,
        "sparklines": build_sparklines(finished, bounds, effective_end),
        "accounts": dict(sorted(accounts.items(), key=lambda kv: kv[1]["profit"], reverse=True)),
        "time_windows": dict(windows),
        "profit_buckets": dict(buckets),
        "hourly_profile": [{"hour": h, "n": hours[h]["n"], "profit": hours[h]["profit"]} for h in range(24)],
    }


def apply_partial_selling_to_periods(periods: dict[str, dict]) -> dict[str, dict]:
    """Compatibility hook: partial SELLING profit is already folded into copilot_profit."""
    return periods


def compute_bankroll_plan(csv_data: dict, config: dict | None = None) -> dict:
    cfg = config or load_bankroll_config()
    accounts = cfg.get("active_accounts") or DEFAULT_ACTIVE_ACCOUNTS
    baselines = {a: parse_num(v) for a, v in (cfg.get("account_baselines") or {}).items()}
    adjustments = {a: parse_num(v) for a, v in (cfg.get("account_adjustments") or {}).items()}
    baseline_at = parse_time(cfg.get("baseline_at")) or dt.datetime(2020, 1, 1)
    rows = list(csv_data.get("analysis_rows", [])) + list(csv_data.get("open_rows", []))
    today_bounds = period_bounds()["today"]
    week_bounds = period_bounds()["last_7_days"]
    plan_accounts = {}
    totals = defaultdict(int)
    for acc in accounts:
        base = baselines.get(acc, 0)
        adj = adjustments.get(acc, 0)
        finished_since = 0
        partial_since = 0
        today_finished = today_partial = week_finished = week_partial = all_finished = 0
        flips_since = 0
        for r in rows:
            if r.get("Account") != acc:
                continue
            status = r.get("Status")
            event = r.get("_sell") or r.get("_event")
            p = r.get("_profit", parse_num(r.get("Profit")))
            partial = status == "SELLING" and r.get("_sold", parse_num(r.get("Sold"))) > 0 and p
            finished = status == "FINISHED"
            if not (finished or partial):
                continue
            if event and event >= baseline_at:
                if finished:
                    finished_since += p
                    flips_since += 1
                elif partial:
                    partial_since += p
            if finished:
                all_finished += p
            if event and today_bounds[0] <= event < today_bounds[1]:
                if finished: today_finished += p
                elif partial: today_partial += p
            if event and week_bounds[0] <= event < week_bounds[1]:
                if finished: week_finished += p
                elif partial: week_partial += p
        profit_since = finished_since + partial_since
        deployed = base + adj
        current = deployed + profit_since
        plan_accounts[acc] = {
            "account": acc,
            "baseline": base,
            "adjustments": adj,
            "deployed_capital": deployed,
            "current_bankroll": current,
            "finished_profit_since_baseline": finished_since,
            "partial_selling_profit_since_baseline": partial_since,
            "profit_since_baseline": profit_since,
            "today_profit": today_finished + today_partial,
            "partial_selling_today_profit": today_partial,
            "week_profit": week_finished + week_partial,
            "partial_selling_week_profit": week_partial,
            "all_time_profit": all_finished,
            "roi_since_baseline": round(profit_since / deployed * 100, 1) if deployed > 0 else None,
            "flips_since_baseline": flips_since,
            "needs_setup": base <= 0,
        }
        totals["total_start"] += base
        totals["total_adjustments"] += adj
        totals["total_deployed_capital"] += deployed
        totals["total_current"] += current
        totals["total_profit_since_baseline"] += profit_since
        totals["finished_profit_since_baseline"] += finished_since
        totals["partial_selling_profit_since_baseline"] += partial_since
        totals["total_today_profit"] += today_finished + today_partial
        totals["partial_selling_today_profit"] += today_partial
        totals["total_week_profit"] += week_finished + week_partial
        totals["partial_selling_week_profit"] += week_partial
        totals["total_all_time_profit"] += all_finished
        totals["accounts_needing_setup"] += int(base <= 0)
    totals["roi_since_baseline"] = round(totals["total_profit_since_baseline"] / totals["total_deployed_capital"] * 100, 1) if totals["total_deployed_capital"] else None

    # Cumulative bankroll trend across active accounts since baseline (for the growth chart)
    trend_events: list[tuple[dt.datetime, int]] = []
    for r in rows:
        if r.get("Account") not in accounts:
            continue
        status = r.get("Status")
        event = r.get("_sell") or r.get("_event")
        if not event or event < baseline_at:
            continue
        p = r.get("_profit", parse_num(r.get("Profit")))
        if status == "FINISHED":
            trend_events.append((event, p))
        elif status == "SELLING" and r.get("_sold", parse_num(r.get("Sold"))) > 0 and p:
            trend_events.append((event, p))
    trend_events.sort(key=lambda x: x[0])
    start_total = int(totals["total_start"])
    trend: dict = {"times": [], "values": [], "start": start_total, "end": start_total}
    if trend_events:
        span_start = baseline_at if baseline_at.year > 2020 else trend_events[0][0]
        if span_start > trend_events[0][0]:
            span_start = trend_events[0][0]
        span_end = dt.datetime.now()
        total_span = max((span_end - span_start).total_seconds(), 1.0)
        buckets = 48
        cum = 0
        idx = 0
        times_out: list = []
        values_out: list = []
        for b in range(1, buckets + 1):
            b_end = span_start + dt.timedelta(seconds=total_span * b / buckets)
            while idx < len(trend_events) and trend_events[idx][0] <= b_end:
                cum += trend_events[idx][1]
                idx += 1
            times_out.append(iso(b_end))
            values_out.append(int(start_total + cum))
        trend = {"times": times_out, "values": values_out, "start": start_total, "end": int(start_total + cum)}

    return {"active_accounts": accounts, "baseline_at": cfg.get("baseline_at"), "accounts": plan_accounts, "totals": dict(totals), "trend": trend, "ledger": load_bankroll_ledger()[-100:], "notes": cfg.get("notes", ""), "owed": max(0, int(parse_num(cfg.get("owed"))))}


def csv_freshness(csv_path: Path | None, rows: list[dict]) -> dict:
    if not csv_path or not csv_path.exists():
        return {"status": "missing", "label": "Missing CSV"}
    now = dt.datetime.now()
    mtime = dt.datetime.fromtimestamp(csv_path.stat().st_mtime)
    flip_times = [r.get("_event") for r in rows if r.get("_event")]
    sell_times = [r.get("_sell") for r in rows if r.get("_sell")]
    latest_flip = max(flip_times) if flip_times else None
    latest_realized = max(sell_times) if sell_times else None
    csv_age_h = (now - mtime).total_seconds() / 3600
    flip_age_h = (now - latest_flip).total_seconds() / 3600 if latest_flip else None
    realized_age_h = (now - latest_realized).total_seconds() / 3600 if latest_realized else None
    status = "fresh" if csv_age_h < 6 else "stale"
    age = realized_age_h if realized_age_h is not None else csv_age_h
    return {
        "status": status,
        "label": f"{'Fresh' if status == 'fresh' else 'Stale'} · CSV/realized stats {age:.1f}h ago",
        "csv_age_hours": round(csv_age_h, 2),
        "latest_flip_age_hours": round(flip_age_h, 2) if flip_age_h is not None else None,
        "latest_realized_age_hours": round(realized_age_h, 2) if realized_age_h is not None else None,
        "csv_mtime": iso(mtime),
        "max_buy_time": iso(max((r.get("_buy") for r in rows if r.get("_buy")), default=None)),
        "max_sell_time": iso(latest_realized),
        "latest_flip_time": iso(latest_flip),
    }


def build_comparisons(periods: dict[str, dict]) -> dict:
    today = periods.get("today", {})
    yesterday = periods.get("yesterday", {})
    week = periods.get("last_7_days", {})
    month = periods.get("last_30_days", {})
    today_profit = today.get("copilot_profit", today.get("profit", 0))
    yesterday_profit = yesterday.get("copilot_profit", yesterday.get("profit", 0))
    week_day = week.get("copilot_profit", week.get("profit", 0)) / 7 if week else 0
    month_day = month.get("copilot_profit", month.get("profit", 0)) / 30 if month else 0
    t_avg = today.get("avg_profit", 0)
    m_avg = month.get("avg_profit", 0)
    return {
        "today_vs_yesterday_pct": round((today_profit - yesterday_profit) / yesterday_profit * 100, 1) if yesterday_profit else None,
        "today_vs_7d_avg_pct": round((today_profit - week_day) / week_day * 100, 1) if week_day else None,
        "today_vs_30d_avg_pct": round((today_profit - month_day) / month_day * 100, 1) if month_day else None,
        "avg_flip_vs_30d_pct": round((t_avg - m_avg) / m_avg * 100, 1) if m_avg else None,
        "yesterday_profit": round(yesterday_profit),
        "week_avg_day": round(week_day),
        "month_avg_day": round(month_day),
    }


def load_csv_metrics(csv_path: Path | None) -> dict:
    rows = load_rows(csv_path)
    finished = [r for r in rows if r.get("Status") == "FINISHED"]
    open_rows = [r for r in rows if r.get("Status") != "FINISHED"]
    return {"available": bool(csv_path), "path": str(csv_path) if csv_path else None, "rows": rows, "analysis_rows": finished, "open_rows": open_rows, "row_count": len(rows)}


def _latest_open_buy_by_item(rows: list[dict]) -> dict[int, dict]:
    matches: dict[int, dict] = {}
    for r in sorted(rows, key=lambda x: x.get("_buy") or dt.datetime.min, reverse=True):
        if r.get("Status") not in {"SELLING", "BOUGHT", "BUYING"}:
            continue
        iid = r.get("_item_id") or (get_item_info(r.get("Item")).get("itemId") if r.get("Item") else 0)
        if not iid or int(iid) in matches:
            continue
        if r.get("_avg_buy"):
            matches[int(iid)] = r
    return matches


def _recent_open_buy_by_item(rows: list[dict], max_age_hours: int = 48) -> dict[int, dict]:
    """Recent API/CSV open rows usable as live cost basis.

    Item-only matching is unsafe across old sessions; require a fresh open row so
    stale months-old Copilot rows cannot create fake live unrealized P/L.
    """
    now = dt.datetime.now()
    cutoff = now - dt.timedelta(hours=max_age_hours)
    matches: dict[int, dict] = {}
    def cost_rank(r: dict) -> tuple[int, dt.datetime]:
        status_priority = {"SELLING": 2, "BOUGHT": 2, "BUYING": 1}.get(r.get("Status"), 0)
        return (status_priority, r.get("_buy") or dt.datetime.min)
    for r in sorted(rows, key=cost_rank, reverse=True):
        # Prefer rows that represent owned/selling inventory as live cost basis.
        # BUYING rows are only a last-resort cost basis for a live SELLING slot;
        # they must not overwrite fresher/safer SELLING or BOUGHT rows.
        if r.get("Status") not in {"SELLING", "BOUGHT", "BUYING"} or not r.get("_avg_buy"):
            continue
        buy_time = r.get("_buy")
        if not buy_time or buy_time < cutoff or buy_time > now + dt.timedelta(hours=2):
            continue
        iid = r.get("_item_id") or (get_item_info(r.get("Item")).get("itemId") if r.get("Item") else 0)
        if not iid or int(iid) in matches:
            continue
        matches[int(iid)] = r
    return matches


def _ge_tax(sale_value: int) -> int:
    return min(5_000_000, int(sale_value * 0.02)) if sale_value > 0 else 0


def _ge_tax_total(unit_price: int, qty: int) -> int:
    """GE tax for qty items at unit_price: 2% per item, capped at 5m PER ITEM
    (the cap is per item, not per offer — taxing the gross undercounts big stacks)."""
    if unit_price <= 0 or qty <= 0:
        return 0
    return qty * min(5_000_000, int(unit_price * 0.02))


def load_wiki_latest_prices(max_age_seconds: int = 300) -> dict[int, dict]:
    """Load OSRS Wiki latest prices with a short local cache."""
    try:
        if WIKI_LATEST_CACHE_PATH.exists():
            age = dt.datetime.now().timestamp() - WIKI_LATEST_CACHE_PATH.stat().st_mtime
            if age < max_age_seconds:
                raw = json.loads(WIKI_LATEST_CACHE_PATH.read_text(encoding="utf-8"))
                return {int(k): v for k, v in raw.get("data", raw).items()}
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            "https://prices.runescape.wiki/api/v1/osrs/latest",
            headers={"User-Agent": WIKI_USER_AGENT},
        )
        raw = json.loads(urllib.request.urlopen(req, timeout=12).read())
        WIKI_LATEST_CACHE_PATH.write_text(json.dumps(raw), encoding="utf-8")
        return {int(k): v for k, v in raw.get("data", {}).items()}
    except Exception:
        return {}


def wiki_get_json(path: str, params: dict[str, Any] | None = None) -> dict:
    """Fetch one OSRS Wiki prices API JSON endpoint."""
    query = f"?{urlencode(params or {})}" if params else ""
    req = urllib.request.Request(
        f"{WIKI_API_BASE}{path}{query}",
        headers={"User-Agent": WIKI_USER_AGENT},
    )
    return json.loads(urllib.request.urlopen(req, timeout=12).read())


def load_wiki_market_bucket(path: str, cache_path: Path, max_age_seconds: int = 600) -> dict:
    try:
        if cache_path.exists():
            age = dt.datetime.now().timestamp() - cache_path.stat().st_mtime
            if age < max_age_seconds:
                return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        raw = wiki_get_json(path)
        cache_path.write_text(json.dumps(raw), encoding="utf-8")
        return raw
    except Exception:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {"data": {}, "timestamp": None}


def load_wiki_1h_market(max_age_seconds: int = 600) -> dict:
    """Load the current all-item 1h OSRS Wiki volume bucket with a short cache."""
    return load_wiki_market_bucket("/1h", WIKI_1H_CACHE_PATH, max_age_seconds)


def load_wiki_5m_market(max_age_seconds: int = 300) -> dict:
    return load_wiki_market_bucket("/5m", WIKI_5M_CACHE_PATH, max_age_seconds)


def load_wiki_24h_market(max_age_seconds: int = 1800) -> dict:
    return load_wiki_market_bucket("/24h", WIKI_24H_CACHE_PATH, max_age_seconds)


_wiki_daily_volumes_cache: dict[int, int] | None = None
_wiki_daily_volumes_at: float = 0.0


def load_wiki_daily_volumes(max_age_seconds: int = 1800) -> dict[int, int]:
    """Real per-item daily traded volume from the wiki /volumes endpoint
    (one batch call). Far more reliable than estimating from a single 1h snapshot."""
    global _wiki_daily_volumes_cache, _wiki_daily_volumes_at
    now = dt.datetime.now().timestamp()
    if _wiki_daily_volumes_cache is not None and (now - _wiki_daily_volumes_at) < max_age_seconds:
        return _wiki_daily_volumes_cache
    raw = load_wiki_market_bucket("/volumes", WIKI_VOLUMES_CACHE_PATH, max_age_seconds)
    data = (raw.get("data") if isinstance(raw, dict) else None) or {}
    out: dict[int, int] = {}
    for k, v in data.items():
        try:
            out[int(k)] = int(v)
        except Exception:
            continue
    if out:
        _wiki_daily_volumes_cache = out
        _wiki_daily_volumes_at = now
    return out


def total_market_volume_from_1h(raw: dict) -> int:
    total = 0
    for row in (raw.get("data") or {}).values():
        total += int(row.get("highPriceVolume") or 0) + int(row.get("lowPriceVolume") or 0)
    return total


def speed_label_from_live_volume(hourly_volume: int | float | None) -> tuple[str, str]:
    """Classify absolute all-item GE volume from OSRS Wiki, not same-hour history."""
    volume = int(hourly_volume or 0)
    if volume <= 0:
        return "Market pace unavailable", "unknown"
    if volume >= 60_000_000:
        return "Market is very active", "very_fast"
    if volume >= 45_000_000:
        return "Market is moving fast", "fast"
    if volume >= 32_000_000:
        return "Market pace is decent", "decent"
    if volume >= 22_000_000:
        return "Market is slow", "slow"
    return "Market is very slow", "very_slow"


def build_market_speed_status() -> dict:
    """Classify current GE activity from live OSRS Wiki all-item volume.

    This intentionally does not compare against same-hour local history. The goal is a general
    read of whether the market is active right now (for example EU work hours vs peak gaming
    hours) backed by live Wiki /1h and /5m volume data.
    """
    raw = load_wiki_1h_market()
    current_volume = total_market_volume_from_1h(raw)
    timestamp = raw.get("timestamp") or int(dt.datetime.now(dt.timezone.utc).timestamp())
    observed = dt.datetime.fromtimestamp(int(timestamp), tz=dt.timezone.utc)
    five_raw = load_wiki_5m_market()
    five_min_volume = total_market_volume_from_1h(five_raw)
    five_min_hourly_rate = int(five_min_volume * 12) if five_min_volume else 0

    # Use the completed/current 1h bucket as the stable signal, with the latest 5m bucket
    # blended in lightly so the card reacts when the market is clearly picking up/dying down.
    if current_volume and five_min_hourly_rate:
        activity_volume = int((current_volume * 0.75) + (five_min_hourly_rate * 0.25))
    else:
        activity_volume = current_volume or five_min_hourly_rate

    label, status = speed_label_from_live_volume(activity_volume)
    trend = "steady"
    if current_volume and five_min_hourly_rate:
        recent_ratio = five_min_hourly_rate / max(current_volume, 1)
        if recent_ratio >= 1.25:
            trend = "picking up"
        elif recent_ratio <= 0.75:
            trend = "cooling off"
    else:
        recent_ratio = None

    return {
        "status": status,
        "label": label,
        "current_hour_utc": observed.hour,
        "current_volume": current_volume,
        "recent_5m_volume": five_min_volume,
        "recent_5m_hourly_rate": five_min_hourly_rate,
        "activity_volume": activity_volume,
        "recent_ratio": round(recent_ratio, 2) if recent_ratio is not None else None,
        "trend": trend,
        "basis": "live absolute OSRS Wiki volume",
        "source": "OSRS Wiki /1h and /5m all-item volume",
    }


def item_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def wiki_icon_url(icon: str | None) -> str | None:
    if not icon:
        return None
    return "https://oldschool.runescape.wiki/images/" + quote(icon.replace(" ", "_"))


def fetch_wiki_mapping(max_age_seconds: int = 86_400) -> list[dict]:
    """Load OSRS Wiki item mapping with a local cache for fast research search."""
    global _wiki_mapping_cache
    if _wiki_mapping_cache is not None:
        return _wiki_mapping_cache
    try:
        if MAPPING_CACHE_PATH.exists():
            age = dt.datetime.now().timestamp() - MAPPING_CACHE_PATH.stat().st_mtime
            if age < max_age_seconds:
                raw = json.loads(MAPPING_CACHE_PATH.read_text(encoding="utf-8"))
                _wiki_mapping_cache = raw.get("data", raw) if isinstance(raw, dict) else raw
                return _wiki_mapping_cache
    except Exception:
        pass
    try:
        raw = wiki_get_json("/mapping")
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        MAPPING_CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
        _wiki_mapping_cache = data
        return data
    except Exception:
        return []


def compact_wiki_item(x: dict) -> dict:
    name = str(x.get("name") or "")
    return {
        "id": int(x.get("id") or 0),
        "name": name,
        "slug": item_slug(name),
        "examine": x.get("examine"),
        "members": bool(x.get("members")),
        "limit": x.get("limit"),
        "lowalch": x.get("lowalch"),
        "highalch": x.get("highalch"),
        "icon": x.get("icon"),
        "icon_url": wiki_icon_url(x.get("icon")),
    }


def search_wiki_items(query: str, limit: int = 20) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return []
    terms = [t for t in re.split(r"\s+", q) if t]
    try:
        latest_prices = wiki_get_json("/latest").get("data", {})
    except Exception:
        latest_prices = {}
    matches: list[tuple[int, dict]] = []
    for raw in fetch_wiki_mapping():
        # Most GE items have a buy limit, but some newer priced/tradeable items
        # temporarily have limit=None in Wiki mapping. Keep those if /latest has
        # price data, otherwise skip quest/junk mapping rows.
        item_id = str(raw.get("id") or "")
        if raw.get("limit") is None and item_id not in latest_prices:
            continue
        name = str(raw.get("name") or "")
        lname = name.lower()
        slug = item_slug(name)
        if all(t in lname or t in slug for t in terms):
            score = 0 if lname == q or slug == q else 1 if lname.startswith(q) or slug.startswith(q) else 2
            matches.append((score, compact_wiki_item(raw)))
    matches.sort(key=lambda x: (x[0], x[1]["name"]))
    return [x for _, x in matches[:limit]]


def _find_wiki_item(identifier: str) -> dict | None:
    ident = unquote(str(identifier or "")).strip()
    slug = item_slug(ident)
    for raw in fetch_wiki_mapping():
        if str(raw.get("id")) == ident or item_slug(str(raw.get("name") or "")) == slug or str(raw.get("name") or "").lower() == ident.lower():
            return raw
    found = search_wiki_items(ident, limit=1)
    if found:
        fid = found[0]["id"]
        for raw in fetch_wiki_mapping():
            if int(raw.get("id") or 0) == fid:
                return raw
    return None


def fetch_wiki_item_detail(identifier: str, timestep: str = "1h", chart_days: int = 7) -> dict:
    raw_item = _find_wiki_item(identifier)
    if not raw_item:
        return {"error": "item not found", "query": identifier}
    item = compact_wiki_item(raw_item)
    item_id = item["id"]
    latest_all = wiki_get_json("/latest").get("data", {})
    latest = latest_all.get(str(item_id), {}) or latest_all.get(item_id, {}) or {}
    try:
        chart = wiki_get_json("/timeseries", {"id": item_id, "timestep": timestep}).get("data", [])
    except Exception:
        chart = []
    high = parse_num(latest.get("high"))
    low = parse_num(latest.get("low"))
    spread = high - low if high and low else 0
    margin_after_tax = high - _ge_tax(high) - low if high and low else 0
    volume_24h = sum(parse_num(p.get("highPriceVolume")) + parse_num(p.get("lowPriceVolume")) for p in chart[-24:]) if timestep == "1h" else 0
    hourly_all = (load_wiki_1h_market(900).get("data") or {})
    hrow = hourly_all.get(str(item_id), {}) or {}
    volume_1h = int(hrow.get("highPriceVolume") or 0) + int(hrow.get("lowPriceVolume") or 0)
    blocked = item_id in _current_blocked_ids()
    limit_val = parse_num(item.get("limit")) or None
    roi_pct = round(margin_after_tax / low * 100, 2) if low else None
    profit_per_limit = int(margin_after_tax * limit_val) if (margin_after_tax > 0 and limit_val) else 0
    chart_days = chart_days if chart_days in {1, 7, 30, 180} else 7
    points_per_day = {"1h": 24, "6h": 4, "24h": 1}.get(timestep, 24)
    chart_limit = max(1, chart_days * points_per_day)
    chart_points = chart[-chart_limit:]
    mid_prices = []
    for point in chart_points:
        ph = parse_num(point.get("avgHighPrice"))
        pl = parse_num(point.get("avgLowPrice"))
        if ph and pl:
            mid_prices.append((ph + pl) / 2)
        elif ph or pl:
            mid_prices.append(ph or pl)
    first_mid = mid_prices[0] if mid_prices else 0
    last_mid = mid_prices[-1] if mid_prices else 0
    price_summary = {
        "low_7d": round(min(mid_prices)) if mid_prices else 0,
        "high_7d": round(max(mid_prices)) if mid_prices else 0,
        "avg_7d": round(sum(mid_prices) / len(mid_prices)) if mid_prices else 0,
        "trend_7d_pct": round((last_mid - first_mid) / first_mid * 100, 1) if first_mid else None,
    }
    market_stats = {
        "market_price": round((high + low) / 2) if high and low else high or low,
        "instant_buy": high,
        "instant_sell": low,
        "spread": spread,
        "margin_after_tax": margin_after_tax,
        "margin_pct": round(margin_after_tax / low * 100, 2) if low else None,
        "tax": _ge_tax(high),
        "roi_pct": roi_pct,
        "volume_24h": volume_24h,
        "volume_1h": volume_1h,
        "daily_volume_est": volume_1h * 24,
        "buy_limit": item.get("limit"),
        "profit_per_limit": profit_per_limit,
        "members": bool(item.get("members")),
        "highalch": parse_num(item.get("highalch")) or None,
        "highalch_delta": parse_num(item.get("highalch")) - low if low and item.get("highalch") else None,
        "trend_7d_pct": price_summary["trend_7d_pct"],
        "blocked": blocked,
    }
    try:
        flipping_stats = get_item_detail(item.get("name") or identifier, period_name="all_time")
    except Exception as exc:
        flipping_stats = {"error": str(exc)}
    if flipping_stats.get("error"):
        flipping_stats = {
            "available": False,
            "n": 0,
            "profit": 0,
            "avg_profit": 0,
            "win_rate": 0,
            "med_dur_h": None,
            "best_hour": None,
            "flips": [],
            "scope_label": "all_time · active accounts",
        }
    else:
        flipping_stats = dict(flipping_stats)
        flipping_stats["available"] = True
        flipping_stats.setdefault("scope_label", "all_time · active accounts")
        flipping_stats["flips"] = list(flipping_stats.get("flips", []))[:12]

    return {
        "item": item,
        "latest": latest,
        "spread": spread,
        "margin_after_tax": margin_after_tax,
        "volume_24h": volume_24h,
        "chart": chart_points,
        "chart_range": f"{chart_days}d",
        "chart_timestep": timestep,
        "price_summary": price_summary,
        "market_stats": market_stats,
        "flipping_stats": flipping_stats,
        "source": "OSRS Wiki prices API",
    }


def _current_blocked_ids() -> set[int]:
    """Item IDs currently blocked in the active Flipping Copilot profile."""
    try:
        raw = json.loads(_copilot_blocklist_profile_path().read_text(encoding="utf-8"))
        return {int(x) for x in raw.get("blockedItemIds", []) if str(x).lstrip("-").isdigit() and int(x) > 0}
    except Exception:
        return set()


def _research_market_row(item_id: int | None, latest: dict, hourly: dict, meta: dict | None) -> dict:
    """Live market metrics for one item from the OSRS Wiki latest + 1h buckets."""
    if not item_id:
        return {}
    row = latest.get(int(item_id), {}) or {}
    high = int(row.get("high") or 0)  # instant-buy price (what you pay)
    low = int(row.get("low") or 0)    # instant-sell price (what you receive)
    if high <= 0 and low <= 0:
        return {}
    if high >= 2_000_000_000 or low >= 2_000_000_000:
        return {}
    hrow = (hourly or {}).get(str(int(item_id)), {}) or {}
    vol = int(hrow.get("highPriceVolume") or 0) + int(hrow.get("lowPriceVolume") or 0)
    dvol = load_wiki_daily_volumes().get(int(item_id), 0) or vol * 24
    tax = _ge_tax(high)
    margin = (high - tax - low) if (high and low) else 0
    roi = round(margin / low * 100, 2) if low else None
    price = round((high + low) / 2) if (high and low) else (high or low)
    limit = (meta or {}).get("limit")
    try:
        limit = int(limit) if limit not in (None, "") else None
    except Exception:
        limit = None
    cap = min(vol, limit) if limit else vol
    potential = int(margin * cap) if margin > 0 else 0
    high_time = row.get("highTime") or 0
    low_time = row.get("lowTime") or 0
    return {
        "price": price,
        "instant_buy": high,
        "instant_sell": low,
        "spread": (high - low) if (high and low) else 0,
        "margin_after_tax": margin,
        "roi_pct": roi,
        "tax": tax,
        "hourly_volume": vol,
        "daily_volume_est": dvol,
        "buy_limit": limit,
        "members": bool((meta or {}).get("members")),
        "highalch": (meta or {}).get("highalch"),
        "potential_hourly": potential,
        "last_trade_ts": max(int(high_time or 0), int(low_time or 0)) or None,
    }


def build_item_research(opportunity_limit: int = 120) -> dict:
    """Research database: your all-time flipped items enriched with live market data,
    plus a market-wide scan of the best current flip opportunities."""
    csv_path, _ = find_latest_csv()
    rows = load_rows(csv_path)
    config = load_bankroll_config()
    bounds = (dt.datetime.min, dt.datetime.max)
    finished = rows_for_scope(rows, bounds, config, all_accounts=False, status="FINISHED")
    personal = item_summary(finished)

    latest = load_wiki_latest_prices(900)
    hourly = (load_wiki_1h_market(900).get("data") or {})
    mapping = fetch_wiki_mapping()
    by_id: dict[int, dict] = {}
    by_name: dict[str, dict] = {}
    for m in mapping:
        if m.get("id") is not None:
            by_id[int(m["id"])] = m
        if m.get("name"):
            by_name[str(m["name"]).lower()] = m
    blocked = _current_blocked_ids()

    flipped = []
    for it in personal:
        info = get_item_info(it.get("item"))
        iid = info.get("itemId")
        meta = by_id.get(int(iid)) if iid else by_name.get(str(it.get("item", "")).lower())
        mk = _research_market_row(iid, latest, hourly, meta) if iid else {}
        flipped.append({
            **it,
            "item_id": iid,
            "slug": item_slug(it.get("item", "")),
            "blocked": bool(iid and int(iid) in blocked),
            "members": bool((meta or {}).get("members")),
            **mk,
        })

    flipped_ids = {int(x["item_id"]) for x in flipped if x.get("item_id")}
    opps = []
    for iid, meta in by_id.items():
        mk = _research_market_row(iid, latest, hourly, meta)
        if not mk:
            continue
        if mk["hourly_volume"] < 30:
            continue
        if mk["margin_after_tax"] <= 0:
            continue
        if mk["price"] < 1_000:
            continue
        roi = mk["roi_pct"]
        if roi is None or roi <= 0 or roi > 60:
            continue
        opps.append({
            "item_id": iid,
            "item": meta.get("name"),
            "name": meta.get("name"),
            "slug": item_slug(meta.get("name") or ""),
            "icon_url": f"https://static.runelite.net/cache/item/icon/{iid}.png",
            "blocked": iid in blocked,
            "flipped_by_me": iid in flipped_ids,
            **mk,
        })
    opps.sort(key=lambda x: x.get("potential_hourly", 0), reverse=True)
    opps = opps[:opportunity_limit]

    # Capital-efficient / fast-turnover scan: high return-on-capital, liquid, mid-value
    # items where the full buy limit clears fast (the "more GP/hour per slot" opportunity).
    efficient = []
    for iid, meta in by_id.items():
        mk = _research_market_row(iid, latest, hourly, meta)
        if not mk:
            continue
        roi = mk["roi_pct"]
        vol = mk["hourly_volume"]
        price = mk["price"]
        lim = mk["buy_limit"]
        margin = mk["margin_after_tax"]
        if margin < 50_000 or roi is None or roi < 1.0 or roi > 60:
            continue
        if price < 100_000 or price > 200_000_000:   # mid-tier: recycles, not mega-capital
            continue
        if not lim or vol < lim:                       # liquid enough to fill the whole limit within ~an hour
            continue
        efficient.append({
            "item_id": iid,
            "item": meta.get("name"),
            "name": meta.get("name"),
            "slug": item_slug(meta.get("name") or ""),
            "icon_url": f"https://static.runelite.net/cache/item/icon/{iid}.png",
            "blocked": iid in blocked,
            "flipped_by_me": iid in flipped_ids,
            "fill_hours": round(lim / vol, 2) if vol else None,
            **mk,
        })
    efficient.sort(key=lambda x: x.get("potential_hourly", 0), reverse=True)
    efficient = efficient[:40]

    market_flipped = [x for x in flipped if x.get("price")]
    rois = [x["roi_pct"] for x in market_flipped if x.get("roi_pct") is not None]
    stats = {
        "flipped_count": len(flipped),
        "total_flips": sum(int(x.get("n", 0)) for x in flipped),
        "total_profit": sum(int(x.get("profit", 0)) for x in flipped),
        "profitable_count": sum(1 for x in flipped if x.get("profit", 0) > 0),
        "blocked_count": sum(1 for x in flipped if x.get("blocked")),
        "avg_roi_pct": round(sum(rois) / len(rois), 2) if rois else None,
        "opportunities_count": len(opps),
        "efficient_count": len(efficient),
        "reviewed_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    return {
        "stats": stats,
        "flipped": flipped,
        "opportunities": opps,
        "efficient": efficient,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "OSRS Wiki prices API + your flip history",
    }


# ---------------------------------------------------------------------------
# Market history: local per-item price/volume history accumulated from the
# OSRS Wiki 1h buckets (plus a one-time throttled timeseries bootstrap).
# This is what enables real trading stats: z-scores, volatility, margin
# consistency and expected-value ranking — the same data the paid sites have.
# ---------------------------------------------------------------------------

HISTORY_RETENTION_DAYS = 14
HISTORY_WINDOW_DAYS = 7          # stats window
HISTORY_SNAPSHOT_MIN_GAP_S = 1800
BOOTSTRAP_MAX_ITEMS = 250
BOOTSTRAP_CALL_GAP_S = 0.35

_history_lock = threading.Lock()
_last_snapshot_ts = 0.0
_history_stats_cache: tuple[float, dict[int, dict]] | None = None
_bootstrap_started = False


def _history_db() -> sqlite3.Connection:
    conn = sqlite3.connect(MARKET_HISTORY_DB_PATH, timeout=15)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hist ("
        " item_id INTEGER NOT NULL, ts INTEGER NOT NULL,"
        " high REAL, low REAL, vol_buy INTEGER, vol_sell INTEGER,"
        " PRIMARY KEY (item_id, ts))"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def record_market_snapshot(hourly_data: dict, snapshot_ts: int | None = None, min_gap_s: int = HISTORY_SNAPSHOT_MIN_GAP_S) -> int:
    """Append the current all-item 1h bucket to local history (rate-limited)."""
    global _last_snapshot_ts
    now = time.time()
    with _history_lock:
        if now - _last_snapshot_ts < min_gap_s:
            return 0
        _last_snapshot_ts = now
    ts = int(snapshot_ts or now)
    ts -= ts % 3600  # bucket to the hour so re-runs upsert instead of duplicating
    rows = []
    for key, row in (hourly_data or {}).items():
        try:
            iid = int(key)
        except Exception:
            continue
        ah = parse_num(row.get("avgHighPrice"))
        al = parse_num(row.get("avgLowPrice"))
        vb = parse_num(row.get("highPriceVolume"))
        vs = parse_num(row.get("lowPriceVolume"))
        if (ah or al) and (vb or vs):
            rows.append((iid, ts, float(ah or al), float(al or ah), vb, vs))
    if not rows:
        return 0
    conn = _history_db()
    try:
        with conn:
            conn.executemany("INSERT OR REPLACE INTO hist VALUES (?,?,?,?,?,?)", rows)
            cutoff = int(now) - HISTORY_RETENTION_DAYS * 86400
            conn.execute("DELETE FROM hist WHERE ts < ?", (cutoff,))
        return len(rows)
    finally:
        conn.close()


def _bootstrap_history(candidate_ids: list[int]) -> None:
    """One-time background backfill of 7d hourly history for liquid items via
    /timeseries, politely throttled. Marks completion in the meta table."""
    conn = _history_db()
    try:
        raw = conn.execute("SELECT v FROM meta WHERE k='bootstrapped_ids'").fetchone()
        done = set(json.loads(raw[0])) if raw else set()
    except Exception:
        done = set()
    finally:
        conn.close()
    todo = [i for i in candidate_ids if i not in done][:BOOTSTRAP_MAX_ITEMS]
    if not todo:
        return
    cutoff = int(time.time()) - HISTORY_WINDOW_DAYS * 86400
    for iid in todo:
        try:
            series = wiki_get_json("/timeseries", {"id": iid, "timestep": "1h"}).get("data") or []
            rows = []
            for p in series:
                ts = parse_num(p.get("timestamp"))
                ah = parse_num(p.get("avgHighPrice"))
                al = parse_num(p.get("avgLowPrice"))
                vb = parse_num(p.get("highPriceVolume"))
                vs = parse_num(p.get("lowPriceVolume"))
                if ts >= cutoff and (ah or al):
                    rows.append((iid, ts, float(ah or al), float(al or ah), vb, vs))
            if rows:
                conn = _history_db()
                try:
                    with conn:
                        conn.executemany("INSERT OR IGNORE INTO hist VALUES (?,?,?,?,?,?)", rows)
                finally:
                    conn.close()
            done.add(iid)
        except Exception:
            done.add(iid)  # don't retry failures forever
        time.sleep(BOOTSTRAP_CALL_GAP_S)
    try:
        conn = _history_db()
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('bootstrapped_ids', ?)", (json.dumps(sorted(done)),))
        conn.close()
    except Exception:
        pass


def maybe_start_history_bootstrap(candidate_ids: list[int]) -> None:
    global _bootstrap_started
    if _bootstrap_started or not candidate_ids:
        return
    _bootstrap_started = True
    threading.Thread(target=_bootstrap_history, args=(candidate_ids,), daemon=True).start()


def compute_history_stats(window_days: int = HISTORY_WINDOW_DAYS, cache_s: int = 300) -> dict[int, dict]:
    """Per-item rolling stats over the history window, computed in one SQL pass.

    Returns {item_id: {mean_mid, std_mid, p10, p90, n_hours, margin_share,
    two_sided_share, vol_hour_mean, spark}}.
    """
    global _history_stats_cache
    now = time.time()
    if _history_stats_cache and now - _history_stats_cache[0] < cache_s:
        return _history_stats_cache[1]
    cutoff = int(now) - window_days * 86400
    out: dict[int, dict] = {}
    try:
        conn = _history_db()
        try:
            agg = conn.execute(
                "SELECT item_id, COUNT(*), AVG((high+low)/2.0), AVG(((high+low)/2.0)*((high+low)/2.0)),"
                " AVG(CASE WHEN (high - CAST(high*0.02 AS INTEGER) - low) > 0 THEN 1.0 ELSE 0.0 END),"
                " AVG(CASE WHEN vol_buy > 0 AND vol_sell > 0 THEN 1.0 ELSE 0.0 END),"
                " AVG(vol_buy + vol_sell), MIN(low), MAX(high)"
                " FROM hist WHERE ts >= ? GROUP BY item_id HAVING COUNT(*) >= 8",
                (cutoff,),
            ).fetchall()
            for iid, n, mean_mid, mean_sq, margin_share, two_sided, vol_mean, lo, hi in agg:
                var = max(0.0, (mean_sq or 0) - (mean_mid or 0) ** 2)
                out[int(iid)] = {
                    "n_hours": int(n),
                    "mean_mid": float(mean_mid or 0),
                    "std_mid": math.sqrt(var),
                    "margin_share": round(float(margin_share or 0), 3),
                    "two_sided_share": round(float(two_sided or 0), 3),
                    "vol_hour_mean": float(vol_mean or 0),
                    "low_7d": float(lo or 0),
                    "high_7d": float(hi or 0),
                }
        finally:
            conn.close()
    except Exception:
        return out
    _history_stats_cache = (now, out)
    return out


def fetch_history_sparks(item_ids: list[int], points: int = 36) -> dict[int, list[float]]:
    """Compact recent mid-price series per item for inline sparklines."""
    if not item_ids:
        return {}
    cutoff = int(time.time()) - HISTORY_WINDOW_DAYS * 86400
    out: dict[int, list[float]] = {}
    try:
        conn = _history_db()
        try:
            marks = ",".join("?" * len(item_ids))
            rows = conn.execute(
                f"SELECT item_id, ts, (high+low)/2.0 FROM hist WHERE ts >= ? AND item_id IN ({marks}) ORDER BY ts",
                [cutoff, *item_ids],
            ).fetchall()
        finally:
            conn.close()
        series: dict[int, list[float]] = defaultdict(list)
        for iid, _ts, mid in rows:
            series[int(iid)].append(float(mid or 0))
        for iid, vals in series.items():
            if len(vals) > points:  # downsample evenly
                step = len(vals) / points
                vals = [vals[int(i * step)] for i in range(points)]
            out[iid] = [round(v, 1) for v in vals]
    except Exception:
        pass
    return out


def load_watchlist() -> set[int]:
    try:
        raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        return {int(x) for x in (raw.get("items") or [])}
    except Exception:
        return set()


def toggle_watchlist(item_id: int) -> dict:
    items = load_watchlist()
    if item_id in items:
        items.discard(item_id)
        watched = False
    else:
        items.add(item_id)
        watched = True
    WATCHLIST_PATH.write_text(json.dumps({"items": sorted(items)}), encoding="utf-8")
    return {"ok": True, "item_id": item_id, "watched": watched, "count": len(items)}


# ---------------------------------------------------------------------------
# Flip Finder: market-wide scan with confidence scoring, risk flags and trends
# ---------------------------------------------------------------------------

def _bucket_mid(row: dict) -> float:
    """Mid price from a wiki 5m/1h/24h bucket row (avgHighPrice/avgLowPrice)."""
    ah = parse_num(row.get("avgHighPrice"))
    al = parse_num(row.get("avgLowPrice"))
    if ah and al:
        return (ah + al) / 2
    return float(ah or al or 0)


def _flip_confidence_score(min_side_vol: int, roi: float | None, trend_1h: float | None,
                           trend_24h: float | None, data_age_s: int) -> int:
    """0-100 confidence score: volume 40, stability 30, spread quality 20, freshness 10.

    Same weighting philosophy the big flip finders use (07Flip/GE Tracker style):
    high two-sided volume and calm prices make a margin trustworthy; extreme ROI
    and stale quotes make it a trap.
    """
    vol_pts = 40.0 * min(1.0, math.log10(1 + max(0, min_side_vol)) / 3.0)
    drift = 0.0
    known = 0
    if trend_1h is not None:
        drift += min(1.0, abs(trend_1h) / 6.0)
        known += 1
    if trend_24h is not None:
        drift += min(1.0, abs(trend_24h) / 15.0)
        known += 1
    stab_pts = 30.0 * (1.0 - drift / known) if known else 15.0
    if roi is None or roi <= 0:
        roi_pts = 0.0
    elif roi < 1.5:
        roi_pts = 20.0 * (roi / 1.5)
    elif roi <= 8:
        roi_pts = 20.0
    elif roi <= 15:
        roi_pts = 20.0 - (roi - 8) / 7 * 12.0
    else:
        roi_pts = 4.0
    fresh_pts = 10.0 if data_age_s <= 300 else max(0.0, 10.0 * (1 - (data_age_s - 300) / 3300))
    return int(round(min(100.0, vol_pts + stab_pts + roi_pts + fresh_pts)))


def _score_grade(score: int) -> str:
    return "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "E" if score >= 20 else "F"


def _personal_flip_stats_by_id() -> dict[int, dict]:
    """All-time per-item flip stats from the user's Copilot CSV, keyed by wiki item id."""
    try:
        csv_path, _ = find_latest_csv()
        rows = load_rows(csv_path)
        config = load_bankroll_config()
        finished = rows_for_scope(rows, (dt.datetime.min, dt.datetime.max), config, all_accounts=False, status="FINISHED")
        out: dict[int, dict] = {}
        for it in item_summary(finished):
            info = get_item_info(it.get("item"))
            iid = info.get("itemId")
            if iid:
                out[int(iid)] = {"my_flips": int(it.get("n") or 0), "my_profit": int(it.get("profit") or 0)}
        return out
    except Exception:
        return {}


def build_flip_finder() -> dict:
    """Full-market flip scan from the OSRS Wiki batch endpoints (latest/5m/1h/24h/volumes).

    Returns every two-sided-priced tradeable with post-tax margin, ROI, volumes,
    short/long trend, fill-time estimate, profit potentials, a 0-100 confidence
    score and risk flags (dump/spike/manipulation/stale/thin). Filtering, presets
    and sorting happen client-side so the UI stays instant.
    """
    latest = load_wiki_latest_prices(120)
    five = (load_wiki_5m_market(180).get("data") or {})
    hourly_raw = load_wiki_1h_market(600)
    hourly = (hourly_raw.get("data") or {})
    daily = (load_wiki_24h_market(1800).get("data") or {})
    day_volumes = load_wiki_daily_volumes()
    blocked = _current_blocked_ids()
    personal = _personal_flip_stats_by_id()
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())

    try:
        record_market_snapshot(hourly, parse_num(hourly_raw.get("timestamp")) or now_ts)
    except Exception:
        pass
    hist_stats = compute_history_stats()
    watchlist = load_watchlist()

    items: list[dict] = []
    for m in fetch_wiki_mapping():
        try:
            iid = int(m.get("id") or 0)
        except Exception:
            continue
        if not iid:
            continue
        lrow = latest.get(iid) or {}
        high = parse_num(lrow.get("high"))   # instant-buy price (you can sell here)
        low = parse_num(lrow.get("low"))     # instant-sell price (you can buy here)
        if high <= 0 or low <= 0 or high >= 2_000_000_000 or low >= 2_000_000_000:
            continue
        if high < low:  # crossed quotes happen on dead items; treat conservatively
            high, low = low, high
        key = str(iid)
        hrow = hourly.get(key) or {}
        frow = five.get(key) or {}
        drow = daily.get(key) or {}
        vol_buy_1h = parse_num(hrow.get("highPriceVolume"))   # filled at high = exit liquidity
        vol_sell_1h = parse_num(hrow.get("lowPriceVolume"))   # filled at low = entry liquidity
        vol_1h = vol_buy_1h + vol_sell_1h
        vol_5m = parse_num(frow.get("highPriceVolume")) + parse_num(frow.get("lowPriceVolume"))
        daily_volume = day_volumes.get(iid) or (parse_num(drow.get("highPriceVolume")) + parse_num(drow.get("lowPriceVolume")))
        if daily_volume <= 0 and vol_1h <= 0:
            continue

        tax = _ge_tax(high)
        margin = high - tax - low
        mid = round((high + low) / 2)
        roi = round(margin / low * 100, 2) if low else None
        limit = m.get("limit")
        try:
            limit = int(limit) if limit not in (None, "") else None
        except Exception:
            limit = None

        mid_5m = _bucket_mid(frow)
        mid_1h = _bucket_mid(hrow)
        mid_24h = _bucket_mid(drow)
        trend_1h = round((mid_5m - mid_1h) / mid_1h * 100, 2) if (mid_5m and mid_1h) else None
        trend_24h = round((mid - mid_24h) / mid_24h * 100, 2) if (mid and mid_24h) else None

        high_time = parse_num(lrow.get("highTime"))
        low_time = parse_num(lrow.get("lowTime"))
        buy_age_s = max(0, now_ts - high_time) if high_time else None
        sell_age_s = max(0, now_ts - low_time) if low_time else None
        data_age_s = max(buy_age_s or 0, sell_age_s or 0)

        min_side_vol = min(vol_buy_1h, vol_sell_1h)
        fill_hours = round(limit / min_side_vol, 2) if (limit and min_side_vol) else None
        limit_profit = int(margin * limit) if (margin > 0 and limit) else 0
        hourly_cap = min(min_side_vol, limit) if limit else min_side_vol
        potential_hourly = int(margin * hourly_cap) if margin > 0 else 0

        # Suggested competitive offers: outbid other flippers' buy offers by 1 gp
        # and undercut their sell offers by 1 gp (classic market-making quotes).
        buy_at = low + 1
        sell_at = max(buy_at + 1, high - 1)
        offer_margin = sell_at - _ge_tax(sell_at) - buy_at
        offer_roi = round(offer_margin / buy_at * 100, 2) if buy_at else None

        # History-derived stats (mean reversion + reliability), when accumulated.
        hs = hist_stats.get(iid)
        z_score = None
        volatility_pct = None
        margin_consistency = None
        fill_reliability = None
        if hs and hs.get("mean_mid"):
            if hs["std_mid"] > 0 and mid:
                z_score = round((mid - hs["mean_mid"]) / hs["std_mid"], 2)
            volatility_pct = round(hs["std_mid"] / hs["mean_mid"] * 100, 2)
            margin_consistency = hs["margin_share"]
            fill_reliability = hs["two_sided_share"]

        # Expected value per GE slot per day. Capture assumes you realistically
        # win ~15% of the thinner side's flow, capped by the 4h buy limit
        # (6 windows/day), discounted by how often the margin actually exists
        # and how often the item trades both ways (history-based when known).
        capture_day = min_side_vol * 24 * 0.15
        if limit:
            capture_day = min(capture_day, limit * 6)
        p_margin = margin_consistency if margin_consistency is not None else 0.65
        p_fill = fill_reliability if fill_reliability is not None else 0.75
        ev_day = int(offer_margin * capture_day * p_margin * p_fill) if offer_margin > 0 else 0

        score = _flip_confidence_score(min_side_vol, roi, trend_1h, trend_24h, data_age_s)
        if hs:
            stab = max(0.0, 1.0 - min(1.0, (volatility_pct or 0) / 8.0))
            score = int(round(score * 0.7 + 30 * (0.5 * p_margin + 0.3 * p_fill + 0.2 * stab)))
        sell_share = vol_sell_1h / vol_1h if vol_1h else 0.0
        sell_pressure = vol_sell_1h / max(1, vol_buy_1h)
        flags: list[str] = []
        if (trend_1h is not None and trend_1h <= -3 and sell_pressure >= 1.5) or (sell_share >= 0.9 and vol_1h >= 20):
            flags.append("dump")
        if trend_1h is not None and trend_1h >= 4:
            flags.append("spike")
        if roi is not None and roi >= 12 and daily_volume < 1000 and mid > 1000:
            flags.append("manip_risk")
        if min_side_vol == 0:
            flags.append("one_sided")
        if data_age_s > 1800:
            flags.append("stale")
        if z_score is not None:
            if z_score <= -1.5 and (trend_1h is not None and trend_1h <= -2):
                flags.append("falling_knife")   # cheap AND still falling: stay out
            elif z_score <= -1.2 and (trend_1h is None or trend_1h > -1) and margin > 0 and "dump" not in flags:
                flags.append("dip_buy")          # cheap and stabilized: mean-reversion entry
            elif z_score >= 2:
                flags.append("overheated")       # rich vs its own history: poor entry
        if hs and hs.get("vol_hour_mean", 0) > 10 and hs.get("n_hours", 0) >= 24 and vol_1h > 4 * hs["vol_hour_mean"]:
            flags.append("unusual_vol")          # volume anomaly: news/merch activity
        if "falling_knife" in flags or "overheated" in flags:
            score = max(0, score - 15)

        mine = personal.get(iid) or {}
        items.append({
            "item_id": iid,
            "name": m.get("name"),
            "slug": item_slug(str(m.get("name") or "")),
            "icon_url": f"https://static.runelite.net/cache/item/icon/{iid}.png",
            "members": bool(m.get("members")),
            "buy_limit": limit,
            "instant_buy": high,
            "instant_sell": low,
            "price": mid,
            "tax": tax,
            "margin_after_tax": margin,
            "roi_pct": roi,
            "vol_1h": vol_1h,
            "vol_buy_1h": vol_buy_1h,
            "vol_sell_1h": vol_sell_1h,
            "vol_5m": vol_5m,
            "daily_volume": daily_volume,
            "trend_1h_pct": trend_1h,
            "trend_24h_pct": trend_24h,
            "buy_age_s": buy_age_s,
            "sell_age_s": sell_age_s,
            "fill_hours": fill_hours,
            "limit_profit": limit_profit,
            "potential_hourly": potential_hourly,
            "buy_at": buy_at,
            "sell_at": sell_at,
            "offer_margin": offer_margin,
            "offer_roi": offer_roi,
            "ev_day": ev_day,
            "z_score": z_score,
            "volatility_pct": volatility_pct,
            "margin_consistency": margin_consistency,
            "fill_reliability": fill_reliability,
            "history_hours": (hs or {}).get("n_hours", 0),
            "low_7d": (hs or {}).get("low_7d"),
            "high_7d": (hs or {}).get("high_7d"),
            "score": score,
            "grade": _score_grade(score),
            "flags": flags,
            "watched": iid in watchlist,
            "blocked": iid in blocked,
            "my_flips": mine.get("my_flips", 0),
            "my_profit": mine.get("my_profit", 0),
        })

    items.sort(key=lambda x: (x["score"], x["ev_day"]), reverse=True)

    # Kick off the one-time history backfill for the items that matter most.
    try:
        # items are already sorted best-first, so the filtered order is the priority order
        bootstrap_ids = [x["item_id"] for x in items
                         if x["daily_volume"] >= 500 and x["margin_after_tax"] > 0][:BOOTSTRAP_MAX_ITEMS]
        maybe_start_history_bootstrap(bootstrap_ids)
    except Exception:
        pass

    # Watchlist alerts: actionable signals on items the user explicitly follows.
    alerts = []
    for x in items:
        if not x["watched"]:
            continue
        if "dip_buy" in x["flags"]:
            alerts.append({"item_id": x["item_id"], "name": x["name"], "kind": "dip",
                           "detail": f"{x['name']} is {abs(x['z_score']):.1f} std below its 7d average and stabilizing — buy @ {x['buy_at']:,} gp"})
        elif (x["offer_roi"] or 0) >= 2 and x["score"] >= 55 and "dump" not in x["flags"]:
            alerts.append({"item_id": x["item_id"], "name": x["name"], "kind": "margin",
                           "detail": f"{x['name']} margin {x['offer_margin']:,} gp ({x['offer_roi']}% ROI) — buy @ {x['buy_at']:,}, sell @ {x['sell_at']:,}"})

    return {
        "items": items,
        "stats": {
            "scanned": len(items),
            "profitable": sum(1 for x in items if x["margin_after_tax"] > 0),
            "grade_a": sum(1 for x in items if x["grade"] == "A"),
            "flagged": sum(1 for x in items if x["flags"]),
            "with_history": sum(1 for x in items if x["history_hours"] >= 8),
            "watched": sum(1 for x in items if x["watched"]),
            "dip_buys": sum(1 for x in items if "dip_buy" in x["flags"]),
        },
        "alerts": alerts,
        "market_speed": build_market_speed_status(),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "OSRS Wiki prices API (latest + 5m + 1h + 24h + volumes) + local 7d history",
    }


# ---------------------------------------------------------------------------
# Portfolio: manual buy/sell position tracking with live valuation
# ---------------------------------------------------------------------------

def load_portfolio() -> dict:
    try:
        raw = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("positions"), list):
            raw.setdefault("next_id", max([int(p.get("id") or 0) for p in raw["positions"]] or [0]) + 1)
            return raw
    except Exception:
        pass
    return {"positions": [], "next_id": 1}


def save_portfolio(data: dict) -> None:
    PORTFOLIO_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _position_sold_qty(pos: dict) -> int:
    return sum(int(parse_num(s.get("qty"))) for s in (pos.get("sells") or []))


def portfolio_add(payload: dict) -> dict:
    ident = str(payload.get("item") or payload.get("item_id") or "").strip()
    if not ident:
        raise ValueError("item or item_id required")
    raw_item = _find_wiki_item(ident)
    if not raw_item:
        raise ValueError(f"item not found: {ident}")
    qty = int(parse_num(payload.get("qty")))
    price = int(parse_num(payload.get("buy_price")))
    if qty <= 0:
        raise ValueError("qty must be > 0")
    if price <= 0:
        raise ValueError("buy_price must be > 0")
    item = compact_wiki_item(raw_item)
    pf = load_portfolio()
    pos = {
        "id": int(pf.get("next_id") or 1),
        "item_id": item["id"],
        "item": item["name"],
        "slug": item["slug"],
        "icon_url": f"https://static.runelite.net/cache/item/icon/{item['id']}.png",
        "qty": qty,
        "buy_price": price,
        "target_sell": int(parse_num(payload.get("target_sell"))) or None,
        "account": str(payload.get("account") or "").strip(),
        "note": str(payload.get("note") or "").strip(),
        "opened_at": dt.datetime.now().isoformat(timespec="seconds"),
        "status": "open",
        "sells": [],
    }
    pf["positions"].append(pos)
    pf["next_id"] = pos["id"] + 1
    save_portfolio(pf)
    return {"ok": True, "position": pos}


def portfolio_sell(payload: dict) -> dict:
    pid = int(parse_num(payload.get("id")))
    qty = int(parse_num(payload.get("qty")))
    price = int(parse_num(payload.get("price")))
    pf = load_portfolio()
    pos = next((p for p in pf["positions"] if int(parse_num(p.get("id"))) == pid), None)
    if not pos:
        raise ValueError(f"position {pid} not found")
    remaining = int(parse_num(pos.get("qty"))) - _position_sold_qty(pos)
    if qty <= 0 or qty > remaining:
        raise ValueError(f"sell qty must be between 1 and {remaining}")
    if price <= 0:
        raise ValueError("price must be > 0")
    pos.setdefault("sells", []).append({"qty": qty, "price": price, "at": dt.datetime.now().isoformat(timespec="seconds")})
    if _position_sold_qty(pos) >= int(parse_num(pos.get("qty"))):
        pos["status"] = "closed"
        pos["closed_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_portfolio(pf)
    return {"ok": True, "position": pos}


def portfolio_edit(payload: dict) -> dict:
    pid = int(parse_num(payload.get("id")))
    pf = load_portfolio()
    pos = next((p for p in pf["positions"] if int(parse_num(p.get("id"))) == pid), None)
    if not pos:
        raise ValueError(f"position {pid} not found")
    if "target_sell" in payload:
        pos["target_sell"] = int(parse_num(payload.get("target_sell"))) or None
    if "note" in payload:
        pos["note"] = str(payload.get("note") or "").strip()
    if "qty" in payload:
        qty = int(parse_num(payload.get("qty")))
        if qty < max(1, _position_sold_qty(pos)):
            raise ValueError("qty cannot be below already-sold quantity")
        pos["qty"] = qty
    if "buy_price" in payload:
        price = int(parse_num(payload.get("buy_price")))
        if price <= 0:
            raise ValueError("buy_price must be > 0")
        pos["buy_price"] = price
    save_portfolio(pf)
    return {"ok": True, "position": pos}


def portfolio_delete(payload: dict) -> dict:
    pid = int(parse_num(payload.get("id")))
    pf = load_portfolio()
    before = len(pf["positions"])
    pf["positions"] = [p for p in pf["positions"] if int(parse_num(p.get("id"))) != pid]
    if len(pf["positions"]) == before:
        raise ValueError(f"position {pid} not found")
    save_portfolio(pf)
    return {"ok": True, "deleted": pid}


def build_portfolio_view() -> dict:
    """Portfolio with live OSRS Wiki valuation: unrealized P/L after tax on open
    positions, realized P/L on sells, and summary KPIs."""
    pf = load_portfolio()
    latest = load_wiki_latest_prices(120)
    open_rows: list[dict] = []
    closed_rows: list[dict] = []
    invested = live_value = unrealized = realized_total = 0
    for pos in pf["positions"]:
        qty = int(parse_num(pos.get("qty")))
        buy_price = int(parse_num(pos.get("buy_price")))
        sold = _position_sold_qty(pos)
        remaining = max(0, qty - sold)
        realized = 0
        for s in pos.get("sells") or []:
            s_qty = int(parse_num(s.get("qty")))
            s_price = int(parse_num(s.get("price")))
            realized += s_qty * s_price - _ge_tax_total(s_price, s_qty) - s_qty * buy_price
        lrow = latest.get(int(parse_num(pos.get("item_id")))) or {}
        cur_sell = parse_num(lrow.get("low"))    # what you'd get selling instantly
        cur_buy = parse_num(lrow.get("high"))
        view = {
            **pos,
            "sold_qty": sold,
            "remaining_qty": remaining,
            "realized_profit": realized,
            "cur_instant_sell": cur_sell or None,
            "cur_instant_buy": cur_buy or None,
            "cost_remaining": remaining * buy_price,
        }
        realized_total += realized
        if pos.get("status") == "open" and remaining > 0:
            if cur_sell:
                gross = remaining * cur_sell
                after_tax = gross - _ge_tax_total(cur_sell, remaining)
                view["live_value_after_tax"] = after_tax
                view["unrealized_profit"] = after_tax - remaining * buy_price
                view["unrealized_roi_pct"] = round(view["unrealized_profit"] / max(1, remaining * buy_price) * 100, 2)
                live_value += after_tax
                unrealized += view["unrealized_profit"]
            target = int(parse_num(pos.get("target_sell")))
            if target > 0:
                tg = remaining * target
                view["target_profit"] = tg - _ge_tax_total(target, remaining) - remaining * buy_price
            invested += remaining * buy_price
            open_rows.append(view)
        else:
            closed_rows.append(view)
    open_rows.sort(key=lambda x: x.get("opened_at") or "", reverse=True)
    closed_rows.sort(key=lambda x: x.get("closed_at") or x.get("opened_at") or "", reverse=True)
    closed_wins = sum(1 for x in closed_rows if x["realized_profit"] > 0)
    return {
        "open": open_rows,
        "closed": closed_rows,
        "summary": {
            "open_count": len(open_rows),
            "closed_count": len(closed_rows),
            "invested": invested,
            "live_value_after_tax": live_value,
            "unrealized_profit": unrealized,
            "realized_profit": realized_total,
            "closed_win_rate": round(closed_wins / len(closed_rows) * 100, 1) if closed_rows else None,
        },
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def build_open_api_position_estimate(rows: list[dict], active_slot_items: set[int]) -> dict:
    """Estimate recent Copilot open/hold rows not currently listed for sale.

    Copilot's API CSV exposes open rows but not the exact in-plugin HOLD/SELL-LATER
    recommendation state. This marks them as API open-position estimates and values
    them against OSRS Wiki latest high price after tax.
    """
    cfg = load_bankroll_config()
    active_accounts = set(cfg.get("active_accounts") or DEFAULT_ACTIVE_ACCOUNTS)
    now = dt.datetime.now()
    cutoff = now - dt.timedelta(hours=48)
    prices = load_wiki_latest_prices()
    positions: list[dict] = []
    total = 0
    market_value = 0
    for r in sorted(rows, key=lambda x: x.get("_buy") or dt.datetime.min, reverse=True):
        # A Copilot API status of BUYING is usually an active buy offer / suggested
        # opportunity, not inventory the player actually owns. Valuing BUYING rows against
        # Wiki latest makes the dashboard show fake losses (e.g. Dexterous scroll)
        # while Copilot may show its own expected profit. Only BOUGHT rows are safe
        # enough to treat as possible held inventory here.
        if r.get("Account") not in active_accounts or r.get("Status") != "BOUGHT":
            continue
        buy_time = r.get("_buy")
        if not buy_time or buy_time < cutoff or buy_time > now + dt.timedelta(hours=2):
            continue
        iid = int(r.get("_item_id") or get_item_info(r.get("Item")).get("itemId") or 0)
        if not iid or iid in active_slot_items:
            continue
        qty = max(0, (r.get("_bought") or 0) - (r.get("_sold") or 0))
        avg_buy = r.get("_avg_buy") or 0
        if not qty or not avg_buy or avg_buy * qty < 1_000_000:
            continue
        px = prices.get(iid, {})
        # Mark unlisted inventory at the mid price: instant-buy (high) is
        # best-case, instant-sell (low) is panic-dump pricing — the mid is
        # a fair "what is this realistically worth" anchor. (Flipping Copilot
        # counts unsold inventory as 0 until it actually sells.)
        ph, pl = parse_num(px.get("high")), parse_num(px.get("low"))
        sell_price = round((ph + pl) / 2) if (ph and pl) else (ph or pl)
        if not sell_price:
            continue
        gross = sell_price * qty
        post_tax = gross - _ge_tax_total(sell_price, qty)
        profit = post_tax - (avg_buy * qty)
        info = get_item_info(iid)
        positions.append({
            "account": r.get("Account"),
            "item_id": iid,
            "item": r.get("Item") or info.get("name") or f"Item {iid}",
            "icon_url": info.get("icon"),
            "quantity": qty,
            "avg_buy": avg_buy,
            "wiki_sell_price": sell_price,
            "post_tax_value": post_tax,
            "estimated_profit": profit,
            "opened_at": iso(buy_time),
            "method": "recent_api_open_row_wiki_latest_high",
        })
        total += profit
        market_value += post_tax
    positions.sort(key=lambda x: abs(x.get("post_tax_value", 0)), reverse=True)
    return {
        "estimated_profit": total,
        "market_value": market_value,
        "count": len(positions),
        "positions": positions[:20],
        "note": "Fresh API BOUGHT rows valued with OSRS Wiki latest high after tax; BUYING rows are excluded because they are buy offers/expected-profit opportunities, not owned inventory. Copilot exact hold/sell-later memory is not exposed, so this is an estimate.",
    }



def _slot_file_parts(path: Path) -> tuple[str, str]:
    stem = path.stem
    if stem.startswith("acc_") and "_" in stem[4:]:
        account_hash, slot = stem[4:].rsplit("_", 1)
        return account_hash, slot
    return "", ""


ACCOUNT_MAP_PATH = BANKROLL_CONFIG_PATH.parent / "account_hash_map.json"
COMPLETE_SLOT_STATES = {"SOLD", "BOUGHT"}


def load_account_map() -> dict:
    try:
        return {str(k): str(v) for k, v in json.loads(ACCOUNT_MAP_PATH.read_text(encoding="utf-8")).items()}
    except Exception:
        return {}


def save_account_map(mapping: dict) -> dict:
    try:
        clean = {str(k): str(v).strip() for k, v in (mapping or {}).items() if str(v).strip()}
        ACCOUNT_MAP_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        return {"saved": True, "map": clean}
    except Exception as e:
        return {"error": str(e)}


TIMEFRAME_HISTORY_PATH = ROOT / "timeframe_history.json"
TIMEFRAME_ANCHOR_PATH = ROOT / "timeframe_anchor.json"


def load_timeframe_anchor() -> "dt.datetime | None":
    try:
        return parse_time(json.loads(TIMEFRAME_ANCHOR_PATH.read_text(encoding="utf-8")).get("ts"))
    except Exception:
        return None


def set_timeframe_anchor(when: "dt.datetime | None" = None) -> str:
    when = when or dt.datetime.now()
    ts = iso(when)
    TIMEFRAME_ANCHOR_PATH.write_text(json.dumps({"ts": ts}), encoding="utf-8")
    return ts


def _load_tf_history() -> list:
    try:
        return json.loads(TIMEFRAME_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _account_timeframes() -> dict:
    """Current Copilot timeframe (minutes) per account hash."""
    out = {}
    if COPILOT_DIR.exists():
        for f in COPILOT_DIR.glob("acc_*_prefs.json"):
            h = f.stem[4:].rsplit("_", 1)[0]
            try:
                out[h] = int(json.loads(f.read_text(encoding="utf-8")).get("timeframe") or 0)
            except Exception:
                continue
    return out


def snapshot_timeframes() -> None:
    """Append a timestamped record whenever an account's timeframe setting changes,
    so flips can later be attributed to the setting that was active at flip time."""
    hist = _load_tf_history()
    last = {}
    for e in hist:
        last[e.get("hash")] = e.get("tf")
    changed = False
    now = iso(dt.datetime.now())
    for h, tf in _account_timeframes().items():
        if tf and last.get(h) != tf:
            hist.append({"ts": now, "hash": h, "tf": tf})
            last[h] = tf
            changed = True
    if changed:
        try:
            TIMEFRAME_HISTORY_PATH.write_text(json.dumps(hist), encoding="utf-8")
        except Exception:
            pass


def build_timeframe_stats(days: int = 30, rows: list | None = None) -> dict:
    """Profit grouped by the Copilot timeframe setting active on each account at flip time."""
    snapshot_timeframes()
    name_to_hash = {v: k for k, v in load_account_map().items()}
    current = _account_timeframes()
    byhash = defaultdict(list)
    for e in _load_tf_history():
        ts = parse_time(e.get("ts"))
        if ts:
            byhash[e.get("hash")].append((ts, e.get("tf")))
    for v in byhash.values():
        v.sort()

    def tf_at(h, when):
        arr = byhash.get(h, [])
        # before the first logged snapshot, use the EARLIEST known timeframe
        # (not the current one) so old flips aren't mislabeled after a change.
        val = arr[0][1] if arr else current.get(h, 0)
        for ts, tf in arr:
            if ts <= when:
                val = tf
            else:
                break
        return val

    if rows is None:
        rows = load_rows(find_latest_csv()[0])
    # Timeframe stats start from the reset anchor (today onward), not the page range.
    cut = load_timeframe_anchor() or (dt.datetime.now() - dt.timedelta(days=days))
    agg: dict[int, dict] = {}
    for r in rows:
        if r.get("Status") != "FINISHED" or not r.get("_sell") or r["_sell"] < cut:
            continue
        h = name_to_hash.get(r.get("Account"))
        tf = tf_at(h, r["_sell"]) if h else current.get(h, 0)
        if not tf:
            continue
        a = agg.setdefault(tf, {"n": 0, "profit": 0, "wins": 0, "hsum": 0.0})
        p = r.get("_profit", 0)
        a["n"] += 1
        a["profit"] += p
        a["wins"] += 1 if p > 0 else 0
        if r.get("_dur_h") is not None:
            a["hsum"] += float(r["_dur_h"])
    out = []
    for tf in sorted(agg):
        a = agg[tf]
        out.append({
            "timeframe_min": tf,
            "flips": a["n"],
            "profit": a["profit"],
            "avg_profit": round(a["profit"] / max(1, a["n"])),
            "win_rate": round(a["wins"] / max(1, a["n"]) * 100, 1),
            "gp_per_slot_hour": round(a["profit"] / a["hsum"]) if a["hsum"] else 0,
        })
    return {"days": days, "rows": out, "current": current, "since": iso(cut), "logged_since": (_load_tf_history()[0]["ts"] if _load_tf_history() else None)}


def build_attention() -> dict:
    """Per-account 'needs collection' state from Copilot slot files, oldest first.

    A slot is 'complete' (ready to collect) when its state is SOLD/BOUGHT or the
    full quantity has transacted. Account names come from the hash->name map.
    """
    name_map = load_account_map()
    slot_files = sorted(COPILOT_DIR.glob("acc_*_*.json")) if COPILOT_DIR.exists() else []
    agg: dict[str, dict] = {}
    for path in slot_files:
        b = path.name
        if "_paused" in b or "_prefs" in b:
            continue
        account_hash, _slot = _slot_file_parts(path)
        if not account_hash:
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        state = str(d.get("state", "")).upper()
        qs = parse_num(d.get("quantitySold"))
        tq = parse_num(d.get("totalQuantity"))
        complete = state in COMPLETE_SLOT_STATES or (tq > 0 and qs >= tq)
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0
        a = agg.setdefault(account_hash, {
            "account_hash": account_hash,
            "name": name_map.get(account_hash),
            "ready_slots": 0,
            "ready_since": None,
            "items": [],
            "slot_count": 0,
        })
        a["slot_count"] += 1
        iid = int(parse_num(d.get("itemId"))) if d.get("itemId") else 0
        nm = get_item_info(iid).get("name") if iid else None
        if nm:
            a["items"].append(nm)
        if complete:
            a["ready_slots"] += 1
            if a["ready_since"] is None or mtime < a["ready_since"]:
                a["ready_since"] = mtime
    out = []
    for a in agg.values():
        a["needs_attention"] = a["ready_slots"] > 0
        a["ready_since_iso"] = iso(dt.datetime.fromtimestamp(a["ready_since"])) if a["ready_since"] else None
        a["items"] = a["items"][:8]
        out.append(a)
    out.sort(key=lambda x: (not x["needs_attention"], x["ready_since"] or 9e18, x["account_hash"]))
    return {"available": bool(slot_files), "accounts": out, "mapped": len(name_map), "generated_at": iso(dt.datetime.now())}


def build_live_unrealized_estimate(rows: list[dict] | None = None) -> dict:
    """Read-only live estimate from Copilot acc_*_[0-7].json slot files."""
    rows = rows if rows is not None else load_rows()
    slot_files = sorted(COPILOT_DIR.glob("acc_*_*.json")) if COPILOT_DIR.exists() else []
    recent_open_buy_by_item = _recent_open_buy_by_item(rows)
    wiki_prices = load_wiki_latest_prices()
    slots: list[dict] = []
    estimated_total = 0
    partial_realized_total = 0
    active_sell_value = 0
    active_buy_locked = 0
    unknown_profit_slots = 0
    selling_count = buying_count = bought_count = 0
    newest_mtime: str | None = None
    selling_slot_items: set[int] = set()
    for path in slot_files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        state = str(raw.get("state") or "").upper()
        if state in {"", "EMPTY"}:
            continue
        item_id = parse_num(raw.get("itemId"))
        info = get_item_info(item_id)
        account_hash, slot = _slot_file_parts(path)
        total_qty = parse_num(raw.get("totalQuantity"))
        sold_qty = parse_num(raw.get("quantitySold"))
        remaining_qty = max(0, total_qty - sold_qty)
        price = parse_num(raw.get("price"))
        spent = parse_num(raw.get("spent"))
        avg_buy = 0
        cost_source = None
        if state == "SELLING" and item_id in recent_open_buy_by_item:
            # In local Copilot slot JSON, SELLING+quantitySold>0 uses `spent` as
            # sold gross value (quantitySold * offer price), not original buy cost.
            # Prefer the API CSV open SELLING/BOUGHT row for cost basis.
            avg_buy = recent_open_buy_by_item[item_id].get("_avg_buy") or 0
            cost_source = "recent_api_csv_avg_buy"
        elif spent and total_qty and not (state == "SELLING" and sold_qty > 0):
            avg_buy = round(spent / max(1, total_qty))
            cost_source = "slot_spent_avg_buy"
        elif item_id in recent_open_buy_by_item:
            avg_buy = recent_open_buy_by_item[item_id].get("_avg_buy") or 0
            cost_source = "recent_api_csv_avg_buy"
        estimated_profit: int | None = None
        post_tax_value = 0
        estimate_method = "unknown_missing_buy_or_sell"
        realized_so_far: int | None = None
        # Partial fills on a SELLING offer execute at the listed ask, so the
        # sold portion's profit is already locked in: sold x (ask - tax - cost).
        if state == "SELLING" and sold_qty > 0 and avg_buy and price:
            realized_so_far = sold_qty * price - _ge_tax_total(price, sold_qty) - sold_qty * avg_buy
            partial_realized_total += realized_so_far
        if state == "SELLING" and remaining_qty and price:
            # Match Flipping Copilot's prediction: the listed ask IS Copilot's
            # suggested sell price, so the expected profit is the full remaining
            # quantity at the ask, after tax. (Items priced above the current
            # instant-buy get an informational marked_below_market note, but the
            # ask still counts — FC predicts the sale at its suggested price.)
            wiki_high = parse_num((wiki_prices.get(int(item_id)) or {}).get("high")) if item_id else 0
            gross = price * remaining_qty
            tax = _ge_tax_total(price, remaining_qty)
            post_tax_value = gross - tax
            active_sell_value += post_tax_value
            if avg_buy:
                estimated_profit = post_tax_value - (avg_buy * remaining_qty)
                estimate_method = cost_source or "known_avg_buy"
                if wiki_high and wiki_high < price:
                    estimate_method = (estimate_method or "") + "_ask_above_market"
                estimated_total += estimated_profit
            else:
                unknown_profit_slots += 1
        elif state == "BOUGHT" and total_qty and avg_buy:
            estimate_method = "bought_waiting_for_sell_offer"
            unknown_profit_slots += 1
        elif state == "BUYING":
            active_buy_locked += max(spent, price * max(1, total_qty))
            estimate_method = "buy_offer_no_unrealized_profit"
        else:
            unknown_profit_slots += 1
        if state == "SELLING":
            selling_count += 1
            if item_id:
                selling_slot_items.add(int(item_id))
        elif state == "BUYING":
            buying_count += 1
        elif state == "BOUGHT":
            bought_count += 1
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
            newest_mtime = max(newest_mtime or mtime, mtime)
        except Exception:
            mtime = None
        slots.append({
            "account_hash": account_hash,
            "slot": slot,
            "state": state,
            "item_id": item_id,
            "item": info.get("name") or f"Item {item_id}",
            "icon_url": info.get("icon"),
            "total_quantity": total_qty,
            "remaining_quantity": remaining_qty,
            "quantity_sold": sold_qty,
            "offer_price": price,
            "avg_buy": avg_buy or None,
            "cost_source": cost_source,
            "post_tax_sell_value": post_tax_value or None,
            "estimated_profit": estimated_profit,
            "realized_so_far": realized_so_far,
            "estimate_method": estimate_method,
            "copilot_price_used": bool(raw.get("copilotPriceUsed")),
            "was_copilot_suggestion": bool(raw.get("wasCopilotSuggestion")),
            "mtime": mtime,
        })
    slots.sort(key=lambda s: (s.get("estimated_profit") is None, -(s.get("estimated_profit") or 0), s.get("item") or ""))
    open_api_positions = build_open_api_position_estimate(rows, selling_slot_items)
    # Headline matches Flipping Copilot: only active sell offers count, at their
    # listed (Copilot-suggested) prices. Unlisted BOUGHT inventory is reported
    # separately in open_api_positions but contributes 0 here, like FC.
    total_unrealized = estimated_total
    return {
        "available": bool(slot_files),
        "read_only": True,
        "slot_count": len(slots),
        "selling_count": selling_count,
        "buying_count": buying_count,
        "bought_count": bought_count,
        "estimated_unrealized_profit": estimated_total,
        "partial_realized_profit": partial_realized_total,
        "open_api_positions": open_api_positions,
        "total_unrealized_profit": total_unrealized,
        "active_sell_value": active_sell_value,
        "active_buy_locked": active_buy_locked,
        "unknown_profit_slots": unknown_profit_slots,
        "source_dir": str(COPILOT_DIR),
        "latest_slot_mtime": newest_mtime,
        "slots": slots[:80],
    }


def build_stats_page(days: int = 0, rows: list | None = None) -> dict:
    """Analytics for the Stats tab. Everything except the timeframe table uses the
    selected window (default 0 = all-time); the timeframe table uses its own anchor."""
    if rows is None:
        rows = load_rows(find_latest_csv()[0])
    cut = (dt.datetime.now() - dt.timedelta(days=days)) if (days and days > 0) else dt.datetime.min
    fin = [r for r in rows if r.get("Status") == "FINISHED" and r.get("_sell") and r["_sell"] >= cut]
    byhour = {h: {"flips": 0, "profit": 0, "wins": 0} for h in range(24)}
    bydow = {d: {"flips": 0, "profit": 0, "wins": 0} for d in range(7)}
    tiers = [("<100k", 0, 100_000), ("100k-1M", 100_000, 1_000_000), ("1M-10M", 1e6, 1e7), ("10M-100M", 1e7, 1e8), ("100M+", 1e8, 1e18)]
    bytier = {lbl: {"flips": 0, "profit": 0, "wins": 0} for lbl, _, _ in tiers}
    byitem = defaultdict(lambda: {"flips": 0, "profit": 0, "wins": 0, "hsum": 0.0})
    byacct = defaultdict(lambda: {"flips": 0, "profit": 0, "wins": 0})
    byday = defaultdict(float)
    total_profit = total_flips = total_wins = 0
    tax_paid = 0
    turnover = 0
    biggest = None
    for r in fin:
        p = r.get("_profit", 0); s = r["_sell"]
        total_profit += p; total_flips += 1; total_wins += 1 if p > 0 else 0
        tax_paid += r.get("_tax", 0) or 0
        turnover += (r.get("_avg_buy", 0) or 0) * (r.get("_bought", 0) or 0)
        for bucket, key in ((byhour, s.hour), (bydow, s.weekday())):
            b = bucket[key]; b["flips"] += 1; b["profit"] += p; b["wins"] += 1 if p > 0 else 0
        it = byitem[r.get("Item", "?")]; it["flips"] += 1; it["profit"] += p; it["wins"] += 1 if p > 0 else 0
        if r.get("_dur_h") is not None:
            it["hsum"] += float(r["_dur_h"])
        ac = byacct[r.get("Account", "?")]; ac["flips"] += 1; ac["profit"] += p; ac["wins"] += 1 if p > 0 else 0
        price = r.get("_avg_buy", 0) or 0
        for lbl, lo, hi in tiers:
            if lo <= price < hi:
                t = bytier[lbl]; t["flips"] += 1; t["profit"] += p; t["wins"] += 1 if p > 0 else 0
                break
        byday[s.date().isoformat()] += p
        if biggest is None or p > biggest["profit"]:
            biggest = {"item": r.get("Item"), "profit": p, "when": iso(s)}

    def pack(d):
        return {"flips": d["flips"], "profit": d["profit"],
                "avg": round(d["profit"] / d["flips"]) if d["flips"] else 0,
                "win_rate": round(d["wins"] / d["flips"] * 100, 1) if d["flips"] else 0}

    hours = [{"hour": h, **pack(byhour[h])} for h in range(24)]
    dows = [{"dow": d, **pack(bydow[d])} for d in range(7)]
    by_tier = [{"label": lbl, **pack(bytier[lbl])} for lbl, _, _ in tiers if bytier[lbl]["flips"]]
    items_sorted = sorted(byitem.items(), key=lambda kv: kv[1]["profit"], reverse=True)
    top_items = [{"item": n, "slug": item_slug(n), **pack(d)} for n, d in items_sorted[:10]]
    worst_items = [{"item": n, "slug": item_slug(n), **pack(d)} for n, d in sorted(byitem.items(), key=lambda kv: kv[1]["profit"]) if d["profit"] < 0][:10]
    by_account = [{"account": a, **pack(d)} for a, d in sorted(byacct.items(), key=lambda kv: kv[1]["profit"], reverse=True)]
    # Slot efficiency: gp earned per hour a slot is tied up, per item (the key
    # tight-list curation metric). Require enough flips/hours to be meaningful.
    def pack_eff(n, d):
        return {"item": n, "slug": item_slug(n), "flips": d["flips"], "profit": d["profit"],
                "gp_slot_hr": round(d["profit"] / d["hsum"]) if d["hsum"] else 0}
    eff_pool = [(n, d) for n, d in byitem.items() if d["flips"] >= 5 and d["hsum"] >= 1.0]
    eff_sorted = sorted(eff_pool, key=lambda kv: kv[1]["profit"] / kv[1]["hsum"], reverse=True)
    fast_items = [pack_eff(n, d) for n, d in eff_sorted[:10]]
    slow_items = [pack_eff(n, d) for n, d in eff_sorted[::-1][:10]]
    days_sorted = sorted(byday.keys())
    cum = 0
    cumulative = []
    for d in days_sorted:
        cum += byday[d]
        cumulative.append({"date": d, "cum": round(cum), "day": round(byday[d])})
    best_day = max(byday.items(), key=lambda kv: kv[1]) if byday else None
    active_hours = [x for x in hours if x["flips"]]
    active_dows = [x for x in dows if x["flips"]]
    tf = build_timeframe_stats(days, rows)
    return {
        "days": days,
        "totals": {"flips": total_flips, "profit": total_profit,
                   "avg": round(total_profit / total_flips) if total_flips else 0,
                   "win_rate": round(total_wins / max(1, total_flips) * 100, 1),
                   "tax_paid": tax_paid, "turnover": turnover,
                   "biggest": biggest,
                   "best_hour": max(active_hours, key=lambda x: x["profit"]) if active_hours else None,
                   "best_dow": max(active_dows, key=lambda x: x["profit"]) if active_dows else None,
                   "best_day": {"date": best_day[0], "profit": round(best_day[1])} if best_day else None},
        "by_hour": hours, "by_dow": dows, "by_tier": by_tier,
        "top_items": top_items, "worst_items": worst_items, "by_account": by_account,
        "fast_items": fast_items, "slow_items": slow_items,
        "cumulative": cumulative,
        "by_timeframe": tf["rows"], "timeframe_current": tf["current"], "timeframe_since": tf.get("since"), "timeframe_logged_since": tf["logged_since"],
    }


def get_summary(start_param: str | None = None, end_param: str | None = None, session_start: str | None = None) -> dict:
    csv_path, sources = find_latest_csv()
    csv_data = load_csv_metrics(csv_path)
    rows = csv_data["rows"]
    snapshot_timeframes()   # keep the timeframe log current whenever the dashboard loads
    config = load_bankroll_config()
    bounds = period_bounds()
    periods = {name: compute_period_stats(rows, b, config, all_accounts=False) for name, b in bounds.items()}
    apply_partial_selling_to_periods(periods)
    cb = custom_bounds(start_param, end_param)
    custom_period = compute_period_stats(rows, cb, config, all_accounts=False) if cb else {}
    all_items = periods.get("all_time", {}).get("all_items", [])
    freshness = csv_freshness(csv_path, rows)
    # Slim legacy payload: the frontend uses active_periods for full range-aware item tables.
    # Keep periods as KPI-only compatibility data so /api/summary does not ship full item arrays twice.
    slim_periods = {}
    for name, pdata in periods.items():
        slim_periods[name] = {k: v for k, v in pdata.items() if k not in {"all_items", "top_items", "worst_items", "problem_items"}}
    summary = {
        "version": VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "requested_period": "today",
        "csv": {
            "available": bool(csv_path),
            "path": str(csv_path) if csv_path else None,
            "row_count": len(rows),
            "latest_flip_time": freshness.get("latest_flip_time"),
            "max_sell_time": freshness.get("max_sell_time"),
            "max_buy_time": freshness.get("max_buy_time"),
            "csv_mtime": freshness.get("csv_mtime"),
            "csv_age_hours": freshness.get("csv_age_hours"),
        },
        "data_sources": {"selected_path": str(csv_path) if csv_path else None, "candidates": sources, "freshness": freshness},
        "overview": {"csv_mtime": freshness.get("csv_mtime"), "latest_flip_time": freshness.get("latest_flip_time")},
        "periods": slim_periods,
        "active_periods": periods,
        "custom_period": custom_period,
        "active_custom_period": custom_period,
        "item_intelligence": {
            "top_performers": all_items[:20],
            "worst_performers": sorted(all_items, key=lambda x: x.get("profit", 0))[:20],
            "problem_items": problem_items_from(all_items),
        },
        "bankroll_plan": compute_bankroll_plan(csv_data, config),
        "live_unrealized_estimate": build_live_unrealized_estimate(rows),
        "live_rate": compute_live_rate(rows, config, session_start),
        "bankroll_management": {},
        "bankroll_inventory": {},
        "sessions": {},
        "analysis_context": {},
        "learning_status": {},
        "experiment": {},
        "optimization": {},
        "recommendations": [],
        "comparisons": build_comparisons(periods),
        "market_speed": build_market_speed_status(),
    }
    return summary


def get_period_for_item(period_name: str, start_param: str | None, end_param: str | None) -> tuple[str, tuple[dt.datetime, dt.datetime] | None]:
    if period_name == "custom":
        return "custom", custom_bounds(start_param, end_param)
    bounds = period_bounds()
    return period_name, bounds.get(period_name, bounds["all_time"])


def get_item_detail(item_name: str, period_name: str = "all_time", start_param: str | None = None, end_param: str | None = None, use_all_accounts: bool = False) -> dict:
    csv_path, _ = find_latest_csv()
    rows = load_rows(csv_path)
    config = load_bankroll_config()
    period, bounds = get_period_for_item(period_name, start_param, end_param)
    needle = item_name.lower().strip()
    scoped = rows_for_scope(rows, bounds, config, all_accounts=use_all_accounts, status="FINISHED")
    finished_rows = [r for r in scoped if r.get("Item", "").lower().strip() == needle]
    accounts_label = "all accounts" if use_all_accounts else "active accounts"
    if not finished_rows:
        return {"error": f"No finished flips found for item: {item_name}", "item": item_name, "period": period, "active_accounts": config.get("active_accounts", DEFAULT_ACTIVE_ACCOUNTS), "scope_label": f"{period} · {accounts_label}"}
    n = len(finished_rows)
    profits = [r.get("_profit", 0) for r in finished_rows]
    wins = sum(1 for p in profits if p > 0)
    durs = [r.get("_dur_h") for r in finished_rows if r.get("_dur_h") is not None]
    buy_prices = [r.get("_avg_buy", 0) for r in finished_rows if r.get("_avg_buy")]
    sell_prices = [r.get("_avg_sell", 0) for r in finished_rows if r.get("_avg_sell")]
    by_account: dict[str, dict] = defaultdict(lambda: {"n": 0, "profit": 0, "wins": 0})
    hour_buckets: dict[int, int] = defaultdict(int)
    icon_url = next((r.get("icon_url") for r in finished_rows if r.get("icon_url")), None)
    flips = []
    for r in finished_rows:
        p = r.get("_profit", 0)
        acc = r.get("Account") or "?"
        by_account[acc]["n"] += 1
        by_account[acc]["profit"] += p
        by_account[acc]["wins"] += int(p > 0)
        sell_dt = r.get("_sell")
        if isinstance(sell_dt, dt.datetime):
            hour_buckets[sell_dt.hour] += 1
        flips.append({
            "account": acc,
            "bought": r.get("Bought"),
            "sold": r.get("Sold"),
            "avg_buy": r.get("_avg_buy"),
            "avg_sell": r.get("_avg_sell"),
            "tax": r.get("_tax"),
            "profit": p,
            "profit_ea": r.get("_profit_ea"),
            "first_buy": iso(r.get("_buy")),
            "last_sell": iso(r.get("_sell")),
            "dur_h": r.get("_dur_h"),
            "status": r.get("Status"),
        })
    flips.sort(key=lambda x: x.get("last_sell") or "", reverse=True)
    best_hour = max(hour_buckets, key=hour_buckets.get) if hour_buckets else None
    return {
        "period": period,
        "active_accounts": config.get("active_accounts", DEFAULT_ACTIVE_ACCOUNTS),
        "scope_label": f"{period} · {accounts_label}",
        "item": item_name,
        "icon_url": icon_url,
        "n": n,
        "profit": sum(profits),
        "win_rate": round(wins / max(1, n) * 100, 1),
        "avg_profit": round(sum(profits) / n),
        "median_profit": median(profits) if profits else 0,
        "wins": wins,
        "losses": n - wins,
        "avg_buy": round(sum(buy_prices) / len(buy_prices)) if buy_prices else None,
        "avg_sell": round(sum(sell_prices) / len(sell_prices)) if sell_prices else None,
        "med_dur_h": round(median(durs), 1) if durs else None,
        "avg_dur_h": round(sum(durs) / len(durs), 1) if durs else None,
        "best_hour": best_hour,
        "hour_distribution": dict(sorted(hour_buckets.items())),
        "by_account": {acc: {"n": v["n"], "profit": v["profit"], "win_rate": round(v["wins"] / max(1, v["n"]) * 100, 1)} for acc, v in by_account.items()},
        "flips": flips,
    }


def get_blocklist_candidates() -> list[dict]:
    summary = get_summary()
    return summary.get("active_periods", {}).get("last_30_days", {}).get("problem_items", [])


def run_copilot_csv_export(dry_run: bool = False) -> dict:
    """Run an optional UI-assisted Copilot export script (configured per machine)."""
    configured = load_local_config().get("copilot_ui_export_script")
    if not configured:
        return {"ok": False, "error": "No export script configured. Export flips.csv manually from the Flipping Copilot plugin (or set copilot_ui_export_script in local_config.json)."}
    script = Path(str(configured))
    if not script.exists():
        return {"ok": False, "error": f"export script not found: {script}"}
    try:
        cmd = [sys.executable, str(script), "--json"]
        if dry_run:
            cmd.insert(2, "--dry-run")
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=90,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    parsed = None
    for ln in reversed(lines):
        try:
            parsed = json.loads(ln)
            break
        except Exception:
            continue
    if parsed is None:
        parsed = {"ok": False, "error": "export script did not return JSON"}
    parsed["exit_code"] = proc.returncode
    parsed["stdout_tail"] = "\n".join(lines[-4:])
    if proc.stderr:
        parsed["stderr_tail"] = "\n".join(proc.stderr.splitlines()[-4:])
    return parsed


def run_copilot_api_csv_export(test: bool = False) -> dict:
    """Run the read-only Copilot API CSV exporter (no UI, no mouse).

    Uses /client-flips-delta protobuf API to fetch flip history and write
    Documents\\flips.csv matching Copilot's manual export format.
    """
    configured = load_local_config().get("copilot_api_export_script")
    script = Path(str(configured)) if configured else ROOT / "scripts" / "flipping_copilot_api_export.py"
    if not script.exists():
        return {"ok": False, "error": f"API export script not found: {script}"}
    try:
        cmd = [sys.executable, str(script), "--out", str(FLIPS_CSV_PATH)]
        if test:
            cmd.append("--test")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # Collect all stdout lines and look for JSON:
    # - First try: single-line JSON anywhere in output (compact output)
    # - Fallback: reconstruct multi-line JSON from all non-empty lines and parse once
    all_lines = (proc.stdout or "").splitlines()
    parsed = None
    # Try each line individually first (compact mode)
    for ln in all_lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            parsed = json.loads(ln)
            break
        except Exception:
            continue
    # Multi-line JSON (indent=2 mode): reconstruct from all non-blank lines
    if parsed is None:
        reconstruct = "\n".join(ln for ln in all_lines if ln.strip())
        try:
            parsed = json.loads(reconstruct)
        except Exception:
            parsed = None
    if parsed is None:
        parsed = {"ok": False, "error": "API export script did not return JSON"}
    elif proc.returncode != 0:
        parsed["ok"] = False
        parsed.setdefault("error", f"API export script exited with code {proc.returncode}")
    else:
        parsed["ok"] = bool(parsed.get("ok", True))
    parsed["exit_code"] = proc.returncode
    parsed["stdout_tail"] = "\n".join(all_lines[-6:])
    if proc.stderr:
        parsed["stderr_tail"] = "\n".join(proc.stderr.splitlines()[-6:])
    return parsed


def build_analysis_context(summary: dict, period_name: str = "today") -> dict:
    period = summary.get("active_periods", {}).get(period_name) or summary.get("periods", {}).get(period_name) or {}
    return {"period": period_name, "summary": period, "bankroll": summary.get("bankroll_plan", {})}


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, data: Any, status: int = 200):
        payload = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/api/health":
            self.send_json({"status": "ok"})
        elif path == "/api/bankroll-config":
            self.send_json(load_bankroll_config())
        elif path == "/api/blocklist":
            self.send_json(get_blocklist())
        elif path == "/api/profiles":
            self.send_json(list_copilot_profiles())
        elif path == "/api/summary":
            summary = get_summary(params.get("start", [None])[0], params.get("end", [None])[0], params.get("session_start", [None])[0])
            summary["requested_period"] = params.get("period", ["today"])[0]
            self.send_json(summary)
        elif path == "/api/research":
            self.send_json(build_item_research())
        elif path == "/api/flip-finder":
            self.send_json(build_flip_finder())
        elif path == "/api/flip-finder/sparks":
            raw_ids = params.get("ids", [""])[0]
            ids = [int(parse_num(x)) for x in raw_ids.split(",") if parse_num(x)][:400]
            self.send_json({"sparks": {str(k): v for k, v in fetch_history_sparks(ids).items()}})
        elif path == "/api/portfolio":
            self.send_json(build_portfolio_view())
        elif path == "/api/stats":
            dval = params.get("days", ["all"])[0]
            days = 0 if str(dval).lower() == "all" else (int(parse_num(dval)) or 0)
            self.send_json(build_stats_page(days))
        elif path == "/api/attention":
            self.send_json(build_attention())
        elif path == "/api/attention/next":
            att = build_attention()
            nxt = next((a for a in att.get("accounts", []) if a.get("needs_attention") and a.get("name")), None)
            self.send_json({"next": (nxt or {}).get("name", ""), "since": (nxt or {}).get("ready_since_iso"), "ready_slots": (nxt or {}).get("ready_slots", 0)})
        elif path == "/api/attention/queue":
            att = build_attention()
            q = [a["name"] for a in att.get("accounts", []) if a.get("needs_attention") and a.get("name")]
            self.send_json({"queue": q})
        elif path == "/api/wiki/items":
            self.send_json({"items": search_wiki_items(params.get("q", [""])[0], limit=parse_num(params.get("limit", [20])[0]) or 20)})
        elif path.startswith("/api/wiki/item/"):
            item_id = unquote(path[len("/api/wiki/item/"):].strip())
            self.send_json(fetch_wiki_item_detail(item_id, timestep=params.get("timestep", ["1h"])[0], chart_days=parse_num(params.get("chart_days", [7])[0]) or 7))
        elif path.startswith("/api/item/"):
            # path == "/api/item/" prefix route; supports period= and all_accounts=1
            item_name = unquote(path[10:].strip())
            if not item_name:
                self.send_json({"error": "item name required"}, status=400)
            else:
                self.send_json(get_item_detail(
                    item_name,
                    period_name=params.get("period", ["all_time"])[0],
                    start_param=params.get("start", [None])[0],
                    end_param=params.get("end", [None])[0],
                    use_all_accounts=params.get("all_accounts", ["0"])[0] == "1",
                ))
        elif path == "/api/export/analysis-context":
            period_name = params.get("period", ["today"])[0]
            self.send_json(build_analysis_context(get_summary(), period_name))
        elif path.startswith("/item/"):
            # item-page route: serve SPA shell; frontend fetches /api/wiki/item/{slug}
            html = ROOT / "index.html"
            if not html.exists():
                self.send_error(404, "index.html not found")
                return
            content = html.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif path in ("/", "/index.html"):
            html = ROOT / "index.html"
            if not html.exists():
                self.send_error(404, "index.html not found")
                return

            content = html.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/api/copilot/export-csv":
            dry_run = params.get("dry_run", ["0"])[0] == "1"
            result = run_copilot_csv_export(dry_run=dry_run)
            self.send_json(result, status=200 if result.get("ok") else 500)
            return
        if path == "/api/copilot/export-csv-api":
            result = run_copilot_api_csv_export(test=params.get("test", ["0"])[0] == "1")
            self.send_json(result, status=200 if result.get("ok") else 500)
            return
        if path == "/api/watchlist/toggle":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                item_id = int(parse_num(incoming.get("item_id")))
                if not item_id:
                    raise ValueError("item_id required")
                result = toggle_watchlist(item_id)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result)
            return
        if path.startswith("/api/portfolio/"):
            action = path[len("/api/portfolio/"):].strip("/")
            handlers = {"add": portfolio_add, "sell": portfolio_sell, "edit": portfolio_edit, "delete": portfolio_delete}
            if action not in handlers:
                self.send_json({"error": f"unknown portfolio action: {action}"}, status=404)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = handlers[action](incoming)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result)
            return
        if path == "/api/bankroll-transfer":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                updated = apply_bankroll_transfer(load_bankroll_config(), incoming.get("from"), incoming.get("to"), incoming.get("amount"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            result = save_bankroll_config(updated)
            if "error" not in result:
                append_bankroll_ledger({"type": "transfer", "from": str(incoming.get("from") or ""), "to": str(incoming.get("to") or ""), "amount": int(parse_num(incoming.get("amount")))})
            self.send_json(result, status=500 if "error" in result else 200)
            return
        if path == "/api/bankroll-adjust":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                kind = str(incoming.get("kind") or "deposit").strip().lower()
                if kind not in ("deposit", "withdraw"):
                    raise ValueError("kind must be 'deposit' or 'withdraw'")
                updated = apply_balance_adjustment(load_bankroll_config(), incoming.get("account"), incoming.get("amount"), kind)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            result = save_bankroll_config(updated)
            if "error" not in result:
                append_bankroll_ledger({"type": kind, "account": str(incoming.get("account") or ""), "amount": int(parse_num(incoming.get("amount")))})
            self.send_json(result, status=500 if "error" in result else 200)
            return
        if path == "/api/session/reset":
            when = save_session_anchor()
            self.send_json({"ok": True, "anchor": iso(when)})
            return
        if path == "/api/account-map":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                current = load_account_map()
                if isinstance(incoming.get("map"), dict):
                    current.update({str(k): str(v) for k, v in incoming["map"].items()})
                elif incoming.get("account_hash"):
                    current[str(incoming["account_hash"])] = str(incoming.get("name") or "")
                else:
                    raise ValueError("provide 'map' dict or 'account_hash'+'name'")
                result = save_account_map(current)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result, status=500 if "error" in result else 200)
            return
        if path == "/api/profiles/active":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                name = str(incoming.get("name") or "").strip()
                if not name:
                    raise ValueError("name required")
                set_active_profile_name(name)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(list_copilot_profiles())
            return
        if path == "/api/profiles/create-suggested":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                result = create_suggested_profile(
                    incoming.get("name"),
                    int(parse_num(incoming.get("min_daily_volume")) or 100),
                    int(parse_num(incoming.get("min_price")) or 50_000),
                    int(parse_num(incoming.get("max_price")) or 2_100_000_000),
                )
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result, status=500 if result.get("error") else 200)
            return
        if path == "/api/blocklist/update":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                result = update_blocklist(incoming.get("action"), incoming.get("item"), incoming.get("item_id"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result)
            return
        if path == "/api/bond-purchase":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                updated = apply_bond_purchase(load_bankroll_config(), incoming.get("account"), incoming.get("amount"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            result = save_bankroll_config(updated)
            if "error" not in result:
                append_bankroll_ledger({"type": "bond", "account": str(incoming.get("account") or ""), "amount": int(parse_num(incoming.get("amount")))})
            self.send_json(result, status=500 if "error" in result else 200)
            return
        if path != "/api/bankroll-config":
            self.send_error(404, "Not Found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            incoming = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self.send_json({"error": "Invalid JSON"}, status=400)
            return
        result = save_bankroll_config(incoming)
        self.send_json(result, status=500 if "error" in result else 200)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    httpd = http.server.ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"OSRS Flipping Copilot Dashboard running at http://{host}:{port}")
    print("Routes:")
    print("  GET  /                        -> Dashboard UI")
    print(f"  GET  /api/summary             -> JSON data ({VERSION})")
    print("  GET  /api/health              -> Health check")
    print("  GET  /api/bankroll-config     -> Bankroll config (GET)")
    print("  POST /api/bankroll-config     -> Bankroll config (POST)")
    print("  POST /api/bankroll-transfer   -> Accounting bankroll transfer")
    print("  POST /api/bond-purchase       -> Bond/membership bankroll deduction")
    print("  POST /api/copilot/export-csv  -> UI-assisted Copilot CSV export")
    print("  POST /api/copilot/export-csv-api -> Read-only Copilot API CSV export")
    print("  GET  /api/item/{item}         -> Range-aware item detail")
    print("  GET  /api/research            -> Item research DB (flips + live market + opportunities)")
    print("  GET  /api/export/blocklist-candidates -> Blocklist JSON")

    print("\nPress Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        httpd.shutdown()


def main():
    parser = argparse.ArgumentParser(description="OSRS Flipping Copilot Dashboard")
    parser.add_argument("--once", action="store_true", help="Print JSON summary and exit")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()
    if args.once:
        print(json.dumps(get_summary(args.start, args.end), indent=2, default=str))
    else:
        run_server(args.host, args.port)


if __name__ == "__main__":
    main()

