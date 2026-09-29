#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
us_mtf_scan.py - TrueFlow multi-timeframe engine (US)

Runs India's mtf_scan.py maths UNCHANGED - it imports Series, build_row,
evidence_for and summarise_evidence from that file - so every state, EMA base
and evidence number means exactly the same thing on both dashboards. Only the
data source and the destination differ:

  reads   us_momentum_stocks (latest session = the universe, with names)
          us_daily_ohlc      (~3 years of daily candles, from us_ohlc_backfill.py)
  writes  us_momentum_mtf    (one row per stock, overwritten nightly)
          us_momentum_mtf_hist (dated copy, without the candle string)
          us_mtf_evidence

US-specific differences, stated plainly:
  * No IPO discovery. India scans the exchange's instrument list for new
    listings; there is no equivalent here. A stock whose history starts more
    than 10 days after the earliest date in us_daily_ohlc gets the listing
    columns filled (listing date = its first candle), exactly as India does
    for universe stocks with short history.
  * avg_turnover_cr is left empty (it is rupees-crore by name).

USAGE
  /root/trueflow/bin/python us_mtf_scan.py --dry-run --symbols AAPL,NVDA
  /root/trueflow/bin/python us_mtf_scan.py
"""
import sys, time, argparse
from datetime import datetime, date, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import requests

sys.path.insert(0, "/root/trueflow")
import tf_config as CFG
import mtf_scan as M          # India engine - maths reused as-is
from supabase import create_client

H = {"apikey": CFG.SUPABASE_KEY, "Authorization": "Bearer " + CFG.SUPABASE_KEY}
THREADS = 8


def log(m):
    print(m, flush=True)


def get(path):
    for attempt in range(4):
        try:
            r = requests.get(CFG.SUPABASE_URL + "/rest/v1/" + path, headers=H, timeout=90)
            if r.status_code == 200:
                return r.json()
            log("  read %s -> %s %s" % (path[:60], r.status_code, r.text[:120]))
        except Exception as e:
            log("  read error %s: %s" % (path[:60], str(e)[:100]))
        time.sleep(2 * (attempt + 1))
    return None


def universe():
    sd = get("us_momentum_stocks?select=session_date&order=session_date.desc&limit=1")[0]["session_date"]
    out, off = {}, 0
    while True:
        p = get("us_momentum_stocks?select=symbol,company_name&session_date=eq.%s"
                "&order=symbol.asc&limit=1000&offset=%d" % (sd, off)) or []
        for x in p:
            if x.get("symbol"):
                out[x["symbol"]] = x.get("company_name")
        if len(p) < 1000:
            break
        off += 1000
    return sd, out


def candles(sym):
    rows = get("us_daily_ohlc?symbol=eq.%s&select=d,o,h,l,c,v&order=d.asc&limit=1000"
               % requests.utils.quote(sym, safe="")) or []
    return [{"date": r["d"], "open": r["o"], "high": r["h"], "low": r["l"],
             "close": r["c"], "volume": r.get("v") or 0}
            for r in rows if r.get("c") is not None and r.get("o") is not None
            and r.get("h") is not None and r.get("l") is not None]


def tg(msg):
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG.TF_ALERTS_TOKEN,
                      data={"chat_id": CFG.TF_US, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-evidence", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    today = datetime.now(timezone.utc).date()

    sd, uni = universe()
    want = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    syms = want or sorted(uni)
    if a.limit:
        syms = syms[:a.limit]
    log("US MTF scan | universe session %s | %d symbols" % (sd, len(syms)))

    with ThreadPoolExecutor(THREADS) as ex:
        data = dict(zip(syms, ex.map(candles, syms)))
    have = {s: c for s, c in data.items() if len(c) >= 2}
    if not have:
        log("No candles in us_daily_ohlc - run us_ohlc_backfill.py first.")
        return
    fetch_from = min(M.to_date(c[0]["date"]) for c in have.values())
    log("candles loaded for %d/%d symbols in %.0fs (history from %s)"
        % (len(have), len(syms), time.time() - t0, fetch_from))

    rows, acc, skipped = [], {}, {}
    for n, sym in enumerate(syms, 1):
        cs = data.get(sym) or []
        if len(cs) < 2:
            skipped["no candles"] = skipped.get("no candles", 0) + 1
            continue
        if (today - M.to_date(cs[-1]["date"])).days > 7:
            skipped["stale (no trade in 7 days)"] = skipped.get("stale (no trade in 7 days)", 0) + 1
            continue
        try:
            S = M.Series(cs)
            row = M.build_row(sym, {"exchange": "US", "name": uni.get(sym), "is_ipo": False},
                              S, fetch_from)
            if row:
                row["avg_turnover_cr"] = None
                rows.append(row)
            if not a.no_evidence and S.n > 260:
                M.evidence_for(S, acc)
        except Exception as e:
            skipped["error"] = skipped.get("error", 0) + 1
            log("  %s: %s" % (sym, str(e)[:100]))
        if n % 250 == 0:
            log("  %d/%d  (%.0fs)" % (n, len(syms), time.time() - t0))

    cnt = {}
    for r in rows:
        cnt[r["mtf_state"]] = cnt.get(r["mtf_state"], 0) + 1
    bases = [r for r in rows if r.get("ema_base")]
    log("ROWS %d   skipped %s" % (len(rows), skipped or "none"))
    for s in M.STATES:
        log("   %-22s %d" % (M.STATE_NAMES[s], cnt.get(s, 0)))
    log("   EMA bases: %d" % len(bases))
    if want:
        for r in rows:
            log("\n  %s  %s | daily %s weekly %s (dev %s) monthly %s%s | %%9E %s %%50E %s %%200E %s ADR %s%%%s" % (
                r["symbol"], M.STATE_NAMES.get(r["mtf_state"], r["mtf_state"]), r["d_state"], r["w_state"],
                r["w_state_dev"], r["m_state"], " (short history)" if r["m_short_history"] else "",
                r["pct_from_ema9_d"], r["pct_from_ema50"], r["pct_from_ema200"], r["adr_pct"],
                (" | EMA base %s %sd %s%%" % (r["ema_base"], r["base_days"], r["base_depth_pct"])) if r.get("ema_base") else ""))

    ev = M.summarise_evidence(acc, today) if acc else []
    if a.dry_run:
        log("\nDRY RUN - nothing written. evidence rows %d  (%.0fs)" % (len(ev), time.time() - t0))
        return

    sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
    w1 = M.write_rows(sb, "us_momentum_mtf", rows, "symbol")
    hist = [dict(r, ohlc_120=None) for r in rows]          # history without the 3 KB candle string
    w3 = M.write_rows(sb, "us_momentum_mtf_hist", hist, "symbol,session_date")
    w2 = M.write_rows(sb, "us_mtf_evidence", ev, "state,horizon") if ev else 0
    msg = ("🧭 <b>US MTF scan done</b>\n%d stocks · %d All Timeframes Up · %d HTF Pullback · %d EMA bases"
           " · history %d · evidence %d · %.0f min" % (w1, cnt.get("allup", 0), cnt.get("htfpb", 0),
                                                        len(bases), w3, w2, (time.time() - t0) / 60))
    log(msg.replace("<b>", "").replace("</b>", ""))
    tg(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL: %s" % e)
        tg("⚠️ <b>US MTF scan failed</b>\n%s" % str(e)[:300])
        raise
