from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")


def test_item_page_auto_refreshes_wiki_prices_every_60s_while_open():
    assert "ITEM_PAGE_REFRESH_MS=60000" in INDEX
    assert "refreshOpenItemPage" in INDEX
    assert "setInterval(refreshOpenItemPage,ITEM_PAGE_REFRESH_MS)" in INDEX


def test_item_page_uses_precise_gp_values_not_billion_suffixes():
    assert "function fmtGp" in INDEX
    assert "fmtGp(high)" in INDEX
    assert "fmtGp(low)" in INDEX
    assert "fmt(high)+' GP'" not in INDEX
    assert "fmt(low)+' GP'" not in INDEX


def test_item_page_recent_trades_section_removed_as_unused_ui_noise():
    assert "Recent trades" not in INDEX
    assert "itemRecentTrades" not in INDEX
    assert "function tradeRows" not in INDEX
    assert "tradeRange" not in INDEX


def test_item_page_graph_has_intuitive_range_buttons():
    assert "ITEM_CHART_RANGE='7d'" in INDEX
    assert "chartRangeControls" in INDEX
    assert "setItemChartRange" in INDEX
    for label in ["1d", "7d", "30d", "180d"]:
        assert f">{label}<" in INDEX
    assert "chart_days=" in INDEX
    assert "chartTimestep" in INDEX
    assert "ITEM_CHART_RANGE==='30d'?'6h'" in INDEX
    assert "ITEM_CHART_RANGE==='180d'?'24h'" in INDEX


def test_item_page_graph_uses_mid_price_and_labels_axis_precisely():
    assert "chartMidPrice" in INDEX
    assert "stroke=\"var(--amber)\"" in INDEX
    assert "priceTicks" in INDEX
    assert "fmtGp(v)" in INDEX


def test_item_price_graph_has_clickable_point_details_without_table_clutter():
    assert "ITEM_CHART_SELECTED=null" in INDEX
    assert "selectChartPoint" in INDEX
    assert "selectedPriceDetail" in INDEX
    assert "Click chart point" in INDEX
    assert "Instant-buy" in INDEX
    assert "Instant-sell" in INDEX
    assert "onclick=\"selectChartPoint(" in INDEX


def test_price_history_removes_static_start_now_range_copy():
    assert "startPriceLabel" not in INDEX
    assert "Start ${fmtGp" not in INDEX
    assert " · Now ${fmtGp" not in INDEX
    assert " · Range ${fmtGp" not in INDEX


def test_market_depth_has_timestamp_axis_and_clickable_volume_details():
    assert "selectVolumePoint" in INDEX
    assert "selectedVolumeDetail" in INDEX
    assert "volumeXAxisLabels" in INDEX
    assert "Click volume bar" in INDEX
    assert "Buy volume" in INDEX
    assert "Sell volume" in INDEX
    assert "onclick=\"selectVolumePoint(" in INDEX


def test_item_graphs_reserve_space_so_lines_and_bars_do_not_overlap_labels():
    assert "chartLeftPad=92" in INDEX
    assert "chartBottomPad=38" in INDEX
    assert "volumeLeftPad=72" in INDEX
    assert "volumeBottomPad=34" in INDEX
    assert "text x=\"${chartLeftPad-8}" in INDEX
    assert "text x=\"${volumeLeftPad-8}" in INDEX
    assert "y=\"${h+volumeBottomPad-8}" in INDEX


def test_market_depth_has_left_side_volume_scale_for_quick_glance():
    assert "volumeTicks" in INDEX
    assert "v===0?'0':fmt(v)" in INDEX
    assert "[max,max/2,0]" in INDEX
    assert "fmt(v)" in INDEX
