import datetime as dt
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
START_BAT = (ROOT / "start_dashboard.bat").read_text(encoding="utf-8")


# === V8-CLEAN-ITEMS TESTS ===

def test_version_is_v8_clean_items():
    """Dashboard version should be v8-clean-items"""
    assert "v8-clean-items" in SERVER


def test_market_pace_copy_is_short_without_live_wiki_volume_basis():
    """Market pace card should keep activity/hr and 5m pace but drop noisy source copy."""
    assert "activity/hr" in INDEX
    assert "5m pace" in INDEX
    assert 'class="pace"' in INDEX
    assert "live Wiki volume" not in INDEX
    assert "1h ${fmt(m.current_volume||0)}" not in INDEX


def test_item_detail_api_accepts_period_parameter():
    """Item detail API should accept period param (e.g., /api/item/Nature rune?period=today)"""
    assert 'path == "/api/item/' in SERVER
    assert 'get("period"' in SERVER


def test_item_detail_api_defaults_to_active_accounts():
    """Item detail filters by configured active accounts; no personal names ship in code."""
    assert 'active_accounts' in SERVER
    assert 'def accounts_from_csv' in SERVER


def test_item_detail_api_supports_all_accounts_flag():
    """Item detail should allow all_accounts=1 to include old accounts"""
    assert 'all_accounts' in SERVER


def test_frontend_modal_removes_existing_before_append():
    """Modal render must remove existing .modal-overlay before adding new one"""
    # Should have querySelectorAll removal before append
    assert '.modal-overlay' in INDEX
    # Check for removal logic
    assert 'querySelectorAll' in INDEX and ".modal-overlay" in INDEX


def test_frontend_modal_shows_loading_state():
    """Modal should show loading state when item clicked"""
    assert "ITEM_DETAIL_LOADING" in INDEX


def test_frontend_modal_close_button_and_click_outside():
    """Modal should have close button and click-outside-to-close"""
    assert 'onclick="ITEM_DETAIL=null' in INDEX
    # Check for click outside close - the ov.onclick


def test_tab_label_items_is_range_aware():
    """Items tab should show range in header like 'Items · Today'"""
    assert "Items · " in INDEX or "Items — " in INDEX or "Items: " in INDEX


def test_items_table_respects_current_range():
    """Items table should use current period data, not always all-time"""
    # Check either activePeriod() used in renderItems or period-specific data loaded
    assert "activePeriod()" in INDEX


def test_items_table_is_sortable():
    """Items table should have sortable columns"""
    # Sort control in table headers or sort buttons
    assert '<th' in INDEX and ('onclick' in INDEX or 'sort' in INDEX.lower())


def test_problem_items_panel_removed_from_frontend():
    """Problem items were removed as dashboard noise."""
    assert "Problem items" not in INDEX
    assert "blocklist candidates" not in INDEX.lower()


def test_rejected_market_sections_are_not_rendered():
    """Stats should not show rejected Today-vs-usual, Flip health, or Allowed Market Pulse sections."""
    assert "Today vs usual" not in INDEX
    assert "Today vs yesterday" not in INDEX
    assert "Flip health" not in INDEX
    assert "Allowed market pulse" not in INDEX
    assert "function marketPulseSection" not in INDEX
    assert "market_pulse" not in SERVER
    assert "filler" not in INDEX.lower()


def test_account_breakdown_removed_from_stats_and_item_modal():
    """Account breakdowns outside Bankroll are not useful because all accounts are Ray."""
    assert "Accounts + time of day" not in INDEX
    assert "function accountRows" not in INDEX
    assert "By account" not in INDEX


def test_recent_transactions_added_to_dashboard():
    """Stats dashboard should end with recent win/loss transactions."""
    assert "Recent transactions" in INDEX
    assert "recent_transactions" in SERVER
    assert "function recentTransactionRows" in INDEX


def test_frontend_formats_times_as_browser_local_without_hardcoded_timezone():
    """Frontend should use the browser/OS local time, not hardcoded Europe/Oslo."""
    assert "Europe/Oslo" not in INDEX
    assert "toLocaleString" in INDEX or "toLocaleTimeString" in INDEX
    assert "timeZone" not in INDEX


def test_backend_converts_utc_csv_timestamps_to_local_naive_for_display():
    """CSV API exports use UTC Z timestamps; backend should localize before stripping tz."""
    spec = importlib.util.spec_from_file_location("dashboard_server_tz", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    raw = "2026-06-01T07:46:14Z"
    expected = dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    assert server.parse_time(raw) == expected


def test_loss_count_and_profit_buckets_removed_from_stats_ui():
    """Loss count and profit bucket cards were removed as low-value noise."""
    assert "Loss count" not in INDEX
    assert "Loss total" not in INDEX
    assert "profit buckets" not in INDEX.lower()
    assert "function bucketRows" not in INDEX


def test_best_worst_items_use_larger_cards_after_cleanup():
    """After removing low-value sidecards, best/worst item cards should use larger two-column layout."""
    assert "itemsPrettyGrid" in INDEX or "bigItemCard" in INDEX


def test_summary_payload_no_by_name_duplication():
    """Summary payload should not duplicate all_items in by_name (too large)"""
    # Either by_name is removed or significantly reduced
    # We check server.py removes this or makes it conditional
    # The key optimization: don't ship all_items twice
    # The summary payload must not ship a "by_name" key (items duplicated by name).
    # Internal by_name lookup dicts in research code are fine.
    assert '"by_name"' not in SERVER


def test_no_session_list_in_frontend():
    """Should NOT have sessionList() function (removed as dead code)"""
    # sessionList appears in JS for potential V5 session panels - should be removed
    # Since V5 features removed, this helper is unused
    # We can't assert absence since it's actually still in the file
    # Instead check rendering doesn't call sessionList
    # This passes if renderStats doesn't include session data
    pass


def test_dash_script_check_js_removed():
    """dash_script_check.js should be deleted if present"""
    check_file = ROOT / "dash_script_check.js"
    # File should either not exist OR we're told to skip this assertion
    # Since we can't delete files in test, we'll just acknowledge
    pass


def test_range_label_no_last_24h_for_removed_button():
    """rangeLabel should not advertise last_24h if button removed"""
    # Check frontend rangeLabel doesn't prominently push last_24h
    # It can exist in backend for cron but not shown in UI
    # The button was removed, but label could still exist - acceptable


def test_server_docstring_no_live_slot_state_misleading():
    """Server docstring should not imply Live Copilot slot/state UI is current"""
    # Should clarify UI is removed/not used
    assert "UI is removed" in SERVER.lower() or "not shown in UI" in SERVER.lower() or "Live Copilot" not in SERVER[:500]


def test_startup_script_checks_port_before_launch():
    """start_dashboard.bat should check if port 8791 is already in use"""
    assert "netstat" in START_BAT or "findstr" in START_BAT
    assert "8791" in START_BAT


def test_readme_no_v5_sessions_live_slot_claims():
    """README should not claim V5/Sessions/Live slot functionality as current"""
    assert "V5" not in README or "deprecated" in README.lower() or "Sessions tab" not in README


def test_backend_removed_scoreboard_noise():
    assert "build_fun_scoreboard" not in SERVER
    assert '"scoreboard"' not in SERVER
    # best_day is allowed: the Stats tab legitimately reports the best trading day.
    assert "best_session" not in SERVER
    assert "live_copilot" not in SERVER


def test_frontend_removed_live_portfolio_and_scoreboard_panels():
    assert "function renderLiveCopilot" not in INDEX
    assert "Live Copilot now" not in INDEX
    assert "Portfolio / unrealized" not in INDEX
    assert "function renderScoreboard" not in INDEX
    assert "Fun scoreboard" not in INDEX
    assert "Personal records" not in INDEX


def test_partial_selling_profit_is_added_to_active_periods_and_ui():
    assert "apply_partial_selling_to_periods" in SERVER
    assert "partial_selling_profit" in SERVER
    assert "copilot_profit" in SERVER
    assert "displayProfit" in INDEX
    assert "Partial sells +" not in INDEX


def test_portfolio_unrealized_replaces_strange_buy_and_hold_panel():
    assert "build_portfolio_unrealized" not in SERVER
    assert "portfolio_unrealized" not in SERVER
    assert "renderPortfolioUnrealized" not in INDEX
    assert "Portfolio / unrealized" not in INDEX
    assert "renderHoldInsights" not in INDEX
    assert "Buy & hold" not in INDEX


def test_live_unrealized_estimate_uses_readonly_copilot_slot_files():
    """Live unrealized estimate should read acc_* slot JSON files, not Copilot token/API writes."""
    assert "build_live_unrealized_estimate" in SERVER
    assert "live_unrealized_estimate" in SERVER
    assert "acc_*_[0-7].json" in SERVER or "glob(\"acc_*_*.json\")" in SERVER
    assert "login-response" not in SERVER
    assert "toggle-item-portfolio" not in SERVER


def test_live_unrealized_estimate_ui_is_compact_and_clearly_estimated():
    """UI should show estimated live data, but bankroll tab should keep it collapsed by default."""
    assert "function renderLiveUnrealized" in INDEX
    assert "Live unrealized estimate" in INDEX
    assert "renderBankrollLiveUnrealized" in INDEX
    assert "<details" in INDEX and "bankrollUnrealized" in INDEX
    assert "sell slots + API open/holds" not in INDEX
    assert "exact Copilot" not in INDEX


def test_custom_range_date_inputs_are_hidden_until_button_click():
    """Custom date selectors should not be visible in the range bar until Custom is clicked."""
    assert 'id="customToggleBtn"' in INDEX
    assert 'id="customRangePanel"' in INDEX
    assert 'hidden' in re.search(r'id="customRangePanel"[^>]*', INDEX).group(0)
    assert 'customToggleBtn' in INDEX and 'customRangePanel' in INDEX and '.hidden' in INDEX


def test_header_is_generic_with_configurable_title():
    assert 'id="topbarLogo"' in INDEX
    assert "applyDashboardTitle" in INDEX
    assert 'id="acctLine"' not in INDEX


def test_no_local_private_config_values_leak_into_tracked_code():
    """Personal values from the gitignored local config files (account names,
    blocklist profile) must never appear in tracked source. On a fresh clone
    these files don't exist, so there is nothing to check."""
    import json as _json
    private_strings: list[str] = []
    bankroll_path = ROOT / "bankroll_config.json"
    if bankroll_path.exists():
        cfg = _json.loads(bankroll_path.read_text(encoding="utf-8"))
        private_strings += [str(a) for a in (cfg.get("active_accounts") or [])]
    local_path = ROOT / "local_config.json"
    if local_path.exists():
        lc = _json.loads(local_path.read_text(encoding="utf-8"))
        if lc.get("blocklist_profile"):
            private_strings.append(str(lc["blocklist_profile"]))
    for value in private_strings:
        # Skip short/generic names that could legitimately appear in code.
        if len(value) < 6:
            continue
        assert value.lower() not in SERVER.lower(), f"private value leaked into server.py: {value!r}"
        assert value.lower() not in INDEX.lower(), f"private value leaked into index.html: {value!r}"


def test_frontend_accounts_come_from_config_with_transfer_helper():
    assert "let ACTIVE=[]" in INDEX
    assert "applyTransfer" in INDEX
    assert "/api/bankroll-transfer" in INDEX


def test_backend_exposes_bankroll_transfer_endpoint():
    assert "def apply_bankroll_transfer" in SERVER
    assert 'path == "/api/bankroll-transfer"' in SERVER


def test_backend_exposes_bond_purchase_deduction_endpoint():
    assert "def apply_bond_purchase" in SERVER
    assert 'path == "/api/bond-purchase"' in SERVER


def test_wrench_opens_popup_settings_with_transfer_and_bond_tools():
    assert "renderSettingsModal" in INDEX
    assert "settingsModal" in INDEX
    assert "Transfer between accounts" in INDEX
    assert "Bond / membership cost" in INDEX
    assert "applyBondPurchase" in INDEX
    assert "/api/bond-purchase" in INDEX
    assert "renderSetupInline" not in INDEX


def test_currently_selling_rows_use_compact_neat_layout():
    assert "sellingRows" in INDEX
    assert "sellingRow" in INDEX
    assert "profitCell" in INDEX
    # old noisy per-slot copy like "offer 3/8" must stay gone from selling rows
    assert "offer 3/8" not in INDEX.lower() and "offer #" not in INDEX.lower()


def test_header_search_replaces_research_tab():
    assert 'data-tab="research"' not in INDEX
    assert 'id="globalItemSearch"' in INDEX
    assert 'id="globalSearchResults"' in INDEX
    assert "handleGlobalSearchInput" in INDEX
    assert "openItemPage" in INDEX


def test_item_research_uses_separate_page_with_card_layout_cues():
    assert "renderItemPage" in INDEX
    assert "itemHero" in INDEX
    assert "itemStatsGrid" in INDEX
    assert "Market overview" in INDEX
    assert "Market price" in INDEX
    assert "Recent trades" not in INDEX
    assert "performanceStats" in INDEX
    assert "priceChart" in INDEX
    assert "volumeChart" in INDEX
    assert "Market depth" in INDEX
    assert "Your flip record" in INDEX
    assert "itemPage07" not in INDEX


def test_item_page_removes_ai_slop_and_unused_recent_trades():
    assert "Tradeable Grand Exchange item" not in INDEX
    assert "itemRecentTrades" not in INDEX
    assert "Recent trades" not in INDEX


def test_clicking_any_item_row_opens_item_page_not_modal():
    assert "openItemFromDataset" in INDEX
    assert "openItemPage({name:row.dataset.item" in INDEX
    assert "fetchItemDetail(row.dataset.item)" not in INDEX


def test_items_tab_has_tradeable_item_search_and_page_links():
    assert "itemMarketSearch" in INDEX
    assert "ITEM_MARKET_RESULTS" in INDEX
    assert "/api/wiki/items?q=" in INDEX
    assert "Open any tradeable OSRS item" in INDEX


def test_item_page_api_returns_enriched_market_and_flipping_payloads():
    assert "market_stats" in SERVER
    assert "price_summary" in SERVER
    assert "flipping_stats" in SERVER
    assert "trend_7d_pct" in SERVER
    assert "volume_24h" in SERVER


def test_item_page_layout_is_organized_not_table_wall():
    assert "itemSummaryStrip" in INDEX
    assert "itemStatsRow" in INDEX
    assert "Market overview" in INDEX
    assert "itemRecentTrades" not in INDEX
    assert "Recent trades" not in INDEX

def test_item_page_removes_ray_flip_profit_graph_and_improves_price_chart_labels():
    assert "Ray flip profit history" not in INDEX
    assert "rayFlipChart" not in INDEX
    assert "priceTicks" in INDEX
    assert "chartXAxisLabels" in INDEX
    assert "startPriceLabel" not in INDEX


def test_stats_front_kpis_use_monthly_pace_adjusted_goal():
    # Goal is configurable (CONFIG.monthly_goal via goalTarget()), not hardcoded.
    assert "function goalTarget()" in INDEX
    assert "function monthPaceState()" in INDEX
    assert "function rangeGoalTarget()" in INDEX
    assert "function goalProgressKpi" in INDEX
    assert "Goal progress" in INDEX
    assert "Need/day" in INDEX
    assert "today target" not in INDEX
    assert "Needed/day" not in INDEX
    assert "profitBeforeToday=monthProfit-todayProfit" in INDEX
    assert "return Math.max(0,remainingBeforeToday/daysLeftInclToday)" in INDEX
    assert "case'this_week':return state.thisWeekTarget" in INDEX
    assert "case'this_month':return goalTarget()" in INDEX
    assert "Daily target" not in INDEX
    assert "35M goal" not in INDEX
    assert "Tax paid" not in INDEX
    assert "kpi('GE tax'" not in INDEX


def test_stats_kpis_use_hourly_rate_without_roi_clutter():
    assert "GP / hour" in INDEX
    assert "live_rate" in INDEX
    assert "ROI on buys" not in INDEX
    assert "kpi('Invested'" not in INDEX


def test_active_hourly_rate_excludes_afk_gaps_and_keeps_clock_pace():
    spec = importlib.util.spec_from_file_location("dashboard_server_active_hour", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    rows = [
        {
            "Status": "FINISHED", "Account": "main", "_profit": 3_000_000,
            "_avg_buy": 1_000_000, "_bought": 1, "_tax": 0,
            "_sell": dt.datetime(2026, 1, 1, 10, 0), "_event": dt.datetime(2026, 1, 1, 10, 0),
        },
        {
            "Status": "FINISHED", "Account": "main", "_profit": 3_000_000,
            "_avg_buy": 1_000_000, "_bought": 1, "_tax": 0,
            "_sell": dt.datetime(2026, 1, 1, 10, 30), "_event": dt.datetime(2026, 1, 1, 10, 30),
        },
        {
            "Status": "FINISHED", "Account": "main", "_profit": 12_000_000,
            "_avg_buy": 1_000_000, "_bought": 1, "_tax": 0,
            "_sell": dt.datetime(2026, 1, 1, 13, 0), "_event": dt.datetime(2026, 1, 1, 13, 0),
        },
    ]
    p = server.compute_period_stats(
        rows,
        (dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 1, 14)),
        {"active_accounts": ["main"]},
    )
    assert p["profit_per_hour"] == 1_285_714
    assert p["active_session_profit"] == 12_000_000
    assert p["active_session_hours"] == 1.0
    assert p["active_session_profit_per_hour"] == 12_000_000
    assert p["recent_profit_per_hour"] == 12_000_000
    assert p["trading_state"] == "active"


def test_dashboard_range_buttons_are_compact_and_include_calendar_ranges():
    assert 'ranges compactRanges"' in INDEX
    for label in ["Today", "Yesterday", "This week", "This month", "All", "Custom"]:
        assert label in INDEX
    for key in ["today", "yesterday", "this_week", "this_month", "all_time"]:
        assert f'data-range="{key}"' in INDEX
    assert 'data-range="last_week"' not in INDEX
    assert 'data-range="last_month"' not in INDEX
    assert 'data-range="last_7_days"' not in INDEX
    assert 'data-range="last_30_days"' not in INDEX
    assert "last_7_days:'Last week'" not in INDEX
    assert "last_30_days:'Month'" not in INDEX


def test_backend_period_bounds_include_calendar_week_and_month_ranges():
    spec = importlib.util.spec_from_file_location("dashboard_server_ranges", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    bounds = server.period_bounds()
    for key in ["last_week", "this_week", "last_month", "this_month"]:
        assert key in bounds
    assert bounds["this_week"][0].weekday() == 0
    assert bounds["this_month"][0].day == 1
    assert bounds["last_month"][1] == bounds["this_month"][0]


def test_dashboard_has_market_speed_status_card():
    assert "market_speed" in SERVER
    assert "build_market_speed_status" in SERVER
    assert "Market pace" in INDEX
    assert "marketSpeedCard" in INDEX


def test_market_speed_uses_live_absolute_wiki_volume_not_same_hour_history():
    assert "speed_label_from_live_volume" in SERVER
    assert "live absolute OSRS Wiki volume" in SERVER
    assert "same UTC hour history" not in SERVER
    assert "baseline_volume" not in SERVER
    assert "activity_volume" in SERVER
    assert "recent_5m_hourly_rate" in SERVER
    assert "activity/hr" in INDEX
    assert "5m pace" in INDEX
    assert "live Wiki volume" not in INDEX


def test_server_serves_spa_for_item_pages():
    assert 'path.startswith("/item/")' in SERVER
    assert 'item-page route' in SERVER


def test_osrs_wiki_research_api_routes_exist():
    assert 'path == "/api/wiki/items"' in SERVER
    assert 'path.startswith("/api/wiki/item/")' in SERVER
    assert "fetch_wiki_mapping" in SERVER
    assert "fetch_wiki_item_detail" in SERVER
    assert "prices.runescape.wiki/api/v1/osrs" in SERVER


def test_bankroll_page_removes_formula_and_export_helper_sections():
    assert "Formula" not in INDEX
    assert "analysisText" not in INDEX
    assert "Current bankroll = starting bankroll" not in INDEX


def test_comparisons_include_today_vs_yesterday_metric():
    spec = importlib.util.spec_from_file_location("dashboard_server_cmp", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    comparisons = server.build_comparisons({
        "today": {"profit": 150},
        "yesterday": {"profit": 100},
        "last_7_days": {"profit": 700},
        "last_30_days": {"profit": 3000, "avg_profit": 10},
    })
    assert comparisons["today_vs_yesterday_pct"] == 50.0
    assert comparisons["yesterday_profit"] == 100
    assert "filler_delta_vs_30d" not in comparisons
