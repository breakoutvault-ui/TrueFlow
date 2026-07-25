#!/usr/bin/env python3
"""
TrueFlow US — Earnings & Episodic-Pivot Tracker
================================================
Source: yfinance (earnings dates + EPS estimate/actual/surprise) and the
price/volume history already stored in us_momentum_stocks. No API key, no login.

What it captures
----------------
For every US stock that reported (or will report) earnings in the tracked
window, it records:
  • EPS estimate / actual / surprise %              (the fundamental catalyst)
  • pre-earnings run-up  (pre_5d, pre_10d)          (was it front-run or neglected?)
  • the reaction         (post_1d, post_3d, post_5d) (Leg 1 — the immediate move)
  • result-day RVOL                                  (conviction behind the move)
  • a LIFECYCLE STATE that tracks the stock through BOTH legs:

      NEGLECTED  → flat before the print (nobody front-ran it)
      GAPPED     → just popped on the result (Leg 1 igniting)
      DRIFTING   → stepping up post-earnings (Leg 1 in progress — buy pullbacks)
      BASING     → consolidating near post-earnings highs
      COILING    → tight contraction at highs (Leg 2 setup forming)
      RESUMED    → broke out of the high base (Leg 2 fired)
      FADED      → gave back the gap (catalyst failed)
      UPCOMING   → earnings still ahead

  • is_delayed_ep : flat pre-run + strong post-earnings pop = the premium
                    "delayed episodic pivot" — a quiet stock that ignites only
                    AFTER the print. Highest-quality Leg-1 entry.

Modes (auto-detected)
  BACKFILL : us_earnings_moves empty  -> LOOKBACK_DAYS history
  DAILY    : refresh window, recompute states for in-progress names

Run: /root/trueflow/bin/python /root/trueflow/us_earnings_tracker.py
"""
import os, time, logging, datetime as dt
import requests
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SUPABASE_URL = "https://tsgltaqbxtisebqmbffg.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRzZ2x0YXFieHRpc2VicW1iZmZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU1NTEyMDEsImV4cCI6MjA5MTEyNzIwMX0.SQGq9E67S7j977RUA-oJqDGV8KgEhQZb0nHfAvHcFys"

TELEGRAM_TOKEN = os.environ.get("TF_TG_TOKEN", "")
TELEGRAM_CHAT  = "1202026803"

LOOKBACK_DAYS  = 120   # how far back we track past earnings
LOOKAHEAD_DAYS = 50    # how far forward we list upcoming earnings
BATCH_SLEEP    = 0.20  # politeness between yfinance calls
MIN_PRE_RUN    = 3.0   # % — below this pre-run counts as "neglected"
GAP_MIN        = 4.0   # % — post_1d at/above this is a real gap

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("us_earnings")

SB = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
      "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def sb_get(path):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SB, timeout=60)
    r.raise_for_status()
    return r.json()


def sb_upsert(table, rows, on_conflict):
    if not rows:
        return
    h = dict(SB); h["Prefer"] = "resolution=merge-duplicates"
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                          headers=h, json=chunk, timeout=120)
        if r.status_code >= 300:
            log.warning("batch upsert %s failed (%s) — retrying row by row", table, r.status_code)
            for one in chunk:
                rr = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                                   headers=h, json=[one], timeout=60)
                if rr.status_code >= 300:
                    log.error("row upsert %s failed: %s %s", one.get("symbol"), rr.status_code, rr.text[:200])


def telegram(msg):
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT, "text": msg,
                            "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        log.warning("telegram failed: %s", e)


# ---------------------------------------------------------------------------
# Universe + price history from us_momentum_stocks (data we already have)
# ---------------------------------------------------------------------------
def latest_session():
    r = sb_get("us_momentum_stocks?select=session_date&order=session_date.desc&limit=1")
    return r[0]["session_date"] if r else None


def load_universe(session):
    """symbol -> (adr_pct, company_name, sector)."""
    uni, off = {}, 0
    while True:
        rows = sb_get(f"us_momentum_stocks?select=symbol,adr_pct,company_name,sector"
                      f"&session_date=eq.{session}&limit=1000&offset={off}")
        if not rows:
            break
        for r in rows:
            uni[r["symbol"].upper()] = (
                float(r["adr_pct"]) if r.get("adr_pct") is not None else None,
                r.get("company_name") or "", r.get("sector") or "")
        if len(rows) < 1000:
            break
        off += 1000
    log.info("Universe from %s: %d symbols", session, len(uni))
    return uni


def price_series(symbol, lo, hi):
    """Daily (date, close) and (vol_today, vol_avg20) between lo and hi, from our DB."""
    rows = sb_get(f"us_momentum_stocks?select=session_date,ltp,vol_today,vol_avg20"
                  f"&symbol=eq.{symbol}&session_date=gte.{lo}&session_date=lte.{hi}"
                  f"&order=session_date&limit=400")
    data = [r for r in rows if r.get("ltp") is not None]
    series = [(dt.date.fromisoformat(r["session_date"]), float(r["ltp"])) for r in data]
    vols = [((float(r["vol_today"]) if r.get("vol_today") is not None else None),
             (float(r["vol_avg20"]) if r.get("vol_avg20") is not None else None)) for r in data]
    return series, vols


# ---------------------------------------------------------------------------
# yfinance earnings dates + EPS surprise
# ---------------------------------------------------------------------------
def earnings_history(symbol):
    """Return list of dicts: {date, eps_est, eps_act, surprise_pct}. Handles gaps."""
    out = []
    try:
        t = yf.Ticker(symbol)
        ed = t.get_earnings_dates(limit=16)
    except Exception as e:
        log.debug("%s earnings_dates err: %s", symbol, str(e)[:80])
        return out
    if ed is None or not len(ed):
        return out
    for idx, row in ed.iterrows():
        try:
            d = idx.date() if hasattr(idx, "date") else pd.to_datetime(idx).date()
        except Exception:
            continue
        def num(v):
            try:
                f = float(v)
                return None if pd.isna(f) else round(f, 4)
            except Exception:
                return None
        out.append({"date": d,
                    "eps_est": num(row.get("EPS Estimate")),
                    "eps_act": num(row.get("Reported EPS")),
                    "surprise_pct": num(row.get("Surprise(%)"))})
    return out


# ---------------------------------------------------------------------------
# Move computation + lifecycle state (the two-leg engine)
# ---------------------------------------------------------------------------
def compute_moves(series, vols, result_date, adr_pct):
    """Pre-run, reaction, RVOL, plus the lifecycle state machine."""
    ri = None
    for i, (d, _c) in enumerate(series):
        if d <= result_date:
            ri = i
        elif d > result_date:
            break
    if ri is None:
        return None
    cl = lambda i: (series[i][1] if 0 <= i < len(series) else None)
    def pct(a, b):
        return round((a / b - 1) * 100, 2) if (a is not None and b not in (None, 0)) else None

    base = cl(ri)
    r = {"result_close": base,
         "pre_5d":  pct(base, cl(ri - 5)),
         "pre_10d": pct(base, cl(ri - 10)),
         "post_1d": pct(cl(ri + 1), base),
         "post_3d": pct(cl(ri + 3), base),
         "post_5d": pct(cl(ri + 5), base)}
    r["pre_5d_adr"]  = round(r["pre_5d"] / adr_pct, 2)  if (adr_pct and r["pre_5d"]  is not None) else None
    r["post_5d_adr"] = round(r["post_5d"] / adr_pct, 2) if (adr_pct and r["post_5d"] is not None) else None
    r["complete"] = cl(ri + 5) is not None

    # result-day RVOL (peak of result day and next two)
    rv = []
    for j in (0, 1, 2):
        k = ri + j
        if 0 <= k < len(vols) and vols[k][0] and vols[k][1]:
            rv.append(round(vols[k][0] / vols[k][1], 2))
    r["result_rvol"] = max(rv) if rv else None

    # ------- neglect + delayed-EP flags -------
    pre = r["pre_5d"]
    p1, p5, rvol = r["post_1d"], r["post_5d"], r["result_rvol"]
    neglected = (pre is not None and abs(pre) < MIN_PRE_RUN)
    strong_pop = ((p1 or 0) >= GAP_MIN or (p5 or 0) >= GAP_MIN) and (rvol or 0) >= 1.5
    r["is_neglected"] = bool(neglected)
    r["is_delayed_ep"] = bool(neglected and strong_pop)

    # ------- LIFECYCLE STATE (two legs) -------
    # Uses price action AFTER the earnings day to place the stock in its cycle.
    # A Leg-2 consolidation (BASING/COILING/RESUMED) only makes sense when the
    # stock actually GAINED ground on the print — otherwise it's just a flat stock.
    post = series[ri:]                       # result day onward
    post_close = [c for _d, c in post]
    state = "GAPPED"
    if len(post_close) >= 2:
        peak = max(post_close)
        peak_i = post_close.index(peak)
        last = post_close[-1]
        days_since = len(post_close) - 1
        gain_from_base = (peak / base - 1) * 100 if base else 0   # how much it rose post-print
        last_vs_base   = (last / base - 1) * 100 if base else 0

        made_real_move = gain_from_base >= GAP_MIN               # did the catalyst actually move it?

        if not made_real_move:
            # never really gapped -> not an EP lifecycle; flat or drifting mildly
            state = "QUIET" if abs(last_vs_base) < 2 else ("DRIFTING" if last_vs_base > 0 else "FADED")
        elif last_vs_base <= 0:                                   # gave the whole gap back
            state = "FADED"
        elif days_since <= 2:
            state = "GAPPED"
        elif peak_i >= days_since - 1 and last >= peak * 0.985:
            state = "DRIFTING"                                    # still making highs -> Leg 1 alive
        else:
            # off the highs but still elevated -> consolidating.
            # Measure tightness of the CONSOLIDATION ONLY (from the peak onward),
            # so the initial gap move isn't counted as "range".
            consol = post_close[peak_i:]                         # peak day -> now
            window = consol[-5:] if len(consol) >= 2 else consol
            tight = (max(window) - min(window)) / max(window) * 100 if window else 99
            if last >= peak * 0.995:
                state = "RESUMED"                                # pushed back to new highs -> Leg 2
            elif tight <= 6:
                state = "COILING"                                # tight contraction at highs -> Leg 2 forming
            else:
                state = "BASING"                                 # looser consolidation, still up
    r["state"] = state

    # ------- headline verdict (kept for the filter chips) -------
    faded = r["state"] == "FADED"
    if p1 is None and p5 is None:
        v = "Anticipation" if (pre is not None and pre >= 4) else "Watch"
    elif r["is_delayed_ep"] and not faded:
        v = "Delayed EP"
    elif faded and (pre or 0) < MIN_PRE_RUN:
        v = "Failed pop"          # neglected stock popped then gave it all back
    elif (p1 or 0) > 0 and (rvol or 0) >= 1.5:
        v = "Positive surprise"
    elif (pre or 0) >= 4 and (p1 or 0) < 0:
        v = "Sell the news"
    elif (p5 or 0) >= 4 and (p1 or 0) >= 0:
        v = "Drifting up"
    elif abs(p1 or 0) < 1.5 and abs(p5 or 0) < 2:
        v = "Quiet"
    else:
        v = "Mixed"
    r["verdict"] = v
    return r


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def run():
    today = dt.date.today()
    log.info("=" * 60)
    log.info("US Earnings & Episodic-Pivot Tracker — %s", today)
    log.info("=" * 60)

    session = latest_session()
    if not session:
        log.error("us_momentum_stocks empty — run the momentum scan first")
        return
    uni = load_universe(session)

    # backfill vs daily
    try:
        existing = sb_get("us_earnings_moves?select=symbol&limit=1")
        backfill = len(existing) == 0
    except Exception:
        backfill = True
    log.info("MODE: %s", "BACKFILL" if backfill else "DAILY")
    if backfill:
        telegram("🇺🇸 <b>US Earnings Tracker</b> — backfill starting "
                 "(earnings dates + EPS surprise for the universe). This takes a while ⏳")

    lo_track = today - dt.timedelta(days=LOOKBACK_DAYS)
    hi_track = today + dt.timedelta(days=LOOKAHEAD_DAYS)

    computed = upcoming = skipped = no_eps = 0
    rows = []
    syms = sorted(uni.keys())
    total = len(syms)

    for n, sym in enumerate(syms, 1):
        if n % 200 == 0:
            log.info("… %d/%d processed (%d computed, %d upcoming)", n, total, computed, upcoming)
        adr, cname, sector = uni[sym]
        hist = earnings_history(sym)
        time.sleep(BATCH_SLEEP)
        if not hist:
            no_eps += 1
            continue

        for ev in hist:
            rdate = ev["date"]
            if rdate < lo_track or rdate > hi_track:
                skipped += 1
                continue

            lo = rdate - dt.timedelta(days=25)
            hi = rdate + dt.timedelta(days=20)
            try:
                series, vols = price_series(sym, str(lo), str(hi))
            except Exception as e:
                log.warning("%s price fetch failed: %s", sym, str(e)[:80])
                series, vols = [], []

            mv = compute_moves(series, vols, rdate, adr) if series else None

            # Complete row template — EVERY row carries the SAME keys so Supabase
            # batch upserts never hit "All object keys must match".
            base = {"symbol": sym, "result_date": str(rdate),
                    "company_name": cname, "sector": sector, "adr_pct": adr,
                    "eps_est": ev["eps_est"], "eps_act": ev["eps_act"],
                    "surprise_pct": ev["surprise_pct"],
                    "status": None, "state": None, "result_close": None,
                    "pre_5d": None, "pre_10d": None,
                    "post_1d": None, "post_3d": None, "post_5d": None,
                    "pre_5d_adr": None, "post_5d_adr": None, "result_rvol": None,
                    "is_neglected": False, "is_delayed_ep": False, "verdict": None}

            if rdate > today:
                base.update({"status": "upcoming", "state": "UPCOMING",
                             "pre_5d": (mv["pre_5d"] if mv else None),
                             "pre_10d": (mv["pre_10d"] if mv else None),
                             "verdict": "Anticipation" if (mv and (mv.get("pre_5d") or 0) >= 4) else "Watch"})
                upcoming += 1
            elif mv:
                base.update({"status": "done",
                             "result_close": mv["result_close"],
                             "pre_5d": mv["pre_5d"], "pre_10d": mv["pre_10d"],
                             "post_1d": mv["post_1d"], "post_3d": mv["post_3d"],
                             "post_5d": mv["post_5d"],
                             "pre_5d_adr": mv["pre_5d_adr"], "post_5d_adr": mv["post_5d_adr"],
                             "result_rvol": mv["result_rvol"],
                             "is_neglected": mv["is_neglected"],
                             "is_delayed_ep": mv["is_delayed_ep"],
                             "state": mv["state"], "verdict": mv["verdict"]})
                computed += 1
            else:
                # date known but no price series (e.g. very new listing)
                base.update({"status": "done", "state": "GAPPED", "verdict": "Watch"})
                computed += 1
            rows.append(base)

        if len(rows) >= 300:
            sb_upsert("us_earnings_moves", rows, "symbol,result_date")
            rows = []

    sb_upsert("us_earnings_moves", rows, "symbol,result_date")

    log.info("Done: %d computed, %d upcoming, %d skipped, %d had no EPS data",
             computed, upcoming, skipped, no_eps)
    telegram(f"🇺🇸 <b>US Earnings Tracker — {'Backfill' if backfill else 'Daily'} complete</b>\n"
             f"✅ Reported: <b>{computed}</b>\n"
             f"⏳ Upcoming: <b>{upcoming}</b>\n"
             f"📊 No EPS data: {no_eps}")


if __name__ == "__main__":
    run()
