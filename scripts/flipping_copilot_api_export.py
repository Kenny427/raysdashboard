#!/usr/bin/env python3
"""Read-only Flipping Copilot API CSV exporter.

Fetches flip history via Copilot's REST+protobuf API and writes
flips.csv (in the dashboard project folder) matching the manual UI export format.

No mouse, no window focus, no RuneLite required. Runs in background.

Requires being logged in to the Flipping Copilot RuneLite plugin at least once,
which stores a login token in ~/.runelite/flipping-copilot/login-response.json.

Incremental sync: the /client-flips-delta endpoint returns a `top_time`
high-water mark. We persist it per account (flip_sync_state.json) and send it
back as the cursor on the next call, so each sync only pulls flips changed
since the last one. New/changed flips are merged into a local id-keyed store
and the full CSV is rewritten from it. The first run (no state) pulls the full
history once; after that, steady-state calls return only a handful of flips —
a tiny payload and query instead of a full-history scan on Copilot's servers.

Usage:
  python flipping_copilot_api_export.py [--test] [--verbose] [--full]
  --out P : write the CSV to path P (the dashboard passes the project flips.csv)
  --test  : write to flips_api_test.csv + separate state file
  --verbose: print per-row details on first few errors
  --full  : ignore the saved cursor and re-pull the entire history once
"""

import json, urllib.request, csv, struct, datetime, os, sys
from pathlib import Path

# ---- Paths -------------------------------------------------------------------

RUNE_TOKEN_PATH = Path.home() / '.runelite' / 'flipping-copilot' / 'login-response.json'
# Default next to the dashboard so everything lives in one project folder.
# The dashboard server passes --out explicitly; both resolve to the same file.
_PROJECT_ROOT   = Path(__file__).resolve().parent.parent
EXPORT_PATH     = _PROJECT_ROOT / 'flips.csv'
EXPORT_TEST_PATH= _PROJECT_ROOT / 'flips_api_test.csv'
ITEM_CACHE_PATH = Path(__file__).parent / 'item_name_cache.json'
STATE_PATH      = Path(__file__).parent / 'flip_sync_state.json'
STATE_TEST_PATH = Path(__file__).parent / 'flip_sync_state_test.json'

# ---- API --------------------------------------------------------------------

API_BASE = 'https://api.flippingcopilot.com/profit-tracking'

# ---- Protobuf field layout (FlipV2, confirmed from live decode) -----------
# Wire types confirmed by byte-exact decode of the /client-flips-delta response.
# Key finding from matching against manual CSV:
#   field 11 = Tax (manual "Tax" column)   [was: profit]
#   field 12 = Profit (manual "Profit" column) [was: taxPaid]
#   avg_sell = (spent + api_profit + api_tax) / closedQty
F = {
    'id'             : 1,   # wire 2 – string (UUID bytes hex)
    'accountId'      : 2,   # wire 0 – varint
    'itemId'         : 3,   # wire 0 – varint
    'openedTime'     : 5,   # wire 0 – varint (epoch seconds)
    'openedQuantity' : 6,   # wire 0 – varint
    'spent'          : 7,   # wire 0 – varint (total gp spent; avg = spent / openedQty)
    'closedTime'     : 8,   # wire 0 – varint (0 = not closed)
    'closedQuantity' : 9,   # wire 0 – varint
    'receivedPostTax': 10,  # wire 0 – varint (post-tax selling revenue; 0 for BUYING)
    'profit'         : 11,  # wire 0 – varint (→ manual CSV "Tax" column)
    'taxPaid'        : 12,  # wire 0 – varint (→ manual CSV "Profit" column; can overflow negative)
    'status'         : 14,  # wire 2 – string ('F'/'O'/'C')
    'updatedTime'    : 16,  # wire 0 – varint
    'deleted'        : 17,  # wire 0 – varint (1 = deleted)
    'portfolioId'    : 18,  # wire 0 – varint
    'seqNo'          : 19,  # wire 0 – varint
    'userId'         : 21,  # wire 0 – varint
}

# PortfolioId values (from PortfolioId.java decompilation)
PORTFOLIO_INCLUDED = {0, 1}   # COFLIP_PORTFOLIO, PERSONAL_PORTFOLIO
PORTFOLIO_EXCLUDED = {-1, -2, -3, -4}  # GHOST, DISAPPEARED_*

STATUS_MAP = {'F': 'FINISHED', 'O': 'BUYING', 'C': 'SELLING'}

CSV_HEADERS = [
    'First buy time', 'Last sell time', 'Account', 'Item',
    'Status', 'Bought', 'Sold', 'Avg. buy price', 'Avg. sell price',
    'Tax', 'Profit', 'Profit ea.'
]


# =============================================================================
# Helpers
# =============================================================================

def get_jwt() -> str:
    obj = json.loads(RUNE_TOKEN_PATH.read_text(encoding='utf-8'))
    token = obj.get('jwt') or obj.get('jwtToken') or obj.get('token')
    if not token:
        raise RuntimeError(f'No JWT found in {RUNE_TOKEN_PATH}')
    return token


def get_accounts(jwt: str) -> dict[str, int]:
    req = urllib.request.Request(
        f'{API_BASE}/rs-account-names',
        headers={'Authorization': f'Bearer {jwt}'}
    )
    return dict(json.loads(urllib.request.urlopen(req, timeout=20).read()))


def load_item_cache(force_refresh: bool = False) -> dict[int, str]:
    if not force_refresh and ITEM_CACHE_PATH.exists():
        try:
            raw = json.loads(ITEM_CACHE_PATH.read_text(encoding='utf-8'))
            cache = {int(k): v for k, v in raw.items()}
            age = datetime.datetime.now() - datetime.datetime.fromtimestamp(
                ITEM_CACHE_PATH.stat().st_mtime
            )
            if age < datetime.timedelta(hours=24):
                return cache
        except Exception:
            pass

    url = 'https://static.runelite.net/cache/item/names.json'
    req = urllib.request.Request(url, headers={'User-Agent': 'osrs-flip-dashboard/1.0 (local self-hosted dashboard)'})
    raw_json = json.loads(urllib.request.urlopen(req, timeout=20).read())
    # RuneLite names.json has string keys: {"0": "Dwarf remains", ...}
    # Write with string keys (safe JSON round-trip), return int-keyed for lookup
    cache = dict(raw_json)
    ITEM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ITEM_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
    return {int(k): v for k, v in cache.items()}


# =============================================================================
# Protobuf decode
# =============================================================================

def _fix_profit_overflow(val: int) -> int:
    """Handle overflow for field 12 (Profit): store as unsigned, convert to signed.

    Large negative values are encoded as unsigned varints that overflow past 2^63-1.
    E.g. -724071 is stored as 18446744073709432714. If raw > 2^63-1, treat as
    unsigned and convert to Python signed: val - 2^64.
    """
    if val > (1 << 63) - 1:
        return val - (1 << 64)
    return val


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    v, shift = 0, 0
    while i < len(buf):
        b = buf[i]; i += 1
        v |= (b & 0x7f) << shift
        if b < 0x80:
            return v, i
        shift += 7
    return v, i


def _skip(buf: bytes, i: int, wire_type: int) -> int:
    if wire_type == 0:   _, i = _read_varint(buf, i); return i
    if wire_type == 1:   return i + 8
    if wire_type == 2:
        l, i = _read_varint(buf, i); return i + l
    if wire_type == 5:   return i + 4
    raise ValueError(f'unexpected wire type {wire_type}')


def _parse_flip(buf: bytes) -> dict:
    """Parse one FlipV2 delimited message from protobuf.

    Field 12 (Profit) can overflow the signed varint range when the value is
    a large negative. Values > 2^63-1 are treated as overflowed unsigned and
    converted to signed: val - 2^64.
    """
    d = {}
    i = 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        fn = tag >> 3
        wt = tag & 7
        if wt == 0:    # varint
            val, i = _read_varint(buf, i)
            if fn == 12:  # overflow fix for Profit field
                val = _fix_profit_overflow(val)
            d[fn] = val
        elif wt == 1:  # 64-bit signed int (big-endian, e.g. receivedPostTax)
            d[fn] = struct.unpack('>q', buf[i:i+8])[0]; i += 8
        elif wt == 2:  # length-delimited
            l, i = _read_varint(buf, i)
            b = buf[i:i+l]; i += l
            d[fn] = b.decode('utf-8', errors='replace') if fn in (4, 14, 18) else b.hex()
        elif wt == 5:  # 32-bit
            d[fn] = struct.unpack('>i', buf[i:i+4])[0]; i += 4
        else:
            i = _skip(buf, i, wt)
    return d


def fetch_flips(jwt: str, account_times: dict[int, int]) -> tuple[int, list[dict]]:
    """Fetch flips via /client-flips-delta. `account_times` maps account_id ->
    last seen top_time (0 = full history for that account). Returns (top_time, flips)."""
    body = json.dumps({'account_id_time': {str(k): int(v) for k, v in account_times.items()}}).encode()
    req = urllib.request.Request(
        f'{API_BASE}/client-flips-delta',
        data=body, method='POST',
        headers={
            'Authorization': f'Bearer {jwt}',
            'Accept': 'application/protobuf',
            'Content-Type': 'application/json',
        }
    )
    raw = urllib.request.urlopen(req, timeout=90).read()

    flips = []
    top_time = None
    i = 0
    while i < len(raw):
        tag, i = _read_varint(raw, i)
        fn = tag >> 3; wt = tag & 7
        if fn == 1 and wt == 0:           # top_time field
            top_time, i = _read_varint(raw, i)
        elif fn == 2 and wt == 2:         # flips field (repeated FlipV2)
            l, i = _read_varint(raw, i)
            flips.append(_parse_flip(raw[i:i+l])); i += l
        else:
            i = _skip(raw, i, wt)

    return top_time, flips


# =============================================================================
# CSV writing
# =============================================================================

def flip_to_row(
    flip: dict,
    account_map: dict[int, str],
    item_names: dict[int, str],
    verbose: bool = False,
) -> dict | None:
    """Convert decoded FlipV2 dict to a CSV row dict, or None if filtered out."""

    # Skip deleted flips
    if flip.get(F['deleted']) == 1:
        return None

    # Skip ghost / disappeared portfolio IDs
    pid = flip.get(F['portfolioId'], 0)
    if pid not in PORTFOLIO_INCLUDED:
        return None

    account_id  = flip.get(F['accountId'], 0)
    item_id     = flip.get(F['itemId'], 0)
    opened_qty  = max(0, flip.get(F['openedQuantity'], 0))
    closed_qty  = max(0, flip.get(F['closedQuantity'], 0))
    spent       = flip.get(F['spent'], 0)
    status_code = flip.get(F['status'], 'O')
    opened_time = flip.get(F['openedTime'], 0)
    closed_time = flip.get(F['closedTime'], 0)

    # receivedPostTax is field 10, a varint. It is 0 for BUYING rows.
    # For FINISHED/SELLING rows: receivedPostTax is the post-tax revenue from selling.
    received = flip.get(F['receivedPostTax'], 0)

    # IMPORTANT: Field 11 = Tax (manual CSV "Tax" column)
    #            Field 12 = Profit (manual CSV "Profit" column)
    # We must use these in that order for the output CSV.
    tax_out     = flip.get(F['profit'], 0)       # field 11 → manual Tax
    profit_out  = flip.get(F['taxPaid'], 0)      # field 12 → manual Profit (can be negative)

    account_name = account_map.get(account_id, str(account_id))
    item_name    = item_names.get(item_id, f'Item:{item_id}')
    status       = STATUS_MAP.get(status_code, 'BUYING')

    first_buy = _fmt_ts(opened_time)
    last_sell = _fmt_ts(closed_time) if closed_time else ''

    # Avg buy = spent / openedQty
    avg_buy = spent // opened_qty if opened_qty else 0

    # Avg sell for FINISHED = (spent + profit + tax) / closedQty — matches the
    # manual CSV (derived from profit = received - spent - tax, and for finished
    # flips spent covers exactly the closed quantity).
    # For partial (SELLING) rows that derivation breaks: `spent` covers ALL
    # bought units, not just the sold ones, which wildly inflates avg sell.
    # Use Copilot's own FlipV2.getAvgSellPrice formula instead:
    # (receivedPostTax + taxPaid) / closedQuantity.
    if closed_qty > 0:
        if status == 'FINISHED':
            avg_sell = (spent + profit_out + tax_out) // closed_qty
        elif received:
            avg_sell = (received + tax_out) // closed_qty
        else:
            avg_sell = 0
    else:
        avg_sell = 0

    profit_ea = profit_out // closed_qty if closed_qty > 0 else 0

    return {
        'First buy time'  : first_buy,
        'Last sell time'  : last_sell,
        'Account'         : account_name,
        'Item'            : item_name,
        'Status'          : status,
        'Bought'          : str(opened_qty),
        'Sold'            : str(closed_qty),
        'Avg. buy price'  : str(avg_buy),
        'Avg. sell price' : str(avg_sell),
        'Tax'             : str(tax_out),
        'Profit'          : str(profit_out),
        'Profit ea.'      : str(profit_ea),
    }


def _fmt_ts(epoch: int) -> str:
    if not epoch:
        return ''
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ'
    )


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    try:
        with tmp.open('w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(path)
    except PermissionError:
        # Windows: file locked by another reader (e.g. dashboard server).
        # Delete target first, then retry replace.
        path.unlink(missing_ok=True)
        tmp.replace(path)


# =============================================================================
# Sync state (per-account cursor + id-keyed flip store)
# =============================================================================

def load_state(path: Path) -> tuple[dict[int, int], dict[str, dict]]:
    """Return (cursor, store). cursor: {account_id: last_top_time}.
    store: {flip_id: csv_row_dict} — the merged full history."""
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        cursor = {int(k): int(v) for k, v in (raw.get('cursor') or {}).items()}
        store = raw.get('flips') or {}
        if isinstance(store, dict):
            return cursor, store
    except Exception:
        pass
    return {}, {}


def save_state(path: Path, cursor: dict[int, int], store: dict[str, dict]) -> None:
    tmp = path.with_suffix('.state.tmp')
    payload = {'cursor': {str(k): int(v) for k, v in cursor.items()}, 'flips': store}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


# =============================================================================
# Main
# =============================================================================

def run(test: bool = False, verbose: bool = False, full: bool = False, out: str | None = None) -> dict:
    target = Path(out) if out else (EXPORT_TEST_PATH if test else EXPORT_PATH)
    state_path = STATE_TEST_PATH if test else STATE_PATH

    jwt = get_jwt()
    accounts = get_accounts(jwt)                       # {name: id}
    account_map = {v: k for k, v in accounts.items()}  # {id: name}
    item_names = load_item_cache()

    cursor, store = ({}, {}) if full else load_state(state_path)

    # Send each account's saved cursor (0 = full history; covers first run and
    # any newly-added account). top_time then advances all of them.
    sent_times = {aid: (0 if full else int(cursor.get(aid, 0))) for aid in accounts.values()}
    was_full = full or all(t == 0 for t in sent_times.values())
    if verbose:
        print(f'JWT ok | accounts={len(accounts)} | mode={"full" if was_full else "delta"} | cursor={sent_times}')

    top_time, flips = fetch_flips(jwt, sent_times)
    if verbose:
        print(f'API returned {len(flips)} flips | top_time={top_time}')

    upserts = removed = 0
    for flip in flips:
        fid = flip.get(F['id'])
        if not fid:
            continue
        row = flip_to_row(flip, account_map, item_names, verbose=verbose)
        if row is None:
            # deleted or now-excluded flip: drop it from the merged history
            if store.pop(fid, None) is not None:
                removed += 1
        else:
            store[fid] = row
            upserts += 1

    # Advance every current account's cursor to the new high-water mark.
    if top_time:
        for aid in accounts.values():
            cursor[aid] = int(top_time)

    rows = sorted(store.values(), key=lambda r: r.get('First buy time') or '')
    write_csv(rows, target)
    save_state(state_path, cursor, store)

    if verbose:
        from collections import Counter
        statuses = Counter(r['Status'] for r in rows)
        print(f'Merged store: {len(rows)} rows — {dict(statuses)} | +{upserts}/-{removed} this sync')
        print(f'Written to {target}')

    return {
        'written'      : len(rows),
        'path'         : str(target),
        'top_time'     : top_time,
        'mode'         : 'full' if was_full else 'delta',
        'api_returned' : len(flips),
        'delta_upserts': upserts,
        'delta_removed': removed,
    }


if __name__ == '__main__':
    test    = '--test'    in sys.argv
    verbose = '--verbose' in sys.argv
    full    = '--full'    in sys.argv
    out     = None
    if '--out' in sys.argv:
        out = sys.argv[sys.argv.index('--out') + 1]
    result  = run(test=test, verbose=verbose, full=full, out=out)
    print(json.dumps(result, indent=2))