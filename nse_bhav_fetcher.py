"""
TrueFlow NSE Bhav Copy Fetcher
================================
Downloads NSE Equity + F&O Bhav Copy (UDiFF format) after market close.
Parses and stores:
  1. Equity Bhav Copy → equity OHLCV + delivery data (supplements eod_ema.py)
  2. F&O Bhav Copy → full option chain OI for all F&O stocks + BankNifty + Nifty
     → Replaces oi_scanner.py (no Kite API dependency)

Stores in Supabase:
  - fo_bhav_oi → F&O OI data per symbol per expiry per strike
  - oi_chain → BankNifty/Nifty option chain (existing table, updated)

Cron: Run at 6:15 PM IST (after NSE publishes bhav copy ~6 PM)
  45 12 * * 1-5 /root/trueflow/bin/python /root/trueflow/nse_bhav_fetcher.py >> /root/trueflow/bhav_fetcher.log 2>&1

No Kite API dependency — runs entirely on free NSE data.
"""

import os
import io
import sys
import argparse
import csv
import time
import zipfile
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

# ─── CONFIG ───────────────────────────────────────────
SUPABASE_URL  = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"
TELEGRAM_TOKEN   = "8768295108:AAEnPXWasPeZJRhhmJPjee-DTmXCauMbjYA"
TELEGRAM_CHAT_ID = "-1004324853933"
IST = ZoneInfo("Asia/Kolkata")

# NSE UDiFF Bhav Copy URLs (new format from July 2024)
NSE_CM_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
NSE_FO_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date}_F_0000.csv.zip"

# NSE requires browser-like headers
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/all-reports",
}

# F&O stocks we track (same as alert_engine.py ALL_FO_STOCKS)
FO_SYMBOLS = {
    "GODFRYPHLP","MOTILALOFS","SIEMENS","FORCEMOT","ASTRAL","BOSCHLTD",
    "NAM-INDIA","HYUNDAI","ANGELONE","360ONE","VVM","OIL","JUBLFOOD","RVNL",
    "INDIANB","VEDL","HCLTECH","ICICIGI","POLICYBZR","UNIONBANK","CAMS",
    "PIDILITIND","PPLPHARMA","SONACOMS","WIPRO","TATATECH","BPCL","INDUSTOWER",
    "SUZLON","MUTHOOTFIN","HINDALCO","DIXON","LTM","IRFC","LODHA","BAJAJ-AUTO",
    "DMART","ICICIPRULI","WAAREEENER","CUMMINSIND","EICHERMOT","PAGEIND",
    "SBICARD","PGEL","DRREDDY","FEDERALBNK","TORNTPHARM","HDFCLIFE","SUNPHARMA",
    "LT","ABB","BANKBARODA","NTPC","COCHINSHIP","AXISBANK","BRITANNIA","MAXHEALTH",
    "TORNTPOWER","LICI","CGPOWER","HDFCAMC","MARUTI","CDSL","DLF","INDIGO",
    "GMRAIRPORT","MANKIND","COLPAL","FORTIS","ABCAPITAL","OBEROI","CIPIA",
    "DELHIVERY","UPL","LAURUSLABS","MCX","MPHASIS","BHARATFORG","IREDA","INOXWIND",
    "HAL","VOLTAS","PNBHOUSING","ZYDUSLIFE","DIVISLAB","BHARTIARTL","BBLBANK",
    "UNITDSPR","APOLLOHOSP","COALINDIA","POWERGRID","TIINDIA","ITC","IOC",
    "PERSISTENT","TITAN","PREMIERENE","CHOLAFIN","POLYCAB","BLUESTARCO","OFSS",
    "GAIL","MANAPPURAM","SBIN","BIOCON","KEI","APLAPOLLO","NESTLEIND","TATASTEEL",
    "DABUR","AUBANK","HDFCBANK","COFORGE","ULTRACEMCO","HAVELLS","IDFCFIRSTB",
    "AMBUJACEM","UNOMINDA","PHOENIXLTD","ADANIPOWER","BANKINDIA","HUDCO","BDL",
    "PAYTM","BEL","RECLTD","PRESTIGE","ADANIENT","HINDZINC","SAMMAANCAP",
    "GODREJCP","ASHOKLEY","PIIND","MARICO","ICICIBANK","CONCOR","KOTAKBANK",
    "ADANIPORTS","BAJAJHLDNG","HEROMOTOCO","JINDALSTEL","SHREECEM","SOLARINDS",
    "TCS","LTF","TECHM","HINDPETRO","MFSL","POWERINDIA","KFINTECH","TVSMOTOR",
    "NBCC","ALKEM","JSWSTEEL","SHRIRAMFIN","KALYANKJIL","BAJAJFINSV","LUPIN",
    "SRF","GODREJPROP","TITAGARH","NUVAMA","PNB","JIOPIN","PATANJALI",
    "ADANIENSOL","CANBK","NATIONALUM","HINDUNILVR","NAUKRI","SWIGGY","RELIANCE",
    "ASIANPAINT","BANDHANBNK","BHEL","YESBANK","M&M","ONGC","IDEA","INDHOTEL",
    "PETRONET","SAIL","GLENMARK","KAYNES","NYKAA","SBILIFE","INDUSINDBK","TRENT",
    "ADANIGREEN","EXIDEIND","TATAELXSI","INFY","NMDC","JSWENERGY","DALBHARAT",
    "ETERNAL","IEX","LICHSGFIN","AMBER","TMPVL","BAJFINANCE","BSE","CROMPTON",
    "TATACONSUM","AUROPHARMA","MOTHERSON","VBL","NHPC","GRASIM","PFC","KPITTECH",
    "MAZDOCK","SUPREMEIND",
}

# Index symbols for full option chain
INDEX_SYMBOLS = {"BANKNIFTY", "NIFTY"}

# NSE holidays 2026
NSE_HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
    "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
}

# ─── LOGGING ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("/root/trueflow/bhav_fetcher.log")]
)
log = logging.getLogger("BhavFetcher")


# ─── HELPERS ──────────────────────────────────────────
def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")


def is_trading_day(today=None):
    today = today or datetime.now(IST).date()
    if today.weekday() >= 5:
        return False
    if str(today) in NSE_HOLIDAYS_2026:
        return False
    return True


def _tf_prepare_rows(table, rows):
    """Added by fix_bhav3.py.
    1. oi_chain_eod rows are packed as session_date/symbol/expiry/strike + data.
    2. Every row is padded to the same key set (PostgREST PGRST102 guard)."""
    if not rows:
        return rows
    if table == "oi_chain_eod":
        packed = {}
        for r in rows:
            exp = str(r.get("expiry") or "")
            key = (str(r.get("session_date")), str(r.get("symbol")), exp,
                   str(r.get("strike")))
            data = {k: v for k, v in r.items()
                    if k not in ("session_date", "symbol", "expiry", "strike")}
            packed[key] = {"session_date": r.get("session_date"),
                           "symbol": r.get("symbol"), "expiry": exp,
                           "strike": r.get("strike"), "data": data}
        rows = list(packed.values())
    keys = set()
    for r in rows:
        keys.update(r.keys())
    return [{k: r.get(k) for k in keys} for r in rows]


def supabase_upsert(table, rows, on_conflict):
    rows = _tf_prepare_rows(table, rows)
    """Upsert rows to Supabase in batches."""
    if not rows:
        return 0
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal,resolution=merge-duplicates"
    }
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    total = 0
    batch_size = 200
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        try:
            resp = requests.post(url, json=batch, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                total += len(batch)
            else:
                log.warning(f"Supabase {table} batch {i//batch_size+1} error: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            log.error(f"Supabase {table} error: {e}")
        time.sleep(0.2)
    return total


def download_nse_zip(url, date_str):
    """Download and extract CSV from NSE zip file."""
    full_url = url.format(date=date_str)
    log.info(f"Downloading: {full_url}")

    # NSE requires a session with cookies from the main page first
    session = requests.Session()
    session.headers.update(NSE_HEADERS)

    # Hit the main page first to get cookies
    try:
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
    except:
        pass

    try:
        resp = session.get(full_url, timeout=60)
        if resp.status_code != 200:
            log.warning(f"Download failed: HTTP {resp.status_code}")
            return None

        # Extract CSV from zip
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        csv_name = z.namelist()[0]  # first file in zip
        csv_data = z.read(csv_name).decode('utf-8')
        log.info(f"Extracted: {csv_name} ({len(csv_data)} bytes)")
        return csv_data

    except Exception as e:
        log.error(f"Download error: {e}")
        return None


# ─── EQUITY BHAV COPY PARSER ─────────────────────────
def parse_equity_bhav(csv_data):
    """
    Parse equity bhav copy (UDiFF format).
    Columns: TckrSymb, SctySrs, OpnPric, HghPric, LwPric, ClsPric, 
             LastPric, PrvsClsgPric, TtlTradgVol, TtlTrfVal, TotalTrades, ...
    """
    rows = []
    reader = csv.DictReader(io.StringIO(csv_data))
    for row in reader:
        try:
            series = row.get('SctySrs', '').strip()
            if series != 'EQ':  # only equity series
                continue

            symbol = row.get('TckrSymb', '').strip()
            if not symbol:
                continue

            rows.append({
                "symbol": symbol,
                "open": float(row.get('OpnPric', 0) or 0),
                "high": float(row.get('HghPric', 0) or 0),
                "low": float(row.get('LwPric', 0) or 0),
                "close": float(row.get('ClsPric', 0) or 0),
                "last": float(row.get('LastPric', 0) or 0),
                "prev_close": float(row.get('PrvsClsgPric', 0) or 0),
                "volume": int(float(row.get('TtlTradgVol', 0) or 0)),
                "turnover": float(row.get('TtlTrfVal', 0) or 0),
                "total_trades": int(float(row.get('TotalTrades', 0) or 0)),
            })
        except Exception as e:
            continue

    log.info(f"Equity bhav: {len(rows)} EQ stocks parsed")
    return rows


# ─── F&O BHAV COPY PARSER ────────────────────────────
def parse_fo_bhav(csv_data, session_date):
    """
    Parse F&O bhav copy (UDiFF format).
    Key columns: TckrSymb, FinInstrmTp (STO=stock option, STF=stock future, 
                 IDO=index option, IDF=index future),
                 StrkPric, XpryDt, OpnPric, HghPric, LwPric, ClsPric,
                 SttlmPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol
    """
    # Separate into futures and options
    futures = []
    options = []
    index_options = []  # BankNifty + Nifty

    reader = csv.DictReader(io.StringIO(csv_data))
    for row in reader:
        try:
            symbol = row.get('TckrSymb', '').strip()
            inst_type = row.get('FinInstrmTp', '').strip()
            
            if not symbol or not inst_type:
                continue

            # Parse common fields
            strike = float(row.get('StrkPric', 0) or 0)
            expiry_raw = row.get('XpryDt', '').strip()
            open_p = float(row.get('OpnPric', 0) or 0)
            high_p = float(row.get('HghPric', 0) or 0)
            low_p = float(row.get('LwPric', 0) or 0)
            close_p = float(row.get('ClsPric', 0) or 0)
            settle = float(row.get('SttlmPric', 0) or 0)
            oi = int(float(row.get('OpnIntrst', 0) or 0))
            oi_chg = int(float(row.get('ChngInOpnIntrst', 0) or 0))
            volume = int(float(row.get('TtlTradgVol', 0) or 0))
            
            # Parse expiry date
            expiry = expiry_raw  # keep as string for now

            # Option type from inst type
            # STO = Stock Option, IDO = Index Option
            # Need to determine CE/PE from OptnTp column
            option_type = row.get('OptnTp', '').strip()  # CE or PE

            if inst_type in ('STF', 'IDF'):
                # Futures
                futures.append({
                    "symbol": symbol,
                    "inst_type": "FUT",
                    "expiry": expiry,
                    "close": close_p,
                    "settle": settle,
                    "oi": oi,
                    "oi_chg": oi_chg,
                    "volume": volume,
                    "session_date": session_date,
                })

            elif inst_type in ('STO', 'IDO') and option_type in ('CE', 'PE'):
                entry = {
                    "symbol": symbol,
                    "inst_type": "OPT",
                    "option_type": option_type,
                    "strike": strike,
                    "expiry": expiry,
                    "close": close_p,
                    "settle": settle,
                    "oi": oi,
                    "oi_chg": oi_chg,
                    "volume": volume,
                    "session_date": session_date,
                }

                if symbol in INDEX_SYMBOLS:
                    index_options.append(entry)
                elif symbol in FO_SYMBOLS:
                    options.append(entry)

        except Exception as e:
            continue

    log.info(f"F&O bhav: {len(futures)} futures, {len(options)} stock options, {len(index_options)} index options")
    return futures, options, index_options


# ─── OI ANALYSIS ─────────────────────────────────────
def compute_stock_oi_summary(futures, options, session_date):
    """
    For each F&O stock, compute:
    - Futures OI and OI change (nearest expiry)
    - Total CE OI vs PE OI across strikes (nearest expiry)
    - PCR
    - OI buildup classification
    """
    results = []

    # Group futures by symbol — pick nearest expiry
    fut_by_sym = defaultdict(list)
    for f in futures:
        if f["symbol"] in FO_SYMBOLS:
            fut_by_sym[f["symbol"]].append(f)

    # Group options by symbol
    opt_by_sym = defaultdict(list)
    for o in options:
        opt_by_sym[o["symbol"]].append(o)

    for symbol in FO_SYMBOLS:
        try:
            # Futures data — nearest expiry
            sym_futs = sorted(fut_by_sym.get(symbol, []), key=lambda x: x["expiry"])
            fut = sym_futs[0] if sym_futs else None

            fut_oi = fut["oi"] if fut else 0
            fut_oi_chg = fut["oi_chg"] if fut else 0
            fut_close = fut["close"] if fut else 0

            # Options — nearest expiry only
            sym_opts = opt_by_sym.get(symbol, [])
            if sym_opts:
                nearest_exp = min(o["expiry"] for o in sym_opts)
                sym_opts = [o for o in sym_opts if o["expiry"] == nearest_exp]

            total_ce_oi = sum(o["oi"] for o in sym_opts if o["option_type"] == "CE")
            total_pe_oi = sum(o["oi"] for o in sym_opts if o["option_type"] == "PE")
            pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0

            # Max OI strikes
            ce_opts = [o for o in sym_opts if o["option_type"] == "CE" and o["oi"] > 0]
            pe_opts = [o for o in sym_opts if o["option_type"] == "PE" and o["oi"] > 0]

            max_ce_strike = max(ce_opts, key=lambda x: x["oi"])["strike"] if ce_opts else None
            max_pe_strike = max(pe_opts, key=lambda x: x["oi"])["strike"] if pe_opts else None
            max_ce_oi = max(ce_opts, key=lambda x: x["oi"])["oi"] if ce_opts else 0
            max_pe_oi = max(pe_opts, key=lambda x: x["oi"])["oi"] if pe_opts else 0

            # OI buildup from futures
            if fut_oi_chg > 0 and fut_close > 0:
                # Need price change — approximate from settle vs prev
                oi_buildup = "Long Buildup" if fut_oi_chg > 0 else "Unknown"
            else:
                oi_buildup = "Unknown"

            # Better: use price change direction from equity bhav if available
            # For now, use a simple heuristic from futures OI change direction

            results.append({
                "symbol": symbol,
                "session_date": session_date,
                "fut_oi": fut_oi,
                "fut_oi_chg": fut_oi_chg,
                "fut_close": fut_close,
                "total_ce_oi": total_ce_oi,
                "total_pe_oi": total_pe_oi,
                "pcr": pcr,
                "max_ce_strike": max_ce_strike,
                "max_ce_oi": max_ce_oi,
                "max_pe_strike": max_pe_strike,
                "max_pe_oi": max_pe_oi,
                "oi_buildup": oi_buildup,
            })

        except Exception as e:
            log.warning(f"{symbol}: OI summary error — {e}")
            continue

    log.info(f"OI summary computed for {len(results)} F&O stocks")
    return results


def compute_index_oi_chain(index_options, session_date):
    """
    Compute full option chain for BankNifty and Nifty.
    Stores per-strike OI data in oi_chain table.
    """
    rows = []

    for symbol in INDEX_SYMBOLS:
        sym_opts = [o for o in index_options if o["symbol"] == symbol]
        if not sym_opts:
            continue

        # Get nearest expiry
        expiries = sorted(set(o["expiry"] for o in sym_opts))
        nearest_exp = expiries[0] if expiries else None
        if not nearest_exp:
            continue

        # Filter to nearest expiry
        chain = [o for o in sym_opts if o["expiry"] == nearest_exp]

        # Get all strikes
        strikes = sorted(set(o["strike"] for o in chain))

        for strike in strikes:
            ce = next((o for o in chain if o["strike"] == strike and o["option_type"] == "CE"), None)
            pe = next((o for o in chain if o["strike"] == strike and o["option_type"] == "PE"), None)

            rows.append({
                "symbol": symbol,
                "session_date": session_date,
                "expiry": nearest_exp,
                "strike": strike,
                "ce_oi": ce["oi"] if ce else 0,
                "ce_oi_chg": ce["oi_chg"] if ce else 0,
                "ce_volume": ce["volume"] if ce else 0,
                "ce_close": ce["close"] if ce else 0,
                "pe_oi": pe["oi"] if pe else 0,
                "pe_oi_chg": pe["oi_chg"] if pe else 0,
                "pe_volume": pe["volume"] if pe else 0,
                "pe_close": pe["close"] if pe else 0,
            })

        # Log summary
        total_ce = sum(r["ce_oi"] for r in rows if r["symbol"] == symbol)
        total_pe = sum(r["pe_oi"] for r in rows if r["symbol"] == symbol)
        pcr = round(total_pe / total_ce, 2) if total_ce > 0 else 0
        log.info(f"{symbol}: {len(strikes)} strikes, PCR={pcr}, Expiry={nearest_exp}")

    return rows


# ─── OI BUILDUP CLASSIFICATION (with equity price data) ──
def classify_oi_buildup(oi_summaries, equity_data):
    """Refine OI buildup using equity price change."""
    eq_map = {e["symbol"]: e for e in equity_data}

    for row in oi_summaries:
        eq = eq_map.get(row["symbol"])
        if not eq or eq["prev_close"] == 0:
            continue

        price_chg_pct = round((eq["close"] - eq["prev_close"]) / eq["prev_close"] * 100, 2)
        oi_chg = row["fut_oi_chg"]

        if price_chg_pct >= 0 and oi_chg > 0:
            row["oi_buildup"] = "Long Buildup"
        elif price_chg_pct < 0 and oi_chg > 0:
            row["oi_buildup"] = "Short Buildup"
        elif price_chg_pct >= 0 and oi_chg < 0:
            row["oi_buildup"] = "Short Covering"
        elif price_chg_pct < 0 and oi_chg < 0:
            row["oi_buildup"] = "Long Unwinding"
        else:
            row["oi_buildup"] = "Neutral"

        row["price_chg_pct"] = price_chg_pct

    return oi_summaries


# ─── MAIN ─────────────────────────────────────────────
def prev_trading_day(d):
    """The last trading day strictly before d."""
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def already_have(session_date):
    """True if fo_bhav_oi already holds rows for that session."""
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/fo_bhav_oi?select=symbol&session_date=eq.{session_date}&limit=1",
                         headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=30)
        return r.status_code == 200 and len(r.json()) > 0
    except Exception as e:
        log.warning(f"Could not check fo_bhav_oi for {session_date}: {e}")
        return False


def run(target=None, catchup=False):
    """target: the session to fetch (default today, IST).
    catchup: morning mode - fetch the previous trading day only if the evening
    runs missed it. NSE sometimes publishes the F&O file after 10 PM IST."""
    today = target or datetime.now(IST).date()
    date_str = today.strftime("%Y%m%d")
    session_date = str(today)

    log.info("=" * 60)
    log.info(f"TrueFlow NSE Bhav Copy Fetcher — {today}")
    log.info("=" * 60)

    if not is_trading_day(today):
        log.info(f"Not a trading day ({today}) — exiting.")
        return
    if catchup and already_have(str(today)):
        log.info(f"Catch-up: fo_bhav_oi already has {today} — nothing to do.")
        return

    # ── STEP 1: Download Equity Bhav Copy ──
    log.info("Downloading Equity Bhav Copy...")
    eq_csv = download_nse_zip(NSE_CM_URL, date_str)
    equity_data = []
    if eq_csv:
        equity_data = parse_equity_bhav(eq_csv)
        log.info(f"Equity: {len(equity_data)} stocks parsed")
    else:
        log.warning("Equity bhav copy not available — will retry")
        # Retry after 10 minutes
        time.sleep(600)
        eq_csv = download_nse_zip(NSE_CM_URL, date_str)
        if eq_csv:
            equity_data = parse_equity_bhav(eq_csv)

    # ── STEP 2: Download F&O Bhav Copy ──
    log.info("Downloading F&O Bhav Copy...")
    fo_csv = download_nse_zip(NSE_FO_URL, date_str)
    if not fo_csv:
        log.warning("F&O bhav copy not available — will retry")
        time.sleep(600)
        fo_csv = download_nse_zip(NSE_FO_URL, date_str)

    if not fo_csv:
        log.error("F&O bhav copy failed after retry")
        # Evening misses are normal when NSE publishes late - the 6:30 AM
        # catch-up fetches it then. Only a miss at catch-up is worth a message.
        if catchup:
            send_telegram(f"❌ <b>Bhav Fetcher Failed</b>\nF&O bhav copy for {today} still not available at the morning catch-up")
        else:
            log.info("Evening miss - the morning catch-up will try again (no Telegram).")
        return

    futures, stock_options, index_options = parse_fo_bhav(fo_csv, session_date)

    # ── STEP 3: Compute OI summaries ──
    log.info("Computing OI summaries...")
    oi_summaries = compute_stock_oi_summary(futures, stock_options, session_date)

    # Classify buildup using equity price changes
    if equity_data:
        oi_summaries = classify_oi_buildup(oi_summaries, equity_data)

    # ── STEP 4: Compute index option chains ──
    log.info("Computing index option chains...")
    index_chain = compute_index_oi_chain(index_options, session_date)

    # ── STEP 5: Store in Supabase ──
    log.info("Saving to Supabase...")

    # OI summaries → fo_bhav_oi
    oi_count = supabase_upsert("fo_bhav_oi", oi_summaries, "symbol,session_date")
    log.info(f"fo_bhav_oi: {oi_count} rows upserted")

    # Index chain → oi_chain
    chain_count = supabase_upsert("oi_chain_eod", index_chain, "session_date,symbol,expiry,strike")  # fix_bhav3: not oi_chain
    log.info(f"oi_chain: {chain_count} rows upserted")

    # ── STEP 6: Summary stats for Telegram ──
    long_buildup = sum(1 for o in oi_summaries if o.get("oi_buildup") == "Long Buildup")
    short_buildup = sum(1 for o in oi_summaries if o.get("oi_buildup") == "Short Buildup")
    short_covering = sum(1 for o in oi_summaries if o.get("oi_buildup") == "Short Covering")
    long_unwinding = sum(1 for o in oi_summaries if o.get("oi_buildup") == "Long Unwinding")

    # Top PCR stocks
    high_pcr = sorted([o for o in oi_summaries if o["pcr"] > 0], key=lambda x: x["pcr"], reverse=True)[:5]
    low_pcr = sorted([o for o in oi_summaries if o["pcr"] > 0], key=lambda x: x["pcr"])[:5]

    # BankNifty/Nifty summary
    bnf_chain = [c for c in index_chain if c["symbol"] == "BANKNIFTY"]
    nifty_chain = [c for c in index_chain if c["symbol"] == "NIFTY"]

    def chain_summary(chain, name):
        if not chain:
            return f"  {name}: No data"
        total_ce = sum(c["ce_oi"] for c in chain)
        total_pe = sum(c["pe_oi"] for c in chain)
        pcr = round(total_pe / total_ce, 2) if total_ce > 0 else 0
        max_ce = max(chain, key=lambda c: c["ce_oi"])
        max_pe = max(chain, key=lambda c: c["pe_oi"])
        return (
            f"  {name}: PCR {pcr}\n"
            f"  🔴 CE Wall: {max_ce['strike']:,.0f} ({max_ce['ce_oi']//1000}K)\n"
            f"  🟢 PE Wall: {max_pe['strike']:,.0f} ({max_pe['pe_oi']//1000}K)"
        )

    msg = (
        f"📦 <b>NSE Bhav Copy Report</b>{' (late file, caught up this morning)' if catchup else ''}\n"
        f"{'━'*28}\n"
        f"📅 {today.strftime('%d %b %Y')}\n\n"
        f"📊 <b>F&O OI Buildup</b>\n"
        f"  🟢 Long Buildup: <b>{long_buildup}</b>\n"
        f"  🔴 Short Buildup: <b>{short_buildup}</b>\n"
        f"  🔵 Short Covering: <b>{short_covering}</b>\n"
        f"  ⚪ Long Unwinding: <b>{long_unwinding}</b>\n\n"
        f"📊 <b>Index OI</b>\n"
        f"{chain_summary(bnf_chain, 'BankNifty')}\n\n"
        f"{chain_summary(nifty_chain, 'Nifty')}\n\n"
        f"📈 <b>Highest PCR (bullish floor)</b>\n"
        f"  {', '.join('%s (%s)' % (o['symbol'], o['pcr']) for o in high_pcr)}\n\n"
        f"📉 <b>Lowest PCR (call heavy)</b>\n"
        f"  {', '.join('%s (%s)' % (o['symbol'], o['pcr']) for o in low_pcr)}\n\n"
        f"✅ Equity: {len(equity_data)} | F&O OI: {oi_count} | Chain: {chain_count}"
    )
    send_telegram(msg)
    log.info("Bhav copy processing complete. Telegram sent.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--catchup", action="store_true",
                    help="morning run: fetch the previous trading day if the evening runs missed it")
    ap.add_argument("--date", help="fetch one specific session, YYYY-MM-DD")
    a = ap.parse_args()
    if a.date:
        run(target=datetime.strptime(a.date, "%Y-%m-%d").date())
    elif a.catchup:
        run(target=prev_trading_day(datetime.now(IST).date()), catchup=True)
    else:
        run()
