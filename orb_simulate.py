#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════
 orb_simulate.py  —  TrueFlow ORB Backtest Engine   (Phase 1)
════════════════════════════════════════════════════════════════════════

WHAT THIS DOES
--------------
For every alert already sitting in Supabase `fo_alerts`, this script goes
back to the raw 5-minute / 15-minute / daily candles from Kite and rebuilds
the trade properly:

  1. Re-stamps the STAGE using one consistent rule set across the whole
     period. (Stages 3 and 4 did not exist before 21 May 2026, so the
     stored labels are not comparable across time. This fixes that.)

  2. Reconstructs the BO candle from the stored orb_level + orb_side,
     then derives the real entry and the real stop (BO candle low/high),
     exactly as the written strategy says.

  3. Walks the 5-minute candles forward and measures FOUR ways out:
        A = BO stop, else 3:20 PM close      <- the written strategy
        B = BO stop, else first 5m close below/above the 9 EMA
        C = BO stop, else first 5m close below/above the 5 EMA
     plus MFE (best it ever got) and MAE (worst heat taken).

  4. Converts everything to R-multiples (R = entry minus stop), so a
     Rs.400 stock and a Rs.6000 stock and BANKNIFTY are all comparable.

  5. Scores SPEED (R per hour) — the metric that actually matters for an
     option buyer, because theta punishes slow winners.

It writes one row per alert into `orb_backtest`, and one row per day
into `session_regime`.

IMPORTANT ASSUMPTIONS (stated up front, not buried)
---------------------------------------------------
  * Within a single 5-minute candle we cannot know whether the high or
    the low came first. We always assume the STOP was hit first. That
    makes every result conservative rather than flattering.
  * The stored `orb_level` is treated as ground truth for where the
    engine saw the opening range. We do not second-guess it.
  * Type 1 Opening Range REVERSAL setups were never recorded by the
    alert engine, so they cannot appear here. Breakouts only.
  * Nothing is ever carried overnight. Hard exit at 15:20.

USAGE
-----
  Full backfill (all history, run this once):
      /root/trueflow/bin/python orb_simulate.py --backfill

  Nightly incremental (last 5 days, safe to re-run):
      /root/trueflow/bin/python orb_simulate.py

  A single day, for testing:
      /root/trueflow/bin/python orb_simulate.py --from 2026-08-04 --to 2026-08-04

  Test on a handful of symbols first:
      /root/trueflow/bin/python orb_simulate.py --backfill --limit-symbols 5
════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import time
import math
import argparse
import traceback
from datetime import datetime, date, timedelta, timezone

# ── config (local file, never committed to GitHub) ────────────────────
try:
    import tf_config as CFG
except ImportError:
    print("FATAL: tf_config.py not found. Create it in /root/trueflow first.")
    sys.exit(1)

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("FATAL: kiteconnect not installed in this venv.")
    sys.exit(1)

try:
    from supabase import create_client
except ImportError:
    print("FATAL: supabase not installed in this venv.")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════
#  TUNABLES  — every threshold that is a judgement call lives here
# ══════════════════════════════════════════════════════════════════════

IST = timezone(timedelta(hours=5, minutes=30))

ORB_START      = (9, 15)    # opening range begins
ORB_END        = (9, 30)    # opening range locks
HARD_EXIT      = (15, 20)   # never carry overnight
BNF_NO_ENTRY   = (15, 0)    # doc: no new BNF entries after 3 PM

TICK           = 0.05       # entry is BO high + 1 tick
SLIPPAGE_PCT   = 0.05       # 0.05% each way, for the "net" columns

ATR_PERIOD     = 14
ADR_PERIOD     = 20
WIDE_ORB_ATR   = 0.40       # doc: ORB > 40% of daily ATR = WIDE

# Day-type classifier (documented, tunable, not magic)
TREND_MAX_CROSSES = 6       # <= this many 5m 9-EMA crosses = trending
RANGE_MAX_ADR     = 0.70    # day range < 0.7x ADR = a Range day

KITE_SLEEP     = 0.35       # ~3 requests/sec, Kite's historical limit
CHUNK_DAYS_5M  = 95         # Kite caps 5-minute history at 100 days
CHUNK_DAYS_15M = 190        # 15-minute caps at 200 days
BATCH_WRITE    = 400        # rows per Supabase upsert

INDEX_SYMBOLS = {           # fo_alerts symbol  ->  Kite instrument name
    "BANKNIFTY": "NIFTY BANK",
    "NIFTY":     "NIFTY 50",
    "FINNIFTY":  "NIFTY FIN SERVICE",
}


# ══════════════════════════════════════════════════════════════════════
#  SMALL MATH HELPERS  (pure python — no pandas/numpy dependency)
# ══════════════════════════════════════════════════════════════════════

def ema_series(values, period):
    """Standard EMA, seeded with an SMA of the first `period` values.
    Returns a list the same length as `values`; None until seeded."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * k + prev
        out[i] = prev
    return out


def atr_series(candles, period):
    """Wilder's ATR over a list of candle dicts. None until seeded."""
    n = len(candles)
    out = [None] * n
    if n < period + 1:
        return out
    trs = [None]
    for i in range(1, n):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    seed = sum(trs[1:period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def adr_pct_series(candles, period):
    """Average Daily Range as a % of close — how much this stock
    normally moves. Used to make 'tight' mean the same thing for a
    steady largecap and a wild smallcap."""
    n = len(candles)
    out = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        window = candles[i - period + 1:i + 1]
        vals = [(c["high"] - c["low"]) / c["close"] * 100.0
                for c in window if c["close"]]
        out[i] = sum(vals) / len(vals) if vals else None
    return out


def safe_div(a, b):
    try:
        if b in (None, 0):
            return None
        if a is None:
            return None
        return a / b
    except Exception:
        return None


def r2(x, nd=4):
    """Round for storage; keep None as None."""
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return round(float(x), nd)
    except Exception:
        return None


def as_ist(dt):
    """Normalise anything Kite or Supabase hands us into IST."""
    if dt is None:
        return None
    if isinstance(dt, str):
        s = dt.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def hhmm(dt, hm):
    """Build an IST datetime on the same date as dt, at (h, m)."""
    return dt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def time_bucket(dt):
    m = dt.hour * 60 + dt.minute
    if m < 10 * 60:
        return "09:30-10:00"
    if m < 11 * 60 + 30:
        return "10:00-11:30"
    if m < 14 * 60:
        return "11:30-14:00"
    return "14:00-15:20"


# ══════════════════════════════════════════════════════════════════════
#  CONNECTIONS
# ══════════════════════════════════════════════════════════════════════

def connect_kite():
    kite = KiteConnect(api_key=CFG.KITE_API_KEY)
    token_path = getattr(CFG, "ACCESS_TOKEN_PATH",
                         "/root/trueflow/access_token.txt")
    with open(token_path) as f:
        kite.set_access_token(f.read().strip())
    prof = kite.profile()
    print("Kite connected: %s" % prof.get("user_name", "?"))
    return kite


def connect_sb():
    sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
    print("Supabase connected.")
    return sb


# ══════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ══════════════════════════════════════════════════════════════════════

def load_alerts(sb, d_from, d_to):
    """Paginated read of fo_alerts. Supabase silently caps at 1000 rows
    per request — that bug has bitten this project before, so we page."""
    rows, page, size = [], 0, 1000
    while True:
        q = (sb.table("fo_alerts")
               .select("id,symbol,direction,stage,orb_type,orb_level,"
                       "orb_side,ltp,pdh_pdl_dist,ema_5m,ema_15m,"
                       "xxl_candle,fired_at,session_date")
               .gte("session_date", d_from.isoformat())
               .lte("session_date", d_to.isoformat())
               .order("id")
               .range(page * size, page * size + size - 1))
        chunk = q.execute().data or []
        rows.extend(chunk)
        if len(chunk) < size:
            break
        page += 1
    seen, uniq = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    if len(uniq) != len(rows):
        print("  de-duplicated %d repeated row(s)" % (len(rows) - len(uniq)))
    rows = uniq
    print("Loaded %d alerts (%s to %s)" % (len(rows), d_from, d_to))
    return rows


def build_token_map(kite, wanted):
    """Map tradingsymbol -> instrument_token for NSE equities + indices."""
    print("Fetching NSE instrument list...")
    inst = kite.instruments("NSE")
    lookup = {}
    for i in inst:
        lookup[i["tradingsymbol"]] = i["instrument_token"]
    tok, missing = {}, []
    for sym in wanted:
        name = INDEX_SYMBOLS.get(sym, sym)
        if name in lookup:
            tok[sym] = lookup[name]
        else:
            missing.append(sym)
    if missing:
        print("  no NSE instrument for %d symbol(s): %s"
              % (len(missing), ", ".join(sorted(missing)[:15])))
    print("  resolved %d/%d symbols" % (len(tok), len(wanted)))
    return tok


def fetch_candles(kite, token, d_from, d_to, interval, chunk_days):
    """Kite limits how many days of intraday history come back per call,
    so we walk the range in chunks and stitch."""
    out, cur = [], d_from
    while cur <= d_to:
        end = min(cur + timedelta(days=chunk_days), d_to)
        for attempt in range(3):
            try:
                part = kite.historical_data(token, cur, end, interval)
                out.extend(part or [])
                break
            except Exception as e:
                if attempt == 2:
                    print("    fetch failed (%s %s..%s): %s"
                          % (interval, cur, end, e))
                else:
                    time.sleep(1.5)
            finally:
                time.sleep(KITE_SLEEP)
        cur = end + timedelta(days=1)
    for c in out:
        c["date"] = as_ist(c["date"])
    out.sort(key=lambda c: c["date"])
    return out


def group_by_day(candles):
    g = {}
    for c in candles:
        g.setdefault(c["date"].date(), []).append(c)
    return g


# ══════════════════════════════════════════════════════════════════════
#  SESSION REGIME  —  what kind of day was it?
# ══════════════════════════════════════════════════════════════════════

def classify_day(bars_5m, day_candle, adr):
    """Count how many times the index flipped across its own 5-minute
    9 EMA. A trending day crosses a handful of times; a chop day crosses
    constantly. That single number separates ORB heaven from ORB hell."""
    closes = [b["close"] for b in bars_5m]
    e9 = ema_series(closes, 9)
    crosses, prev_side = 0, None
    for i, b in enumerate(bars_5m):
        if e9[i] is None:
            continue
        side = 1 if b["close"] > e9[i] else -1
        if prev_side is not None and side != prev_side:
            crosses += 1
        prev_side = side

    rng = day_candle["high"] - day_candle["low"]
    range_vs_adr = safe_div(rng, adr) if adr else None

    above = (day_candle["close"] > day_candle["open"])
    if crosses <= TREND_MAX_CROSSES:
        day_type = "Trend Up" if above else "Trend Down"
    elif range_vs_adr is not None and range_vs_adr < RANGE_MAX_ADR:
        day_type = "Range"
    else:
        day_type = "Chop"
    return day_type, crosses, range_vs_adr


def build_regime(kite, sb, d_from, d_to):
    print("\n--- Building session_regime ---")
    tokens = build_token_map(kite, ["NIFTY", "BANKNIFTY"])
    if "NIFTY" not in tokens:
        print("  NIFTY 50 not resolvable — skipping regime build.")
        return {}

    pad = d_from - timedelta(days=60)   # seed EMAs/ATR
    regime = {}

    store = {}
    for key in ("NIFTY", "BANKNIFTY"):
        if key not in tokens:
            continue
        d5 = group_by_day(fetch_candles(kite, tokens[key], d_from, d_to,
                                        "5minute", CHUNK_DAYS_5M))
        dd = fetch_candles(kite, tokens[key], pad, d_to, "day", 2000)
        atr = atr_series(dd, ATR_PERIOD)
        adr = {}
        for i, c in enumerate(dd):
            adr[c["date"].date()] = atr[i]
        store[key] = (d5, {c["date"].date(): c for c in dd}, adr)

    n5, nday, nadr = store.get("NIFTY", ({}, {}, {}))
    b5, bday, badr = store.get("BANKNIFTY", ({}, {}, {}))

    rows = []
    for sd in sorted(n5.keys()):
        if sd not in nday:
            continue
        dt_, cr, rva = classify_day(n5[sd], nday[sd], nadr.get(sd))
        prev_close = None
        keys = sorted(nday.keys())
        if sd in keys:
            idx = keys.index(sd)
            if idx > 0:
                prev_close = nday[keys[idx - 1]]["close"]
        gap = None
        if prev_close:
            gap = (nday[sd]["open"] - prev_close) / prev_close * 100.0

        b_type, b_cr = None, None
        if sd in b5 and sd in bday:
            b_type, b_cr, _ = classify_day(b5[sd], bday[sd], badr.get(sd))

        row = {
            "session_date":    sd.isoformat(),
            "day_type":        dt_,
            "cross_count":     cr,
            "range_vs_adr":    r2(rva),
            "gap_pct":         r2(gap),
            "nifty_close":     r2(nday[sd]["close"], 2),
            "nifty_chg_pct":   r2(safe_div(nday[sd]["close"] - nday[sd]["open"],
                                           nday[sd]["open"]) * 100.0
                                  if nday[sd]["open"] else None),
            "bnf_day_type":    b_type,
            "bnf_cross_count": b_cr,
            "dow":             sd.strftime("%a"),
        }
        rows.append(row)
        regime[sd] = dt_

    for i in range(0, len(rows), BATCH_WRITE):
        sb.table("session_regime").upsert(
            rows[i:i + BATCH_WRITE], on_conflict="session_date").execute()
    print("  wrote %d session_regime rows" % len(rows))

    tally = {}
    for v in regime.values():
        tally[v] = tally.get(v, 0) + 1
    print("  day types: %s" % tally)
    return regime


# ══════════════════════════════════════════════════════════════════════
#  THE CORE  —  rebuild one trade from candles
# ══════════════════════════════════════════════════════════════════════

def find_bo_candle(bars, orb_level, is_bull, fired_at):
    """Find the breakout candle this alert actually refers to.

    A stock can break its opening range several times in a day. Taking
    the FIRST break would pair a 2 PM alert with a 9:30 candle and
    measure a trade that was never taken. So we consider every candle
    that closes outside the ORB, work out when each one's high/low would
    have triggered an entry, and keep the one whose trigger lands
    closest to the moment the alert actually fired.

    Returns (bo_index, entry_index), or "NEVER" if breakouts existed but
    none ever triggered, or None if there was no breakout at all.
    """
    if not bars:
        return None
    lock = hhmm(bars[0]["date"], ORB_END)

    fired_i = None
    for i, b in enumerate(bars):
        if b["date"] <= fired_at:
            fired_i = i
        else:
            break
    if fired_i is None:
        return None

    any_break = False
    best = None
    for i in range(len(bars)):
        b = bars[i]
        if b["date"] < lock or b["date"] > fired_at:
            continue
        outside = (b["close"] > orb_level) if is_bull \
            else (b["close"] < orb_level)
        if not outside:
            continue
        any_break = True
        trig = (b["high"] + TICK) if is_bull else (b["low"] - TICK)
        t_i = None
        for j in range(i + 1, len(bars)):
            hit = (bars[j]["high"] >= trig) if is_bull \
                else (bars[j]["low"] <= trig)
            if hit:
                t_i = j
                break
        if t_i is None:
            continue
        gap = abs(t_i - fired_i)
        # on a tie, prefer the LATER breakout candle - it is the one
        # closest in time to the alert we are trying to reproduce
        if best is None or gap <= best[0]:
            best = (gap, i, t_i)

    if best is not None:
        return best[1], best[2]
    return "NEVER" if any_break else None


def walk_trade(bars, start_i, entry, stop, is_bull, e5, e9, hard_exit_dt):
    """Walk forward candle by candle from entry and measure everything.

    Returns MFE/MAE plus three exits:
        A = stop, else hard 15:20 close
        B = stop, else first close through the 9 EMA
        C = stop, else first close through the 5 EMA

    Intrabar we always assume the stop hit first — conservative by design.
    """
    res = {
        "mfe": 0.0, "mae": 0.0,
        "a": None, "b": None, "c": None,
    }
    best = entry
    worst = entry
    done_a = done_b = done_c = False

    for i in range(start_i, len(bars)):
        b = bars[i]
        if b["date"] > hard_exit_dt:
            break

        if is_bull:
            best = max(best, b["high"])
            worst = min(worst, b["low"])
            stop_hit = b["low"] <= stop
        else:
            best = min(best, b["low"])
            worst = max(worst, b["high"])
            stop_hit = b["high"] >= stop

        mins = int((b["date"] - bars[start_i]["date"]).total_seconds() / 60) + 5

        # --- stop first, always ---
        if stop_hit:
            for k, flag in (("a", done_a), ("b", done_b), ("c", done_c)):
                if not flag:
                    res[k] = (stop, mins, "stop")
            done_a = done_b = done_c = True
            break

        # --- EMA trail exits (close-based, per the rule) ---
        if not done_b and e9[i] is not None:
            through = (b["close"] < e9[i]) if is_bull else (b["close"] > e9[i])
            if through:
                res["b"] = (b["close"], mins, "ema9")
                done_b = True
        if not done_c and e5[i] is not None:
            through = (b["close"] < e5[i]) if is_bull else (b["close"] > e5[i])
            if through:
                res["c"] = (b["close"], mins, "ema5")
                done_c = True

        last_bar, last_mins = b, mins

    # --- anything still open exits at the hard cutoff ---
    if not done_a or not done_b or not done_c:
        try:
            px, mn = last_bar["close"], last_mins
        except UnboundLocalError:
            return None
        for k, flag in (("a", done_a), ("b", done_b), ("c", done_c)):
            if not flag and res[k] is None:
                res[k] = (px, mn, "eod")

    res["mfe"] = (best - entry) if is_bull else (entry - best)
    res["mae"] = (entry - worst) if is_bull else (worst - entry)
    return res


def build_row(alert, bars5, e5, e9, e20, bars15, day_ctx, seq_day, seq_sym):
    """Turn one alert + its candles into one orb_backtest row."""
    sym      = alert["symbol"]
    is_bull  = (alert.get("direction") == "bull")
    fired    = as_ist(alert.get("fired_at"))
    orb_lvl  = alert.get("orb_level")

    row = {
        "alert_id":     alert["id"],
        "symbol":       sym,
        "session_date": alert["session_date"],
        "fired_at":     alert.get("fired_at"),
        "direction":    alert.get("direction"),
        "stage_orig":   alert.get("stage"),
        "orb_type":     alert.get("orb_type"),
        "xxl_candle":   alert.get("xxl_candle"),
        "pdh_pdl_dist": r2(alert.get("pdh_pdl_dist")),
        "seq_no":       seq_day,
        "seq_no_symbol": seq_sym,
        "status":       "ok",
    }

    if fired is None or orb_lvl is None or not bars5:
        row["status"] = "no_data"
        return row

    found = find_bo_candle(bars5, float(orb_lvl), is_bull, fired)
    if found is None:
        row["status"] = "no_bo_candle"
        return row
    if found == "NEVER":
        row["status"] = "never_triggered"
        return row
    bo_i, entry_i = found

    bo = bars5[bo_i]
    entry = (bo["high"] + TICK) if is_bull else (bo["low"] - TICK)
    stop  = bo["low"] if is_bull else bo["high"]
    risk  = abs(entry - stop)
    if risk <= 0:
        row["status"] = "zero_risk"
        return row

    entry_dt  = bars5[entry_i]["date"]
    hard_exit = hhmm(entry_dt, HARD_EXIT)

    walked = walk_trade(bars5, entry_i, entry, stop, is_bull,
                        e5, e9, hard_exit)
    if walked is None:
        row["status"] = "no_forward_bars"
        return row

    atr_d   = day_ctx.get("atr")
    adr_p   = day_ctx.get("adr_pct")
    gap_p   = day_ctx.get("gap_pct")

    # ── re-stamped stage: one rule set for the entire history ──────────
    p5, p9, p20 = (bars5[entry_i]["close"], e9[entry_i], e20[entry_i])
    a5 = None
    if p9 is not None and p20 is not None:
        a5 = (p9 > p20) if is_bull else (p9 < p20)

    a15 = None
    if bars15:
        c15 = [b["close"] for b in bars15]
        f9, f20 = ema_series(c15, 9), ema_series(c15, 20)
        j = None
        for i in range(len(bars15) - 1, -1, -1):
            if bars15[i]["date"] <= entry_dt:
                j = i
                break
        if j is not None and f9[j] is not None and f20[j] is not None:
            a15 = (f9[j] > f20[j]) if is_bull else (f9[j] < f20[j])

    ad = day_ctx.get("daily_bull")
    aD = None
    if ad is not None:
        aD = (ad is True) if is_bull else (ad is False)

    stage_new = None
    if a5 is not None:
        if a5 and a15:
            stage_new = 4 if aD else 2
        elif a5:
            stage_new = 3 if aD else 1

    # 5 EMA state at entry — feeds the 5-vs-9 entry filter question
    pe5 = e5[entry_i]
    ema5_aligned = None
    ema5_over_9  = None
    if pe5 is not None and p20 is not None:
        ema5_aligned = (pe5 > p20) if is_bull else (pe5 < p20)
    if pe5 is not None and p9 is not None:
        ema5_over_9 = (pe5 > p9) if is_bull else (pe5 < p9)

    row.update({
        "bo_time":       bo["date"].isoformat(),
        "bo_high":       r2(bo["high"], 2),
        "bo_low":        r2(bo["low"], 2),
        "entry_time":    entry_dt.isoformat(),
        "entry_price":   r2(entry, 2),
        "stop_price":    r2(stop, 2),
        "risk_pts":      r2(risk, 2),
        "risk_pct":      r2(safe_div(risk, entry) * 100.0 if entry else None),
        "stage_new":     stage_new,
        "ema5m_aligned": a5,
        "ema15m_aligned": a15,
        "emad_aligned":  aD,
        "ema5_aligned":  ema5_aligned,
        "ema5_over_ema9": ema5_over_9,
        "atr_d":         r2(atr_d, 2),
        "adr_pct":       r2(adr_p),
        "gap_pct":       r2(gap_p),
        "orb_type_calc": day_ctx.get("orb_type_calc"),
        "dist_9ema_atr": r2(safe_div(abs(p5 - p9), atr_d)
                            if (p9 is not None and atr_d) else None),
        "ema_sep_pct":   r2(safe_div(abs(p9 - p20), p5) * 100.0
                            if (p9 is not None and p20 is not None and p5)
                            else None),
        "time_bucket":   time_bucket(entry_dt),
        "minutes_from_open": int((entry_dt - hhmm(entry_dt, ORB_START))
                                 .total_seconds() / 60),
        "mfe_pts":       r2(walked["mfe"], 2),
        "mae_pts":       r2(walked["mae"], 2),
        "mfe_r":         r2(safe_div(walked["mfe"], risk)),
        "mae_r":         r2(safe_div(walked["mae"], risk)),
    })

    slip = entry * (SLIPPAGE_PCT / 100.0) * 2.0    # round trip

    for key, label in (("a", "a"), ("b", "b"), ("c", "c")):
        got = walked[key]
        if not got:
            continue
        px, mins, why = got
        pts = (px - entry) if is_bull else (entry - px)
        rr  = safe_div(pts, risk)
        row["exit_%s_pts" % label]    = r2(pts, 2)
        row["exit_%s_r" % label]      = r2(rr)
        row["exit_%s_min" % label]    = mins
        row["exit_%s_reason" % label] = why
        row["exit_%s_rph" % label]    = r2(safe_div(rr, mins / 60.0)
                                           if (rr is not None and mins) else None)
        row["net_%s_r" % label]       = r2(safe_div(pts - slip, risk))
        if walked["mfe"] and walked["mfe"] > 0:
            row["eff_%s" % label] = r2(safe_div(pts, walked["mfe"]))

    return row


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def flush(sb, rows):
    """Upsert a batch, guaranteeing no duplicate alert_id inside it.
    Postgres rejects an ON CONFLICT batch that touches the same row
    twice, so we keep only the last version of each alert_id."""
    if not rows:
        return
    uniq = {}
    for r in rows:
        uniq[r["alert_id"]] = r
    sb.table("orb_backtest").upsert(
        list(uniq.values()), on_conflict="alert_id").execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="process the entire history in fo_alerts")
    ap.add_argument("--from", dest="d_from", default=None)
    ap.add_argument("--to", dest="d_to", default=None)
    ap.add_argument("--days", type=int, default=5,
                    help="incremental lookback when not backfilling")
    ap.add_argument("--limit-symbols", type=int, default=0,
                    help="only process N symbols (for a quick test run)")
    ap.add_argument("--skip-regime", action="store_true")
    args = ap.parse_args()

    today = datetime.now(IST).date()
    if args.backfill:
        d_from, d_to = date(2026, 1, 1), today
    else:
        d_from = date.fromisoformat(args.d_from) if args.d_from \
                 else today - timedelta(days=args.days)
        d_to   = date.fromisoformat(args.d_to) if args.d_to else today

    print("=" * 62)
    print(" TrueFlow ORB Backtest Engine — %s to %s" % (d_from, d_to))
    print("=" * 62)

    kite = connect_kite()
    sb   = connect_sb()

    alerts = load_alerts(sb, d_from, d_to)
    if not alerts:
        print("Nothing to do.")
        return

    # real bounds of the data we actually got
    real_from = min(date.fromisoformat(a["session_date"]) for a in alerts)
    real_to   = max(date.fromisoformat(a["session_date"]) for a in alerts)

    if not args.skip_regime:
        try:
            build_regime(kite, sb, real_from, real_to)
        except Exception as e:
            print("Regime build failed (continuing): %s" % e)

    # group alerts by symbol, and number them within each day
    by_sym = {}
    for a in alerts:
        by_sym.setdefault(a["symbol"], []).append(a)

    day_counter = {}
    for a in sorted(alerts, key=lambda x: (x["session_date"],
                                           x.get("fired_at") or "")):
        sd = a["session_date"]
        day_counter[sd] = day_counter.get(sd, 0) + 1
        a["_seq_day"] = day_counter[sd]

    symbols = sorted(by_sym.keys())
    if args.limit_symbols:
        symbols = symbols[:args.limit_symbols]
        print("TEST MODE: only %d symbols" % len(symbols))

    tokens = build_token_map(kite, symbols)

    pad     = real_from - timedelta(days=90)   # seed daily EMA/ATR/ADR
    pending = []
    stats   = {"ok": 0, "skipped": 0}
    reasons = {}
    t0      = time.time()

    for n, sym in enumerate(symbols, 1):
        if sym not in tokens:
            for a in by_sym[sym]:
                stats["skipped"] += 1
                reasons["no_token"] = reasons.get("no_token", 0) + 1
            continue

        try:
            tok = tokens[sym]
            b5  = fetch_candles(kite, tok, real_from, real_to,
                                "5minute", CHUNK_DAYS_5M)
            b15 = fetch_candles(kite, tok, real_from, real_to,
                                "15minute", CHUNK_DAYS_15M)
            bd  = fetch_candles(kite, tok, pad, real_to, "day", 2000)

            if not b5 or not bd:
                for a in by_sym[sym]:
                    stats["skipped"] += 1
                    reasons["no_candles"] = reasons.get("no_candles", 0) + 1
                continue

            d5, d15 = group_by_day(b5), group_by_day(b15)
            atrs    = atr_series(bd, ATR_PERIOD)
            adrs    = adr_pct_series(bd, ADR_PERIOD)
            dcl     = [c["close"] for c in bd]
            de9     = ema_series(dcl, 9)
            de20    = ema_series(dcl, 20)

            dctx = {}
            for i, c in enumerate(bd):
                sd = c["date"].date()
                bull = None
                if de9[i] is not None and de20[i] is not None:
                    bull = (de9[i] > de20[i] and c["close"] > de9[i])
                    if not bull and de9[i] < de20[i] and c["close"] < de9[i]:
                        bull = False
                    elif not bull:
                        bull = None          # genuinely neutral, not bearish
                gap = None
                if i > 0 and bd[i - 1]["close"]:
                    gap = ((c["open"] - bd[i - 1]["close"])
                           / bd[i - 1]["close"] * 100.0)
                dctx[sd] = {"atr": atrs[i], "adr_pct": adrs[i],
                            "daily_bull": bull, "gap_pct": gap}

            sym_seq = {}
            for a in sorted(by_sym[sym],
                            key=lambda x: (x["session_date"],
                                           x.get("fired_at") or "")):
                sd = date.fromisoformat(a["session_date"])
                sym_seq[sd] = sym_seq.get(sd, 0) + 1

                bars5 = d5.get(sd, [])
                ctx   = dict(dctx.get(sd, {}))

                if bars5 and ctx.get("atr"):
                    lock = hhmm(bars5[0]["date"], ORB_END)
                    orb = [b for b in bars5 if b["date"] <= lock]
                    if orb:
                        rng = max(x["high"] for x in orb) - \
                              min(x["low"] for x in orb)
                        ctx["orb_type_calc"] = (
                            "wide" if rng > WIDE_ORB_ATR * ctx["atr"]
                            else "narrow")

                closes = [b["close"] for b in bars5]
                e5  = ema_series(closes, 5)
                e9  = ema_series(closes, 9)
                e20 = ema_series(closes, 20)

                row = build_row(a, bars5, e5, e9, e20, d15.get(sd, []),
                                ctx, a.get("_seq_day"), sym_seq[sd])
                pending.append(row)

                if row["status"] == "ok":
                    stats["ok"] += 1
                else:
                    stats["skipped"] += 1
                    reasons[row["status"]] = reasons.get(row["status"], 0) + 1

            if len(pending) >= BATCH_WRITE:
                flush(sb, pending)
                pending = []

        except Exception as e:
            print("  ERROR on %s: %s" % (sym, e))
            traceback.print_exc()

        if n % 10 == 0 or n == len(symbols):
            el = time.time() - t0
            print("  [%d/%d] %s  ok=%d skipped=%d  %.1f min elapsed"
                  % (n, len(symbols), sym, stats["ok"], stats["skipped"],
                     el / 60.0))

    if pending:
        flush(sb, pending)

    print("\n" + "=" * 62)
    print(" DONE in %.1f minutes" % ((time.time() - t0) / 60.0))
    print("  measured : %d" % stats["ok"])
    print("  skipped  : %d" % stats["skipped"])
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("      %-18s %d" % (k, v))
    print("=" * 62)
    print("\nNow run the summary query in Supabase to see the results.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:
        print("FATAL: %s" % e)
        traceback.print_exc()
        sys.exit(1)
