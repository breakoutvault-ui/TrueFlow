#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
market_history.py - one row per trading day per market, for the Market tab

From the daily prices already stored (daily_ohlc / us_daily_ohlc) it rebuilds,
for every day: breadth (% above the 9/20/50/200 EMA), advancers vs decliners,
52-week highs vs lows, market turnover vs its 20-day average, stocks trading
2x+ their average volume (up vs down), momentum bursts, big 1-month movers,
each sector's average move, the trend-state mix (from the MTF history) and the
index close (Nifty 50 / S&P 500, from Yahoo) for the overlay.

Every run rewrites the whole history, so a fix to a rule applies to the past too.
Thresholds live in SETTINGS, one set per market - India and US may need
different numbers (e.g. what counts as a "burst").

CAVEAT: it uses TODAY's stock list, so stocks that dropped out of the universe
are missing from the past - old breadth reads slightly healthier than it was.

USAGE  /root/trueflow/bin/python market_history.py --market IN   [--dry-run]
       /root/trueflow/bin/python market_history.py --market US
"""
import sys, time, json, argparse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests

sys.path.insert(0, "/root/trueflow")
import tf_config as CFG

H = {"apikey": CFG.SUPABASE_KEY, "Authorization": "Bearer " + CFG.SUPABASE_KEY, "Content-Type": "application/json"}
SETTINGS = {
    "IN": dict(uni="momentum_stocks", ohlc="daily_ohlc", mtf="momentum_mtf_hist", index="^NSEI",
               burst_pct=4.0, burst_vol=1.0, big_pct=25.0, big_days=21, rvol_x=2.0),
    "US": dict(uni="us_momentum_stocks", ohlc="us_daily_ohlc", mtf="us_momentum_mtf_hist", index="^GSPC",
               burst_pct=4.0, burst_vol=1.0, big_pct=25.0, big_days=21, rvol_x=2.0),
}
MIN_STOCKS = 50          # a day with fewer stocks priced is skipped (holidays, gaps)


def log(m):
    print(m, flush=True)


def get(path):
    for a in range(4):
        try:
            r = requests.get(CFG.SUPABASE_URL + "/rest/v1/" + path, headers=H, timeout=90)
            if r.status_code == 200:
                return r.json()
            log("  read %s -> %s" % (path[:60], r.status_code))
        except Exception as e:
            log("  read error: %s" % str(e)[:80])
        time.sleep(2 * (a + 1))
    return None


def ema(vals, n):
    k, out, e = 2.0 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def index_closes(ticker, start):
    try:
        import yfinance as yf
        df = yf.download(ticker, start=start, interval="1d", auto_adjust=False, progress=False)
        if df is None or not len(df):
            return {}
        col = df["Close"]
        if hasattr(col, "columns"):          # multi-level columns
            col = col.iloc[:, 0]
        return {i.strftime("%Y-%m-%d"): round(float(v), 2) for i, v in col.dropna().items()}
    except Exception as e:
        log("  index download failed (%s) - overlay will be empty" % str(e)[:80])
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["IN", "US"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    S = SETTINGS[a.market]
    t0 = time.time()
    sd = get("%s?select=session_date&order=session_date.desc&limit=1" % S["uni"])[0]["session_date"]
    uni, off = {}, 0
    while True:
        p = get("%s?select=symbol,sector&session_date=eq.%s&order=symbol.asc&limit=1000&offset=%d" % (S["uni"], sd, off)) or []
        for x in p:
            uni[x["symbol"]] = x.get("sector") or "—"
        if len(p) < 1000:
            break
        off += 1000

    def load(sym):
        rows = get("%s?symbol=eq.%s&select=d,o,h,l,c,v&order=d.asc&limit=1000"
                   % (S["ohlc"], requests.utils.quote(sym, safe=""))) or []
        return [r for r in rows if r.get("c") and r.get("h") and r.get("l")]

    syms = sorted(uni)
    with ThreadPoolExecutor(8) as ex:
        data = dict(zip(syms, ex.map(load, syms)))
    log("Market history %s | %d stocks loaded (%.0fs)" % (a.market, len(data), time.time() - t0))

    D = {}                                   # date -> accumulators
    def acc(d):
        x = D.get(d)
        if x is None:
            x = D[d] = dict(n=0, a9=0, a20=0, a50=0, n200=0, a200=0, adv=0, dec=0, chg=0.0, nhl=0, hi=0, lo=0,
                            turn=0.0, rv_up=0, rv_dn=0, b_up=0, b_dn=0, nbig=0, big_up=0, big_dn=0, sec={})
        return x

    for sym, bars in data.items():
        n = len(bars)
        if n < 30:
            continue
        c = [b["c"] for b in bars]; h = [b["h"] for b in bars]; l = [b["l"] for b in bars]
        v = [b.get("v") or 0 for b in bars]
        e9, e20, e50, e200 = ema(c, 9), ema(c, 20), ema(c, 50), ema(c, 200)
        sec = uni.get(sym, "—")
        for i in range(21, n):
            d = bars[i]["d"]; x = acc(d)
            x["n"] += 1
            x["a9"] += c[i] > e9[i]; x["a20"] += c[i] > e20[i]; x["a50"] += c[i] > e50[i]
            if i >= 200:
                x["n200"] += 1; x["a200"] += c[i] > e200[i]
            chg = (c[i] / c[i - 1] - 1) * 100 if c[i - 1] else 0.0
            x["adv"] += chg > 0; x["dec"] += chg < 0; x["chg"] += chg
            s = x["sec"].setdefault(sec, [0.0, 0]); s[0] += chg; s[1] += 1
            x["turn"] += c[i] * v[i]
            av = sum(v[i - 20:i]) / 20.0
            if av > 0:
                if v[i] >= S["rvol_x"] * av:
                    x["rv_up" if chg > 0 else "rv_dn"] += 1
                if v[i] >= S["burst_vol"] * av:
                    if chg >= S["burst_pct"]:
                        x["b_up"] += 1
                    elif chg <= -S["burst_pct"]:
                        x["b_dn"] += 1
            if i >= S["big_days"] and c[i - S["big_days"]]:
                x["nbig"] += 1
                m = (c[i] / c[i - S["big_days"]] - 1) * 100
                x["big_up"] += m >= S["big_pct"]; x["big_dn"] += m <= -S["big_pct"]
            if i >= 251:
                x["nhl"] += 1
                hi52, lo52 = max(h[i - 251:i + 1]), min(l[i - 251:i + 1])
                x["hi"] += c[i] >= hi52 * 0.99; x["lo"] += c[i] <= lo52 * 1.01

    days = sorted(d for d, x in D.items() if x["n"] >= MIN_STOCKS)
    log("%d trading days with %d+ stocks (%s .. %s)" % (len(days), MIN_STOCKS, days[0] if days else "-", days[-1] if days else "-"))

    # trend-state mix, from the MTF history (only as far back as it goes)
    mix = {}
    off = 0
    while True:
        p = get("%s?select=session_date,mtf_state&order=session_date.asc&limit=1000&offset=%d" % (S["mtf"], off))
        if not p:
            break
        for r in p:
            if r.get("mtf_state"):
                m = mix.setdefault(str(r["session_date"])[:10], {})
                m[r["mtf_state"]] = m.get(r["mtf_state"], 0) + 1
        if len(p) < 1000:
            break
        off += 1000
    idx = index_closes(S["index"], days[0] if days else "2023-01-01")
    log("trend-mix days %d · index closes %d" % (len(mix), len(idx)))

    rows, turns = [], []
    for d in days:
        x = D[d]
        turns.append(x["turn"])
        prev = turns[-21:-1]
        tr = (x["turn"] / (sum(prev) / len(prev))) if len(prev) == 20 and sum(prev) > 0 else None
        pc = lambda k, base: round(100.0 * x[k] / x[base], 1) if x[base] else None
        rows.append(dict(market=a.market, d=d, n=x["n"], pct9=pc("a9", "n"), pct20=pc("a20", "n"), pct50=pc("a50", "n"),
                         pct200=pc("a200", "n200"), adv=x["adv"], dec=x["dec"], avg_chg=round(x["chg"] / x["n"], 3),
                         hi52=x["hi"] if x["nhl"] >= MIN_STOCKS else None, lo52=x["lo"] if x["nhl"] >= MIN_STOCKS else None,
                         turn_ratio=round(tr, 3) if tr else None, rvol2_up=x["rv_up"], rvol2_dn=x["rv_dn"],
                         burst_up=x["b_up"], burst_dn=x["b_dn"],
                         big_up=x["big_up"] if x["nbig"] >= MIN_STOCKS else None, big_dn=x["big_dn"] if x["nbig"] >= MIN_STOCKS else None,
                         idx_close=idx.get(d),
                         sectors={k: round(v[0] / v[1], 3) for k, v in x["sec"].items() if v[1] >= 3 and k != "—"},
                         mtf=mix.get(d)))
    if rows:
        r = rows[-1]
        log("latest %s: above 20E %s%% · A/D %d/%d · highs %s lows %s · turnover x%s · bursts %d/%d · big %s/%s · index %s"
            % (r["d"], r["pct20"], r["adv"], r["dec"], r["hi52"], r["lo52"], r["turn_ratio"], r["burst_up"], r["burst_dn"],
               r["big_up"], r["big_dn"], r["idx_close"]))
    # each stock's move over 6 periods + how far below its 1-year high (for Big movers)
    mv = []
    for sym, bars in data.items():
        if len(bars) < 2:
            continue
        c = [b["c"] for b in bars]; h = [b["h"] for b in bars]; n = len(c)
        def ret(k):
            return round((c[-1] / c[-1 - k] - 1) * 100, 2) if n > k and c[-1 - k] else None
        hi = max(h[-252:])
        mv.append(dict(market=a.market, symbol=sym, d=bars[-1]["d"], sector=uni.get(sym), close=round(c[-1], 4),
                       r1d=ret(1), r1w=ret(5), r1m=ret(21), r3m=ret(63), r6m=ret(126), r1y=ret(252),
                       below_high=round((hi - c[-1]) / hi * 100, 2) if hi else None))
    if mv:
        best = sorted([m for m in mv if m["r3m"] is not None], key=lambda m: -m["r3m"])[:3]
        log("movers: %d stocks · top 3M: %s" % (len(mv), ", ".join("%s %+.0f%%" % (m["symbol"], m["r3m"]) for m in best)))
    if a.dry_run:
        log("DRY RUN - nothing written (%.0fs)" % (time.time() - t0))
        return
    for i in range(0, len(rows), 300):
        rr = requests.post(CFG.SUPABASE_URL + "/rest/v1/market_history?on_conflict=market,d",
                           headers=dict(H, Prefer="resolution=merge-duplicates,return=minimal"),
                           json=rows[i:i + 300], timeout=120)
        if rr.status_code >= 300:
            raise SystemExit("write failed %s: %s" % (rr.status_code, rr.text[:200]))
    requests.delete(CFG.SUPABASE_URL + "/rest/v1/market_movers?market=eq.%s" % a.market, headers=H, timeout=60)
    for i in range(0, len(mv), 500):
        rr = requests.post(CFG.SUPABASE_URL + "/rest/v1/market_movers?on_conflict=market,symbol",
                           headers=dict(H, Prefer="resolution=merge-duplicates,return=minimal"), json=mv[i:i + 500], timeout=120)
        if rr.status_code >= 300:
            raise SystemExit("movers write failed %s: %s" % (rr.status_code, rr.text[:200]))
    log("WROTE market_history %s: %d days · movers %d (%.0fs)" % (a.market, len(rows), len(mv), time.time() - t0))


if __name__ == "__main__":
    main()
