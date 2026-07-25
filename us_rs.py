#!/usr/bin/env python3
"""
TrueFlow US — Relative Strength vs SPY / QQQ
=============================================
Standalone by design: it does NOT touch us_momentum_scan.py. It writes to its
own table (us_rs) which the dashboard joins on symbol, so the pipeline that
feeds everything else is untouched.

What it computes, per stock:
  rs_1m / rs_3m / rs_6m : the stock's % return MINUS SPY's % return over the
                          same window. Positive = outperforming the market.
  rs_rank_1m / rs_rank_3m : percentile rank (0-99) of that outperformance
                          across the whole universe. 99 = strongest in the US.
  rs_vs_qqq_3m          : same idea against QQQ, useful for tech/growth names.
  rs_line_high          : True if the stock's RS line (price/SPY ratio) is at
                          its highest in 3 months — the classic "new RS high"
                          that leadership screens look for.

Source: yfinance batch download (no key, no login). ~20 requests total.
Run: /root/trueflow/bin/python /root/trueflow/us_rs.py
"""
import os, time, logging, datetime as dt
import requests
import pandas as pd
import numpy as np
import yfinance as yf

SUPABASE_URL = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"

TELEGRAM_TOKEN = os.environ.get("TF_TG_TOKEN", "")
TELEGRAM_CHAT  = "1202026803"

BATCH_SIZE  = 100
BATCH_SLEEP = 1.0
FETCH_DAYS  = 260          # calendar days of history (~6 months of sessions)
W1M, W3M, W6M = 21, 63, 126   # trading sessions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("us_rs")

SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
      "Content-Type": "application/json"}


def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB, timeout=60)
    r.raise_for_status()
    return r.json()


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return
    h = dict(SB); h["Prefer"] = "resolution=merge-duplicates"
    for i in range(0, len(rows), 300):
        chunk = rows[i:i + 300]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                          headers=h, json=chunk, timeout=120)
        if r.status_code >= 300:
            log.warning("batch upsert failed (%s) — retrying row by row", r.status_code)
            for one in chunk:
                rr = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                                   headers=h, json=[one], timeout=60)
                if rr.status_code >= 300:
                    log.error("row upsert %s failed: %s %s", one.get("symbol"),
                              rr.status_code, rr.text[:180])


def telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
                      timeout=20)
    except Exception as e:
        log.warning("telegram failed: %s", e)


def load_universe():
    syms, off = [], 0
    while True:
        rows = sb_get(f"us_universe?select=symbol&limit=1000&offset={off}")
        if not rows:
            break
        syms += [r["symbol"].upper() for r in rows]
        if len(rows) < 1000:
            break
        off += 1000
    log.info("Universe: %d symbols", len(syms))
    return sorted(set(syms))


def close_series(df, sym):
    """Pull the Close series for one symbol out of a yfinance batch frame."""
    try:
        if isinstance(df.columns, pd.MultiIndex):
            if sym not in df.columns.get_level_values(0):
                return None
            s = df[sym]["Close"]
        else:
            s = df["Close"]
        s = s.dropna()
        return s if len(s) > 5 else None
    except Exception:
        return None


def pct_change_over(s, n):
    if s is None or len(s) <= n:
        return None
    try:
        a, b = float(s.iloc[-1]), float(s.iloc[-1 - n])
        if b == 0:
            return None
        return (a / b - 1.0) * 100.0
    except Exception:
        return None


def main():
    log.info("=" * 60)
    log.info("US Relative Strength vs SPY / QQQ — %s", dt.date.today())
    log.info("=" * 60)

    # 1) benchmarks
    end = dt.date.today() + dt.timedelta(days=1)
    start = dt.date.today() - dt.timedelta(days=FETCH_DAYS)
    bench = yf.download(["SPY", "QQQ"], start=str(start), end=str(end),
                        group_by="ticker", auto_adjust=False, progress=False, threads=True)
    spy = close_series(bench, "SPY")
    qqq = close_series(bench, "QQQ")
    if spy is None:
        log.error("could not fetch SPY — aborting")
        return
    spy_1m, spy_3m, spy_6m = pct_change_over(spy, W1M), pct_change_over(spy, W3M), pct_change_over(spy, W6M)
    qqq_3m = pct_change_over(qqq, W3M) if qqq is not None else None
    log.info("SPY 1m %.2f%% | 3m %.2f%% | 6m %.2f%%",
             spy_1m or 0, spy_3m or 0, spy_6m or 0)

    # SPY series aligned by date for the RS-line calculation
    spy_by_date = {d.date() if hasattr(d, "date") else d: float(v)
                   for d, v in spy.items()}

    universe = load_universe()
    recs = []

    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i:i + BATCH_SIZE]
        try:
            df = yf.download(batch, start=str(start), end=str(end), group_by="ticker",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            log.warning("batch %d failed: %s", i // BATCH_SIZE, str(e)[:100])
            time.sleep(BATCH_SLEEP)
            continue

        for sym in batch:
            s = close_series(df, sym)
            if s is None:
                continue
            r1, r3, r6 = pct_change_over(s, W1M), pct_change_over(s, W3M), pct_change_over(s, W6M)
            rs1 = round(r1 - spy_1m, 2) if (r1 is not None and spy_1m is not None) else None
            rs3 = round(r3 - spy_3m, 2) if (r3 is not None and spy_3m is not None) else None
            rs6 = round(r6 - spy_6m, 2) if (r6 is not None and spy_6m is not None) else None
            rsq = round(r3 - qqq_3m, 2) if (r3 is not None and qqq_3m is not None) else None

            # RS line at a 3-month high? (price / SPY ratio)
            rs_line_high = None
            try:
                ratio = []
                for d, v in s.items():
                    dd = d.date() if hasattr(d, "date") else d
                    b = spy_by_date.get(dd)
                    if b:
                        ratio.append(float(v) / b)
                if len(ratio) > W3M // 2:
                    tail = ratio[-W3M:] if len(ratio) >= W3M else ratio
                    rs_line_high = bool(tail[-1] >= max(tail) * 0.999)
            except Exception:
                rs_line_high = None

            if rs1 is None and rs3 is None:
                continue
            recs.append({"symbol": sym, "rs_1m": rs1, "rs_3m": rs3, "rs_6m": rs6,
                         "rs_vs_qqq_3m": rsq, "rs_line_high": rs_line_high})

        log.info("… %d/%d symbols processed (%d with RS)",
                 min(i + BATCH_SIZE, len(universe)), len(universe), len(recs))
        time.sleep(BATCH_SLEEP)

    # 2) percentile ranks across the universe
    def add_rank(key, out_key):
        vals = [(r["symbol"], r[key]) for r in recs if r.get(key) is not None]
        vals.sort(key=lambda x: x[1])
        n = len(vals)
        rank = {}
        for idx, (sym, _v) in enumerate(vals):
            rank[sym] = int(round(idx / max(n - 1, 1) * 99))
        for r in recs:
            r[out_key] = rank.get(r["symbol"])

    add_rank("rs_1m", "rs_rank_1m")
    add_rank("rs_3m", "rs_rank_3m")

    now = dt.datetime.utcnow().isoformat() + "Z"
    session = str(dt.date.today())
    for r in recs:
        r["session_date"] = session
        r["updated_at"] = now
        # make sure every row carries identical keys (Supabase batch requirement)
        for k in ("rs_1m", "rs_3m", "rs_6m", "rs_vs_qqq_3m", "rs_line_high",
                  "rs_rank_1m", "rs_rank_3m"):
            r.setdefault(k, None)

    sb_upsert("us_rs", recs, "symbol")

    leaders = sum(1 for r in recs if (r.get("rs_rank_3m") or 0) >= 90)
    newhigh = sum(1 for r in recs if r.get("rs_line_high"))
    log.info("Done: %d symbols with RS, %d in top decile, %d at RS-line highs",
             len(recs), leaders, newhigh)
    telegram(f"🇺🇸 <b>US Relative Strength updated</b>\n"
             f"Symbols ranked: <b>{len(recs)}</b>\n"
             f"🏆 Top decile vs SPY (3m): <b>{leaders}</b>\n"
             f"📈 RS line at 3-month high: <b>{newhigh}</b>\n"
             f"SPY 3m: {spy_3m:+.1f}%" if spy_3m is not None else "")


if __name__ == "__main__":
    main()
