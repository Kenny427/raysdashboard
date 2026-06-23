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
import os
import sqlite3
import threading
import time
import re
import subprocess
import sys
import urllib.request
import urllib.error
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

# --- Market Insight (updates/rumors + AI synthesis) ---
MARKET_INSIGHT_CACHE_PATH = ROOT / "market_insight_cache.json"
UPDATE_SEEN_PATH = ROOT / "update_alert_seen.json"   # last OSRS update the user dismissed
INSIGHT_SEEN_PATH = ROOT / "insight_alert_seen.json"  # dismissed insight heads-up keys (updates + rumours)
INSIGHT_RUNLOG_PATH = ROOT / "insight_run_log.json"   # per-AI-run log: when, why, tokens, est. cost
MARKET_INSIGHT_TTL_S = 3600           # (retained for compatibility; no longer drives staleness rebuilds)
INSIGHT_AUTO_GAP_S = 2700             # min gap between AUTOMATIC AI rebuilds (45m) — rate-limits a flurry of events
MARKET_AI_MODEL = "claude-opus-4-8"   # set MARKET_AI_MODEL env to override
OSRS_NEWS_RSS = "https://secure.runescape.com/m=news/latest_news.rss?oldschool=true"
REDDIT_SUBS = ["2007scape", "OSRSflipping"]   # subreddits scanned for market chatter
# Colloquial → canonical item name, so article/reddit mentions resolve to real items
INSIGHT_ALIASES = {
    "tbow": "Twisted bow", "twisted bow": "Twisted bow", "scythe": "Scythe of vitur (uncharged)",
    "shadow": "Tumeken's shadow (uncharged)", "tumeken": "Tumeken's shadow (uncharged)",
    "bowfa": "Bow of faerdhinen (inactive)", "fang": "Osmumten's fang",
    "sang": "Sanguinesti staff (uncharged)", "zcb": "Zaryte crossbow", "tassets": "Bandos tassets",
    "inquis": "Inquisitor's mace", "inquisitor": "Inquisitor's mace", "oathplate": "Oathplate helm",
    "voidwaker": "Voidwaker", "masori": "Masori body",
}
SENT_BEARISH = ("nerf", "crash", "crashing", "dump", "dumping", "tank", "tanking", "falling",
                "removed", "deleted", "worse", "useless", "dead content", "obsolete", "replace")
SENT_BULLISH = ("buff", "bis", "best in slot", "spike", "spiking", "mooning", "rising", "soar",
                "new best", "rally", "pump", "demand", "must have", "meta")
SENT_VOLATILE = ("proposal", "debate", "uncertain", "rumor", "rumour", "speculation", "poll",
                 "rework", "controversial", "leak", "datamine", "unsure", "might", "could")
X_HANDLES = ["JagexAsh", "OldSchoolRS"]              # dev / official OSRS accounts
NITTER_HOSTS = ["nitter.net", "nitter.poast.org", "nitter.privacyredirect.com"]
# A pool item triggers a "swinging hard" temp-block alert past any of these
SWING_ALERT_24H = 15.0                 # % move in 24h
SWING_ALERT_7D = 50.0                  # % move over 7d
SWING_ALERT_VOL = 30.0                 # % multi-day volatility
INSIGHT_USER_AGENT = "osrs-market-dashboard/1.0 (personal self-hosted flip dashboard)"
INSIGHT_KEYWORDS = (
    "update", "nerf", "buff", "bis", "best in slot", "raids", "toa", "tombs",
    "drop rate", "droprate", "price", "crash", "spike", "dump", "meta", "rework",
    "poll", "release", "removed", "alch", "nightmare", "inferno", "rumor", "rumour",
    "leak", "datamine", "announce", "shadow", "scythe", "tbow", "twisted",
)
# Heavy, market-moving phrases. A rumour only auto-triggers the AI if it BOTH names a
# real tradeable item AND carries one of these (the "Shadow rework done but unannounced" tier).
RUMOUR_KEYWORDS = (
    "rework", "reworked", "confirmed", "confirms", "nerf", "nerfed", "buff", "buffed",
    "leak", "leaked", "datamine", "datamined", "removed", "removal", "deleted",
    "announced", "announcement", "bis", "best in slot", "meta shift", "rebalance",
    "poll passed", "drop rate", "released", "release date", "discontinued",
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
VERSION = "v8-clean-items"
# Accounts are configured in bankroll_config.json (gitignored). When empty, the
# dashboard automatically uses every account found in the Copilot CSV.
DEFAULT_ACTIVE_ACCOUNTS: list[str] = []
DEFAULT_BLOCKLIST_PROFILE = "Dashboard blocklist"
DEFAULT_MONTHLY_GOAL = 1_000_000_000
DEFAULT_GOAL_DAYS = 31
TREND_MIN_DAYS = 30  # growth chart always shows at least this many days, even after a baseline reset

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


def insight_llm_config() -> dict:
    """Market Insight LLM provider/model/key — from local_config.json, with an
    ANTHROPIC_API_KEY env fallback. Key stays server-side."""
    cfg = load_local_config()
    provider = str(cfg.get("insight_llm_provider") or "").strip().lower()
    key = str(cfg.get("insight_llm_key") or "").strip()
    if not (provider and key) and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        provider, key = "anthropic", os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    return {"provider": provider, "model": str(cfg.get("insight_llm_model") or "").strip(), "key": key}


def save_insight_llm_config(provider: str | None, model: str | None, key: str | None, clear_key: bool = False) -> dict:
    """Persist Market Insight LLM settings to local_config.json (gitignored). Reads
    the file FRESH (not the cache) so an existing key is never dropped, and only
    removes the key on an explicit clear_key=True."""
    global _local_config_cache
    try:
        cfg = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception:
        cfg = dict(load_local_config())
    if provider is not None:
        cfg["insight_llm_provider"] = str(provider or "").strip().lower()
    if model is not None:
        cfg["insight_llm_model"] = str(model or "").strip()
    if clear_key:
        cfg.pop("insight_llm_key", None)
    elif key:                                  # only overwrite when a NEW key is supplied
        cfg["insight_llm_key"] = str(key).strip()
    # otherwise (key blank, clear_key False) the existing key is preserved untouched
    try:
        LOCAL_CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        _local_config_cache = cfg
    except Exception as e:
        return {"error": str(e)}
    return {"saved": True, **insight_llm_public()}


def insight_llm_public() -> dict:
    """Non-secret view for the UI: provider, model, and whether a key is set."""
    c = insight_llm_config()
    return {"provider": c["provider"], "model": c["model"], "key_set": bool(c["key"])}


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
                "members": bool(it.get("members")),
                "limit": it.get("limit"),
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
        "rank_goal": 0,
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
            "rank_goal": max(0, int(parse_num(data.get("rank_goal")))),
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
        "rank_goal": max(0, int(parse_num(config.get("rank_goal")))) if config.get("rank_goal") is not None else max(0, int(parse_num(load_bankroll_config().get("rank_goal")))),
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


def copilot_profile_blocked_sets() -> dict:
    """Blocked item-id list for every Copilot profile, for cross-profile compare
    (e.g. 'allowed here but blocked in my main list')."""
    active = active_profile_name()
    out = []
    if COPILOT_DIR.exists():
        for p in sorted(COPILOT_DIR.glob("*.profile.json")):
            name = p.name[:-len(".profile.json")]
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                ids = sorted({int(x) for x in raw.get("blockedItemIds", []) if str(x).lstrip("-").isdigit() and int(x) > 0})
            except Exception:
                ids = []
            out.append({"name": name, "active": name == active, "blocked_ids": ids})
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
    try:
        reconcile_temp_blocks()  # release expired temp blocks before reading
    except Exception:
        pass
    review = build_blocklist_review(save=True)
    review["temp_blocks"] = active_temp_blocks()
    return review


def active_temp_blocks() -> list[dict]:
    """Currently-active temporary blocks, soonest-to-expire first, with remaining seconds."""
    temp = load_temp_blocks()
    now = dt.datetime.now()
    out = []
    for rec in temp.values():
        until = parse_time(rec.get("until"))
        if not until:
            continue
        info = get_item_info(rec.get("item_id"))
        out.append({
            "item_id": rec.get("item_id"),
            "name": rec.get("name") or info.get("name"),
            "icon_url": info.get("icon"),
            "until": rec.get("until"),
            "created": rec.get("created"),
            "minutes": rec.get("minutes"),
            "was_blocked": bool(rec.get("was_blocked")),
            "remaining_seconds": max(0, int((until - now).total_seconds())),
        })
    out.sort(key=lambda x: x["remaining_seconds"])
    return out


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


# ---------------------------------------------------------------------------
# Temporary blocks. Block an item for a fixed duration, then automatically let
# it back into the Copilot trading pool when the timer expires. The release is
# reconciled both on a background thread (so it fires even with the UI closed)
# and at the top of every blocklist/summary read, so it cannot be "stuck on".
# ---------------------------------------------------------------------------

TEMP_BLOCKS_PATH = ROOT / "temp_blocks.json"
_temp_block_lock = threading.Lock()


def load_temp_blocks() -> dict:
    """Map of item_id(str) -> {until, created, name, was_blocked}."""
    try:
        data = json.loads(TEMP_BLOCKS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_temp_blocks(data: dict) -> None:
    try:
        TEMP_BLOCKS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _blocked_ids_list() -> list[int]:
    """Current blockedItemIds from the active Copilot profile (de-duped)."""
    profile_path = _copilot_blocklist_profile_path()
    if not profile_path.exists():
        return []
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    ids, seen = [], set()
    for value in raw.get("blockedItemIds", []):
        try:
            iid = int(value)
        except Exception:
            continue
        if iid > 0 and iid not in seen:
            ids.append(iid)
            seen.add(iid)
    return ids


def reconcile_temp_blocks() -> int:
    """Release any expired temporary blocks back into the pool. Returns the
    number of items released. Cheap and safe to call on every request."""
    temp = load_temp_blocks()
    if not temp:
        return 0
    now = dt.datetime.now()
    with _temp_block_lock:
        temp = load_temp_blocks()  # re-read inside the lock
        if not temp:
            return 0
        ids = _blocked_ids_list()
        id_set = set(ids)
        released = 0
        changed = False
        for key in list(temp.keys()):
            rec = temp.get(key) or {}
            try:
                iid = int(key)
            except Exception:
                temp.pop(key, None); changed = True
                continue
            until = parse_time(rec.get("until"))
            # Drop the record if the user manually un-blocked the item early.
            if iid not in id_set:
                temp.pop(key, None); changed = True
                continue
            if until and now >= until:
                # Only un-block if WE temp-blocked it (it wasn't already blocked).
                if not rec.get("was_blocked") and iid in id_set:
                    id_set.discard(iid)
                    released += 1
                temp.pop(key, None); changed = True
        if released:
            save_blocklist({"blockedItemIds": [i for i in ids if i in id_set]})
        if changed:
            _save_temp_blocks(temp)
        return released


def temp_block_item(item: str | None, item_id: int | None, minutes: float) -> dict:
    """Block an item now and schedule its automatic release after `minutes`."""
    try:
        minutes = float(minutes)
    except Exception:
        return {"error": "Invalid duration"}
    if minutes <= 0:
        return {"error": "Duration must be greater than 0"}
    resolved_id = None
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
    with _temp_block_lock:
        ids = _blocked_ids_list()
        was_blocked = resolved_id in ids
        if not was_blocked:
            ids.append(resolved_id)
            save_blocklist({"blockedItemIds": ids})
        now = dt.datetime.now()
        until = now + dt.timedelta(minutes=minutes)
        temp = load_temp_blocks()
        temp[str(resolved_id)] = {
            "item_id": resolved_id,
            "name": get_item_info(resolved_id).get("name") or f"Item {resolved_id}",
            "created": iso(now),
            "until": iso(until),
            "minutes": minutes,
            "was_blocked": was_blocked,
        }
        _save_temp_blocks(temp)
    return get_blocklist()


def cancel_temp_block(item_id: int | None, item: str | None = None) -> dict:
    """End a temporary block early, releasing the item back into the pool now
    (unless it was already permanently blocked before the temp block)."""
    resolved_id = None
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
    with _temp_block_lock:
        temp = load_temp_blocks()
        rec = temp.pop(str(resolved_id), None)
        _save_temp_blocks(temp)
        if rec and not rec.get("was_blocked"):
            ids = [i for i in _blocked_ids_list() if i != resolved_id]
            save_blocklist({"blockedItemIds": ids})
    return get_blocklist()


def _temp_block_reconcile_loop() -> None:
    while True:
        try:
            reconcile_temp_blocks()
        except Exception:
            pass
        time.sleep(30)


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
    return {"id": int(item_id), "item_id": int(item_id), "name": name, "item": name, "icon_url": info.get("icon"), "members": bool(info.get("members")), "buy_limit": info.get("limit"), **market, "personal": pst, "score": score, "reason": " · ".join(reason_bits) or "basic market candidate"}


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

    def safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0

    for priority, label, root in folders:
        try:
            root_exists = root.exists()
        except OSError:
            root_exists = False
        if not root_exists:
            continue
        for pat in ("flips.csv", "*flips*.csv"):
            try:
                paths = list(root.glob(pat))
            except OSError:
                continue
            for p in paths:
                try:
                    if not p.is_file() or p.name.lower() == "flips_api_test.csv":
                        continue
                    exact_rank = 0 if p.name.lower() == "flips.csv" else 1
                    candidates.append((priority, exact_rank, label, p))
                except OSError:
                    continue
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
    selected = max(
        (p for pri, rank, _, p in candidates if pri == best_priority and rank == best_exact_rank),
        key=safe_mtime,
    )
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

    # Cumulative bankroll trend (for the growth chart). Anchored at the REAL current bankroll and
    # walked backward through realized profit over a window, so it always ends at the true total and
    # never goes blank after a baseline reset. Window = since baseline, or the last TREND_MIN_DAYS
    # (whichever is longer) so a fresh "today" baseline still shows recent growth.
    now_dt = dt.datetime.now()
    end_total = int(totals["total_current"])
    all_events: list[tuple[dt.datetime, int]] = []
    for r in rows:
        if r.get("Account") not in accounts:
            continue
        status = r.get("Status")
        event = r.get("_sell") or r.get("_event")
        if not event:
            continue
        p = r.get("_profit", parse_num(r.get("Profit")))
        if status == "FINISHED":
            all_events.append((event, p))
        elif status == "SELLING" and r.get("_sold", parse_num(r.get("Sold"))) > 0 and p:
            all_events.append((event, p))
    all_events.sort(key=lambda x: x[0])
    win_start = min(baseline_at, now_dt - dt.timedelta(days=TREND_MIN_DAYS)) if baseline_at.year > 2020 else dt.datetime(2020, 1, 1)
    window_events = [e for e in all_events if e[0] >= win_start]
    profit_in_window = int(sum(p for _, p in window_events))
    start_total = end_total - profit_in_window          # so the curve ends exactly at the real current bankroll
    # is the window bounded by the baseline (older than the min-days view) or by the rolling fallback?
    since_baseline = bool(baseline_at.year > 2020 and baseline_at <= now_dt - dt.timedelta(days=TREND_MIN_DAYS))
    span_first = window_events[0][0] if window_events else win_start
    window_days = max(1, round((now_dt - min(win_start, span_first)).total_seconds() / 86400))
    trend: dict = {"times": [], "values": [], "start": start_total, "end": end_total,
                   "since_baseline": since_baseline, "window_days": window_days}
    if window_events:
        span_start = min(win_start, window_events[0][0])
        total_span = max((now_dt - span_start).total_seconds(), 1.0)
        buckets = 48
        cum = 0
        idx = 0
        times_out: list = []
        values_out: list = []
        for b in range(1, buckets + 1):
            b_end = span_start + dt.timedelta(seconds=total_span * b / buckets)
            while idx < len(window_events) and window_events[idx][0] <= b_end:
                cum += window_events[idx][1]
                idx += 1
            times_out.append(iso(b_end))
            values_out.append(int(start_total + cum))
        trend = {"times": times_out, "values": values_out, "start": start_total, "end": int(start_total + cum),
                 "since_baseline": since_baseline, "window_days": window_days}

    return {"active_accounts": accounts, "baseline_at": cfg.get("baseline_at"), "accounts": plan_accounts, "totals": dict(totals), "trend": trend, "ledger": load_bankroll_ledger()[-100:], "notes": cfg.get("notes", ""), "owed": max(0, int(parse_num(cfg.get("owed"))))}


DEFAULT_PROFIT_GOAL = 5_000_000_000  # first headline goal: 5B total profit


def compute_goal_tracker(csv_data: dict, config: dict | None = None) -> dict:
    """Rich progress/ETA tracker for the headline total-profit goal (default 5B).

    Reconstructs the realized-profit timeline from finished + partial-selling
    events across the active accounts (same basis as the bankroll growth chart),
    then derives: how far in, how long it took, multi-window pace, per-pace ETAs,
    acceleration vs. lifetime, ahead/behind, per-milestone cross dates, a forward
    projection series and a recent daily-profit history. All accounting-only.
    """
    cfg = config or load_bankroll_config()
    accounts = cfg.get("active_accounts") or DEFAULT_ACTIVE_ACCOUNTS
    target = max(0, int(parse_num(cfg.get("rank_goal")))) or DEFAULT_PROFIT_GOAL
    rows = list(csv_data.get("analysis_rows", [])) + list(csv_data.get("open_rows", []))

    # Realized-profit events (finished flips + booked partial sells), oldest first.
    events: list[tuple[dt.datetime, int]] = []
    for r in rows:
        if r.get("Account") not in accounts:
            continue
        status = r.get("Status")
        event = r.get("_sell") or r.get("_event")
        if not event:
            continue
        p = int(r.get("_profit", parse_num(r.get("Profit"))))
        if status == "FINISHED":
            events.append((event, p))
        elif status == "SELLING" and r.get("_sold", parse_num(r.get("Sold"))) > 0 and p:
            events.append((event, p))
    events.sort(key=lambda x: x[0])

    now = dt.datetime.now()
    current = int(sum(p for _, p in events))
    remaining = max(0, target - current)
    pct = round(min(100.0, current / target * 100), 2) if target > 0 else 0.0
    reached = current >= target
    started_at = events[0][0] if events else None
    elapsed_days = max((now - started_at).total_seconds() / 86400, 1.0 / 24) if started_at else 0.0

    # Walk the cumulative curve once: capture milestone-cross dates, the goal-cross
    # date, a downsampled history series and per-day net profit.
    milestone_fracs = [0.2, 0.4, 0.6, 0.8, 1.0]
    milestone_vals = [int(round(target * f)) for f in milestone_fracs]
    milestone_reached_at: dict[int, dt.datetime] = {}
    reached_at = None
    daily: dict[str, int] = defaultdict(int)
    cum = 0
    next_mi = 0
    for ev, p in events:
        prev = cum
        cum += p
        daily[ev.strftime("%Y-%m-%d")] += p
        while next_mi < len(milestone_vals) and cum >= milestone_vals[next_mi] and prev < milestone_vals[next_mi]:
            milestone_reached_at[milestone_vals[next_mi]] = ev
            next_mi += 1
        if reached_at is None and cum >= target:
            reached_at = ev

    # Pace over rolling windows (gp/day). d1/d7/d30 = realized in that trailing window.
    def window_pace(days: float) -> int:
        cutoff = now - dt.timedelta(days=days)
        s = sum(p for ev, p in events if ev >= cutoff)
        return int(s / days) if days > 0 else 0

    pace_lifetime = int(current / elapsed_days) if elapsed_days > 0 else 0
    pace_d30 = window_pace(30)
    pace_d7 = window_pace(7)
    pace_d1 = window_pace(1)
    paces = {"lifetime": pace_lifetime, "d30": pace_d30, "d7": pace_d7, "today": pace_d1}

    def eta_for(pace: int) -> dict | None:
        if reached or remaining <= 0:
            return {"days": 0, "date": iso(now)}
        if pace <= 0:
            return None
        days = remaining / pace
        return {"days": round(days, 1), "date": iso(now + dt.timedelta(days=days))}

    etas = {k: eta_for(v) for k, v in {"lifetime": pace_lifetime, "d30": pace_d30, "d7": pace_d7, "today": pace_d1}.items()}

    # Primary pace: prefer the 7-day pace (near-term reality), fall back to 30-day,
    # then lifetime, so a quiet week still yields a sensible ETA.
    if pace_d7 > 0:
        chosen_key, chosen_pace, chosen_label = "d7", pace_d7, "last 7-day pace"
    elif pace_d30 > 0:
        chosen_key, chosen_pace, chosen_label = "d30", pace_d30, "last 30-day pace"
    elif pace_lifetime > 0:
        chosen_key, chosen_pace, chosen_label = "lifetime", pace_lifetime, "lifetime pace"
    else:
        chosen_key, chosen_pace, chosen_label = "d7", 0, "last 7-day pace"
    primary_eta = etas.get(chosen_key)

    accel_pct = round((pace_d7 - pace_lifetime) / pace_lifetime * 100, 1) if pace_lifetime > 0 else None
    # Ahead/behind: days saved (or lost) finishing at recent pace vs. lifetime pace.
    ahead_days = None
    if not reached and remaining > 0 and chosen_pace > 0 and pace_lifetime > 0:
        ahead_days = round(remaining / pace_lifetime - remaining / chosen_pace, 1)

    # Milestone descriptors with cross dates + projected dates for unreached ones.
    milestones = []
    for f, v in zip(milestone_fracs, milestone_vals):
        m_reached = current >= v
        cross = milestone_reached_at.get(v)
        eta_date = None
        if not m_reached and chosen_pace > 0:
            d = (v - current) / chosen_pace
            if d >= 0:
                eta_date = iso(now + dt.timedelta(days=d))
        milestones.append({
            "label": _gp_short(v), "value": v, "pct": round(f * 100, 1),
            "reached": m_reached, "reached_at": iso(cross) if cross else None,
            "eta_date": eta_date,
        })

    # Downsampled cumulative history (so the projection chart has a clean baseline).
    hist = []
    if events:
        span_start = started_at
        total_span = max((now - span_start).total_seconds(), 1.0)
        buckets = 40
        idx = 0
        c = 0
        for b in range(1, buckets + 1):
            b_end = span_start + dt.timedelta(seconds=total_span * b / buckets)
            while idx < len(events) and events[idx][0] <= b_end:
                c += events[idx][1]
                idx += 1
            hist.append({"t": iso(b_end), "v": int(c)})

    # Forward projection from now to the goal at the chosen pace.
    proj = []
    if not reached and chosen_pace > 0 and remaining > 0:
        days_to_goal = remaining / chosen_pace
        proj.append({"t": iso(now), "v": current})
        steps = 24
        for s in range(1, steps + 1):
            dd = days_to_goal * s / steps
            proj.append({"t": iso(now + dt.timedelta(days=dd)), "v": int(min(target, current + chosen_pace * dd))})

    # Recent daily history (last 30 days) + best day + positive-day streak.
    day_list = []
    for i in range(29, -1, -1):
        d = (now - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        day_list.append({"date": d, "profit": int(daily.get(d, 0))})
    best_day = None
    if daily:
        bk = max(daily, key=lambda k: daily[k])
        best_day = {"date": bk, "profit": int(daily[bk])}
    streak = 0
    if daily:
        active_days = sorted(daily.keys())
        cur = dt.datetime.strptime(active_days[-1], "%Y-%m-%d").date()
        # only count an ongoing streak (last active day is today or yesterday)
        if (now.date() - cur).days <= 1:
            while daily.get(cur.strftime("%Y-%m-%d"), 0) > 0:
                streak += 1
                cur = cur - dt.timedelta(days=1)

    return {
        "target": target, "current": current, "remaining": remaining, "pct": pct,
        "reached": reached, "reached_at": iso(reached_at) if reached_at else None,
        "started_at": iso(started_at) if started_at else None,
        "elapsed_days": round(elapsed_days, 2), "flips": len(events),
        "paces": paces, "etas": etas,
        "chosen_key": chosen_key, "chosen_pace": chosen_pace, "chosen_pace_label": chosen_label,
        "eta": primary_eta, "gp_per_sec": round(chosen_pace / 86400, 4) if chosen_pace > 0 else 0,
        "accel_pct": accel_pct, "ahead_days": ahead_days,
        "milestones": milestones, "projection": {"hist": hist, "proj": proj},
        "daily": day_list, "best_day": best_day, "streak_days": streak,
        "is_default_goal": not bool(parse_num(cfg.get("rank_goal"))),
    }


def _gp_short(v: int) -> str:
    v = int(v)
    if abs(v) >= 1_000_000_000:
        s = v / 1_000_000_000
        return (f"{s:.1f}".rstrip("0").rstrip(".")) + "B"
    if abs(v) >= 1_000_000:
        s = v / 1_000_000
        return (f"{s:.0f}") + "M"
    if abs(v) >= 1_000:
        return f"{v / 1000:.0f}k"
    return str(v)


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
    chart_days = chart_days if chart_days in {1, 3, 7, 30, 90, 180, 365} else 7
    points_per_day = {"5m": 288, "1h": 24, "6h": 4, "24h": 1}.get(timestep, 24)
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
    p = formula_params()
    wv, ws, wr, wf = p["score_w_vol"], p["score_w_stab"], p["score_w_roi"], p["score_w_fresh"]
    vol_pts = wv * min(1.0, math.log10(1 + max(0, min_side_vol)) / 3.0)
    drift = 0.0
    known = 0
    if trend_1h is not None:
        drift += min(1.0, abs(trend_1h) / 6.0)
        known += 1
    if trend_24h is not None:
        drift += min(1.0, abs(trend_24h) / 15.0)
        known += 1
    stab_pts = ws * (1.0 - drift / known) if known else ws / 2
    if roi is None or roi <= 0:
        roi_pts = 0.0
    elif roi < 1.5:
        roi_pts = wr * (roi / 1.5)
    elif roi <= 8:
        roi_pts = wr
    elif roi <= 15:
        roi_pts = wr - (roi - 8) / 7 * (wr * 0.6)
    else:
        roi_pts = wr * 0.2
    fresh_pts = wf if data_age_s <= 300 else max(0.0, wf * (1 - (data_age_s - 300) / 3300))
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
    params = formula_params()          # self-tuning knobs (defaults unless calibrated)
    ev_mult = ev_scale()               # A: EV correction factor from calibration
    fill_model = load_calibration().get("fill_model")  # D: learned fill probability

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
        capture_day = min_side_vol * 24 * params["capture_share"]
        if limit:
            capture_day = min(capture_day, limit * 6)
        p_margin = margin_consistency if margin_consistency is not None else params["p_margin_default"]
        _fill_default = fill_reliability if fill_reliability is not None else params["p_fill_default"]
        if fill_model is not None:
            _pf = predict_fill_prob(fill_model, _fill_feature_vec(
                z_score or 0, volatility_pct or 0, min_side_vol, p_margin, _fill_default, offer_roi or 0, trend_1h or 0))
            p_fill = _pf if _pf is not None else _fill_default
        else:
            p_fill = _fill_default
        ev_day = int(offer_margin * capture_day * p_margin * p_fill * ev_mult) if offer_margin > 0 else 0

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
            if z_score <= params["knife_z"] and (trend_1h is not None and trend_1h <= -2):
                flags.append("falling_knife")   # cheap AND still falling: stay out
            elif z_score <= params["dip_z"] and (trend_1h is None or trend_1h > -1) and margin > 0 and "dump" not in flags:
                flags.append("dip_buy")          # cheap and stabilized: mean-reversion entry
            elif z_score >= params["overheated_z"]:
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

    try:
        log_finder_signals(items)   # E: snapshot top recommendations for forward scoring
    except Exception:
        pass

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
# Flip Finder backtest: replay the live EV/signal formula on Wiki /timeseries
# history and measure how the predictions would actually have played out.
#
# Honest modelling (v1): fills are approximated from each bucket's high/low
# envelope (a buy at bid+1 "fills" if a later bucket's avgLow dips to it; a sell
# at ask-1 "fills" if a later bucket's avgHigh reaches it). This ignores order
# queue position and competition, so it is OPTIMISTIC. Prices are bucket
# AVERAGES, not exact quotes. Lookback is capped by the Wiki API (~365 points
# per timestep). Treat output as directional EV calibration, not a P&L promise.
# ---------------------------------------------------------------------------

# horizon = forward buckets allowed to complete a round-trip; win = trailing
# buckets used for z-score/mean-reversion stats (~7d worth per timestep).
BACKTEST_CONFIGS = {
    "1h": {"label": "15d @ 1h", "hours_per_bucket": 1, "horizon": 24, "win": 168},
    "6h": {"label": "90d @ 6h", "hours_per_bucket": 6, "horizon": 8, "win": 28},
    "24h": {"label": "1y @ 24h", "hours_per_bucket": 24, "horizon": 5, "win": 30},
}
_backtest_cache: dict[str, tuple[float, dict]] = {}


def _bt_series(item_id: int, timestep: str) -> list[dict]:
    """Clean high/low/volume buckets for one item from Wiki /timeseries."""
    try:
        data = wiki_get_json("/timeseries", {"id": item_id, "timestep": timestep}).get("data") or []
    except Exception:
        return []
    out = []
    for p in data:
        ah = parse_num(p.get("avgHighPrice"))
        al = parse_num(p.get("avgLowPrice"))
        if ah <= 0 or al <= 0:
            continue
        if ah < al:
            ah, al = al, ah
        out.append({
            "ts": int(parse_num(p.get("timestamp"))),
            "high": ah, "low": al,
            "vbuy": parse_num(p.get("highPriceVolume")),
            "vsell": parse_num(p.get("lowPriceVolume")),
        })
    return out


def _bt_replay_item(series: list[dict], cfg: dict, limit: int | None, params: dict | None = None) -> list[dict]:
    """Replay the flip-finder signal/EV formula at each bucket (no look-ahead),
    then score the outcome over the forward horizon. Returns per-signal records.

    `params` (capture_share, p_*_default, dip_z) lets the optimiser/backtest run
    the live formula or candidate parameter sets through the same engine."""
    p = params or formula_params()
    hpb, H, W = cfg["hours_per_bucket"], cfg["horizon"], cfg["win"]
    recs: list[dict] = []
    n = len(series)
    warmup = 12  # need some trailing history for stats
    for i in range(warmup, n - 1):
        cur = series[i]
        high, low = cur["high"], cur["low"]
        mid = (high + low) / 2.0
        margin = high - _ge_tax(int(high)) - low
        if margin <= 0:
            continue
        # --- trailing-window stats (only data up to and including i) ---
        window = series[max(0, i - W):i + 1]
        mids = [(b["high"] + b["low"]) / 2.0 for b in window]
        mean = sum(mids) / len(mids)
        var = max(0.0, sum(m * m for m in mids) / len(mids) - mean * mean)
        std = var ** 0.5
        z = (mid - mean) / std if std > 0 else 0.0
        margin_share = sum(1 for b in window if (b["high"] - _ge_tax(int(b["high"])) - b["low"]) > 0) / len(window)
        two_sided = sum(1 for b in window if b["vbuy"] > 0 and b["vsell"] > 0) / len(window)
        # --- competitive offers + EV, identical constants to build_flip_finder ---
        min_side = min(cur["vbuy"], cur["vsell"]) / hpb  # normalise volume to per-hour
        buy_at = low + 1
        sell_at = max(buy_at + 1, high - 1)
        offer_margin = sell_at - _ge_tax(int(sell_at)) - buy_at
        if offer_margin <= 0:
            continue
        capture_day = min_side * 24 * p["capture_share"]
        if limit:
            capture_day = min(capture_day, limit * 6)
        p_margin = margin_share if margin_share else p["p_margin_default"]
        p_fill = two_sided if two_sided else p["p_fill_default"]
        ev_day = offer_margin * capture_day * p_margin * p_fill
        if ev_day <= 0:
            continue
        prev = series[i - 1]
        pmid = (prev["high"] + prev["low"]) / 2.0
        trend = (mid - pmid) / pmid * 100 if pmid else 0.0
        is_dip = z <= p["dip_z"] and trend > -1
        vol_pct = (std / mean * 100) if mean else 0.0
        feat = _fill_feature_vec(z, vol_pct, min_side, margin_share, two_sided,
                                 offer_margin / buy_at * 100 if buy_at else 0.0, trend)
        # --- outcome over forward horizon H ---
        fut = series[i + 1:i + 1 + H]
        entered = completed = False
        realized_unit = 0.0
        bj = next((j for j, b in enumerate(fut) if b["low"] <= buy_at), None)
        if bj is not None:
            entered = True
            sell = next((b for b in fut[bj + 1:] if b["high"] >= sell_at), None)
            if sell is not None:
                completed = True
                realized_unit = sell_at - _ge_tax(int(sell_at)) - buy_at
            # else: stuck — the margin was not captured this window. We count it
            # as 0 realized (not a fire-sale loss): a flipper holds or re-prices
            # rather than dumping at the floor. This is a margin-CAPTURE metric,
            # so it does not model holding-period downside.
        recs.append({
            "ev_day": ev_day,
            "realized_ev": realized_unit * capture_day,
            "offer_margin": offer_margin,
            "realized_unit": realized_unit,
            "entered": entered, "completed": completed, "is_dip": is_dip,
            "feat": feat,
        })
    return recs


def run_flip_backtest(sample_size: int = 36, timesteps: tuple[str, ...] = ("1h", "6h"), cache_s: int = 1800) -> dict:
    """Backtest the flip finder over a stratified sample of liquid items.

    Replays the live EV formula on historical buckets and reports how
    predictions held up — focused on EV calibration (predicted vs realized)."""
    ck = f"{sample_size}|{','.join(timesteps)}"
    now = time.time()
    hit = _backtest_cache.get(ck)
    if hit and now - hit[0] < cache_s:
        return hit[1]

    finder = build_flip_finder()
    pool = [x for x in finder.get("items", [])
            if x.get("daily_volume", 0) >= 1000 and x.get("margin_after_tax", 0) > 0 and not x.get("blocked")]
    pool.sort(key=lambda x: x.get("daily_volume", 0), reverse=True)
    # stratify into 3 liquidity tiers and sample evenly so it is representative
    sample: list[dict] = []
    if pool:
        per = max(1, len(pool) // 3)
        tiers = [pool[:per], pool[per:2 * per], pool[2 * per:]]
        take = max(1, sample_size // 3)
        for tier in tiers:
            if not tier:
                continue
            step = max(1, len(tier) // take)
            sample.extend(tier[::step][:take])
    sample = sample[:sample_size]
    limits = {x["item_id"]: x.get("buy_limit") for x in sample}

    results = {}
    for ts in timesteps:
        cfg = BACKTEST_CONFIGS.get(ts)
        if not cfg:
            continue
        all_recs: list[dict] = []
        items_used = 0
        for x in sample:
            series = _bt_series(x["item_id"], ts)
            if len(series) < 20:
                continue
            recs = _bt_replay_item(series, cfg, limits.get(x["item_id"]))
            if recs:
                items_used += 1
                all_recs.extend(recs)
            time.sleep(0.3)  # be polite to the Wiki API
        results[ts] = _bt_summarize(all_recs, cfg, items_used)

    out = {
        "configs": {ts: BACKTEST_CONFIGS[ts]["label"] for ts in timesteps if ts in BACKTEST_CONFIGS},
        "sample_size": len(sample),
        "results": results,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "assumptions": [
            "Fills approximated from bucket high/low envelope — ignores queue position & competition (optimistic).",
            "Prices are bucket averages, not exact instant quotes.",
            "Lookback capped by Wiki API (~365 points/timestep).",
            "Directional EV calibration, not a profit guarantee.",
        ],
    }
    _backtest_cache[ck] = (now, out)
    return out


def _bt_summarize(recs: list[dict], cfg: dict, items_used: int) -> dict:
    n = len(recs)
    if not n:
        return {"label": cfg["label"], "signals": 0, "items": items_used}
    completed = sum(1 for r in recs if r["completed"])
    entered = sum(1 for r in recs if r["entered"])
    pred_total = sum(r["ev_day"] for r in recs)
    real_total = sum(r["realized_ev"] for r in recs)
    realized_pct = (real_total / pred_total * 100) if pred_total > 0 else None
    optimism = (pred_total / real_total) if real_total > 0 else None
    dips = [r for r in recs if r["is_dip"]]
    dip_hits = sum(1 for r in dips if r["completed"])
    return {
        "label": cfg["label"],
        "signals": n,
        "items": items_used,
        "completion_rate": round(completed / n * 100, 1),       # % of signals that fully round-tripped
        "entry_rate": round(entered / n * 100, 1),
        "stuck_rate": round((entered - completed) / n * 100, 1),  # bought but didn't hit the ask in-window
        "predicted_ev_total": int(pred_total),
        "realized_ev_total": int(real_total),
        "realized_pct_of_predicted": round(realized_pct, 1) if realized_pct is not None else None,
        "ev_optimism_factor": round(optimism, 2) if optimism is not None else None,
        "avg_predicted_margin": int(sum(r["offer_margin"] for r in recs) / n),
        "avg_realized_margin": int(sum(r["realized_unit"] for r in recs) / n),
        "dip_signals": len(dips),
        "dip_revert_rate": round(dip_hits / len(dips) * 100, 1) if dips else None,
    }


# ---------------------------------------------------------------------------
# Self-tuning / calibration. The formula has free knobs (capture share, fill/
# margin priors, z-thresholds, score weights). We learn them from: backtest
# history (A: EV scale), a stratified grid search (B), the user's REAL flips
# (C), a learned fill-probability model (D), and a forward outcome log (E).
# HUMAN-IN-THE-LOOP: analyze_* functions PROPOSE; nothing changes the live
# formula until apply_calibration() writes calibration.json (gitignored).
# ---------------------------------------------------------------------------

CALIBRATION_PATH = ROOT / "calibration.json"
DEFAULT_FORMULA_PARAMS = {
    "capture_share": 0.15,     # fraction of thin-side hourly flow you realistically win
    "p_margin_default": 0.65,  # P(margin still exists) when no history
    "p_fill_default": 0.75,    # P(both sides fill) when no history and no model
    "dip_z": -1.2,             # dip_buy trigger (z vs 7d mean)
    "knife_z": -1.5,           # falling_knife trigger
    "overheated_z": 2.0,       # overheated trigger
    "score_w_vol": 40.0, "score_w_stab": 30.0, "score_w_roi": 20.0, "score_w_fresh": 10.0,
}
FILL_FEATURES = ["z", "volatility_pct", "log_minvol", "margin_consistency", "fill_reliability", "roi", "trend"]
_calibration_cache: dict | None = None
_signal_log_last = 0.0


def load_calibration() -> dict:
    global _calibration_cache
    if _calibration_cache is None:
        try:
            _calibration_cache = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        except Exception:
            _calibration_cache = {}
    return _calibration_cache


def save_calibration(data: dict) -> None:
    global _calibration_cache
    data["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    CALIBRATION_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _calibration_cache = data


def formula_params() -> dict:
    p = dict(DEFAULT_FORMULA_PARAMS)
    for k, v in (load_calibration().get("params") or {}).items():
        if k in p:
            try:
                p[k] = float(v)
            except Exception:
                pass
    return p


def ev_scale() -> float:
    try:
        return float(load_calibration().get("ev_scale", 1.0)) or 1.0
    except Exception:
        return 1.0


def _fill_feature_vec(z, vol_pct, min_side_hourly, margin_cons, fill_rel, roi, trend) -> list[float]:
    return [float(z or 0), float(vol_pct or 0), math.log10(1 + max(0.0, float(min_side_hourly or 0))),
            float(margin_cons or 0), float(fill_rel or 0), float(roi or 0), float(trend or 0)]


def predict_fill_prob(model: dict | None, feats: list[float]) -> float | None:
    if not model:
        return None
    try:
        import ev_model
        return ev_model.predict_proba(model, feats)
    except Exception:
        return None


# ---- E: forward outcome log (build your own ground truth over time) ----
def _ensure_signal_log(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS signal_log ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, ts INTEGER, horizon_ts INTEGER,"
        " buy_at REAL, sell_at REAL, offer_margin REAL, ev_day REAL, z REAL, dip INTEGER,"
        " resolved INTEGER DEFAULT 0, completed INTEGER, realized_unit REAL)")


def log_finder_signals(items: list[dict], min_gap_s: int = 1800, top_n: int = 25) -> int:
    """Snapshot the finder's top live recommendations so we can later score them
    against what actually happened — forward ground truth for self-teaching."""
    global _signal_log_last
    now = time.time()
    if now - _signal_log_last < min_gap_s:
        return 0
    recs = [x for x in items if x.get("ev_day", 0) > 0 and x.get("score", 0) >= 55 and not x.get("blocked")][:top_n]
    if not recs:
        return 0
    _signal_log_last = now
    ts = int(now)
    horizon = ts + 24 * 3600
    try:
        conn = _history_db()
        try:
            _ensure_signal_log(conn)
            with conn:
                conn.executemany(
                    "INSERT INTO signal_log (item_id,ts,horizon_ts,buy_at,sell_at,offer_margin,ev_day,z,dip)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    [(x["item_id"], ts, horizon, x.get("buy_at"), x.get("sell_at"), x.get("offer_margin"),
                      x.get("ev_day"), x.get("z_score"), 1 if "dip_buy" in (x.get("flags") or []) else 0) for x in recs])
        finally:
            conn.close()
        return len(recs)
    except Exception:
        return 0


def resolve_signal_outcomes(max_items: int = 40) -> dict:
    """For matured logged signals, look up via /timeseries whether the suggested
    round-trip would have filled, and record the outcome."""
    now = int(time.time())
    conn = _history_db()
    try:
        _ensure_signal_log(conn)
        rows = conn.execute(
            "SELECT id,item_id,ts,horizon_ts,buy_at,sell_at FROM signal_log WHERE resolved=0 AND horizon_ts<=? ORDER BY ts LIMIT 4000",
            (now,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"resolved": 0}
    by_item: dict[int, list] = defaultdict(list)
    for r in rows:
        by_item[int(r[1])].append(r)
    resolved = 0
    for iid, group in list(by_item.items())[:max_items]:
        series = _bt_series(iid, "1h")
        if not series:
            continue
        updates = []
        for (sid, _iid, ts, hts, buy_at, sell_at) in group:
            win = [b for b in series if ts < b["ts"] <= hts]
            completed = False
            realized = 0.0
            bj = next((k for k, b in enumerate(win) if b["low"] <= buy_at), None)
            if bj is not None:
                sell = next((b for b in win[bj + 1:] if b["high"] >= sell_at), None)
                if sell is not None:
                    completed = True
                    realized = sell_at - _ge_tax(int(sell_at)) - buy_at
            updates.append((1 if completed else 0, realized, sid))
        conn = _history_db()
        try:
            with conn:
                conn.executemany("UPDATE signal_log SET resolved=1, completed=?, realized_unit=? WHERE id=?", updates)
        finally:
            conn.close()
        resolved += len(updates)
        time.sleep(0.3)
    return {"resolved": resolved}


def signal_log_stats() -> dict:
    try:
        conn = _history_db()
        try:
            _ensure_signal_log(conn)
            total = conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0]
            r = conn.execute("SELECT COUNT(*), SUM(completed), SUM(ev_day), SUM(CASE WHEN completed=1 THEN offer_margin ELSE 0 END)"
                             " FROM signal_log WHERE resolved=1").fetchone()
        finally:
            conn.close()
    except Exception:
        return {"logged": 0, "resolved": 0}
    n = r[0] or 0
    return {
        "logged": total, "resolved": n,
        "completion_rate": round((r[1] or 0) / n * 100, 1) if n else None,
    }


# ---- A: EV scale from backtest (predicted vs realized) ----
def analyze_ev_scale(timestep: str = "6h") -> dict:
    """Propose an EV scale = realized/predicted from the backtest (the 90d/6h
    window is the more conservative, so it is the default basis)."""
    bt = run_flip_backtest(timesteps=(timestep,))
    r = (bt.get("results") or {}).get(timestep) or {}
    pct = r.get("realized_pct_of_predicted")
    scale = round(max(0.1, min(3.0, (pct or 100) / 100.0)), 3)
    return {"basis": r.get("label", timestep), "realized_pct_of_predicted": pct,
            "proposed_ev_scale": scale, "signals": r.get("signals", 0)}


# ---- C: calibrate against the user's REAL flips ----
def analyze_real_flips() -> dict:
    """Compare the finder's current predicted per-unit margin to the per-unit
    margin you ACTUALLY realized (Profit ea. from flips.csv) on the same items."""
    try:
        rows = load_rows(find_latest_csv()[0])
        finished = rows_for_scope(rows, (dt.datetime.min, dt.datetime.max), load_bankroll_config(), all_accounts=False, status="FINISHED")
    except Exception:
        finished = []
    by_name: dict[str, list] = defaultdict(list)
    for r in finished:
        name = r.get("Item")
        ea = parse_num(r.get("_profit_ea"))
        if not ea:  # fall back to total profit / quantity sold
            sold = parse_num(r.get("_sold"))
            ea = (parse_num(r.get("_profit")) / sold) if sold else 0
        if name and ea:
            by_name[name].append(ea)
    finder = build_flip_finder()
    pred = {x["item_id"]: x for x in finder.get("items", [])}
    num = den = 0.0
    pairs = 0
    for name, eas in by_name.items():
        if len(eas) < 2:
            continue
        iid = get_item_info(name).get("itemId")
        x = pred.get(int(iid)) if iid else None
        if not x or x.get("offer_margin", 0) <= 0:
            continue
        realized_ea = sum(eas) / len(eas)
        w = len(eas)
        num += realized_ea * w
        den += x["offer_margin"] * w
        pairs += 1
    ratio = (num / den) if den > 0 else None
    return {"items_matched": pairs,
            "realized_vs_predicted_pct": round(ratio * 100, 1) if ratio is not None else None,
            "proposed_ev_scale": round(max(0.1, min(3.0, ratio)), 3) if ratio is not None else None,
            "note": "Real-trade anchor: your Profit ea. vs the finder's current suggested margin (selection-biased, different times)."}


# ---- B: grid-search the dip-buy threshold on a train/validation split ----
def analyze_optimize_params(sample_size: int = 24, timestep: str = "1h") -> dict:
    finder = build_flip_finder()
    sample = [x for x in finder.get("items", []) if x.get("daily_volume", 0) >= 1000 and x.get("margin_after_tax", 0) > 0][:sample_size]
    cfg = BACKTEST_CONFIGS[timestep]
    series_map = {}
    for x in sample:
        s = _bt_series(x["item_id"], timestep)
        if len(s) >= 50:
            series_map[x["item_id"]] = (s, x.get("buy_limit"))
        time.sleep(0.3)
    if not series_map:
        return {"ok": False, "error": "not enough history"}
    base = formula_params()
    candidates = [-2.0, -1.6, -1.4, -1.2, -1.0, -0.8]

    def eval_dz(dz, seg):
        hits = tot = 0
        for s, lim in series_map.values():
            cut = int(len(s) * 0.6)
            part = s[:cut] if seg == "train" else s[cut:]
            pr = dict(base); pr["dip_z"] = dz
            for rec in _bt_replay_item(part, cfg, lim, params=pr):
                if rec["is_dip"]:
                    tot += 1
                    if rec["completed"]:
                        hits += 1
        return tot, (hits / tot * 100 if tot else 0.0)

    grid = []
    for dz in candidates:
        ttot, _ = eval_dz(dz, "train")
        vtot, vrate = eval_dz(dz, "val")
        grid.append({"dip_z": dz, "val_signals": vtot, "val_completion": round(vrate, 1)})
    valid = [g for g in grid if g["val_signals"] >= 15]
    best = max(valid, key=lambda g: g["val_completion"]) if valid else None
    return {"ok": True, "grid": grid, "current_dip_z": base["dip_z"],
            "proposed_dip_z": best["dip_z"] if best else None,
            "proposed_val_completion": best["val_completion"] if best else None}


# ---- D: learned fill-probability model ----
def analyze_fill_model(sample_size: int = 30, timestep: str = "1h") -> dict:
    try:
        import ev_model
    except Exception as exc:
        return {"ok": False, "error": f"ev_model unavailable: {exc}"}
    finder = build_flip_finder()
    sample = [x for x in finder.get("items", []) if x.get("daily_volume", 0) >= 1000 and x.get("margin_after_tax", 0) > 0][:sample_size]
    cfg = BACKTEST_CONFIGS[timestep]
    rows: list[list[float]] = []
    labels: list[int] = []
    for x in sample:
        s = _bt_series(x["item_id"], timestep)
        if len(s) < 50:
            continue
        for rec in _bt_replay_item(s, cfg, x.get("buy_limit")):
            if rec.get("feat"):
                rows.append(rec["feat"])
                labels.append(1 if rec["completed"] else 0)
        time.sleep(0.3)
    if len(rows) < 150 or len(set(labels)) < 2:
        return {"ok": False, "error": f"not enough labelled samples ({len(rows)})"}
    tr, trl, vr, vl = ev_model.train_val_split(rows, labels, 0.3)
    model = ev_model.train_logistic(tr, trl, feature_names=FILL_FEATURES)
    val = ev_model.evaluate(model, vr, vl)
    base = sum(labels) / len(labels)
    baseline_acc = max(base, 1 - base)
    beats = val["accuracy"] > baseline_acc + 0.02 and val["auc"] > 0.55
    return {"ok": True, "model": model, "val": val, "baseline_acc": round(baseline_acc, 3),
            "samples": len(rows), "beats_baseline": beats}


# ---- Orchestration + human-in-the-loop apply ----
def self_tune_analyze() -> dict:
    """Run every calibration analysis and return PROPOSALS (nothing applied)."""
    out = {"generated_at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        out["resolve_log"] = resolve_signal_outcomes()
    except Exception as e:
        out["resolve_log"] = {"error": str(e)}
    for key, fn in (("ev_scale", analyze_ev_scale), ("real_flips", analyze_real_flips),
                    ("optimize", analyze_optimize_params), ("fill_model", analyze_fill_model)):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["current"] = calibration_status()
    return out


def apply_calibration(payload: dict) -> dict:
    if payload.get("reset"):
        save_calibration({})
        return {"ok": True, "calibration": calibration_status()}
    cal = dict(load_calibration())
    if "ev_scale" in payload and payload["ev_scale"] is not None:
        cal["ev_scale"] = float(payload["ev_scale"])
    if isinstance(payload.get("params"), dict):
        merged = dict(cal.get("params") or {})
        for k, v in payload["params"].items():
            if k in DEFAULT_FORMULA_PARAMS and v is not None:
                merged[k] = float(v)
        cal["params"] = merged
    if payload.get("fill_model"):
        cal["fill_model"] = payload["fill_model"]
        cal["fill_model_val"] = payload.get("fill_model_val")
    if payload.get("drop_fill_model"):
        cal.pop("fill_model", None)
        cal.pop("fill_model_val", None)
    save_calibration(cal)
    return {"ok": True, "calibration": calibration_status()}


def calibration_status() -> dict:
    cal = load_calibration()
    return {
        "ev_scale": round(ev_scale(), 3),
        "params": {k: formula_params()[k] for k in DEFAULT_FORMULA_PARAMS},
        "params_customized": bool(cal.get("params")),
        "fill_model": bool(cal.get("fill_model")),
        "fill_model_val": cal.get("fill_model_val"),
        "updated_at": cal.get("updated_at"),
        "signal_log": signal_log_stats(),
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


def build_timeframe_stats(days: int = 30, rows: list | None = None, bounds: tuple | None = None) -> dict:
    """Profit grouped by the Copilot timeframe setting active on each account at flip time.
    If `bounds` (start, end) is given, clean flips are additionally scoped to that date
    range (by sell time) so the Stats range selector also filters this table."""
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
    # Timeframe stats start from the reset anchor (today onward); the page range, if
    # any, further narrows the window (never earlier than the anchor).
    cut = load_timeframe_anchor() or (dt.datetime.now() - dt.timedelta(days=days))
    eff_start = max(cut, bounds[0]) if bounds else cut
    agg: dict[int, dict] = {}
    for r in rows:
        if r.get("Status") != "FINISHED":
            continue
        buy = r.get("_buy")
        sell = r.get("_sell")
        if not buy or buy < cut:
            continue
        if bounds:
            ev = sell or buy
            if not (bounds[0] <= ev < bounds[1]):
                continue
        h = name_to_hash.get(r.get("Account"))
        if not h:
            continue
        tf = tf_at(h, buy)
        if not tf or tf != tf_at(h, sell or buy):  # clean flips only: timeframe unchanged buy->sell
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
    return {"days": days, "rows": out, "current": current, "since": iso(eff_start), "logged_since": (_load_tf_history()[0]["ts"] if _load_tf_history() else None)}


# ---------------------------------------------------------------------------
# Min-predicted-profit stats: attribute each flip to the min-profit setting that
# was active on its account at BUY time. Starts logging from first run ("now").
# ---------------------------------------------------------------------------
MINPROFIT_HISTORY_PATH = ROOT / "minprofit_history.json"
MINPROFIT_ANCHOR_PATH = ROOT / "minprofit_anchor.json"


def _normalize_minprofit_setting(value, none_as_auto: bool = False) -> "int | str | None":
    if value is None:
        return "auto" if none_as_auto else None
    s = str(value).strip()
    if not s:
        return "auto" if none_as_auto else None
    if s.lower() == "auto":
        return "auto"
    return int(parse_num(value))


def _account_minprofit_state() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if COPILOT_DIR.exists():
        for f in COPILOT_DIR.glob("acc_*_prefs.json"):
            h = f.stem[4:].rsplit("_", 1)[0]
            try:
                pf = json.loads(f.read_text(encoding="utf-8"))
                mp = _normalize_minprofit_setting(pf.get("minPredictedProfit"), none_as_auto=True)
                out[h] = {"mp": mp, "mtime": dt.datetime.fromtimestamp(f.stat().st_mtime)}
            except Exception:
                continue
    return out


def _account_minprofit() -> dict[str, "int | str"]:
    out = {}
    for h, state in _account_minprofit_state().items():
        mp = state.get("mp")
        if mp is not None:
            out[h] = mp
    return out


def _minprofit_sort_key(mp):
    return (-1, 0) if str(mp).lower() == "auto" else (0, int(mp))


def _load_mp_history() -> list:
    try:
        return json.loads(MINPROFIT_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _minprofit_anchor() -> "dt.datetime":
    """'Start from right now': set once on first call, then fixed."""
    try:
        a = parse_time(json.loads(MINPROFIT_ANCHOR_PATH.read_text(encoding="utf-8")).get("ts"))
        if a:
            return a
    except Exception:
        pass
    now = dt.datetime.now()
    try:
        MINPROFIT_ANCHOR_PATH.write_text(json.dumps({"ts": iso(now)}), encoding="utf-8")
    except Exception:
        pass
    return now


def snapshot_minprofit() -> None:
    """Append a record whenever an account's min-predicted-profit changes."""
    _minprofit_anchor()  # ensure the 'now' anchor exists
    hist = _load_mp_history()
    last = {}
    for e in hist:
        last[e.get("hash")] = e.get("mp")
    changed = False
    now_dt = dt.datetime.now()
    for h, state in _account_minprofit_state().items():
        mp = state.get("mp")
        if mp is None:
            continue
        if last.get(h) != mp:
            change_dt = state.get("mtime") if isinstance(state.get("mtime"), dt.datetime) else now_dt
            hist.append({"ts": iso(change_dt), "hash": h, "mp": mp})
            last[h] = mp
            changed = True
    if changed:
        try:
            MINPROFIT_HISTORY_PATH.write_text(json.dumps(hist), encoding="utf-8")
        except Exception:
            pass


def build_minprofit_stats(rows: list | None = None, bounds: tuple | None = None) -> dict:
    """Profit/efficiency grouped by the min-predicted-profit setting active on each
    account at the flip's BUY time. Only counts flips bought after logging began.
    If `bounds` (start, end) is given, clean flips are also scoped to that date range
    (by sell time) so the Stats range selector filters this table too."""
    snapshot_minprofit()
    name_to_hash = {v: k for k, v in load_account_map().items()}
    current = _account_minprofit()
    byhash = defaultdict(list)
    for e in _load_mp_history():
        ts = parse_time(e.get("ts"))
        mp = _normalize_minprofit_setting(e.get("mp"))
        if ts and mp is not None:
            byhash[e.get("hash")].append((ts, mp))
    for v in byhash.values():
        v.sort()

    def mp_at(h, when):
        arr = byhash.get(h, [])
        val = arr[0][1] if arr else current.get(h)
        for ts, mp in arr:
            if ts <= when:
                val = mp
            else:
                break
        return val

    if rows is None:
        rows = load_rows(find_latest_csv()[0])
    anchor = _minprofit_anchor()
    eff_start = max(anchor, bounds[0]) if bounds else anchor
    agg: dict[int | str, dict] = {}
    for r in rows:
        if r.get("Status") != "FINISHED":
            continue
        buy = r.get("_buy")
        sell = r.get("_sell")
        if not buy or buy < anchor:
            continue
        if bounds:
            ev = sell or buy
            if not (bounds[0] <= ev < bounds[1]):
                continue
        h = name_to_hash.get(r.get("Account"))
        if not h:
            continue
        mp = mp_at(h, buy)
        if mp is None or mp != mp_at(h, sell or buy):  # clean flips only: setting unchanged buy->sell
            continue
        a = agg.setdefault(mp, {"n": 0, "profit": 0, "wins": 0, "hsum": 0.0})
        p = r.get("_profit", 0)
        a["n"] += 1
        a["profit"] += p
        a["wins"] += 1 if p > 0 else 0
        if r.get("_dur_h") is not None:
            a["hsum"] += float(r["_dur_h"])
    out = []
    for mp in sorted(agg, key=_minprofit_sort_key):
        a = agg[mp]
        out.append({
            "min_profit": mp, "flips": a["n"], "profit": a["profit"],
            "avg_profit": round(a["profit"] / max(1, a["n"])),
            "win_rate": round(a["wins"] / max(1, a["n"]) * 100, 1),
            "gp_per_slot_hour": round(a["profit"] / a["hsum"]) if a["hsum"] else 0,
        })
    return {"rows": out, "current": current, "since": iso(eff_start)}


def build_timeframe_minprofit_stats(days: int = 30, rows: list | None = None, bounds: tuple | None = None) -> dict:
    """Profit grouped by Copilot timeframe and min-predicted-profit together.
    Counts only flips where both settings stayed unchanged from buy to sell.
    If `bounds` (start, end) is given, clean flips are also scoped to that date range
    (by sell time) so the Stats range selector filters this table too."""
    snapshot_timeframes()
    snapshot_minprofit()
    name_to_hash = {v: k for k, v in load_account_map().items()}

    current_tf = _account_timeframes()
    tf_byhash = defaultdict(list)
    for e in _load_tf_history():
        ts = parse_time(e.get("ts"))
        tf = parse_num(e.get("tf"))
        if ts and tf:
            tf_byhash[e.get("hash")].append((ts, tf))
    for v in tf_byhash.values():
        v.sort()

    current_mp = _account_minprofit()
    mp_byhash = defaultdict(list)
    for e in _load_mp_history():
        ts = parse_time(e.get("ts"))
        mp = _normalize_minprofit_setting(e.get("mp"))
        if ts and mp is not None:
            mp_byhash[e.get("hash")].append((ts, mp))
    for v in mp_byhash.values():
        v.sort()

    def setting_at(arr, current, h, when, default=None):
        vals = arr.get(h, [])
        val = vals[0][1] if vals else current.get(h, default)
        for ts, setting in vals:
            if ts <= when:
                val = setting
            else:
                break
        return val

    if rows is None:
        rows = load_rows(find_latest_csv()[0])
    tf_cut = load_timeframe_anchor() or (dt.datetime.now() - dt.timedelta(days=days))
    mp_cut = _minprofit_anchor()
    cut = max(tf_cut, mp_cut)
    eff_start = max(cut, bounds[0]) if bounds else cut
    agg: dict[tuple[int, int | str], dict] = {}
    for r in rows:
        if r.get("Status") != "FINISHED":
            continue
        buy = r.get("_buy")
        sell = r.get("_sell")
        if not buy or buy < cut:
            continue
        if bounds:
            ev = sell or buy
            if not (bounds[0] <= ev < bounds[1]):
                continue
        h = name_to_hash.get(r.get("Account"))
        if not h:
            continue
        tf = setting_at(tf_byhash, current_tf, h, buy, 0)
        mp = setting_at(mp_byhash, current_mp, h, buy)
        if not tf or mp is None:
            continue
        if tf != setting_at(tf_byhash, current_tf, h, sell or buy, 0):
            continue
        if mp != setting_at(mp_byhash, current_mp, h, sell or buy):
            continue
        a = agg.setdefault((int(tf), mp), {"n": 0, "profit": 0, "wins": 0, "hsum": 0.0})
        p = r.get("_profit", 0)
        a["n"] += 1
        a["profit"] += p
        a["wins"] += 1 if p > 0 else 0
        if r.get("_dur_h") is not None:
            a["hsum"] += float(r["_dur_h"])

    out = []
    for tf, mp in sorted(agg, key=lambda x: (x[0], _minprofit_sort_key(x[1]))):
        a = agg[(tf, mp)]
        out.append({
            "timeframe_min": tf,
            "min_profit": mp,
            "flips": a["n"],
            "profit": a["profit"],
            "avg_profit": round(a["profit"] / max(1, a["n"])),
            "win_rate": round(a["wins"] / max(1, a["n"]) * 100, 1),
            "gp_per_slot_hour": round(a["profit"] / a["hsum"]) if a["hsum"] else 0,
        })
    return {"days": days, "rows": out, "since": iso(eff_start)}


def _attn_include_empty(params: dict) -> bool:
    """Query toggle for the attention endpoints: ?empty=0 -> collect-only queue."""
    return str(params.get("empty", ["1"])[0]).strip().lower() not in ("0", "false", "no", "off")


def build_attention(include_empty: bool = True) -> dict:
    """Per-account 'needs action' state from Copilot slot files, oldest first.

    Two file-derived signals (Copilot's live modify/abort reprice suggestions are
    NOT written to any file, so they cannot be seen here):
      * COLLECT - a slot is complete (SOLD/BOUGHT or fully transacted) -> items to collect.
      * PLACE   - a slot is EMPTY on a non-paused account -> a free GE slot to fill.
    ready_since / empty_since use the slot file mtime (when it entered that state);
    attn_since is the older of the two and drives the oldest-waiting-first ordering.
    include_empty=False ignores the PLACE signal, so only collect-ready accounts
    count as needing attention (a sharper, less noisy queue). Names come from the map.
    """
    name_map = load_account_map()
    slot_files = sorted(COPILOT_DIR.glob("acc_*_*.json")) if COPILOT_DIR.exists() else []

    # Accounts paused in Copilot get no place suggestions -> don't flag EMPTY slots
    # for them (they'd otherwise sit in the queue forever). Collect still counts.
    paused: set[str] = set()
    if COPILOT_DIR.exists():
        for p in COPILOT_DIR.glob("acc_*_paused.json"):
            h = p.stem[4:].rsplit("_", 1)[0]
            try:
                v = json.loads(p.read_text(encoding="utf-8"))
                if (v.get("isPaused") or v.get("paused")) if isinstance(v, dict) else bool(v):
                    paused.add(h)
            except Exception:
                pass

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
        is_empty = (not complete) and state == "EMPTY"
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0
        a = agg.setdefault(account_hash, {
            "account_hash": account_hash,
            "name": name_map.get(account_hash),
            "ready_slots": 0,
            "ready_since": None,
            "empty_slots": 0,
            "empty_since": None,
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
        elif is_empty:
            a["empty_slots"] += 1
            if a["empty_since"] is None or mtime < a["empty_since"]:
                a["empty_since"] = mtime
    out = []
    for a in agg.values():
        is_paused = a["account_hash"] in paused
        a["paused"] = is_paused
        place_needed = include_empty and a["empty_slots"] > 0 and not is_paused
        a["needs_attention"] = a["ready_slots"] > 0 or place_needed
        # Oldest waiting moment across the active signals -> drives the queue order.
        sinces = [s for s in (a["ready_since"], a["empty_since"] if place_needed else None) if s is not None]
        a["attn_since"] = min(sinces) if sinces else None
        a["ready_since_iso"] = iso(dt.datetime.fromtimestamp(a["ready_since"])) if a["ready_since"] else None
        a["attn_since_iso"] = iso(dt.datetime.fromtimestamp(a["attn_since"])) if a["attn_since"] else None
        a["items"] = a["items"][:8]
        out.append(a)
    out.sort(key=lambda x: (not x["needs_attention"], x["attn_since"] or 9e18, x["account_hash"]))
    return {"available": bool(slot_files), "accounts": out, "mapped": len(name_map), "generated_at": iso(dt.datetime.now())}


# Files the Flipping Copilot plugin rewrites on real GE activity (offers
# placed/filled, transactions pending upload). Excludes *_session_data.jsonl,
# which is a heartbeat that ticks whenever RuneLite is open even when idle.
COPILOT_ACTIVITY_GLOBS = ("acc_*_[0-7].json", "*_un_acked.jsonl")
COPILOT_TRADING_WINDOW_S = 600  # "actively flipping" = an activity file touched in the last 10 min


def copilot_activity_signal() -> dict:
    """Cheap local-only probe of Copilot's live files: the newest mtime across
    the slot + un-acked files. No Copilot API call and no JSON parse — just
    os.stat — so the frontend can poll it every couple of seconds for free.

    Drives event-based sync: the dashboard only calls the (delta) flip API when
    this mtime advances past the last value it synced for, and only while
    actively flipping. mtime is epoch seconds (0.0 when nothing is found)."""
    newest = 0.0
    if COPILOT_DIR.exists():
        for pat in COPILOT_ACTIVITY_GLOBS:
            for p in COPILOT_DIR.glob(pat):
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if m > newest:
                    newest = m
    now = time.time()
    age = (now - newest) if newest else None
    return {
        "mtime": round(newest, 3),
        "age_s": round(age, 1) if age is not None else None,
        "trading": bool(newest and age is not None and age < COPILOT_TRADING_WINDOW_S),
    }


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


# ---------------------------------------------------------------------------
# Sell-side self-competition detector (same item listed for sale on 2+
# accounts). Surfaced as a warning banner on the main dashboard.
# ---------------------------------------------------------------------------

def _sell_competition_from_slots(slots: list[dict], name_map: dict | None = None) -> list[dict]:
    """Items being SOLD on 2+ accounts at once (you'd be undercutting yourself)."""
    name_map = name_map if name_map is not None else load_account_map()

    def acc_name(h):
        return name_map.get(h) or ("Acct " + str(h)[:6])

    sell_by_item: dict[int, list] = defaultdict(list)
    for s in slots:
        if s.get("state") == "SELLING" and s.get("item_id"):
            sell_by_item[int(s["item_id"])].append(s)
    out = []
    for iid, lst in sell_by_item.items():
        accs = sorted({acc_name(s["account_hash"]) for s in lst})
        if len(accs) < 2:
            continue
        prices = [s.get("offer_price") or 0 for s in lst if s.get("offer_price")]
        out.append({
            "item_id": iid, "item": lst[0].get("item"), "icon_url": lst[0].get("icon_url"),
            "accounts": accs, "n_accounts": len(accs), "n_slots": len(lst),
            "total_qty": int(sum((s.get("remaining_quantity") or 0) for s in lst)),
            "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
            "sell_value": int(sum((s.get("post_tax_sell_value") or 0) for s in lst)),
        })
    out.sort(key=lambda c: (c["n_accounts"], c["sell_value"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Stale / stuck offer detector. Copilot only rewrites a slot file when the offer
# actually changes (a fill, a partial, a reprice), so a slot file that has gone
# untouched far longer than the account's timeframe is an offer Copilot has
# effectively "forgotten" — a buy that won't fill or a sell that won't move.
# Threshold scales with the per-account Copilot timeframe setting (minutes).
# ---------------------------------------------------------------------------

# (timeframe_min cap, stale-after minutes). First row whose cap >= the account's
# timeframe wins. Tune freely — these are Ray's starting baselines.
STALE_OFFER_THRESHOLDS = [
    (5, 60),     # 5-min timeframe  -> stale after 1 hour
    (30, 90),    # 30-min timeframe -> stale after 90 min
    (120, 180),  # 2-hour timeframe -> stale after 3 hours
]
STALE_OFFER_DEFAULT_MULT = 1.5  # timeframes above the table: timeframe * this
STALE_OFFER_UNKNOWN_MIN = 60    # account timeframe unreadable -> 1h default
STALE_OFFER_STATES = {"BUYING", "SELLING", "BOUGHT"}


def stale_threshold_minutes(timeframe_min) -> int:
    """Minutes a slot may sit untouched before its offer counts as stale."""
    tf = int(timeframe_min or 0)
    if tf <= 0:
        return STALE_OFFER_UNKNOWN_MIN
    for tf_cap, thresh in STALE_OFFER_THRESHOLDS:
        if tf <= tf_cap:
            return thresh
    return int(tf * STALE_OFFER_DEFAULT_MULT)


def _stale_offers_from_slots(slots: list[dict], name_map: dict | None = None,
                             timeframes: dict | None = None) -> list[dict]:
    """Open offers whose slot file has gone untouched past the stale threshold."""
    name_map = name_map if name_map is not None else load_account_map()
    timeframes = timeframes if timeframes is not None else _account_timeframes()
    now = dt.datetime.now()

    def acc_name(h):
        return name_map.get(h) or ("Acct " + str(h)[:6])

    out = []
    for s in slots:
        if s.get("state") not in STALE_OFFER_STATES:
            continue
        mt = parse_time(s.get("mtime"))
        if not mt:
            continue
        age_min = (now - mt).total_seconds() / 60.0
        tf = int(timeframes.get(s.get("account_hash"), 0) or 0)
        threshold = stale_threshold_minutes(tf)
        if age_min < threshold:
            continue
        out.append({
            "account": acc_name(s.get("account_hash")),
            "account_hash": s.get("account_hash"),
            "slot": s.get("slot"),
            "state": s.get("state"),
            "item_id": s.get("item_id"),
            "item": s.get("item"),
            "icon_url": s.get("icon_url"),
            "offer_price": s.get("offer_price"),
            "quantity_sold": s.get("quantity_sold"),
            "remaining_quantity": s.get("remaining_quantity"),
            "age_minutes": round(age_min, 1),
            "timeframe_min": tf,
            "threshold_minutes": threshold,
        })
    out.sort(key=lambda x: x["age_minutes"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Loss radar: flag OPEN positions that match the historical big-loss profile,
# so they can be cut/re-listed before they become a -10M flip. Tells from 20k
# flips: 88% of big losses held >2h, 64% deployed >150M, 77% on <500/day items.
# ---------------------------------------------------------------------------

LOSS_RADAR_MIN_CAPITAL = 50_000_000   # ignore small positions — a big loss needs real capital at risk


def loss_radar(slots: list[dict], name_map: dict | None = None, dvol: dict | None = None) -> list[dict]:
    """Open positions (BUYING/SELLING/BOUGHT) that look like a developing big loss — large
    capital, aged past a few hours, thin liquidity, or already listed below cost."""
    name_map = name_map if name_map is not None else load_account_map()
    dvol = dvol if dvol is not None else load_wiki_daily_volumes()
    now = dt.datetime.now()
    out = []
    for s in slots:
        if s.get("state") not in STALE_OFFER_STATES:
            continue
        iid = s.get("item_id")
        qty = int(s.get("total_quantity") or s.get("remaining_quantity") or 0)
        if s.get("state") == "BUYING":
            cap = int((s.get("offer_price") or 0) * (s.get("remaining_quantity") or qty))
        else:
            cap = int((s.get("avg_buy") or s.get("offer_price") or 0) * qty)
        if cap < LOSS_RADAR_MIN_CAPITAL:
            continue
        mt = parse_time(s.get("mtime"))
        age_h = ((now - mt).total_seconds() / 3600) if mt else 0
        dv = int(dvol.get(iid, 0)) if iid else 0
        avg_buy = int(s.get("avg_buy") or 0)
        offer = int(s.get("offer_price") or 0)
        underwater = bool(s.get("state") in ("SELLING", "BOUGHT") and avg_buy and offer and offer < avg_buy)
        # Gate on an actual SYMPTOM — a position only becomes a big loss once it stops moving
        # (aging) or is listed below cost. Size/liquidity then set the severity.
        if not (age_h >= 1.5 or underwater):
            continue
        reasons, score = [], 0
        if age_h >= 4:
            score += 2; reasons.append(f"{age_h:.1f}h unsold")
        elif age_h >= 1.5:
            score += 1; reasons.append(f"{age_h:.1f}h unsold")
        if underwater:
            score += 2; reasons.append("listed below your cost")
        if cap >= 150_000_000:
            score += 2; reasons.append("oversized (>150M)")
        elif cap >= 75_000_000:
            score += 1; reasons.append("large position")
        if dv and dv < 500:
            score += 1; reasons.append(f"thin liquidity ({dv}/day)")
        if score < 3:
            continue
        out.append({
            "account": name_map.get(s.get("account_hash")) or ("Acct " + str(s.get("account_hash"))[:6]),
            "item": s.get("item"), "item_id": iid, "slug": item_slug(s.get("item") or ""),
            "icon_url": s.get("icon_url"), "state": s.get("state"), "capital": cap, "qty": qty,
            "age_h": round(age_h, 1), "daily_volume": dv, "underwater": underwater,
            "avg_buy": avg_buy or None, "offer_price": offer or None,
            "level": "high" if score >= 4 else "watch", "reasons": reasons[:3],
            "blocked": bool(iid and int(iid) in _current_blocked_ids()),
        })
    out.sort(key=lambda x: (x["level"] != "high", -x["capital"]))
    return out[:20]


def account_throughput() -> dict:
    """Per-account flipping throughput from finished flips — gp per SLOT-HOUR (the metric a
    slot-bound account is optimised for), win%, avg hold, profit. Powers the Strategy A/B view."""
    by: dict = defaultdict(lambda: {"flips": 0, "profit": 0, "wins": 0, "hold": 0.0, "held_flips": 0})
    for r in load_rows():
        if r.get("Status") != "FINISHED":
            continue
        acc = str(r.get("Account") or "").strip()
        if not acc:
            continue
        a = by[acc]
        a["flips"] += 1
        a["profit"] += r.get("_profit", 0)
        if r.get("_profit", 0) > 0:
            a["wins"] += 1
        tb = parse_time(r.get("First buy time")) or parse_time(str(r.get("First buy time") or "").rstrip("Z"))
        ts = r.get("_event") or parse_time(r.get("Last sell time")) or parse_time(str(r.get("Last sell time") or "").rstrip("Z"))
        if tb and ts:
            h = (ts - tb).total_seconds() / 3600
            if 0 < h < 240:        # ignore absurd holds (clock/parse glitches)
                a["hold"] += h
                a["held_flips"] += 1
    out = []
    for acc, a in by.items():
        if a["flips"] < 5:
            continue
        out.append({
            "account": acc, "flips": a["flips"], "profit": int(a["profit"]),
            "win_pct": round(a["wins"] / a["flips"] * 100, 1),
            "avg_hold_h": round(a["hold"] / a["held_flips"], 2) if a["held_flips"] else None,
            "gp_per_slot_hour": int(a["profit"] / a["hold"]) if a["hold"] > 0 else None,
            "profit_per_flip": int(a["profit"] / a["flips"]),
        })
    out.sort(key=lambda x: -(x["gp_per_slot_hour"] or 0))
    return {"accounts": out, "generated_at": iso(dt.datetime.now())}


# ---------------------------------------------------------------------------
# Accounts: consolidated per-account Flipping Copilot settings (so you don't
# have to check each account one-by-one in RuneLite).
# ---------------------------------------------------------------------------

def _account_prefs() -> dict[str, dict]:
    """Full Copilot prefs per account hash from acc_*_prefs.json."""
    out: dict[str, dict] = {}
    if COPILOT_DIR.exists():
        for f in COPILOT_DIR.glob("acc_*_prefs.json"):
            h = f.stem[4:].rsplit("_", 1)[0]
            try:
                out[h] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
    return out


MEMBERSHIP_PATH = ROOT / "membership.json"
BOND_DAYS = 14  # one bond = 14 days of membership


def load_membership() -> dict:
    try:
        d = json.loads(MEMBERSHIP_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def set_account_membership(account: str, mode: str, expires: str | None = None) -> dict:
    """Per-account membership override. mode: 'recurring' | 'manual' | 'auto'.
    'auto'/'clear' reverts to bond-ledger derivation."""
    key = str(account or "").strip().lower()
    if not key:
        return {"error": "account required"}
    data = load_membership()
    if mode in ("auto", "clear"):
        data.pop(key, None)
    elif mode == "recurring":
        data[key] = {"mode": "recurring"}
    elif mode == "manual":
        t = parse_time(expires)
        if not t:
            return {"error": "valid expires date required"}
        data[key] = {"mode": "manual", "expires": iso(t)}
    else:
        return {"error": f"unknown mode: {mode}"}
    try:
        MEMBERSHIP_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True}


def _bond_expiry_by_account() -> dict[str, "dt.datetime"]:
    """Membership expiry per account from logged bonds (each bond = 14 days,
    stacking from the later of the current expiry or the bond's redemption date)."""
    bonds = sorted(
        [e for e in load_bankroll_ledger() if e.get("type") == "bond" and parse_time(e.get("ts"))],
        key=lambda e: parse_time(e["ts"]))
    out: dict[str, dt.datetime] = {}
    for e in bonds:
        acc = str(e.get("account") or "").strip().lower()
        if not acc:
            continue
        ts = parse_time(e["ts"])
        cur = out.get(acc)
        out[acc] = (max(cur, ts) if cur else ts) + dt.timedelta(days=BOND_DAYS)
    return out


def _account_profit_stats(rows: list[dict]) -> dict[str, dict]:
    now = dt.datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out: dict[str, dict] = defaultdict(lambda: {"profit_today": 0, "profit_all": 0, "flips_today": 0, "last_flip": None})
    for r in rows:
        if r.get("Status") != "FINISHED":
            continue
        acc = str(r.get("Account") or "").strip().lower()
        if not acc:
            continue
        o = out[acc]
        o["profit_all"] += r.get("_profit", 0)
        ev = r.get("_event")
        if ev:
            if o["last_flip"] is None or ev > o["last_flip"]:
                o["last_flip"] = ev
            if ev >= today0:
                o["profit_today"] += r.get("_profit", 0)
                o["flips_today"] += 1
    return out


def build_account_settings() -> dict:
    name_map = load_account_map()
    prefs = _account_prefs()
    profit_by = _account_profit_stats(load_rows())
    bond_exp = _bond_expiry_by_account()
    overrides = load_membership()
    now = dt.datetime.now()

    def membership_for(name: str) -> dict:
        key = name.strip().lower()
        ov = overrides.get(key) or {}
        if ov.get("mode") == "recurring":
            return {"mode": "recurring", "expires": None, "days_left": None}
        cands = []
        if bond_exp.get(key):
            cands.append(bond_exp[key])
        if ov.get("expires") and parse_time(ov["expires"]):
            cands.append(parse_time(ov["expires"]))
        exp = max(cands) if cands else None
        if not exp:
            return {"mode": "none", "expires": None, "days_left": None}
        return {"mode": "manual" if ov.get("expires") else "bond",
                "expires": iso(exp), "days_left": round((exp - now).total_seconds() / 86400, 1)}

    paused: set[str] = set()
    if COPILOT_DIR.exists():
        for p in COPILOT_DIR.glob("acc_*_paused.json"):
            h = p.stem[4:].rsplit("_", 1)[0]
            try:
                v = json.loads(p.read_text(encoding="utf-8"))
                if (v.get("paused") if isinstance(v, dict) else bool(v)):
                    paused.add(h)
            except Exception:
                pass

    def acc_name(h):
        return name_map.get(h) or ("Acct " + str(h)[:6])

    accounts = []
    for h in sorted(prefs.keys(), key=lambda x: acc_name(x).lower()):
        pf = prefs[h]
        name = acc_name(h)
        ps = profit_by.get(name.strip().lower()) or {}
        accounts.append({
            "hash": h, "name": name,
            "min_predicted_profit": _normalize_minprofit_setting(pf.get("minPredictedProfit"), none_as_auto=True),
            "timeframe": pf.get("timeframe"),
            "risk_level": pf.get("riskLevel"),
            "f2p_only": bool(pf.get("f2pOnlyMode")),
            "buy_and_hold": bool(pf.get("buyAndHold")),
            "reserved_slots": int(parse_num(pf.get("reservedSlots"))),
            "dump_suggestions": bool(pf.get("receiveDumpSuggestions")),
            "paused": h in paused,
            "profit_today": int(ps.get("profit_today", 0)),
            "profit_all": int(ps.get("profit_all", 0)),
            "flips_today": ps.get("flips_today", 0),
            "last_flip": iso(ps["last_flip"]) if ps.get("last_flip") else None,
            "membership": membership_for(name),
        })
    bl = list_copilot_profiles()
    blocked_count = next((p.get("blocked_count") for p in bl.get("profiles", []) if p.get("active")), None)

    keys = ("min_predicted_profit", "timeframe", "risk_level", "f2p_only", "buy_and_hold", "reserved_slots", "dump_suggestions")
    uniform = {k: len({json.dumps(a.get(k)) for a in accounts}) <= 1 for k in keys}
    return {
        "accounts": accounts,
        "blocklist": {"profile": bl.get("active"), "blocked_count": blocked_count, "profiles": bl.get("profiles", [])},
        "uniform": uniform,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# Slot occupancy: log how long each GE slot is actually tied up (buy-wait +
# holding + selling + cancels) from the live slot files. Captures the dead time
# the flip-duration metric misses (unfilled buys, re-lists). Logged from now,
# only while the dashboard is open and polling.
# ---------------------------------------------------------------------------
SLOT_LOG_MIN_GAP_S = 12
_slot_log_last = 0.0


def _ensure_slot_tables(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS slot_episode (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " account_hash TEXT, slot INTEGER, item_id INTEGER, item TEXT, start_ts INTEGER,"
                 " last_ts INTEGER, first_fill_ts INTEGER, reprices INTEGER DEFAULT 0, last_price REAL,"
                 " max_sold INTEGER DEFAULT 0, total_qty INTEGER DEFAULT 0, state TEXT, open INTEGER DEFAULT 1)")
    conn.execute("CREATE TABLE IF NOT EXISTS slot_poll (ts INTEGER PRIMARY KEY, occupied INTEGER, total INTEGER)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_ep_open ON slot_episode(open)")


def record_slot_episodes(slots) -> None:
    """Snapshot live slot occupancy into episodes (one item occupying one slot
    for its whole buy->sell lifecycle). Throttled; safe to call every refresh."""
    global _slot_log_last
    now = time.time()
    if now - _slot_log_last < SLOT_LOG_MIN_GAP_S:
        return
    _slot_log_last = now
    nowi = int(now)
    cur: dict[tuple, dict] = {}
    for s in slots or []:
        h, sl = s.get("account_hash"), s.get("slot")
        if not h or sl is None:
            continue
        try:
            cur[(h, int(sl))] = s
        except Exception:
            continue
    try:
        n_accounts = len({_slot_file_parts(p)[0] for p in COPILOT_DIR.glob("acc_*_[0-7].json")}) if COPILOT_DIR.exists() else 0
    except Exception:
        n_accounts = 0
    try:
        conn = _history_db()
        try:
            _ensure_slot_tables(conn)
            with conn:
                opens = {}
                for row in conn.execute("SELECT id,account_hash,slot,item_id,first_fill_ts,reprices,last_price,max_sold FROM slot_episode WHERE open=1").fetchall():
                    opens[(row[1], int(row[2]))] = row
                for (h, sl), s in cur.items():
                    iid = int(parse_num(s.get("item_id")))
                    state = s.get("state") or ""
                    price = parse_num(s.get("offer_price"))
                    qsold = int(parse_num(s.get("quantity_sold")))
                    tot = int(parse_num(s.get("total_quantity")))
                    try:
                        mt = parse_time(s.get("mtime"))
                        mts = int(mt.timestamp()) if mt else nowi
                    except Exception:
                        mts = nowi
                    filled_now = state in ("BOUGHT", "SELLING") or qsold > 0
                    ep = opens.get((h, sl))
                    if ep and ep[3] == iid:
                        eid, _, _, _, ff, rep, lastp, maxs = ep
                        rep2 = (rep or 0) + (1 if (state == "BUYING" and lastp is not None and price != lastp) else 0)
                        ff2 = ff or (nowi if filled_now else None)
                        conn.execute("UPDATE slot_episode SET last_ts=?, state=?, reprices=?, last_price=?, max_sold=?, total_qty=?, first_fill_ts=? WHERE id=?",
                                     (nowi, state, rep2, price, max(maxs or 0, qsold), tot, ff2, eid))
                    else:
                        if ep:
                            conn.execute("UPDATE slot_episode SET open=0 WHERE id=?", (ep[0],))
                        conn.execute("INSERT INTO slot_episode (account_hash,slot,item_id,item,start_ts,last_ts,first_fill_ts,reprices,last_price,max_sold,total_qty,state,open)"
                                     " VALUES (?,?,?,?,?,?,?,0,?,?,?,?,1)",
                                     (h, sl, iid, s.get("item"), mts, nowi, (mts if filled_now else None), price, qsold, tot, state))
                for (h, sl), ep in opens.items():
                    if (h, sl) not in cur:
                        conn.execute("UPDATE slot_episode SET open=0 WHERE id=?", (ep[0],))
                conn.execute("INSERT OR REPLACE INTO slot_poll (ts,occupied,total) VALUES (?,?,?)", (nowi, len(cur), n_accounts * 8))
        finally:
            conn.close()
    except Exception:
        pass


def build_slot_occupancy_stats(rows: list | None = None) -> dict:
    """Aggregate true slot occupancy: real slot-hours, utilization, buy-fill time,
    cancel rate, and per-item profit ÷ actual slot-hours."""
    try:
        conn = _history_db()
        try:
            _ensure_slot_tables(conn)
            eps = conn.execute("SELECT item_id,item,start_ts,last_ts,first_fill_ts,reprices,max_sold,open FROM slot_episode").fetchall()
            occ_sum, tot_sum = conn.execute("SELECT COALESCE(SUM(occupied),0), COALESCE(SUM(total),0) FROM slot_poll").fetchone()
        finally:
            conn.close()
    except Exception:
        return {"episodes": 0, "since": None, "items": []}
    if not eps:
        return {"episodes": 0, "since": None, "items": []}
    anchor = min(e[2] for e in eps)
    by_item: dict[str, dict] = defaultdict(lambda: {"episodes": 0, "occ_h": 0.0, "filled": 0, "cancels": 0, "reprices": 0, "bw_h": 0.0, "bw_n": 0})
    total_occ_h = 0.0
    n = filled = cancels = bw_n = 0
    bw_sum = 0.0
    for (iid, item, start, last, ff, rep, maxs, open_) in eps:
        dur = max(0, (last or start) - start) / 3600.0
        total_occ_h += dur
        n += 1
        key = item or f"Item {iid}"
        e = by_item[key]
        e["episodes"] += 1
        e["occ_h"] += dur
        e["reprices"] += rep or 0
        if ff:
            filled += 1
            e["filled"] += 1
            bw = max(0, ff - start) / 3600.0
            bw_sum += bw
            bw_n += 1
            e["bw_h"] += bw
            e["bw_n"] += 1
        elif open_ == 0 and (maxs or 0) == 0:
            cancels += 1
            e["cancels"] += 1
    if rows is None:
        rows = load_rows(find_latest_csv()[0])
    anchor_dt = dt.datetime.fromtimestamp(anchor)
    prof_by_item: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("Status") != "FINISHED":
            continue
        b = r.get("_buy")
        if not b or b < anchor_dt:
            continue
        prof_by_item[(r.get("Item") or "").strip().lower()] += r.get("_profit", 0)
    items = []
    for name, e in by_item.items():
        prof = prof_by_item.get((name or "").strip().lower(), 0)
        items.append({
            "item": name, "episodes": e["episodes"], "occupied_h": round(e["occ_h"], 1),
            "avg_h": round(e["occ_h"] / e["episodes"], 2) if e["episodes"] else 0,
            "fill_rate": round(e["filled"] / e["episodes"] * 100) if e["episodes"] else 0,
            "cancels": e["cancels"], "reprices": e["reprices"],
            "avg_buy_fill_h": round(e["bw_h"] / e["bw_n"], 2) if e["bw_n"] else None,
            "profit": int(prof),
            "gp_per_occ_hr": int(prof / e["occ_h"]) if e["occ_h"] > 0 else 0,
        })
    items.sort(key=lambda x: x["occupied_h"], reverse=True)
    # only attribute profit for items we actually observed occupying a slot,
    # so the numerator and the occupancy denominator cover the same set
    total_profit = sum(x["profit"] for x in items)
    return {
        "since": iso(anchor_dt), "episodes": n, "occupied_hours": round(total_occ_h, 1),
        "utilization_pct": round((occ_sum or 0) / tot_sum * 100, 1) if tot_sum else None,
        "avg_occupancy_h": round(total_occ_h / n, 2) if n else 0,
        "avg_buy_fill_h": round(bw_sum / bw_n, 2) if bw_n else None,
        "cancel_rate": round(cancels / n * 100, 1) if n else 0,
        "true_gp_per_occ_hr": int(total_profit / total_occ_h) if total_occ_h > 0 else 0,
        "items": items[:25],
    }


def build_stats_page(days: int = 0, rows: list | None = None, bounds: tuple | None = None) -> dict:
    """Analytics for the Stats tab. The item/overview aggregation uses either an
    explicit (start, end) `bounds` window (today/yesterday/custom, matching the
    index-page date ranges) or a `days` lookback (default 0 = all-time); the
    timeframe/min-profit setting tables always track from their own reset anchor."""
    if rows is None:
        rows = load_rows(find_latest_csv()[0])
    if bounds:
        fin = [r for r in rows if r.get("Status") == "FINISHED" and r.get("_sell") and bounds[0] <= r["_sell"] < bounds[1]]
    else:
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
    tf = build_timeframe_stats(days, rows, bounds=bounds)
    mp = build_minprofit_stats(rows, bounds=bounds)
    tfmp = build_timeframe_minprofit_stats(days, rows, bounds=bounds)
    slot_occ = build_slot_occupancy_stats(rows)
    durs = [r["_dur_h"] for r in fin if r.get("_dur_h") is not None]
    trading_days = len(byday)
    worst_day = min(byday.items(), key=lambda kv: kv[1]) if byday else None
    top5_profit = sum(d["profit"] for _, d in items_sorted[:5] if d["profit"] > 0)
    gross = total_profit + tax_paid
    most_flipped = [{"item": n, "slug": item_slug(n), **pack(d)} for n, d in sorted(byitem.items(), key=lambda kv: kv[1]["flips"], reverse=True)[:10]]
    best_win = [{"item": n, "slug": item_slug(n), **pack(d)} for n, d in
                sorted([(n, d) for n, d in byitem.items() if d["flips"] >= 10], key=lambda kv: kv[1]["wins"] / kv[1]["flips"], reverse=True)[:10]]
    return {
        "days": days,
        "totals": {"flips": total_flips, "profit": total_profit,
                   "avg": round(total_profit / total_flips) if total_flips else 0,
                   "win_rate": round(total_wins / max(1, total_flips) * 100, 1),
                   "tax_paid": tax_paid, "turnover": turnover,
                   "biggest": biggest,
                   "trading_days": trading_days,
                   "avg_per_day": round(total_profit / trading_days) if trading_days else 0,
                   "flips_per_day": round(total_flips / trading_days, 1) if trading_days else 0,
                   "avg_hold_h": round(sum(durs) / len(durs), 1) if durs else None,
                   "tax_pct": round(tax_paid / gross * 100, 1) if gross > 0 else None,
                   "top5_share": round(top5_profit / total_profit * 100, 1) if total_profit > 0 else None,
                   "best_hour": max(active_hours, key=lambda x: x["profit"]) if active_hours else None,
                   "best_dow": max(active_dows, key=lambda x: x["profit"]) if active_dows else None,
                   "best_day": {"date": best_day[0], "profit": round(best_day[1])} if best_day else None,
                   "worst_day": {"date": worst_day[0], "profit": round(worst_day[1])} if worst_day else None},
        "by_hour": hours, "by_dow": dows, "by_tier": by_tier,
        "top_items": top_items, "worst_items": worst_items, "by_account": by_account,
        "most_flipped": most_flipped, "best_win": best_win,
        "fast_items": fast_items, "slow_items": slow_items,
        "cumulative": cumulative,
        "by_timeframe": tf["rows"], "timeframe_current": tf["current"], "timeframe_since": tf.get("since"), "timeframe_logged_since": tf["logged_since"],
        "by_minprofit": mp["rows"], "minprofit_current": mp["current"], "minprofit_since": mp["since"],
        "by_timeframe_minprofit": tfmp["rows"], "timeframe_minprofit_since": tfmp["since"],
        "slot_occupancy": slot_occ,
    }


# ===========================================================================
# Market Insight — scrape OSRS updates + reddit sentiment, detect real price
# movers from local history, then (optionally) use Claude to synthesise which
# items are risky/volatile to flip right now and why.
# ===========================================================================

def _insight_fetch(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": INSIGHT_USER_AGENT,
        "Accept": "application/json, text/xml, application/xml, */*",
    })
    return urllib.request.urlopen(req, timeout=timeout).read()


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", s).strip()


def _osrs_news_rss(limit: int = 12) -> list[dict]:
    """Latest official OSRS news from the RSS feed (timeliest, has summaries)."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(_insight_fetch(OSRS_NEWS_RSS))  # bytes: ET honors the ISO-8859-1 decl
        out = []
        for item in root.iter("item"):
            def t(tag):
                el = item.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            pub = t("pubDate")
            try:
                from email.utils import parsedate_to_datetime
                iso = parsedate_to_datetime(pub).astimezone().replace(tzinfo=None).isoformat(timespec="seconds") if pub else ""
            except Exception:
                iso = pub
            out.append({"title": _strip_html(t("title")), "url": t("link"), "date": iso,
                        "category": _strip_html(t("category")), "summary": _strip_html(t("description"))[:400]})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _wiki_updates(limit: int = 12) -> list[dict]:
    """Game updates from the OSRS Wiki MediaWiki API (reliable no-auth backbone)."""
    try:
        url = ("https://oldschool.runescape.wiki/api.php?action=query&format=json&list=recentchanges"
               f"&rcnamespace=112&rctype=new&rclimit={limit}&rcprop=title|timestamp&rcdir=older")
        j = json.loads(_insight_fetch(url))
        out = []
        for c in (j.get("query", {}).get("recentchanges") or []):
            title = str(c.get("title", "")).split("Update:", 1)[-1].strip()
            slug = str(c.get("title", "")).replace(" ", "_")
            out.append({"title": title, "url": "https://oldschool.runescape.wiki/w/" + quote(slug),
                        "date": str(c.get("timestamp", "")), "category": "Wiki", "summary": ""})
        return out
    except Exception:
        return []


def fetch_osrs_news(limit: int = 12) -> list[dict]:
    """Combined official updates: RSS (timely + summaries) backfilled by the Wiki API."""
    merged, seen = [], set()
    for it in _osrs_news_rss(limit) + _wiki_updates(limit):
        key = re.sub(r"[^a-z0-9]", "", (it.get("title") or "").lower())[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(it)
    return merged[:limit]


def _reddit_relevant(title: str, body: str, score: int, min_score: int) -> bool:
    blob = (title + " " + body).lower()
    if any(k in blob for k in INSIGHT_KEYWORDS):
        return True
    return score >= max(min_score * 4, 300)


def _reddit_json(sub: str, min_score: int) -> list[dict]:
    """Free JSON API — works from residential IPs; blocked (403) from datacenters."""
    seen, posts = set(), []
    for endpoint in (f"/r/{sub}/hot.json?limit=100", f"/r/{sub}/top.json?t=day&limit=100"):
        try:
            raw = json.loads(_insight_fetch("https://www.reddit.com" + endpoint))
        except Exception:
            continue
        for child in (raw.get("data", {}).get("children") or []):
            d = child.get("data", {})
            pid = d.get("id")
            if not pid or pid in seen or d.get("stickied"):
                continue
            seen.add(pid)
            title, body, score = str(d.get("title") or ""), str(d.get("selftext") or ""), int(d.get("score") or 0)
            if not _reddit_relevant(title, body, score, min_score):
                continue
            posts.append({
                "title": title[:300], "url": "https://reddit.com" + str(d.get("permalink") or ""),
                "score": score, "num_comments": int(d.get("num_comments") or 0),
                "flair": str(d.get("link_flair_text") or ""), "snippet": _strip_html(body)[:300],
                "created": dt.datetime.fromtimestamp(d.get("created_utc") or 0).isoformat(timespec="minutes"),
            })
    return posts


def _reddit_rss(sub: str) -> list[dict]:
    """Atom fallback (no score/comments) — dodges the JSON block."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(_insight_fetch(f"https://www.reddit.com/r/{sub}/.rss"))
        ns = "{http://www.w3.org/2005/Atom}"
        out = []
        for e in root.iter(ns + "entry"):
            title = _strip_html((e.findtext(ns + "title") or ""))
            link_el = e.find(ns + "link")
            url = link_el.get("href") if link_el is not None else ""
            body = _strip_html(e.findtext(ns + "content") or "")
            if not _reddit_relevant(title, body, 0, 40):
                continue
            out.append({"title": title[:300], "url": url, "score": 0, "num_comments": 0,
                        "flair": "", "snippet": body[:300], "created": (e.findtext(ns + "updated") or "")[:16]})
        return out
    except Exception:
        return []


def fetch_reddit_signals(limit: int = 40, min_score: int = 40) -> list[dict]:
    """Recent market-relevant posts across the configured subs. JSON API first
    (residential IPs), Atom RSS fallback when the JSON API is blocked."""
    posts = []
    for sub in REDDIT_SUBS:
        got = _reddit_json(sub, min_score) or _reddit_rss(sub)
        for p in got:
            p["sub"] = sub
        posts.extend(got)
    posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    return posts[:limit]


# --- Deep research: article bodies, item matching, per-item reddit, sentiment, X ---

_NOTABLE_NAMES: dict | None = None


def _notable_names(min_price: int = 300_000) -> dict:
    """{lowercased name: item_id} for high-value items (the gear updates move).
    Lets us pull affected items straight out of an article body, even if their
    price hasn't reacted yet. Cached for the process lifetime."""
    global _NOTABLE_NAMES
    if _NOTABLE_NAMES is None:
        latest = load_wiki_latest_prices()
        idx = {}
        for it in fetch_wiki_mapping():
            try:
                iid = int(it["id"])
            except Exception:
                continue
            nm = str(it.get("name") or "").strip()
            if len(nm) < 5:
                continue
            lr = latest.get(iid) or {}
            mid = ((parse_num(lr.get("high")) or 0) + (parse_num(lr.get("low")) or 0)) / 2
            if mid >= min_price:
                idx[nm.lower()] = iid
        _NOTABLE_NAMES = idx
    return _NOTABLE_NAMES


def _extract_notable(text: str, limit: int = 8) -> list[dict]:
    """Notable items named anywhere in `text` (article body / thread) — via the
    colloquial alias map first, then high-value full names."""
    if not text:
        return []
    low = text.lower()
    out: dict = {}
    for alias, canon in INSIGHT_ALIASES.items():
        if re.search(r"\b" + re.escape(alias) + r"\b", low):   # word-bounded (no "ely" in "merely")
            info = get_item_info(canon)
            iid = info.get("itemId")
            if iid:
                out[int(iid)] = info.get("name") or canon
    for nm, iid in _notable_names().items():
        if len(out) >= limit:
            break
        if re.search(r"\b" + re.escape(nm) + r"\b", low):
            out.setdefault(iid, nm)
    return [{"item_id": i, "name": n} for i, n in list(out.items())[:limit]]


def _match_terms(name: str) -> list[str]:
    """Lowercase strings that, if present in text, mean this item is referenced —
    the full name plus any colloquial alias that maps to it."""
    nm = (name or "").strip().lower()
    terms = {nm} if len(nm) >= 4 else set()
    for alias, canon in INSIGHT_ALIASES.items():
        if canon.lower() == nm:
            terms.add(alias)
    return [t for t in terms if t]


def _reddit_search_term(name: str) -> str:
    """Distinctive query for reddit search — drop possessives / 'of X' suffixes."""
    nm = (name or "").strip()
    nm = re.sub(r"\b(\w+)'s\b", r"\1", nm)            # Inquisitor's -> Inquisitor
    nm = re.sub(r"\s+of\s+\w+.*$", "", nm, flags=re.I)  # Scythe of vitur -> Scythe
    return nm.strip() or name


_ARTICLE_BODY_CACHE: dict = {}


def _article_body(url: str) -> str:
    if not url:
        return ""
    if url in _ARTICLE_BODY_CACHE:
        return _ARTICLE_BODY_CACHE[url]
    try:
        html = _insight_fetch(url, timeout=18).decode("utf-8", "replace")
        body = _strip_html(re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html))
    except Exception:
        body = ""
    _ARTICLE_BODY_CACHE[url] = body
    return body[:24000]


def fetch_osrs_news_deep(limit: int = 8) -> list[dict]:
    """Updates with their FULL article body scraped (so we can see which items an
    update actually affects — not just the 73-char RSS blurb)."""
    news = fetch_osrs_news(limit)
    for n in news:
        body = _article_body(n.get("url", ""))
        n["body"] = body[:1200]            # trimmed for context/UI
        n["_body_low"] = body.lower()      # full, for matching (not serialised)
    return news


_REDDIT_SEARCH_CACHE: dict = {}


def reddit_search(query: str, subs: list[str] | None = None, max_age_s: int = 900) -> list[dict]:
    """Reverse-search reddit for an item name (RSS — dodges the JSON block). The
    deterministic 'why is this item moving' signal, with citations."""
    subs = subs or REDDIT_SUBS
    key = query.lower()
    hit = _REDDIT_SEARCH_CACHE.get(key)
    if hit and time.time() - hit[0] < max_age_s:
        return hit[1]
    out, seen = [], set()
    for sub in subs:
        try:
            import xml.etree.ElementTree as ET
            url = f"https://www.reddit.com/r/{sub}/search.rss?q={quote(query)}&restrict_sr=1&sort=new&t=month&limit=15"
            root = ET.fromstring(_insight_fetch(url))
            ns = "{http://www.w3.org/2005/Atom}"
            for e in root.iter(ns + "entry"):
                title = _strip_html(e.findtext(ns + "title") or "")
                link_el = e.find(ns + "link")
                u = link_el.get("href") if link_el is not None else ""
                if not title or title in seen:
                    continue
                seen.add(title)
                out.append({"title": title[:200], "url": u, "sub": sub,
                            "created": (e.findtext(ns + "updated") or "")[:10]})
        except Exception:
            continue
    out = out[:8]
    _REDDIT_SEARCH_CACHE[key] = (time.time(), out)
    return out


def _sentiment(text: str) -> str:
    t = (text or "").lower()
    bear = sum(t.count(w) for w in SENT_BEARISH)
    bull = sum(t.count(w) for w in SENT_BULLISH)
    vol = sum(t.count(w) for w in SENT_VOLATILE)
    if vol and vol >= max(bear, bull):
        return "volatile"
    if bear > bull:
        return "bearish"
    if bull > bear:
        return "bullish"
    return "volatile" if vol else "neutral"


def fetch_x_signals(limit: int = 12) -> list[dict]:
    """Best-effort dev/official tweets via Nitter RSS (no auth). Nitter instances
    are flaky — tries a few and degrades silently to []."""
    out, seen = [], set()
    for handle in X_HANDLES:
        for host in NITTER_HOSTS:
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(_insight_fetch(f"https://{host}/{handle}/rss", timeout=8))
                for item in root.iter("item"):
                    def t(tag):
                        el = item.find(tag)
                        return (el.text or "").strip() if el is not None and el.text else ""
                    title = _strip_html(t("title"))
                    if not title or title in seen:
                        continue
                    seen.add(title)
                    out.append({"handle": handle, "title": title[:240], "url": t("link"), "date": t("pubDate")[:16]})
                break  # this host worked for this handle
            except Exception:
                continue
    return out[:limit]


def market_movers(limit: int = 30, min_price: int = 100_000, min_daily_vol: int = 100, days: int = 7) -> list[dict]:
    """Biggest movers + multi-day volatility from local hourly history (the `hist`
    table). Restricted to liquid, meaningful items (so update/speculation-driven
    gear surfaces, not illiquid junk) with median-based stats + outlier guards."""
    latest = load_wiki_latest_prices()
    vols = load_wiki_daily_volumes()
    cutoff = int(time.time()) - days * 86400
    try:
        conn = _history_db()
        try:
            rows = conn.execute("SELECT item_id, ts, high, low FROM hist WHERE ts>=? ORDER BY item_id, ts", (cutoff,)).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []
    series: dict[int, list] = defaultdict(list)
    for iid, ts, high, low in rows:
        mid = ((high or low) + (low or high)) / 2 if (high or low) else None
        if mid and mid > 0:
            series[int(iid)].append((int(ts), float(mid)))
    now = int(time.time())
    out = []
    for iid, pts in series.items():
        if len(pts) < 24:                 # need ~a day of real hourly history
            continue
        dvol = int(vols.get(iid, 0))
        if dvol < min_daily_vol:          # liquid only — excludes erratic junk
            continue
        lr = latest.get(iid)
        if not lr:                        # must still be live on the GE
            continue
        cur = (parse_num(lr.get("high")) or 0) + (parse_num(lr.get("low")) or 0)
        cur_mid = cur / 2 if cur else pts[-1][1]
        if not cur_mid or cur_mid < min_price:
            continue
        mids = sorted(m for _, m in pts)
        med = mids[len(mids) // 2]
        if med <= 0:
            continue
        mn, mx = mids[0], mids[-1]
        # robust volatility: inter-quartile-ish spread vs median (drops single-print spikes)
        lo_q, hi_q = mids[len(mids) // 10], mids[-len(mids) // 10 - 1]
        vol_pct = round((hi_q - lo_q) / med * 100, 1)
        swing_pct = round((mx - mn) / mn * 100, 1) if mn else 0
        def mid_near(target_ts):
            best, bd = None, 1e18
            for ts, m in pts:
                d = abs(ts - target_ts)
                if d < bd:
                    bd, best = d, m
            return best
        m24 = mid_near(now - 86400) or pts[0][1]
        chg_24h = round((cur_mid - m24) / m24 * 100, 1) if m24 else 0
        chg_7d = round((cur_mid - pts[0][1]) / pts[0][1] * 100, 1) if pts[0][1] else 0
        # outlier guard: liquid gear essentially never moves this much in a day —
        # values past these bounds are bad-print noise, not a real market move.
        if abs(chg_24h) > 90 or abs(chg_7d) > 200 or vol_pct > 120:
            continue
        if abs(chg_24h) < 2 and abs(chg_7d) < 4 and vol_pct < 4:
            continue                      # not actually moving — skip the calm majority
        info = get_item_info(iid)
        out.append({
            "item_id": iid, "item": info.get("name") or f"Item {iid}", "slug": item_slug(info.get("name") or ""),
            "icon_url": info.get("icon"), "price": int(cur_mid),
            "change_24h_pct": chg_24h, "change_7d_pct": chg_7d,
            "swing_7d_pct": swing_pct, "volatility_pct": vol_pct, "daily_volume": dvol,
            "_score": vol_pct + abs(chg_24h) * 1.5 + abs(chg_7d) * 0.5,
        })
    out.sort(key=lambda x: x["_score"], reverse=True)
    for x in out:
        x.pop("_score", None)
    return out[:limit]


_INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "market_mood": {"type": "string"},
        "narratives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "sentiment": {"type": "string", "enum": ["bullish", "bearish", "volatile", "neutral"]},
                    "source": {"type": "string", "enum": ["update", "rumor", "market"]},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "summary", "sentiment", "source", "items"],
                "additionalProperties": False,
            },
        },
        "flagged_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "signal": {"type": "string", "enum": ["avoid", "watch", "opportunity"]},
                    "direction": {"type": "string", "enum": ["up", "down", "uncertain"]},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason": {"type": "string"},
                },
                "required": ["item", "signal", "direction", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_mood", "narratives", "flagged_items"],
    "additionalProperties": False,
}

_INSIGHT_SYSTEM = (
    "You are an Old School RuneScape Grand Exchange analyst with deep game knowledge, helping a high-volume "
    "flipper decide which items are risky to flip RIGHT NOW. The user message hands you the full text of recent "
    "OSRS update articles, the items currently moving on price, and the reddit threads discussing them.\n\n"
    "Your most important job is to REASON ABOUT IMPACT: a single update ripples across many items. Think through "
    "the causal chain and name the specific items affected — including ones whose price has NOT moved yet "
    "(anticipatory speculation is exactly what creates flip risk). Examples of the reasoning expected:\n"
    "- A new raid that is crush-combat themed + a proposed crush-BIS armour → crush weapons/gear rise (e.g. "
    "Inquisitor's armour/mace), slash weapons fall (e.g. Scythe of vitur, since it's slash and weak vs the new raid).\n"
    "- A proposed item/armour that synergises with an existing weapon → that weapon rises (e.g. a magic-damage armour "
    "lifting Twinflame staff / Tumeken's shadow).\n"
    "- A new item that does the same job as an existing one → the existing one may fall (substitution).\n"
    "Use the article text + community sentiment as your grounding, plus your knowledge of which OSRS items fill which "
    "combat niche. Be concrete and name real items. Never label a market-moving proposal 'neutral'.\n\n"
    "SCOPE: your PRIMARY signal is the latest 1-2 updates and the reddit/X rumours — reason about the specific gear they "
    "name or imply and name those items even if their price has NOT moved yet (e.g. a 'Shadow rework done but not announced' "
    "rumour → Tumeken's shadow; a crush-themed raid → Scythe of vitur down / Inquisitor's up). The price-mover list in the "
    "user message is only SUPPORTING CONTEXT, not a whitelist — do NOT restrict your analysis to it, and don't waste slots on "
    "low-value seasonal/DMM junk that happens to be moving. Only analyse items the user can TRADE; never output a blocked/"
    "untradeable item. Cover the highest-impact items (~6-12), prioritising the rumour- and update-driven gear. "
    "Name SPECIFIC, individually GE-tradeable items only — never an armour/equipment SET or category (e.g. 'Void knight "
    "equipment', 'Barrows gear', 'Bandos armour'); if a set matters, name the specific tradeable piece (e.g. 'Bandos chestplate')."
)


def _insight_context(flags: list, news: list, movers: list, reddit: list, x: list, blocked: set) -> str:
    """Latest 1-2 update articles + reddit/X rumours + the user's TRADEABLE movers,
    then a tight JSON spec. Blocked items are excluded to focus the read and save tokens."""
    allowed = [m for m in movers if m.get("item_id") not in (blocked or set())]
    upd = [n for n in news if str(n.get("category", "")).lower() in ("game updates", "future updates", "dev blogs")]
    upd = (upd or news)[:2]
    parts = ["=== THE LATEST OSRS UPDATE(S) — full article text, reason about market impact ==="]
    for n in upd:
        body = (n.get("body") or n.get("summary", "") or "")
        parts.append(f"\n--- ({n.get('date','')}) [{n.get('category','')}] {n.get('title','')} ---\n{body[:3000]}")
    if reddit:
        parts.append("\n=== REDDIT RUMOURS / SENTIMENT (r/2007scape, r/OSRSflipping) ===")
        for p in reddit[:12]:
            parts.append(f"- ({p.get('score',0)}^) {p.get('title','')}" + (f" — {p['snippet'][:220]}" if p.get('snippet') else ""))
    if x:
        parts.append("\n=== DEV / OFFICIAL POSTS (X) ===")
        for t in x[:10]:
            parts.append(f"- @{t.get('handle','')}: {t.get('title','')}")
    parts.append("\n=== ITEMS ALREADY MOVING ON PRICE (supporting context only — ALSO reason about the gear named in the updates/rumours above, even if it's NOT in this list) ===")
    flagged_names = {f['item'] for f in flags}
    for f in flags[:12]:
        th = " | ".join(t['title'] for t in f.get('threads', [])[:3])
        parts.append(f"- {f['item']}: {f['price']:,} gp · 24h {f['change_24h_pct']:+}% · vol {f['volatility_pct']}%" + (f" · reddit: {th}" if th else ""))
    for m in allowed[:15]:
        if m['item'] not in flagged_names:
            parts.append(f"- {m['item']}: 24h {m['change_24h_pct']:+}% · vol {m['volatility_pct']}%")
    parts.append(
        "\nRespond with ONLY a JSON object (no markdown, no preamble):\n"
        '{ "market_mood":"1-2 sentences", '
        '"impacts":[{"item":"exact tradeable item name","direction":"up|down|uncertain","magnitude":"high|medium|low","reason":"causal logic","driver":"the update/rumour"}], '
        '"narratives":[{"title":"...","summary":"...","sentiment":"bullish|bearish|volatile|neutral","source":"update|rumor|market","items":["..."]}], '
        '"flagged_items":[{"item":"exact tradeable item name","signal":"avoid|watch|opportunity","direction":"up|down|uncertain","confidence":"low|medium|high","reason":"one line"}] }\n'
        "RULES: anchor on the latest 1-2 updates + the reddit/X rumours above and name the specific gear they affect — the "
        "price-mover list is extra context, NOT the limit, so include rumour-driven gear (e.g. Tumeken's shadow, Scythe of vitur) "
        "whose price hasn't moved yet, and skip low-value seasonal/DMM junk. ONLY items the user can TRADE — never a blocked/"
        "untradeable item; if unsure, leave it out. Give `impacts` for the highest-impact items (~6-12)."
    )
    return "\n".join(parts)


def _parse_json_blob(text: str):
    if not text:
        return None
    t = re.sub(r"```(?:json)?|```", "", text, flags=re.I).strip()
    try:
        return json.loads(t)
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                return None
    return None


def _llm_chat(system: str, user: str, max_tokens: int = 8000) -> dict | None:
    """One JSON chat completion via the configured provider (OpenRouter or
    Anthropic), raw HTTP so no SDK install is needed. Returns {text, model} or
    {error} or None when AI is off."""
    cfg = insight_llm_config()
    provider, key, model = cfg["provider"], cfg["key"], cfg["model"]
    if provider in ("", "off") or not key:
        return None
    try:
        if provider == "openrouter":
            model = model or "anthropic/claude-sonnet-4.5"
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "response_format": {"type": "json_object"}}
            req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                         data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                  "HTTP-Referer": "http://localhost", "X-Title": "OSRS Market Dashboard"})
            raw = json.loads(urllib.request.urlopen(req, timeout=150).read())
            u = raw.get("usage") or {}
            return {"text": raw["choices"][0]["message"]["content"], "model": raw.get("model", model),
                    "tokens_in": u.get("prompt_tokens", 0), "tokens_out": u.get("completion_tokens", 0)}
        else:  # anthropic
            model = model or MARKET_AI_MODEL
            body = {"model": model, "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": user}]}
            req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                         data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                                  "content-type": "application/json"})
            raw = json.loads(urllib.request.urlopen(req, timeout=150).read())
            text = "".join(b.get("text", "") for b in raw.get("content", []) if b.get("type") == "text")
            u = raw.get("usage") or {}
            return {"text": text, "model": raw.get("model", model),
                    "tokens_in": u.get("input_tokens", 0), "tokens_out": u.get("output_tokens", 0)}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read()[:300].decode("utf-8", "replace")
        except Exception:
            pass
        return {"error": f"{provider} HTTP {e.code}: {detail}"}
    except Exception as e:
        return {"error": f"{provider}: {e}"}


def _insight_ai_synthesize(context: str) -> dict | None:
    """Synthesise via the configured LLM. None = AI off; {_error} = call failed."""
    res = _llm_chat(_INSIGHT_SYSTEM, context)
    if res is None:
        return None
    if res.get("error"):
        return {"_error": res["error"]}
    data = _parse_json_blob(res.get("text", ""))
    if not isinstance(data, dict):
        return {"_error": "model did not return parseable JSON"}
    data["_model"] = res.get("model")
    data["_tokens_in"] = res.get("tokens_in", 0)
    data["_tokens_out"] = res.get("tokens_out", 0)
    return data


def _insight_item_info(name: str) -> dict:
    """`get_item_info`, but tolerant of the loose names the AI and news use — falls back
    to the INSIGHT_ALIASES canonical and to charged/inactive variants so icons resolve
    (e.g. 'Scythe of vitur' → 'Scythe of vitur (uncharged)')."""
    info = get_item_info(name)
    if info.get("itemId"):
        return info
    low = str(name or "").strip().lower()
    cand = INSIGHT_ALIASES.get(low)
    if not cand:
        for k, v in INSIGHT_ALIASES.items():
            if re.search(r"\b" + re.escape(k) + r"\b", low):
                cand = v
                break
    if cand:
        info = get_item_info(cand)
        if info.get("itemId"):
            return info
    for suf in (" (uncharged)", " (inactive)", " (empty)"):
        info = get_item_info(name + suf)
        if info.get("itemId"):
            return info
    return get_item_info(name)


def _live_price(iid, latest: dict):
    """Mid of the OSRS Wiki latest high/low for an item id — so rumour-driven items
    that aren't current price-movers still show a real price on their card."""
    lp = latest.get(int(iid)) if iid else None
    if not lp:
        return None
    hi, lo = lp.get("high"), lp.get("low")
    if hi and lo:
        return int((hi + lo) / 2)
    return hi or lo


def _price_changes(item_id, latest: dict | None = None) -> dict:
    """24h/7d % change for ANY item from the local hist DB (no liquidity/'is it moving'
    filter that market_movers applies) — so rumour-driven cards show a real % even when the
    item isn't a current 'mover'. Cheap local SQLite read; returns {} if no history."""
    try:
        iid = int(item_id)
    except Exception:
        return {}
    now = int(time.time())
    try:
        conn = _history_db()
        try:
            rows = conn.execute("SELECT ts, high, low FROM hist WHERE item_id=? AND ts>=? ORDER BY ts",
                                (iid, now - 7 * 86400)).fetchall()
        finally:
            conn.close()
    except Exception:
        rows = []
    pts = []
    for ts, high, low in rows:
        mid = ((high or low) + (low or high)) / 2 if (high or low) else None
        if mid and mid > 0:
            pts.append((int(ts), float(mid)))
    if len(pts) < 2:
        return {}
    latest = latest if latest is not None else load_wiki_latest_prices()
    lr = latest.get(iid) or {}
    cur = (parse_num(lr.get("high")) or 0) + (parse_num(lr.get("low")) or 0)
    cur_mid = cur / 2 if cur else pts[-1][1]
    near = min(pts, key=lambda p: abs(p[0] - (now - 86400)))[1] or pts[0][1]
    out = {}
    if near:
        out["change_24h_pct"] = round((cur_mid - near) / near * 100, 1)
    if pts[0][1]:
        out["change_7d_pct"] = round((cur_mid - pts[0][1]) / pts[0][1] * 100, 1)
    return out


def _resolve_impacts(impacts: list, movers: list) -> list:
    """Attach item_id/slug/icon/blocked + current price move to AI-reasoned impacts."""
    by_name = {m["item"].lower(): m for m in movers}
    blocked_ids = _current_blocked_ids()
    latest = load_wiki_latest_prices(900)
    out = []
    for im in impacts or []:
        name = str(im.get("item") or "").strip()
        if not name:
            continue
        info = _insight_item_info(name)
        iid = info.get("itemId")
        if not iid:
            continue   # drop untradeable categories / unresolvable names (e.g. "Void knight equipment")
        mv = by_name.get(name.lower()) or {}
        chg = mv if mv.get("change_24h_pct") is not None else _price_changes(iid, latest)
        out.append({
            "item": info.get("name") or name, "item_id": iid,
            "slug": item_slug(info.get("name") or name), "icon_url": info.get("icon"),
            "direction": im.get("direction", "uncertain"), "magnitude": im.get("magnitude", "medium"),
            "reason": im.get("reason", ""), "driver": im.get("driver", ""),
            "blocked": bool(iid and int(iid) in blocked_ids),
            "price": mv.get("price") or _live_price(iid, latest),
            "change_24h_pct": chg.get("change_24h_pct"),
            "change_7d_pct": chg.get("change_7d_pct"),
            "volatility_pct": mv.get("volatility_pct"),
        })
    return out


def _resolve_flagged(flagged: list, movers: list, det_by_name: dict | None = None) -> list:
    by_name = {m["item"].lower(): m for m in movers}
    det_by_name = det_by_name or {}
    blocked_ids = _current_blocked_ids()
    latest = load_wiki_latest_prices(900)
    out = []
    for f in flagged or []:
        name = str(f.get("item") or "").strip()
        if not name:
            continue
        info = _insight_item_info(name)
        iid = info.get("itemId")
        if not iid:
            continue   # drop untradeable categories / unresolvable names (e.g. "Void knight equipment")
        mv = by_name.get(name.lower()) or {}
        det = det_by_name.get(name.lower(), {})
        c24 = mv.get("change_24h_pct", f.get("change_24h_pct"))
        chg = {} if c24 is not None else _price_changes(iid, latest)
        out.append({
            **f,
            "item_id": iid,
            "slug": item_slug(info.get("name") or name),
            "icon_url": info.get("icon"),
            "blocked": bool(iid and int(iid) in blocked_ids),
            "price": mv.get("price") or f.get("price") or _live_price(iid, latest),
            "change_24h_pct": c24 if c24 is not None else chg.get("change_24h_pct"),
            "change_7d_pct": mv.get("change_7d_pct", f.get("change_7d_pct")) if c24 is not None else chg.get("change_7d_pct"),
            "volatility_pct": mv.get("volatility_pct", f.get("volatility_pct")),
            "threads": f.get("threads") or det.get("threads") or [],
            "updates": f.get("updates") or det.get("updates") or [],
        })
    return out


# --- Deterministic core: movers-first flags + narratives (works with AI off) ---

def _news_mentions(news: list, item_name: str) -> list:
    terms = _match_terms(item_name)
    return [n for n in news if any(t in n.get("_body_low", "") for t in terms)]


def _market_flags_deterministic(movers: list, news: list) -> list:
    """Start from items actually MOVING (ground truth), then attach the cause:
    update articles that name them + reddit threads discussing them."""
    blocked = _current_blocked_ids()
    cands = []
    for m in movers:
        if m["item_id"] in blocked:
            continue
        rel = _news_mentions(news, m["item"])
        sev = (abs(m["change_24h_pct"]) >= SWING_ALERT_24H or abs(m["change_7d_pct"]) >= SWING_ALERT_7D
               or m["volatility_pct"] >= SWING_ALERT_VOL)
        if not (sev or rel):
            continue
        cands.append((m, rel))
        if len(cands) >= 14:
            break
    flags = []
    for m, rel in cands:
        threads = reddit_search(_reddit_search_term(m["item"]))
        evid = " ".join([n["title"] + " " + (n.get("body", "")[:300]) for n in rel] + [t["title"] for t in threads])
        sent = _sentiment(evid)
        chg = m["change_24h_pct"] or m["change_7d_pct"]
        direction = "up" if chg > 0 else "down" if chg < 0 else "uncertain"
        big = abs(m["change_24h_pct"]) >= 20 or m["volatility_pct"] >= 35
        signal = "avoid" if (sent in ("volatile", "bearish") and big) else "watch"
        nev = (1 if rel else 0) + (1 if threads else 0)
        confidence = "high" if (rel and threads) else "medium" if nev >= 1 else "low"
        bits = [f"{m['change_24h_pct']:+}% in 24h, {m['volatility_pct']}% volatility"]
        if rel:
            bits.append(f"named in '{rel[0]['title']}'")
        if threads:
            bits.append(f"{len(threads)} reddit thread{'s' if len(threads) != 1 else ''} on it (e.g. “{threads[0]['title'][:70]}”)")
        flags.append({
            "item": m["item"], "signal": signal, "direction": direction, "confidence": confidence,
            "reason": " — ".join(bits) + ".", "item_id": m["item_id"], "slug": m["slug"], "icon_url": m["icon_url"],
            "price": m["price"], "change_24h_pct": m["change_24h_pct"], "change_7d_pct": m["change_7d_pct"],
            "volatility_pct": m["volatility_pct"], "threads": threads[:4],
            "updates": [{"title": n["title"], "url": n.get("url", "")} for n in rel[:2]],
        })
    rank = {"avoid": 0, "watch": 1, "opportunity": 2}
    flags.sort(key=lambda f: (rank.get(f["signal"], 3), -abs(f["change_24h_pct"] or 0)))
    return flags


def _narratives_deterministic(news: list, movers: list, reddit: list) -> list:
    narr = []
    for n in news:
        body = n.get("_body_low", "")
        # affected items straight from the article body (named gear) ∪ current movers it mentions
        named = [x["name"] for x in _extract_notable(n.get("body", "") + " " + body[:6000])]
        movhit = [m["item"] for m in movers if any(t in body for t in _match_terms(m["item"]))]
        items = list(dict.fromkeys(named + movhit))
        if not items:
            continue
        narr.append({"title": n["title"], "summary": (n.get("body") or n.get("summary", "") or "")[:260],
                     "sentiment": _sentiment(body + " " + " ".join(items)), "source": "update",
                     "items": items[:8], "url": n.get("url", "")})
    for p in reddit[:10]:
        text = (p.get("title", "") + " " + p.get("snippet", ""))
        items = [x["name"] for x in _extract_notable(text)] + \
                [m["item"] for m in movers if any(t in text.lower() for t in _match_terms(m["item"]))]
        items = list(dict.fromkeys(items))
        if not items and not any(k in text.lower() for k in INSIGHT_KEYWORDS):
            continue
        narr.append({"title": p.get("title", ""), "summary": p.get("snippet", "")[:200],
                     "sentiment": _sentiment(text), "source": "rumor", "items": items[:6], "url": p.get("url", "")})
    return narr[:10]


def _market_mood_deterministic(flags: list, narr: list) -> str:
    drivers = [n["title"] for n in narr if n.get("source") == "update" and n.get("items")]
    if not flags:
        if drivers:
            return f"No unblocked pool items are swinging hard right now, but updates are stirring the market — watch: {drivers[0]}."
        return "Market looks calm — no update- or rumor-driven swings on tradeable pool items right now."
    n = len(flags)
    s = f"{n} pool item{'s' if n != 1 else ''} swinging on updates/rumors"
    if drivers:
        s += f"; biggest driver: {drivers[0]}"
    return s + "."


_swing_alerts_cache: dict = {"at": 0.0, "data": []}


def market_swing_alerts(max_age_s: int = 300) -> list[dict]:
    """Pool items (not already blocked) swinging unnaturally hard — the cheap,
    always-fresh price-only signal behind the Dashboard 'consider temp-blocking'
    alert. No AI/scraping; just local price history. Cached ~5 min so it's free to
    call on every summary load."""
    now = time.time()
    if now - _swing_alerts_cache["at"] < max_age_s:
        return _swing_alerts_cache["data"]
    blocked = _current_blocked_ids()
    out = []
    for m in market_movers(limit=60):
        iid = m.get("item_id")
        if iid in blocked:                       # already handled — don't alert
            continue
        c24, c7, vol = abs(m["change_24h_pct"]), abs(m["change_7d_pct"]), m["volatility_pct"]
        if not (c24 >= SWING_ALERT_24H or c7 >= SWING_ALERT_7D or vol >= SWING_ALERT_VOL):
            continue
        if m["change_24h_pct"] and c24 >= SWING_ALERT_24H:
            reason = f"swung {m['change_24h_pct']:+}% in 24h"
        elif vol >= SWING_ALERT_VOL:
            reason = f"{vol}% volatile ({m['change_7d_pct']:+}% over 7d)"
        else:
            reason = f"{m['change_7d_pct']:+}% over 7d"
        out.append({
            "item_id": iid, "item": m["item"], "slug": m["slug"], "icon_url": m["icon_url"],
            "price": m["price"], "change_24h_pct": m["change_24h_pct"],
            "change_7d_pct": m["change_7d_pct"], "volatility_pct": m["volatility_pct"], "reason": reason,
        })
        if len(out) >= 12:
            break
    _swing_alerts_cache.update(at=now, data=out)
    return out


_update_alert_cache: dict = {"at": 0.0, "data": {}}


def _update_seen_key() -> str:
    try:
        return str(json.loads(UPDATE_SEEN_PATH.read_text(encoding="utf-8")).get("key") or "")
    except Exception:
        return ""


def ack_update_alert(key: str) -> dict:
    """Mark a game update as seen so its dashboard heads-up stops showing."""
    try:
        UPDATE_SEEN_PATH.write_text(json.dumps({"key": str(key or ""), "at": iso(dt.datetime.now())}), encoding="utf-8")
        if _update_alert_cache.get("data"):
            _update_alert_cache["data"]["new"] = False
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def latest_update_alert(max_age_s: int = 1800) -> dict:
    """Newest official OSRS update + the items it names (deterministic, no AI) —
    drives the 'new update' heads-up popup. Cached ~30 min so it's free on every
    summary poll; the `new` flag re-syncs with the dismiss file each call."""
    now = time.time()
    seen = _update_seen_key()
    cached = _update_alert_cache.get("data") or {}
    if cached and now - _update_alert_cache["at"] < max_age_s:
        return {**cached, "new": bool(cached.get("key") and cached["key"] != seen)}
    try:
        news = fetch_osrs_news(8)
    except Exception:
        news = []
    upd = next((n for n in news if str(n.get("category", "")).lower() in ("game updates", "future updates", "dev blogs")),
               news[0] if news else None)
    if not upd:
        _update_alert_cache.update(at=now, data={})
        return {}
    key = re.sub(r"[^a-z0-9]", "", (upd.get("title") or "").lower())[:50]
    body = _article_body(upd.get("url", ""))
    items = [x["name"] for x in _extract_notable((upd.get("body") or "") + " " + body[:6000])][:6]
    data = {"key": key, "title": upd.get("title"), "url": upd.get("url"), "date": upd.get("date"),
            "category": upd.get("category"), "items": items}
    _update_alert_cache.update(at=now, data=data)
    return {**data, "new": bool(key and key != seen)}


_rumour_cache: dict = {"at": 0.0, "data": []}


def high_signal_rumours(max_age_s: int = 480, max_n: int = 6) -> list[dict]:
    """STRICT, deterministic (no AI) detection of reddit/X rumours worth a fresh read —
    a post that names a real TRADEABLE (unblocked) item AND carries a heavy market-moving
    keyword (rework/confirmed/nerf/leak…), OR a very-high-upvote post with ≥2 such keywords.
    Cached ~8 min so it's free to call on every summary poll / signature check."""
    now = time.time()
    # Reuse a non-empty result for the full window; retry an EMPTY result sooner (90s) so a
    # flaky reddit fetch can't blind the detector for 8 min.
    ttl = max_age_s if _rumour_cache["data"] else 90
    if now - _rumour_cache["at"] < ttl:
        return _rumour_cache["data"]
    try:
        reddit = fetch_reddit_signals()
    except Exception:
        reddit = []
    try:
        x = fetch_x_signals()
    except Exception:
        x = []
    blocked = _current_blocked_ids()
    posts = [{"title": p.get("title", ""), "snippet": p.get("snippet", ""), "url": p.get("url", ""),
              "score": p.get("score") or 0, "src": "reddit"} for p in reddit]
    posts += [{"title": t.get("title", ""), "snippet": "", "url": t.get("url", ""),
               "score": 0, "src": "x", "handle": t.get("handle", "")} for t in x]
    out = []
    for p in posts:
        text = (p["title"] + " " + p["snippet"])
        low = text.lower()
        kw = [k for k in RUMOUR_KEYWORDS if k in low]
        items = [it for it in _extract_notable(text)
                 if it.get("item_id") and int(it["item_id"]) not in blocked]   # tradeable + unblocked only
        strong = (items and kw) or (p["score"] >= 200 and len(kw) >= 2)        # STRICT bar
        if not strong:
            continue
        fp = re.sub(r"[^a-z0-9]", "", (p["url"] or p["title"]).lower())[:60]
        out.append({"fp": fp, "title": p["title"], "url": p["url"], "src": p["src"],
                    "score": p["score"], "keywords": kw[:4],
                    "items": [it["name"] for it in items][:5],
                    "item_ids": [int(it["item_id"]) for it in items][:5]})
    # de-dupe by fingerprint, rank by (keyword weight, upvotes)
    seen_fp, uniq = set(), []
    for r in sorted(out, key=lambda r: (-len(r["keywords"]), -(r["score"] or 0))):
        if r["fp"] in seen_fp:
            continue
        seen_fp.add(r["fp"])
        uniq.append(r)
    uniq = uniq[:max_n]
    _rumour_cache.update(at=now, data=uniq)
    return uniq


def _insight_seen_keys() -> set:
    try:
        return set(json.loads(INSIGHT_SEEN_PATH.read_text(encoding="utf-8")).get("keys", []))
    except Exception:
        return set()


def ack_insight_alert(key: str) -> dict:
    """Dismiss an insight heads-up (update or rumour) by key so it stops warning."""
    key = str(key or "")
    try:
        keys = _insight_seen_keys()
        keys.add(key)
        INSIGHT_SEEN_PATH.write_text(json.dumps({"keys": sorted(keys)[-200:]}), encoding="utf-8")
        if _update_alert_cache.get("data", {}).get("key") == key:
            ack_update_alert(key)   # keep the legacy update-seen file in sync
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def insight_alert() -> dict:
    """The single most important NEW thing to warn about on the dashboard — a fresh Jagex
    update (priority) or a strong new rumour — deterministic & free. `new` reflects what the
    user hasn't dismissed yet. 'See impact →' jumps to the (background-prebuilt) AI read."""
    seen = _insight_seen_keys()
    upd = latest_update_alert() or {}
    if upd.get("key") and upd.get("new") and upd["key"] not in seen:
        return {"kind": "update", "key": upd["key"], "title": upd.get("title", ""),
                "url": upd.get("url", ""), "items": upd.get("items", []), "new": True}
    for r in high_signal_rumours():
        if r["fp"] not in seen:
            why = ", ".join(r.get("keywords", [])[:3])
            return {"kind": "rumour", "key": r["fp"], "title": r["title"], "url": r["url"],
                    "items": r["items"], "why": why, "new": True}
    return {}


def _restamp_blocked(result: dict) -> dict:
    """Refresh each flagged item's `blocked` flag against the CURRENT blocklist, drop any
    untradeable/unresolvable entries (no item_id — e.g. a cached 'Void knight equipment'),
    and keep AI impacts tradeable-only — all independent of the cache."""
    blocked_ids = _current_blocked_ids()
    flags = [f for f in ((result or {}).get("flagged_items", []) or []) if f.get("item_id")]
    for f in flags:
        f["blocked"] = bool(int(f["item_id"]) in blocked_ids)
    if result is not None:
        result["flagged_items"] = flags
    if (result or {}).get("impacts"):
        result["impacts"] = [i for i in result["impacts"]
                             if i.get("item_id") and int(i["item_id"]) not in blocked_ids]
    return result


def _insight_input_sig() -> dict:
    """Cheap fingerprint of what should trigger a fresh *AI* analysis: a new Jagex update
    OR a new STRONG rumour (names a tradeable item + heavy keyword). Both come from ~8-min
    cached deterministic detectors, so this costs no tokens. Hard price-swingers are NOT
    here — they're overlaid for free by `_overlay_swing_flags`, so they never burn tokens."""
    upd = latest_update_alert() or {}
    rumours = sorted(r["fp"] for r in high_signal_rumours())
    return {"update": upd.get("key", ""), "rumours": rumours}


def _insight_needs_rebuild(cache: dict) -> bool:
    """Rebuild ONLY on a genuinely new event: the Jagex update key changed, or a STRONG
    rumour fingerprint appeared that the last analysis hadn't seen. Deliberately one-way —
    a rumour *disappearing* (or a transient reddit fetch returning fewer results) never
    triggers, so a flaky fetch can't burn tokens."""
    sig = (cache or {}).get("input_sig") or {}
    upd = latest_update_alert() or {}
    if (sig.get("update") or "") != (upd.get("key") or ""):
        return True
    analyzed = set(sig.get("rumours") or [])
    current = {r["fp"] for r in high_signal_rumours()}
    return bool(current - analyzed)   # a NEW strong rumour the last read didn't cover


# Background auto-build state: one rebuild in flight at a time, rate-limited across events.
_insight_build_state: dict = {"at": 0.0, "running": False}
_insight_build_lock = threading.Lock()
_insight_state_seeded = False


def _seed_insight_state() -> None:
    """Anchor the rate-limit clock to the PERSISTED cache's build time on first use, so a
    server restart resumes where it left off — the 45-min auto-gap survives restarts and the
    AI doesn't re-run on boot unless a genuinely new update/rumour landed AND the gap passed."""
    global _insight_state_seeded
    if _insight_state_seeded:
        return
    _insight_state_seeded = True
    try:
        g = (load_market_insight_cache() or {}).get("generated_at")
        if g:
            ts = parse_time(g).timestamp()
            if ts > _insight_build_state["at"]:
                _insight_build_state["at"] = ts
    except Exception:
        pass


def maybe_autobuild_insight() -> None:
    """Called cheaply on every dashboard summary poll. If a NEW update/rumour changed the
    signature, the AI is configured, and we haven't rebuilt within INSIGHT_AUTO_GAP_S, kick
    a background Sonnet rebuild so the read is ready the moment the user clicks through.
    Deduped (signature) + rate-limited (gap) + single-flight — so token spend tracks real
    events, never page views or a flurry of rumours."""
    cfg = insight_llm_config()
    if cfg["provider"] in ("", "off") or not cfg["key"]:
        return                                   # AI off → nothing to pre-build
    _seed_insight_state()                        # resume the rate-limit clock across restarts
    try:
        cache = load_market_insight_cache()
        if not _insight_needs_rebuild(cache):
            return                               # nothing genuinely new since the last analysis
        now = time.time()
        with _insight_build_lock:
            if _insight_build_state["running"] or (now - _insight_build_state["at"]) < INSIGHT_AUTO_GAP_S:
                return                           # already building, or rebuilt too recently
            _insight_build_state.update(running=True, at=now)

        def _run():
            try:
                build_market_insight(force=True)
            except Exception:
                pass
            finally:
                _insight_build_state["running"] = False

        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        _insight_build_state["running"] = False


def _overlay_swing_flags(result: dict) -> dict:
    """Ensure the Insight 'items to watch' always includes every current hard price-
    swinger (the exact set behind the Dashboard banner), even when serving a cached AI
    read. Price-only, no AI, no scraping (`market_swing_alerts` is 5-min cached) — so
    insane swings auto-appear on tab-in for free and stay consistent with the banner."""
    try:
        swings = market_swing_alerts()
    except Exception:
        return result
    flags = (result or {}).get("flagged_items") or []
    have = {f.get("item_id") for f in flags if f.get("item_id")}
    blocked = _current_blocked_ids()
    for s in swings:
        iid = s.get("item_id")
        if not iid or iid in have or int(iid) in blocked:
            continue
        c24 = s.get("change_24h_pct") or 0
        big = abs(c24) >= 20 or (s.get("volatility_pct") or 0) >= 35
        flags.append({
            "item": s.get("item"), "signal": "avoid" if big else "watch",
            "direction": "up" if c24 > 0 else "down" if c24 < 0 else "uncertain",
            "confidence": "medium", "blocked": False,
            "reason": (s.get("reason") or "") + " — sharp price swing; sit it out until it settles.",
            "item_id": iid, "slug": s.get("slug"), "icon_url": s.get("icon_url"),
            "price": s.get("price"), "change_24h_pct": s.get("change_24h_pct"),
            "change_7d_pct": s.get("change_7d_pct"), "volatility_pct": s.get("volatility_pct"),
            "threads": [], "updates": [],
        })
        have.add(iid)
    result["flagged_items"] = flags
    return result


def _overlay_rumour_flags(result: dict) -> dict:
    """Guarantee every STRONG rumour the dashboard banner flags also appears as a Watch-list
    card — same consistency guarantee as `_overlay_swing_flags` for price swingers. Without
    this the deterministic banner ('Voidwaker spike') and the AI cards can disagree, because
    the AI curates its own list and may drop a rumour item it judged lower-impact. Free: uses
    the 8-min-cached `high_signal_rumours()`, no AI/tokens."""
    try:
        rumours = high_signal_rumours()
    except Exception:
        return result
    if not rumours:
        return result
    flags = (result or {}).get("flagged_items") or []
    have = {f.get("item_id") for f in flags if f.get("item_id")}
    blocked = _current_blocked_ids()
    latest = load_wiki_latest_prices()
    for r in rumours:
        for iid in r.get("item_ids", []):
            if not iid or iid in have or int(iid) in blocked:
                continue
            info = get_item_info(iid)
            ch = _price_changes(iid, latest)
            flags.append({
                "item": info.get("name") or f"Item {iid}", "signal": "watch",
                "direction": "uncertain", "confidence": "medium", "blocked": False,
                "reason": "Community rumour — " + (r.get("title") or "")[:140] + ". Watch for speculative swings.",
                "item_id": iid, "slug": item_slug(info.get("name") or ""), "icon_url": info.get("icon"),
                "price": _live_price(iid, latest),
                "change_24h_pct": ch.get("change_24h_pct"), "change_7d_pct": ch.get("change_7d_pct"),
                "volatility_pct": None, "updates": [],
                "threads": [{"title": r.get("title", ""), "url": r.get("url", "")}] if r.get("url") else [],
            })
            have.add(iid)
    result["flagged_items"] = flags
    return result


def _model_price(model_id: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens — Anthropic list prices; OpenRouter ~matches.
    Used only for a friendly ESTIMATE shown to the user, not billing."""
    m = (model_id or "").lower()
    if "haiku" in m:
        return (1.0, 5.0)
    if "sonnet" in m:
        return (3.0, 15.0)
    if "opus" in m:
        return (5.0, 25.0)
    if "fable" in m:
        return (10.0, 50.0)
    return (3.0, 15.0)   # default to Sonnet-tier


def _est_cost(model_id: str, tin: int, tout: int) -> float:
    pin, pout = _model_price(model_id)
    return round(((tin or 0) * pin + (tout or 0) * pout) / 1_000_000, 5)


def _insight_trigger_reason(cache: dict) -> tuple[str, str, list]:
    """Why a (non-manual) rebuild is happening — for the run log. (kind, title, items)."""
    sig = (cache or {}).get("input_sig") or {}
    upd = latest_update_alert() or {}
    if (sig.get("update") or "") != (upd.get("key") or ""):
        return ("update", upd.get("title") or "new OSRS update", upd.get("items", []))
    analyzed = set(sig.get("rumours") or [])
    for r in high_signal_rumours():
        if r["fp"] not in analyzed:
            return ("rumour", r.get("title") or "new rumour", r.get("items", []))
    return ("event", "new market signal", [])


def _log_insight_run(kind: str, title: str, items: list, model: str, tin: int, tout: int) -> None:
    """Append one AI-run entry (newest first, capped) with its estimated cost."""
    try:
        try:
            runs = json.loads(INSIGHT_RUNLOG_PATH.read_text(encoding="utf-8")).get("runs", [])
        except Exception:
            runs = []
        runs.insert(0, {
            "at": dt.datetime.now().isoformat(timespec="seconds"),
            "kind": kind, "title": (title or "")[:120], "items": (items or [])[:5],
            "model": model, "tokens_in": tin or 0, "tokens_out": tout or 0,
            "cost_usd": _est_cost(model, tin, tout),
        })
        INSIGHT_RUNLOG_PATH.write_text(json.dumps({"runs": runs[:60]}, indent=2), encoding="utf-8")
    except Exception:
        pass


def insight_runs() -> dict:
    """Run log + cost roll-ups for the Insight tab (today / 30-day / total)."""
    try:
        runs = json.loads(INSIGHT_RUNLOG_PATH.read_text(encoding="utf-8")).get("runs", [])
    except Exception:
        runs = []
    today = dt.date.today().isoformat()
    month_ago = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    cost_today = sum(r.get("cost_usd", 0) for r in runs if str(r.get("at", ""))[:10] == today)
    cost_30d = sum(r.get("cost_usd", 0) for r in runs if str(r.get("at", ""))[:10] >= month_ago)
    return {
        "runs": runs[:20],
        "count": len(runs),
        "cost_today": round(cost_today, 4),
        "cost_30d": round(cost_30d, 4),
        "cost_total": round(sum(r.get("cost_usd", 0) for r in runs), 4),
        "runs_today": sum(1 for r in runs if str(r.get("at", ""))[:10] == today),
    }


def _age_hint(generated_at: str | None) -> str:
    """A gentle nudge when an event-driven read has been sitting a while with nothing new —
    so a lingering snapshot of a now-dated rumour is obvious (we removed time-based rebuilds)."""
    try:
        hrs = (dt.datetime.now() - parse_time(generated_at)).total_seconds() / 3600
    except Exception:
        return ""
    if hrs >= 18:
        return f"This read is {int(round(hrs))}h old — no new update or rumour since. Hit Refresh to re-check."
    return ""


def _enrich_changes(result: dict) -> dict:
    """Fill in 24h/7d % for any flagged/impact item still missing it (rumour-driven items
    that aren't movers) from local history — so the card shows a real % now, no rebuild."""
    try:
        latest = load_wiki_latest_prices()
        for key in ("flagged_items", "impacts"):
            for it in (result or {}).get(key) or []:
                if it.get("change_24h_pct") is None and it.get("item_id"):
                    c = _price_changes(it["item_id"], latest)
                    if c:
                        it["change_24h_pct"] = c.get("change_24h_pct")
                        if it.get("change_7d_pct") is None:
                            it["change_7d_pct"] = c.get("change_7d_pct")
    except Exception:
        pass
    return result


def _serve_insight(result: dict, building: bool = False) -> dict:
    """Final shaping for every served read: overlay live swingers, re-stamp blocked/untradeable,
    fill missing price-changes, attach the run log + cost roll-up + age hint, and build flag."""
    out = _enrich_changes(_overlay_rumour_flags(_overlay_swing_flags(_restamp_blocked(result))))
    if building:
        out["building"] = True
    out["runs"] = insight_runs()
    out["age_hint"] = _age_hint(out.get("generated_at"))
    return out


def build_market_insight(force: bool = False, reason: str | None = None) -> dict:
    """EVENT-DRIVEN read. Serves the remembered AI analysis for free (no tokens, forever)
    unless: (a) the user forces a refresh, or (b) a NEW update / strong rumour changed the
    signature and we're past the auto-gap rate-limit. There is NO time-based staleness — an
    idle market never spends tokens. New hard price-swingers are overlaid for free on every
    read. A `building` flag is set while a background pre-build is in flight."""
    _seed_insight_state()                        # resume the rate-limit clock across restarts
    cache = load_market_insight_cache()
    if not force and cache.get("generated_at"):
        try:
            needs = _insight_needs_rebuild(cache)
            building = _insight_build_state["running"]
            recent = (time.time() - _insight_build_state["at"]) < INSIGHT_AUTO_GAP_S
            # Reuse the cache (free) when nothing new, OR a pre-build is already handling the
            # new event, OR we rebuilt within the gap (rate-limit). Overlay live swingers.
            if not needs or building or recent:
                return _serve_insight(cache, building=building)
            # New event + past the gap + nothing in flight → fall through and rebuild now.
        except Exception:
            pass
    # Capture WHY we're (re)building, for the run log.
    if reason == "manual":
        log_kind, log_title, log_items = "manual", "Manual refresh", []
    else:
        log_kind, log_title, log_items = _insight_trigger_reason(cache)
    news = fetch_osrs_news_deep(8)
    reddit = fetch_reddit_signals()
    movers = market_movers(60)
    x = fetch_x_signals()
    blocked = _current_blocked_ids()
    # --- deterministic core (always produces a real read, no AI required) ---
    det_flags = _market_flags_deterministic(movers, news)
    det_narr = _narratives_deterministic(news, movers, reddit)
    det_by_name = {f["item"].lower(): f for f in det_flags}
    # --- optional AI enrichment: only the latest updates + reddit/X, tradeable items only ---
    ai = _insight_ai_synthesize(_insight_context(det_flags, news, movers, reddit, x, blocked))
    ai_error = (ai or {}).get("_error") if isinstance(ai, dict) else None
    used_ai = bool(ai and not ai_error and ai.get("flagged_items") is not None)
    if used_ai:
        impacts = [i for i in _resolve_impacts(ai.get("impacts", []), movers) if not i.get("blocked")]  # tradeable only
        # Merge the deterministic price-swing flags the AI didn't mention, so the
        # Dashboard "swinging hard" alert and this tab stay consistent (a raw -40%
        # swing with no update narrative still belongs in the watch list).
        ai_flags = _resolve_flagged(ai.get("flagged_items", []), movers, det_by_name)
        ai_ids = {f.get("item_id") for f in ai_flags if f.get("item_id")}
        extra = [f for f in _resolve_flagged(det_flags, movers, det_by_name) if f.get("item_id") not in ai_ids]
        result = {
            "market_mood": ai.get("market_mood", ""),
            "narratives": ai.get("narratives") or det_narr,
            "impacts": impacts,
            "flagged_items": ai_flags + extra,
            "ai": True, "model": ai.get("_model"), "provider": insight_llm_config()["provider"],
        }
        _log_insight_run(log_kind, log_title, log_items, ai.get("_model"),
                         ai.get("_tokens_in"), ai.get("_tokens_out"))
    else:
        result = {
            "market_mood": _market_mood_deterministic(det_flags, det_narr),
            "narratives": det_narr,
            "impacts": [],   # causal impact reasoning is AI-only
            "flagged_items": _resolve_flagged(det_flags, movers, det_by_name),
            "ai": False, "ai_error": ai_error,
        }
    news_pub = [{k: v for k, v in n.items() if not k.startswith("_")} for n in news]  # drop _body_low
    result.update({
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "input_sig": _insight_input_sig(),   # what this analysis was based on (drives smart re-runs)
        "sources": {"news": news_pub, "reddit": reddit[:20], "movers": movers, "x": x},
        "counts": {"news": len(news), "reddit": len(reddit), "movers": len(movers),
                   "x": len(x), "flagged": len(result["flagged_items"])},
    })
    _save_market_insight_cache(result)
    _insight_build_state["at"] = time.time()   # any build (manual or auto) resets the auto-gap
    return _serve_insight(result)


def load_market_insight_cache() -> dict:
    try:
        return json.loads(MARKET_INSIGHT_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_market_insight_cache(data: dict) -> None:
    try:
        MARKET_INSIGHT_CACHE_PATH.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def get_summary(start_param: str | None = None, end_param: str | None = None, session_start: str | None = None) -> dict:
    csv_path, sources = find_latest_csv()
    csv_data = load_csv_metrics(csv_path)
    rows = csv_data["rows"]
    snapshot_timeframes()   # keep the timeframe log current whenever the dashboard loads
    try:
        reconcile_temp_blocks()  # release expired temp blocks on every dashboard poll
    except Exception:
        pass
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
    live_est = build_live_unrealized_estimate(rows)
    try:
        snapshot_minprofit()  # log min-profit settings continuously so flips can be attributed
    except Exception:
        pass
    try:
        record_slot_episodes(live_est.get("slots", []))  # log real GE-slot occupancy over time
    except Exception:
        pass
    try:
        maybe_autobuild_insight()  # background Sonnet pre-build IFF a new update/strong rumour landed
    except Exception:
        pass
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
        "goal_tracker": compute_goal_tracker(csv_data, config),
        "live_unrealized_estimate": live_est,
        "sell_competition": _sell_competition_from_slots(live_est.get("slots", [])),
        "stale_offers": _stale_offers_from_slots(live_est.get("slots", [])),
        "loss_radar": loss_radar(live_est.get("slots", [])),   # open positions matching the big-loss profile
        "market_alerts": market_swing_alerts(),
        "update_alert": latest_update_alert(),
        "insight_alert": insight_alert(),                       # new update OR strong rumour (free, deterministic)
        "insight_generated_at": (load_market_insight_cache() or {}).get("generated_at"),
        "insight_building": _insight_build_state["running"],
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


def build_flip_detail(item_name: str, sell_ms: str | None = None, profit: str | None = None, account: str | None = None) -> dict:
    """Deep dive on a single flip: the trade's economics, a price chart of the
    item over the hold window with the buy/sell points marked, how this flip
    compares to your history of the item, and the current market."""
    rows = load_rows()
    needle = (item_name or "").lower().strip()
    if not needle:
        return {"error": "item required"}
    cands = [r for r in rows if r.get("Item", "").lower().strip() == needle
             and r.get("Status") in ("FINISHED", "SELLING") and r.get("_sell")]
    if account:
        acc_l = account.lower().strip()
        narrowed = [r for r in cands if (r.get("Account") or "").lower().strip() == acc_l]
        cands = narrowed or cands
    if not cands:
        return {"error": f"No flip found for {item_name}", "item": item_name}

    target = None
    if sell_ms:
        try:
            target = dt.datetime.fromtimestamp(int(float(sell_ms)) / 1000)
        except Exception:
            target = None
    pnum = None
    if profit not in (None, ""):
        try:
            pnum = int(float(profit))
        except Exception:
            pnum = None

    def score(r):
        s = 0.0
        if target:
            s += abs((r["_sell"] - target).total_seconds())
        if pnum is not None:
            s += abs(r.get("_profit", 0) - pnum) * 0.001  # tie-breaker only
        return s

    r = min(cands, key=score) if (target or pnum is not None) else max(cands, key=lambda x: x["_sell"])

    qty = int(r.get("_sold") or r.get("_bought") or 0)
    avg_buy = r.get("_avg_buy") or 0
    avg_sell = r.get("_avg_sell") or 0
    p = r.get("_profit", 0)
    dur_h = r.get("_dur_h")
    buy_dt, sell_dt = r.get("_buy"), r.get("_sell")
    invested = int(avg_buy * qty) if (avg_buy and qty) else 0
    item_id = int(r.get("_item_id") or 0) or None
    info = get_item_info(item_id or r.get("Item"))
    if not item_id:
        item_id = info.get("itemId")
    flip = {
        "item": r.get("Item"), "item_id": item_id, "slug": item_slug(r.get("Item", "")),
        "icon_url": r.get("icon_url") or info.get("icon"),
        "account": r.get("Account"), "status": r.get("Status"),
        "in_progress": r.get("Status") == "SELLING",
        "members": bool(info.get("members")), "buy_limit": info.get("limit"),
        "bought": r.get("_bought"), "sold": r.get("_sold"), "qty": qty,
        "avg_buy": avg_buy, "avg_sell": avg_sell, "tax": r.get("_tax"),
        "profit": p, "profit_ea": r.get("_profit_ea"),
        "margin_ea": (avg_sell - avg_buy) if (avg_sell and avg_buy) else None,
        "invested": invested, "return_value": invested + p if invested else None,
        "roi_pct": round(p / invested * 100, 2) if invested else None,
        "duration_h": dur_h, "gp_per_hour": round(p / dur_h) if (dur_h and dur_h > 0) else None,
        "buy_time": iso(buy_dt), "sell_time": iso(sell_dt),
    }

    # Price chart over the hold window (granularity scaled to the flip's age/span).
    chart = {"points": [], "timestep": None}
    if item_id and buy_dt and sell_dt:
        now = dt.datetime.now()
        age_h = (now - buy_dt).total_seconds() / 3600
        ts = "5m" if age_h <= 26 else "1h" if age_h <= 14 * 24 else "6h" if age_h <= 90 * 24 else "24h"
        try:
            data = wiki_get_json("/timeseries", {"id": item_id, "timestep": ts}).get("data") or []
        except Exception:
            data = []
        span = max((sell_dt - buy_dt).total_seconds(), 1)
        pad = max(span * 0.6, {"5m": 3600, "1h": 6 * 3600, "6h": 2 * 86400, "24h": 7 * 86400}[ts])
        lo = (buy_dt - dt.timedelta(seconds=pad)).timestamp()
        hi = (sell_dt + dt.timedelta(seconds=pad)).timestamp()
        pts = []
        for d in data:
            tsec = parse_num(d.get("timestamp"))
            if not tsec or tsec < lo or tsec > hi:
                continue
            ph, pl = parse_num(d.get("avgHighPrice")), parse_num(d.get("avgLowPrice"))
            mid = (ph + pl) / 2 if (ph and pl) else (ph or pl)
            if not mid:
                continue
            pts.append({"t": int(tsec * 1000), "high": ph or None, "low": pl or None, "mid": round(mid),
                        "hv": int(parse_num(d.get("highPriceVolume"))), "lv": int(parse_num(d.get("lowPriceVolume")))})
        chart = {
            "points": pts[:250], "timestep": ts,
            "buy": {"t": int(buy_dt.timestamp() * 1000), "price": avg_buy},
            "sell": {"t": int(sell_dt.timestamp() * 1000), "price": avg_sell},
        }

    # How this flip compares to your full history of the item.
    ctx = {}
    try:
        det = get_item_detail(r.get("Item"), period_name="all_time", use_all_accounts=True)
        if not det.get("error"):
            profits = sorted(f.get("profit", 0) for f in det.get("flips", []))
            pct = round(sum(1 for x in profits if x <= p) / len(profits) * 100) if profits else None
            ctx = {
                "n": det.get("n"), "total_profit": det.get("profit"), "avg_profit": det.get("avg_profit"),
                "win_rate": det.get("win_rate"), "avg_buy": det.get("avg_buy"), "avg_sell": det.get("avg_sell"),
                "med_dur_h": det.get("med_dur_h"), "best_hour": det.get("best_hour"),
                "profit_percentile": pct,
                "vs_avg_buy": (avg_buy - det["avg_buy"]) if (det.get("avg_buy") and avg_buy) else None,
                "vs_avg_sell": (avg_sell - det["avg_sell"]) if (det.get("avg_sell") and avg_sell) else None,
                "vs_avg_profit": (p - det["avg_profit"]) if det.get("avg_profit") is not None else None,
            }
    except Exception:
        ctx = {}

    # Current market (what it would cost/return right now).
    market = {}
    try:
        latest_all = wiki_get_json("/latest").get("data", {})
        l = latest_all.get(str(item_id), {}) or {}
        h, lo2 = parse_num(l.get("high")), parse_num(l.get("low"))
        market = {"instant_buy": h or None, "instant_sell": lo2 or None,
                  "mid": round((h + lo2) / 2) if (h and lo2) else (h or lo2 or None),
                  "sell_vs_now": (avg_sell - lo2) if (avg_sell and lo2) else None}
    except Exception:
        market = {}

    return {"flip": flip, "chart": chart, "item_context": ctx, "market": market,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds")}


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


_CSV_API_EXPORT_LOCK = threading.Lock()
_LAST_EXPORT_AT = 0.0           # monotonic time of the last actual FC export call
EXPORT_THROTTLE_S = 3.5         # global min interval between FC API calls (tunable)


def run_copilot_api_csv_export(test: bool = False, force: bool = False) -> dict:
    """Single-flight + globally throttled wrapper around the delta CSV export.

    Every dashboard / session-HUD window drives event-based syncing, so export
    requests pile up — and each one spawns a subprocess that hits Flipping
    Copilot's API and rewrites flips.csv. To protect FC (and our CPU) the FC call
    is rate-limited *globally* here, independent of how many windows/clients are
    open: a request that arrives within EXPORT_THROTTLE_S of the last export is a
    cheap no-op ("throttled") — the delta exporter already grabs ALL new flips on
    the next run, so nothing is lost; clients just re-read the fresh CSV. A
    non-blocking lock also makes a concurrent overlapping call a safe no-op.
    `force=True` (manual "sync now") bypasses the throttle but not the lock.
    """
    global _LAST_EXPORT_AT
    now = time.monotonic()
    if not test and not force and (now - _LAST_EXPORT_AT) < EXPORT_THROTTLE_S:
        return {"ok": True, "skipped": "throttled", "since_last_s": round(now - _LAST_EXPORT_AT, 2)}
    if not _CSV_API_EXPORT_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "in_progress", "note": "another export is already running"}
    try:
        # Re-check after acquiring the lock: another window's export may have just
        # finished while we waited, making this FC call redundant.
        now = time.monotonic()
        if not test and not force and (now - _LAST_EXPORT_AT) < EXPORT_THROTTLE_S:
            return {"ok": True, "skipped": "throttled", "since_last_s": round(now - _LAST_EXPORT_AT, 2)}
        result = _run_copilot_api_csv_export_impl(test=test)
        _LAST_EXPORT_AT = time.monotonic()
        return result
    finally:
        _CSV_API_EXPORT_LOCK.release()


def _run_copilot_api_csv_export_impl(test: bool = False) -> dict:
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
        elif path == "/api/copilot/activity":
            self.send_json(copilot_activity_signal())
        elif path == "/api/bankroll-config":
            self.send_json(load_bankroll_config())
        elif path == "/api/blocklist":
            self.send_json(get_blocklist())
        elif path == "/api/profiles":
            self.send_json(list_copilot_profiles())
        elif path == "/api/profiles/blocked-sets":
            self.send_json(copilot_profile_blocked_sets())
        elif path == "/api/account-settings":
            self.send_json(build_account_settings())
        elif path == "/api/account-throughput":
            self.send_json(account_throughput())
        elif path == "/api/summary":
            summary = get_summary(params.get("start", [None])[0], params.get("end", [None])[0], params.get("session_start", [None])[0])
            summary["requested_period"] = params.get("period", ["today"])[0]
            self.send_json(summary)
        elif path == "/api/research":
            self.send_json(build_item_research())
        elif path == "/api/market-insight":
            # cached read; rebuilds only if stale (respects the TTL)
            self.send_json(build_market_insight(force=False))
        elif path == "/api/insight-llm-config":
            self.send_json(insight_llm_public())
        elif path == "/api/flip-finder":
            self.send_json(build_flip_finder())
        elif path == "/api/flip-finder/sparks":
            raw_ids = params.get("ids", [""])[0]
            ids = [int(parse_num(x)) for x in raw_ids.split(",") if parse_num(x)][:400]
            self.send_json({"sparks": {str(k): v for k, v in fetch_history_sparks(ids).items()}})
        elif path == "/api/flip-finder/backtest":
            sample = int(parse_num(params.get("sample", ["36"])[0])) or 36
            ts_raw = params.get("timesteps", ["1h,6h"])[0]
            tsv = tuple(t for t in (s.strip() for s in ts_raw.split(",")) if t in BACKTEST_CONFIGS) or ("1h", "6h")
            try:
                self.send_json(run_flip_backtest(sample_size=max(6, min(60, sample)), timesteps=tsv))
            except Exception as exc:
                self.send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/flip-finder/calibration":
            self.send_json(calibration_status())
        elif path == "/api/flip-finder/self-tune":
            try:
                self.send_json(self_tune_analyze())
            except Exception as exc:
                self.send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/portfolio":
            self.send_json(build_portfolio_view())
        elif path == "/api/stats":
            rng = params.get("range", [None])[0]
            if rng:
                if rng == "custom":
                    b = custom_bounds(params.get("start", [None])[0], params.get("end", [None])[0])
                else:
                    b = period_bounds().get(rng)
                self.send_json(build_stats_page(bounds=b))
            else:
                dval = params.get("days", ["all"])[0]
                days = 0 if str(dval).lower() == "all" else (int(parse_num(dval)) or 0)
                self.send_json(build_stats_page(days))
        elif path == "/api/attention":
            self.send_json(build_attention(_attn_include_empty(params)))
        elif path == "/api/attention/next":
            att = build_attention(_attn_include_empty(params))
            nxt = next((a for a in att.get("accounts", []) if a.get("needs_attention") and a.get("name")), None)
            self.send_json({"next": (nxt or {}).get("name", ""), "since": (nxt or {}).get("attn_since_iso"), "ready_slots": (nxt or {}).get("ready_slots", 0)})
        elif path == "/api/attention/queue":
            att = build_attention(_attn_include_empty(params))
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
        elif path == "/api/flip":
            self.send_json(build_flip_detail(
                params.get("item", [""])[0],
                sell_ms=params.get("sell_ms", [None])[0],
                profit=params.get("profit", [None])[0],
                account=params.get("account", [None])[0],
            ))
        elif path == "/api/export/analysis-context":
            period_name = params.get("period", ["today"])[0]
            self.send_json(build_analysis_context(get_summary(), period_name))
        elif path.startswith("/item/") or path.startswith("/flip/"):
            # item-page / flip-detail routes: serve SPA shell; the frontend reads the
            # path and fetches /api/wiki/item/{slug} or /api/flip respectively.
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
        elif path in ("/session", "/session.html", "/hud"):
            # Standalone gamified live session HUD (separate ~1000x820 window).
            page = ROOT / "session.html"
            if not page.exists():
                self.send_error(404, "session.html not found")
                return
            content = page.read_bytes()
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
            result = run_copilot_api_csv_export(
                test=params.get("test", ["0"])[0] == "1",
                force=params.get("force", ["0"])[0] == "1",
            )
            self.send_json(result, status=200 if result.get("ok") else 500)
            return
        if path == "/api/flip-finder/calibration/apply":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                self.send_json(apply_calibration(body))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
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
        if path == "/api/market-insight/refresh":
            try:
                result = build_market_insight(force=True, reason="manual")
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=500)
                return
            self.send_json(result)
            return
        if path == "/api/update-alert/ack":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = ack_update_alert(body.get("key"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result, status=500 if result.get("error") else 200)
            return
        if path == "/api/insight-alert/ack":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = ack_insight_alert(body.get("key"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result, status=500 if result.get("error") else 200)
            return
        if path == "/api/insight-llm-config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = save_insight_llm_config(body.get("provider"), body.get("model"),
                                                 body.get("key"), bool(body.get("clear_key")))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result, status=500 if result.get("error") else 200)
            return
        if path == "/api/blocklist/temp-block":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                result = temp_block_item(incoming.get("item"), incoming.get("item_id"), incoming.get("minutes"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result, status=500 if result.get("error") else 200)
            return
        if path == "/api/blocklist/temp-cancel":
            try:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
                result = cancel_temp_block(incoming.get("item_id"), incoming.get("item"))
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
                return
            self.send_json(result, status=500 if result.get("error") else 200)
            return
        if path == "/api/account-settings/membership":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                result = set_account_membership(body.get("account"), body.get("mode"), body.get("expires"))
                self.send_json(result, status=200 if result.get("ok") else 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, status=400)
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
    threading.Thread(target=_temp_block_reconcile_loop, daemon=True).start()
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

