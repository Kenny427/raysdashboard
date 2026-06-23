"""Tests for slot-occupancy aggregation (synthetic episodes in a temp db)."""
import datetime as dt
import server


def test_slot_occupancy_aggregation(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MARKET_HISTORY_DB_PATH", tmp_path / "hist.db")
    now = int(dt.datetime.now().timestamp())
    h = now - 3600  # 1 hour ago
    conn = server._history_db()
    server._ensure_slot_tables(conn)
    with conn:
        # A: filled flip, occupied 1h, buy filled 0.5h after start
        conn.execute("INSERT INTO slot_episode (account_hash,slot,item_id,item,start_ts,last_ts,first_fill_ts,reprices,last_price,max_sold,total_qty,state,open)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a", 0, 100, "Whip", h, now, h + 1800, 1, 5, 10, 10, "SELLING", 0))
        # B: cancelled-unfilled buy, occupied 1h, never filled
        conn.execute("INSERT INTO slot_episode (account_hash,slot,item_id,item,start_ts,last_ts,first_fill_ts,reprices,last_price,max_sold,total_qty,state,open)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("a", 1, 200, "Rune", h, now, None, 3, 9, 0, 5, "BUYING", 0))
        conn.execute("INSERT INTO slot_poll (ts,occupied,total) VALUES (?,?,?)", (now, 2, 4))
    conn.close()

    rows = [{"Status": "FINISHED", "Item": "Whip", "_buy": dt.datetime.fromtimestamp(h + 600), "_profit": 3600}]
    so = server.build_slot_occupancy_stats(rows)
    assert so["episodes"] == 2
    assert abs(so["occupied_hours"] - 2.0) < 0.05          # two ~1h episodes
    assert so["cancel_rate"] == 50.0                        # 1 of 2 freed unfilled
    assert so["utilization_pct"] == 50.0                    # 2 of 4 slots
    assert abs(so["avg_buy_fill_h"] - 0.5) < 0.05           # only the filled episode counts
    whip = [x for x in so["items"] if x["item"] == "Whip"][0]
    assert whip["profit"] == 3600 and whip["gp_per_occ_hr"] == 3600


def test_slot_occupancy_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MARKET_HISTORY_DB_PATH", tmp_path / "hist2.db")
    so = server.build_slot_occupancy_stats([])
    assert so["episodes"] == 0 and so["items"] == []
