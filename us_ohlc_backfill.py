#!/root/trueflow/bin/python
"""
us_ohlc_backfill.py - US daily OHLCV into us_daily_ohlc (for TrueFlow Charts US)

  Full   : /root/trueflow/bin/python us_ohlc_backfill.py
           ~3 years for every symbol in the latest us_momentum_scan session.
           Resumable: finished symbols are kept in us_ohlc_done.json, so if
           Yahoo rate-limits and it dies, just run the same command again.
  Nightly: /root/trueflow/bin/python us_ohlc_backfill.py --append
           last 10 sessions for every symbol (overwrites, so late fixes land).
  Test   : /root/trueflow/bin/python us_ohlc_backfill.py --symbols AAPL,MSFT

Source is yfinance (unofficial Yahoo). Prices are split-adjusted, not
dividend-adjusted (auto_adjust=False), the same basis TradingView shows.
"""
import sys, os, json, time, argparse, math
from datetime import date, timedelta
import requests
import tf_config as CFG
import yfinance as yf

TABLE   = "us_daily_ohlc"
DONE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "us_ohlc_done.json")
CHUNK   = 40          # tickers per Yahoo request
PAUSE   = 2.0         # seconds between Yahoo requests
YEARS   = 3
H = {"apikey": CFG.SUPABASE_KEY, "Authorization": "Bearer " + CFG.SUPABASE_KEY,
     "Content-Type": "application/json"}

def log(m): print(m, flush=True)

def tg(msg):
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG.TF_ALERTS_TOKEN,
                      data={"chat_id": CFG.TF_US, "text": msg}, timeout=20)
    except Exception as e:
        log("telegram failed: %s" % e)

def universe():
    """every symbol in the latest us_momentum_stocks session (paged - 1,000-row cap)"""
    r = requests.get(CFG.SUPABASE_URL + "/rest/v1/us_momentum_stocks?select=session_date"
                     "&order=session_date.desc&limit=1", headers=H, timeout=60)
    r.raise_for_status()
    sd = r.json()[0]["session_date"]
    out, off = [], 0
    while True:
        r = requests.get(CFG.SUPABASE_URL + "/rest/v1/us_momentum_stocks?select=symbol"
                         "&session_date=eq.%s&order=symbol.asc&limit=1000&offset=%d" % (sd, off),
                         headers=H, timeout=60)
        r.raise_for_status()
        p = r.json()
        out += [x["symbol"] for x in p if x.get("symbol")]
        if len(p) < 1000: break
        off += 1000
    return sd, sorted(set(out))

def yahoo(sym):   # BRK.B -> BRK-B on Yahoo
    return sym.replace(".", "-").replace("/", "-")

def num(x, p=4):
    try:
        x = float(x)
        return None if (math.isnan(x) or math.isinf(x)) else round(x, p)
    except Exception:
        return None

def frame_rows(sym, df):
    """one ticker's DataFrame (Open/High/Low/Close/Volume) -> table rows"""
    rows = []
    if df is None or len(df) == 0: return rows
    for idx, r in df.iterrows():
        o, h, l, c = num(r.get("Open")), num(r.get("High")), num(r.get("Low")), num(r.get("Close"))
        if c is None or o is None or h is None or l is None: continue
        v = num(r.get("Volume"), 0)
        rows.append({"symbol": sym, "d": idx.strftime("%Y-%m-%d"),
                     "o": o, "h": h, "l": l, "c": c, "v": int(v) if v is not None else None})
    return rows

def split(data, syms):
    """yf.download result -> {symbol: DataFrame}; handles single and multi ticker shapes"""
    out = {}
    if data is None or len(data) == 0: return out
    cols = data.columns
    multi = getattr(cols, "nlevels", 1) > 1
    for s in syms:
        y = yahoo(s)
        try:
            if multi:
                lv0 = cols.get_level_values(0)
                df = data[y] if y in lv0 else (data.xs(y, axis=1, level=1) if y in cols.get_level_values(1) else None)
            else:
                df = data if len(syms) == 1 else None
            if df is not None: out[s] = df
        except Exception:
            pass
    return out

def push(rows):
    n = 0
    for i in range(0, len(rows), 1000):
        chunk = rows[i:i+1000]
        for attempt in range(3):
            r = requests.post(CFG.SUPABASE_URL + "/rest/v1/%s?on_conflict=symbol,d" % TABLE,
                              headers=dict(H, Prefer="resolution=merge-duplicates,return=minimal"),
                              data=json.dumps(chunk), timeout=120)
            if r.status_code < 300: n += len(chunk); break
            log("  write failed %s: %s" % (r.status_code, r.text[:200])); time.sleep(5)
        else:
            raise SystemExit("Supabase write kept failing - stopping so nothing is half-written silently.")
    return n

def load_done():
    try: return set(json.load(open(DONE)))
    except Exception: return set()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    t0 = time.time()
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]; sd = "manual"
    else:
        sd, syms = universe()
    start = date.today() - (timedelta(days=15) if a.append else timedelta(days=365 * YEARS + 10))
    done = set() if (a.append or a.symbols) else load_done()
    todo = [s for s in syms if s not in done]
    log("US OHLC %s | session %s | %d symbols, %d to fetch, from %s" %
        ("append" if a.append else "full", sd, len(syms), len(todo), start))
    wrote, empty, failed = 0, [], []
    for i in range(0, len(todo), CHUNK):
        batch = todo[i:i+CHUNK]
        data = None
        for attempt in range(3):
            try:
                data = yf.download([yahoo(s) for s in batch], start=start.isoformat(), interval="1d",
                                   auto_adjust=False, actions=False, group_by="ticker",
                                   threads=True, progress=False)
                if data is not None and len(data): break
            except Exception as e:
                log("  yahoo error (%s) - waiting %ds" % (str(e)[:120], 30 * (attempt + 1)))
            time.sleep(30 * (attempt + 1))
        frames = split(data, batch)
        rows = []
        for s in batch:
            rr = frame_rows(s, frames.get(s))
            if rr: rows += rr
            else: empty.append(s)
        if rows:
            wrote += push(rows)
        if not a.append and not a.symbols:
            done.update(s for s in batch if s not in empty)
            json.dump(sorted(done), open(DONE, "w"))
        log("  %d/%d  rows so far %d  no data %d" % (min(i + CHUNK, len(todo)), len(todo), wrote, len(empty)))
        time.sleep(PAUSE)
    msg = ("US OHLC %s done: %d rows, %d symbols, %d with no data, %.0f min"
           % ("append" if a.append else "backfill", wrote, len(todo), len(empty), (time.time() - t0) / 60))
    if empty: msg += "\nNo data: " + ", ".join(empty[:40]) + (" ..." if len(empty) > 40 else "")
    log(msg)
    if not a.symbols: tg(msg)

if __name__ == "__main__":
    main()
