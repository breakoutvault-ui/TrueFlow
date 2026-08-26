#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════
 nextday_scorer.py  —  TrueFlow  (Next Day WL v2, script 2 of 4)
════════════════════════════════════════════════════════════════════════

WHAT THIS DOES
--------------
Every evening it scores the whole F&O universe and writes the highest
conviction setups for the next trading day into `nextday_picks_v2`.

It does NOT touch `watchlist_generator.py` or `watchlist_picks`. The old
scorer keeps running untouched. For the next several weeks both models
score the same stocks and both get graded, so the Journal can prove
which one is actually better before anything is retired.

WHAT CHANGED FROM THE OLD MODEL
-------------------------------
  * Fresh Crossover (5 pts) deleted. It was rewarding stocks whose EMAs
    had only just crossed — which is exactly the sideways criss-cross
    chop that kept showing up in the picks.
  * Range compression (NR4 / NR7 / contraction / consolidation) added at
    15 pts. A narrow range day is stored energy, and it precedes the
    expansion day an ORB needs. In the backtest narrow ORBs returned
    +0.130 R against +0.020 R for wide ones.
  * Room to run added at 10 pts. Distance to the nearest level overhead,
    measured in ADRs. A breakout with resistance half an ADR away stalls
    while your option decays.
  * The stock's own ORB track record added at 5 pts, read from the 8,962
    trades already measured in `orb_backtest`.
  * Hard filters now reject tangled EMAs and low-ADR stocks.

EVERY THRESHOLD BELOW CAME FROM YOUR OWN DATA, NOT FROM AN OPINION
-----------------------------------------------------------------
Measured across 8,621 clean trades in `orb_backtest`:

  ADR%      1.5-2.0 = -0.202 R   <- losing
            2.0-2.5 = -0.024 R   <- still losing
            2.5-3.5 = +0.099 R
            >3.5    = +0.334 R   => MIN_ADR default 2.5

  Daily 9/20 gap
            <0.25%  = +0.008 R   <- no edge
            0.25-0.50 = +0.028 R <- no edge
            >0.50%  = +0.078 R and better
                                 => MIN_EMA_SEP default 0.50

  Daily trend alignment showed NO measurable edge (S3/S4 with-trend
  returned +0.158 / +0.168 R against S1/S2 counter-trend +0.176 /
  +0.261 R). So `w_trend` ships at weight 0 — the value is still
  computed and stored on every pick, so the Journal can settle it on
  forward data instead of on 335 historical trades.

THRESHOLDS LIVE IN SUPABASE, NOT IN THIS FILE
---------------------------------------------
Weights and cutoffs are read from `nextday_weights` where active = true.
Tuning the model never requires editing this script.

USAGE
-----
  Nightly, after nse_bhav_fetcher (6:15 PM) and daily_levels:
      /root/trueflow/bin/python nextday_scorer.py

  Dry run — print what it would pick, write nothing:
      /root/trueflow/bin/python nextday_scorer.py --dry-run

  Score a specific session:
      /root/trueflow/bin/python nextday_scorer.py --date 2026-08-25

  More picks per side (you said the count can go up, never down):
      /root/trueflow/bin/python nextday_scorer.py --top 15

  Explain one stock's score line by line:
      /root/trueflow/bin/python nextday_scorer.py --explain RELIANCE
════════════════════════════════════════════════════════════════════════
"""

import sys
import time
import argparse
from datetime import datetime, date, timedelta, timezone

try:
    import tf_config as CFG
except ImportError:
    print("FATAL: tf_config.py not found. Create it in /root/trueflow first.")
    sys.exit(1)

try:
    from supabase import create_client
except ImportError:
    print("FATAL: supabase not installed in this venv.")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════
#  TUNABLES  (defaults only — the active row in nextday_weights wins)
# ══════════════════════════════════════════════════════════════════════

IST = timezone(timedelta(hours=5, minutes=30))

TOP_N          = 10      # picks per side
SLOPE_DAYS     = 5       # sessions used to measure EMA slope
HISTORY_DAYS   = 14      # calendar days of momentum_stocks to load
EARNINGS_WARN  = 5       # badge when results are within N sessions
EXTENDED_PCT   = 7.0     # badge when price is this far from the 9 EMA
OPT_LIQ_FLOOR  = 10.0    # reject the bottom N% of option liquidity
ORBHIST_MIN_N  = 20      # trades needed before a stock's ORB record counts
DELIVERY_MIN_COVERAGE = 50.0   # pct of universe needed to score delivery
BATCH_WRITE    = 200

DEFAULT_WEIGHTS = {
    "w_oi": 20.0, "w_ema_struct": 15.0, "w_ema_prox": 15.0,
    "w_compression": 15.0, "w_volume": 15.0, "w_room": 10.0,
    "w_sector": 5.0, "w_orbhist": 5.0, "w_trend": 0.0,
    "min_score": 60.0, "min_adr": 2.5, "min_ema_sep": 0.50,
}

# OI buildup label -> how well it supports each direction (0..1)
OI_SUPPORT = {
    "bull": {"long buildup": 1.00, "short covering": 0.60,
             "long unwinding": 0.20, "short buildup": 0.00},
    "bear": {"short buildup": 1.00, "long unwinding": 0.60,
             "short covering": 0.20, "long buildup": 0.00},
}


# ══════════════════════════════════════════════════════════════════════
#  SMALL HELPERS
# ══════════════════════════════════════════════════════════════════════

def f(x, default=None):
    """Coerce to float, tolerating None and junk."""
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def r2(x, nd=4):
    if x is None:
        return None
    try:
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def band(value, table, default=0.0):
    """table = [(upper_bound_exclusive, points), ...] ascending.
    Use float('inf') for the final bound."""
    if value is None:
        return default
    for upper, pts in table:
        if value < upper:
            return pts
    return default


def next_trading_day(d):
    """Next weekday. NSE holidays are not in the database, so a pick
    generated before a holiday carries that date; the grader simply
    finds no candles and marks it skipped. Harmless."""
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:
        nd += timedelta(days=1)
    return nd


def pct_rank(value, sorted_values):
    """Percentile of value within an ascending list. 0..100."""
    if value is None or not sorted_values:
        return None
    lo, hi = 0, len(sorted_values)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return 100.0 * lo / len(sorted_values)


# ══════════════════════════════════════════════════════════════════════
#  CONNECTION + PAGED READS
# ══════════════════════════════════════════════════════════════════════

def connect_sb():
    sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
    print("Supabase connected.")
    return sb


def page_select(builder, size=1000):
    """Supabase silently caps at 1000 rows per request. Page on a UNIQUE
    key (id) and de-duplicate — paging on session_date once duplicated
    rows and destroyed a 20-minute run."""
    rows, page = [], 0
    while True:
        chunk = builder(page * size, page * size + size - 1) or []
        rows.extend(chunk)
        if len(chunk) < size:
            break
        page += 1
    seen, uniq = set(), []
    for r in rows:
        k = r.get("id")
        if k is not None:
            if k in seen:
                continue
            seen.add(k)
        uniq.append(r)
    return uniq


# ══════════════════════════════════════════════════════════════════════
#  LOADERS
# ══════════════════════════════════════════════════════════════════════

def load_weights(sb):
    w = dict(DEFAULT_WEIGHTS)
    w["version"] = 1
    try:
        rows = (sb.table("nextday_weights").select("*")
                  .eq("active", True).order("version", desc=True)
                  .limit(1).execute().data or [])
        if rows:
            row = rows[0]
            for k in DEFAULT_WEIGHTS:
                v = f(row.get(k))
                if v is not None:
                    w[k] = v
            w["version"] = row.get("version", 1)
            print("  weights v%s loaded from Supabase" % w["version"])
        else:
            print("  no active weights row — using built-in defaults")
    except Exception as e:
        print("  weights read failed (%s) — using built-in defaults" % e)
    return w


def load_bhav(sb, session_date=None):
    """Latest fo_bhav_oi session. This is also the F&O universe."""
    if session_date is None:
        r = (sb.table("fo_bhav_oi").select("session_date")
               .order("session_date", desc=True).limit(1)
               .execute().data or [])
        if not r:
            return None, {}
        session_date = r[0]["session_date"]
    rows = page_select(lambda a, b: (
        sb.table("fo_bhav_oi")
          .select("id,symbol,fut_oi,fut_oi_chg,fut_close,total_ce_oi,"
                  "total_pe_oi,pcr,oi_buildup,price_chg_pct")
          .eq("session_date", session_date).order("id")
          .range(a, b).execute().data))
    out = {r["symbol"]: r for r in rows if r.get("symbol")}
    print("  fo_bhav_oi %s: %d symbols" % (session_date, len(out)))
    return session_date, out


def load_momentum(sb, d_from, d_to):
    """momentum_stocks over a window, grouped by symbol, newest first."""
    cols = ("id,symbol,session_date,ltp,day_chg_pct,ema5_daily,ema9_daily,"
            "ema20_daily,above_ema5_daily,above_ema9_daily,"
            "above_ema20_daily,pct_from_ema9_daily,pct_from_ema5_daily,"
            "adr_pct,vol_ratio,is_nr4,is_nr7,nr_range_pct,consol_days,"
            "qm_contraction,qm_pivot_level,qm_base_days,breakout_price,"
            "high_52w,low_52w,day_high,day_low,sector,industry,"
            "company_name,days_in_uptrend,days_below_ema9,move_3d_pct")
    rows = page_select(lambda a, b: (
        sb.table("momentum_stocks").select(cols)
          .gte("session_date", d_from.isoformat())
          .lte("session_date", d_to.isoformat())
          .order("id").range(a, b).execute().data))
    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: x["session_date"], reverse=True)
    print("  momentum_stocks %s..%s: %d rows / %d symbols"
          % (d_from, d_to, len(rows), len(by_sym)))
    return by_sym


def load_sector_pulse(sb, session_date):
    rows = page_select(lambda a, b: (
        sb.table("sector_pulse").select("id,sector,adv,dec,total,avg_chg")
          .eq("session_date", session_date).order("id")
          .range(a, b).execute().data))
    if not rows:
        r = (sb.table("sector_pulse").select("session_date")
               .order("session_date", desc=True).limit(1)
               .execute().data or [])
        if r:
            return load_sector_pulse(sb, r[0]["session_date"])
        return {}
    out = {}
    for r in rows:
        tot = f(r.get("total"), 0) or 0
        adv = f(r.get("adv"), 0) or 0
        if tot > 0:
            out[r["sector"]] = {"adv_pct": 100.0 * adv / tot,
                                "avg_chg": f(r.get("avg_chg"))}
    print("  sector_pulse: %d sectors" % len(out))
    return out


def load_earnings(sb, today):
    rows = page_select(lambda a, b: (
        sb.table("earnings_moves").select("id,symbol,result_date,status")
          .gte("result_date", today.isoformat())
          .order("id").range(a, b).execute().data))
    out = {}
    for r in rows:
        try:
            d = date.fromisoformat(r["result_date"])
        except Exception:
            continue
        days = (d - today).days
        prev = out.get(r["symbol"])
        if prev is None or days < prev:
            out[r["symbol"]] = days
    print("  earnings_moves: %d symbols with upcoming results" % len(out))
    return out


def load_orb_history(sb):
    """Each stock's own ORB track record from the measured backtest.
    Exit method A (hold with the BO stop to 3:20) is the reference,
    because it was the only method that made money."""
    rows = page_select(lambda a, b: (
        sb.table("orb_backtest").select("alert_id,symbol,exit_a_r,status")
          .eq("status", "ok").order("alert_id").range(a, b)
          .execute().data), size=1000)
    agg = {}
    for r in rows:
        v = f(r.get("exit_a_r"))
        if v is None:
            continue
        a = agg.setdefault(r["symbol"], {"n": 0, "sum": 0.0, "wins": 0})
        a["n"] += 1
        a["sum"] += v
        if v > 0:
            a["wins"] += 1
    for sym, a in agg.items():
        a["avg_r"] = a["sum"] / a["n"] if a["n"] else None
        a["win_pct"] = 100.0 * a["wins"] / a["n"] if a["n"] else None
    print("  orb_backtest: track record for %d symbols" % len(agg))
    return agg


def load_delivery(sb, session_date):
    """Delivery % per symbol.

    The old scorer stores del_pct on watchlist_picks, but nothing
    upstream in the database exposes a delivery column, so the source is
    not visible from here. Until it is identified this returns whatever
    the old picks recorded for the same session (a partial map), and the
    volume component re-weights itself for any stock without a value.
    Nothing is invented."""
    out = {}
    try:
        rows = page_select(lambda a, b: (
            sb.table("watchlist_picks").select("id,symbol,del_pct")
              .eq("session_date", session_date).order("id")
              .range(a, b).execute().data))
        for r in rows:
            v = f(r.get("del_pct"))
            if v is not None:
                out[r["symbol"]] = v
    except Exception as e:
        print("  delivery read failed (continuing): %s" % e)
    print("  delivery: %d symbols have a value" % len(out))
    return out


# ══════════════════════════════════════════════════════════════════════
#  COMPONENT SCORES  — each returns points on its own 0..max scale
# ══════════════════════════════════════════════════════════════════════

def score_oi(bhav, direction, oi_chg_pct_rank, wmax):
    label = (bhav.get("oi_buildup") or "").strip().lower()
    base = OI_SUPPORT[direction].get(label)
    if base is None:
        base = 0.35            # unrecognised label: neutral, not zero
    # 75% from the label, 25% from how big the OI change was
    mag = 0.0 if oi_chg_pct_rank is None else oi_chg_pct_rank / 100.0
    return wmax * (0.75 * base + 0.25 * base * mag)


def score_ema_struct(slope9, slope20, sep_pct, direction, wmax):
    """Slope (8/15) + separation (7/15), scaled to wmax."""
    want_up = direction == "bull"
    s9 = slope9 if want_up else (-slope9 if slope9 is not None else None)
    s20 = slope20 if want_up else (-slope20 if slope20 is not None else None)
    slope_pts = 0.0
    if s9 is not None and s20 is not None and s9 > 0 and s20 > 0:
        # 0.5%/5d = fair, 2%/5d = strong
        slope_pts = 8.0 * min(1.0, (s9 + s20) / 2.0 / 2.0)
        slope_pts = max(slope_pts, 2.0)      # both sloping is worth a floor
    sep_pts = band(sep_pct, [(0.50, 0.0), (1.00, 3.0), (2.00, 5.0),
                             (3.00, 6.0), (float("inf"), 7.0)])
    return wmax * (slope_pts + sep_pts) / 15.0


def score_ema_prox(pct_from_9, wmax):
    d = abs(pct_from_9) if pct_from_9 is not None else None
    pts = band(d, [(1.0, 15.0), (2.0, 12.0), (4.0, 8.0),
                   (7.0, 4.0), (float("inf"), 0.0)])
    return wmax * pts / 15.0


def score_compression(row, wmax):
    pts = 0.0
    if row.get("is_nr7"):
        pts += 8.0
    elif row.get("is_nr4"):
        pts += 5.0
    contr = f(row.get("qm_contraction"))
    if contr is not None:
        # smaller contraction value = tighter base
        pts += band(contr, [(0.4, 4.0), (0.6, 3.0), (0.8, 2.0),
                            (float("inf"), 0.0)])
    cd = f(row.get("consol_days"))
    if cd is not None:
        pts += band(cd, [(3.0, 0.0), (5.0, 1.0), (21.0, 3.0),
                         (40.0, 1.5), (float("inf"), 0.0)])
    return wmax * min(pts, 15.0) / 15.0


def score_volume(vol_ratio, del_pct, wmax, delivery_enabled):
    """vol 8 + delivery 7.

    Delivery is all-or-nothing across the whole universe. If coverage is
    too thin the component is switched off for everyone and volume is
    scaled to the full weight. Doing this per-stock instead would hand a
    stock with MISSING delivery the same score as one with excellent
    delivery — rewarding absent data."""
    v = band(vol_ratio, [(1.0, 0.0), (1.2, 2.0), (1.5, 4.0),
                         (2.0, 6.0), (float("inf"), 8.0)])
    if not delivery_enabled:
        return wmax * (v / 8.0)
    d = band(del_pct, [(40.0, 1.0), (50.0, 3.0), (60.0, 5.0),
                       (float("inf"), 7.0)], default=1.0)
    return wmax * (v + d) / 15.0


def nearest_level(row, ltp, direction):
    """Closest level standing in the way of the move."""
    cands = []
    for key, name in (("day_high", "PDH"), ("high_52w", "52W high"),
                      ("qm_pivot_level", "QM pivot"),
                      ("breakout_price", "BO level")):
        v = f(row.get(key))
        if v is None or v <= 0:
            continue
        if direction == "bull" and v > ltp:
            cands.append((v, name))
    for key, name in (("day_low", "PDL"), ("low_52w", "52W low"),
                      ("qm_pivot_level", "QM pivot"),
                      ("breakout_price", "BO level")):
        v = f(row.get(key))
        if v is None or v <= 0:
            continue
        if direction == "bear" and v < ltp:
            cands.append((v, name))
    if not cands:
        return None, None
    if direction == "bull":
        lvl, name = min(cands, key=lambda x: x[0])
    else:
        lvl, name = max(cands, key=lambda x: x[0])
    return lvl, name


def score_room(room_adr, wmax):
    if room_adr is None:            # clear sky, nothing overhead
        return wmax
    pts = band(room_adr, [(0.5, 0.0), (1.0, 2.0), (2.0, 5.0),
                          (3.0, 8.0), (float("inf"), 10.0)])
    return wmax * pts / 10.0


def score_sector(adv_pct, direction, wmax):
    if adv_pct is None:
        return wmax * 0.2
    x = adv_pct if direction == "bull" else (100.0 - adv_pct)
    pts = band(x, [(45.0, 0.0), (55.0, 1.0), (70.0, 3.0),
                   (float("inf"), 5.0)])
    return wmax * pts / 5.0


def score_orbhist(rec, wmax):
    if not rec or rec["n"] < ORBHIST_MIN_N:
        return wmax * 0.4          # unknown, not bad
    avg = rec.get("avg_r")
    pts = band(avg, [(-0.05, 0.0), (0.05, 2.0), (0.20, 3.5),
                     (float("inf"), 5.0)])
    return wmax * pts / 5.0


def score_trend(daily_trend, direction, wmax):
    """Kept at weight 0 in v1 — the backtest found no edge. Stored on
    every pick so the Journal can decide on forward data."""
    if wmax <= 0:
        return 0.0
    aligned = ((direction == "bull" and daily_trend == "bull") or
               (direction == "bear" and daily_trend == "bear"))
    return wmax if aligned else 0.0


# ══════════════════════════════════════════════════════════════════════
#  SCORING ONE STOCK
# ══════════════════════════════════════════════════════════════════════

def slope_pct(hist, key, days):
    """Percent change of an EMA over `days` sessions."""
    if len(hist) <= days:
        return None
    now, then = f(hist[0].get(key)), f(hist[days].get(key))
    if now is None or then in (None, 0):
        return None
    return (now - then) / abs(then) * 100.0


def evaluate(sym, hist, bhav, sectors, earnings, orbhist, delivery,
             w, oi_rank, liq_rank, delivery_enabled, explain=False):
    """Returns (pick_dict, reject_reason). One of the two is always None."""
    row = hist[0]
    ltp   = f(row.get("ltp"))
    e9    = f(row.get("ema9_daily"))
    e20   = f(row.get("ema20_daily"))
    adr   = f(row.get("adr_pct"))
    if ltp is None or e9 is None or e20 is None or ltp <= 0:
        return None, "no price/EMA data"

    sep_pct = abs(e9 - e20) / ltp * 100.0

    if ltp > e9 > e20:
        direction, daily_trend = "bull", "bull"
    elif ltp < e9 < e20:
        direction, daily_trend = "bear", "bear"
    else:
        return None, "not stacked (price/9E/20E)"

    if adr is None or adr < w["min_adr"]:
        return None, "ADR %.2f%% below %.2f%%" % (adr or 0, w["min_adr"])

    if sep_pct < w["min_ema_sep"]:
        return None, "9/20 gap %.2f%% below %.2f%% (tangled)" % (
            sep_pct, w["min_ema_sep"])

    slope9  = slope_pct(hist, "ema9_daily",  SLOPE_DAYS)
    slope20 = slope_pct(hist, "ema20_daily", SLOPE_DAYS)
    if slope9 is None or slope20 is None:
        return None, "not enough history for slope"
    if direction == "bull" and (slope9 <= 0 or slope20 <= 0):
        return None, "EMAs not rising"
    if direction == "bear" and (slope9 >= 0 or slope20 >= 0):
        return None, "EMAs not falling"

    if liq_rank is not None and liq_rank < OPT_LIQ_FLOOR:
        return None, "option liquidity in bottom %.0f%%" % OPT_LIQ_FLOOR

    lvl, lvl_name = nearest_level(row, ltp, direction)
    room_adr = None
    if lvl is not None and adr:
        room_adr = abs(lvl - ltp) / ltp * 100.0 / adr

    sec      = row.get("sector")
    adv_pct  = (sectors.get(sec) or {}).get("adv_pct")
    del_pct  = delivery.get(sym)
    rec      = orbhist.get(sym)

    sc = {
        "sc_oi":          score_oi(bhav, direction, oi_rank, w["w_oi"]),
        "sc_ema_struct":  score_ema_struct(slope9, slope20, sep_pct,
                                           direction, w["w_ema_struct"]),
        "sc_ema_prox":    score_ema_prox(f(row.get("pct_from_ema9_daily")),
                                         w["w_ema_prox"]),
        "sc_compression": score_compression(row, w["w_compression"]),
        "sc_volume":      score_volume(f(row.get("vol_ratio")), del_pct,
                                       w["w_volume"], delivery_enabled),
        "sc_room":        score_room(room_adr, w["w_room"]),
        "sc_sector":      score_sector(adv_pct, direction, w["w_sector"]),
        "sc_orbhist":     score_orbhist(rec, w["w_orbhist"]),
        "sc_trend":       score_trend(daily_trend, direction, w["w_trend"]),
    }
    total = sum(sc.values())

    if explain:
        print("  %-14s %s  total %.1f" % (sym, direction.upper(), total))
        for k, v in sc.items():
            print("      %-16s %6.2f" % (k, v))
        print("      %-16s %6.2f%%  9/20 gap" % ("sep_pct", sep_pct))
        print("      %-16s %6.2f%%  ADR" % ("adr_pct", adr))
        print("      %-16s %s" % ("nearest level",
                                  "none (clear)" if lvl is None
                                  else "%s @ %.2f (%.2f ADR)"
                                       % (lvl_name, lvl, room_adr or 0)))

    if total < w["min_score"]:
        return None, "score %.1f below %.0f" % (total, w["min_score"])

    badges, reasons = [], []
    ed = earnings.get(sym)
    if ed is not None and ed <= EARNINGS_WARN:
        badges.append("RESULTS_%dD" % ed)
    if row.get("is_nr7"):
        badges.append("NR7")
        reasons.append("NR7 compression")
    elif row.get("is_nr4"):
        badges.append("NR4")
        reasons.append("NR4 compression")
    p9 = f(row.get("pct_from_ema9_daily"))
    if p9 is not None and abs(p9) > EXTENDED_PCT:
        badges.append("EXTENDED")
    if lvl is None:
        badges.append("CLEAR")
        reasons.append("no level in the way")
    else:
        reasons.append("%s at %.2f, %.1f ADR away" % (lvl_name, lvl,
                                                      room_adr or 0))
    reasons.append("%s" % (bhav.get("oi_buildup") or "OI n/a"))
    reasons.append("9/20 gap %.2f%%, ADR %.2f%%" % (sep_pct, adr))
    if rec and rec["n"] >= ORBHIST_MIN_N:
        reasons.append("own ORB record %.2fR over %d trades"
                       % (rec["avg_r"], rec["n"]))

    conviction = "HIGH" if total >= 75 else "MEDIUM"

    pick = {
        "symbol": sym, "direction": direction,
        "score": r2(total, 2), "conviction": conviction,
        "weights_version": w["version"],
        "ltp": r2(ltp, 2),
        "ema5_daily": r2(f(row.get("ema5_daily")), 2),
        "ema9_daily": r2(e9, 2), "ema20_daily": r2(e20, 2),
        "pct_from_ema9": r2(f(row.get("pct_from_ema9_daily")), 3),
        "pct_from_ema5": r2(f(row.get("pct_from_ema5_daily")), 3),
        "ema_sep_pct": r2(sep_pct, 3),
        "ema9_slope_5d": r2(slope9, 3), "ema20_slope_5d": r2(slope20, 3),
        "adr_pct": r2(adr, 3), "vol_ratio": r2(f(row.get("vol_ratio")), 3),
        "del_pct": r2(del_pct, 2),
        "oi_buildup": bhav.get("oi_buildup"),
        "fut_oi": bhav.get("fut_oi"), "fut_oi_chg": bhav.get("fut_oi_chg"),
        "opt_liq_pct": r2(liq_rank, 2),
        "is_nr4": bool(row.get("is_nr4")), "is_nr7": bool(row.get("is_nr7")),
        "nr_range_pct": r2(f(row.get("nr_range_pct")), 3),
        "consol_days": row.get("consol_days"),
        "qm_contraction": r2(f(row.get("qm_contraction")), 3),
        "prev_day_high": r2(f(row.get("day_high")), 2),
        "prev_day_low": r2(f(row.get("day_low")), 2),
        "high_52w": r2(f(row.get("high_52w")), 2),
        "low_52w": r2(f(row.get("low_52w")), 2),
        "nearest_level": r2(lvl, 2), "nearest_level_type": lvl_name,
        "room_adr": r2(room_adr, 3),
        "daily_trend": daily_trend, "sector": sec,
        "sector_adv_pct": r2(adv_pct, 2),
        "orbhist_trades": (rec or {}).get("n"),
        "orbhist_avg_r": r2((rec or {}).get("avg_r"), 3),
        "earnings_in_days": ed,
        "badges": ",".join(badges) if badges else None,
        "reasons": " | ".join(reasons),
    }
    for k, v in sc.items():
        pick[k] = r2(v, 2)
    return pick, None


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="session date to score (default: latest bhav)")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", default=None,
                    help="print the score breakdown for one symbol")
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 62)
    print(" TrueFlow Next Day WL v2 — scorer")
    print("=" * 62)

    sb = connect_sb()
    w = load_weights(sb)

    session_date, bhav = load_bhav(sb, args.date)
    if not session_date:
        print("FATAL: fo_bhav_oi has no data.")
        return
    sd = date.fromisoformat(session_date)
    target = next_trading_day(sd)
    print("  session %s  ->  target %s" % (sd, target))

    hist = load_momentum(sb, sd - timedelta(days=HISTORY_DAYS), sd)
    sectors  = load_sector_pulse(sb, session_date)
    earnings = load_earnings(sb, sd)
    orbhist  = load_orb_history(sb)
    delivery = load_delivery(sb, session_date)

    # Delivery is scored only if it covers most of the universe.
    coverage = 100.0 * len(delivery) / max(len(bhav), 1)
    delivery_enabled = coverage >= DELIVERY_MIN_COVERAGE
    print("  delivery coverage %.0f%% -> component %s"
          % (coverage, "ON" if delivery_enabled else "OFF (volume takes "
             "the full weight)"))

    # percentile scales, computed once across the universe
    oi_chgs = sorted(abs(f(b.get("fut_oi_chg"), 0) or 0)
                     for b in bhav.values())
    liqs = sorted((f(b.get("total_ce_oi"), 0) or 0) +
                  (f(b.get("total_pe_oi"), 0) or 0) for b in bhav.values())

    picks, rejects = [], {}
    for sym, b in bhav.items():
        h = hist.get(sym)
        if not h:
            rejects["no momentum_stocks row"] = \
                rejects.get("no momentum_stocks row", 0) + 1
            continue
        if h[0]["session_date"] != session_date:
            rejects["stale momentum row"] = \
                rejects.get("stale momentum row", 0) + 1
            continue
        oi_rank = pct_rank(abs(f(b.get("fut_oi_chg"), 0) or 0), oi_chgs)
        liq_rank = pct_rank((f(b.get("total_ce_oi"), 0) or 0) +
                            (f(b.get("total_pe_oi"), 0) or 0), liqs)
        explain = (args.explain or "").upper() == sym
        pick, why = evaluate(sym, h, b, sectors, earnings, orbhist,
                             delivery, w, oi_rank, liq_rank,
                             delivery_enabled, explain)
        if pick:
            picks.append(pick)
        else:
            key = why.split("(")[0].strip()
            rejects[key] = rejects.get(key, 0) + 1

    if args.explain:
        print("\nExplain mode — nothing written.")
        return

    bulls = sorted([p for p in picks if p["direction"] == "bull"],
                   key=lambda p: -p["score"])[:args.top]
    bears = sorted([p for p in picks if p["direction"] == "bear"],
                   key=lambda p: -p["score"])[:args.top]

    for lst in (bulls, bears):
        for i, p in enumerate(lst, 1):
            p["rank"] = i
            p["session_date"] = session_date
            p["target_date"] = target.isoformat()

    print("-" * 62)
    print("PASSED FILTERS: %d   (%d bull / %d bear before top-N)"
          % (len(picks),
             sum(1 for p in picks if p["direction"] == "bull"),
             sum(1 for p in picks if p["direction"] == "bear")))
    print("REJECTED:")
    for k, v in sorted(rejects.items(), key=lambda x: -x[1]):
        print("   %-40s %d" % (k[:40], v))

    print("-" * 62)
    for name, lst in (("BULLISH", bulls), ("BEARISH", bears)):
        print("%s (%d)" % (name, len(lst)))
        for p in lst:
            print("  %2d. %-13s %5.1f  %-6s %-22s %s"
                  % (p["rank"], p["symbol"], p["score"], p["conviction"],
                     (p["oi_buildup"] or "")[:22], p["badges"] or ""))

    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        return

    rows = bulls + bears
    if not rows:
        print("\nNo picks to write.")
        return
    written = 0
    for i in range(0, len(rows), BATCH_WRITE):
        chunk = rows[i:i + BATCH_WRITE]
        try:
            (sb.table("nextday_picks_v2")
               .upsert(chunk, on_conflict="target_date,symbol,direction")
               .execute())
            written += len(chunk)
        except Exception as e:
            print("  write failed: %s" % e)
    print("\nWrote %d pick(s) for %s in %.0fs"
          % (written, target, time.time() - t0))


if __name__ == "__main__":
    main()
