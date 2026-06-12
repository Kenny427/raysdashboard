from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")


def test_open_item_page_clears_global_search_input():
    assert "function clearGlobalSearch" in INDEX
    assert "clearGlobalSearch()" in INDEX
    assert "input.value=''" in INDEX
    assert "input.value=item.name" not in INDEX


def test_header_does_not_show_csv_auto_refresh_controls():
    assert 'id="csvAutoMinutes"' not in INDEX
    assert 'id="csvAutoToggle"' not in INDEX
    assert "Set auto-refresh" not in INDEX
    assert "Stop auto-refresh" not in INDEX
    assert "AUTO_REFRESH_MINUTES_DEFAULT" not in INDEX
    assert "CSV_AUTO_REFRESH_TIMER" not in INDEX


def test_opening_page_loads_cached_summary_first():
    assert "async function initialLoadWithCsvFetch" not in INDEX
    assert "await loadConfig();await load()" in INDEX


def test_visible_dashboard_auto_fetches_copilot_api_csv_on_a_timer():
    assert "CSV_API_ACTIVE_FETCH_MS" in INDEX
    assert "function shouldAutoFetchCsv()" in INDEX
    assert "function syncTick()" in INDEX
    assert "if(shouldAutoFetchCsv())" in INDEX or "shouldAutoFetchCsv()" in INDEX
    assert "refreshCopilotCsv(true)" in INDEX


def test_tab_focus_and_visibility_do_not_immediately_fetch_copilot_api_csv():
    assert "function maybeRefreshCsvOnPageActivation" not in INDEX
    assert "document.addEventListener('visibilitychange'" not in INDEX
    assert "window.addEventListener('focus'" not in INDEX


def test_live_sync_pill_manually_fetches_copilot_api_csv():
    assert 'id="liveSync"' in INDEX
    assert "async function refreshCopilotCsv" in INDEX
    assert "$('liveSync').onclick=()=>refreshCopilotCsv(false)" in INDEX
