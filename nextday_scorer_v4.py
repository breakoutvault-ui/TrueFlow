#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════
 nextday_scorer.py  —  TrueFlow  (Next Day WL v2)          rev 2
════════════════════════════════════════════════════════════════════════

WHAT THIS DOES
--------------
Every evening it scores the whole F&O universe and writes the highest
conviction setups for the next trading day into `nextday_picks_v2`.

It does NOT touch watchlist_generator.py or watchlist_picks. The old
scorer keeps running untouched. Both models score the same stocks and
both get graded, so the Journal can prove which is better before
anything is retired.

WHERE THE DATA COMES FROM
-------------------------
  ema_filter        PRIMARY. The F&O universe itself (~215/session), plus
                    trend, ema9, ema20, ltp, prev_high, prev_low, atr,
                    high_52w, vol_today, vol_ratio, del_pct, oi_buildup,
                    oi_change_pct, sector. ~85 sessions of history, which
                    is what the EMA slope is measured from.
  momentum_stocks   SUPPLEMENTARY. Range compression (NR4/NR7,
                    contraction, consol days), qm_pivot_level,
                    breakout_price, low_52w, ema5_daily.
  sector_pulse      Sector breadth.
  earnings_moves    Upcoming result dates (badge only, never a reject).
  orb_backtest      Each stock's own measured ORB track record.

  fo_bhav_oi is NOT used. It has zero rows — nse_bhav_fetcher.py dies on
  a SyntaxError at startup, so the table was never populated. Option
  chain liquidity therefore cannot be measured; a turnover floor stands
  in for it and is labelled as such rather than pretending otherwise.

DIRECTION RULE (strict)
-----------------------
A stock is a bull candidate only if BOTH agree:
    price > ema9 > ema20         (structure)
    ema_filter.trend = Bullish   (what the alert engine believes)
Bear is the mirror. Disagreement means skip. This makes it impossible
for a pick and its own intraday alert to contradict each other.

WHAT CHANGED FROM THE OLD MODEL
-------------------------------
  * Fresh Crossover (5 pts) deleted — it rewarded stocks whose EMAs had
    only just crossed, which is the sideways criss-cross chop that kept
    appearing in the picks.
  * Range compression added at 15 pts. Narrow ORBs returned +0.130 R
    against +0.020 R for wide ones in your own backtest.
  * Room to run added at 10 pts — distance to the nearest level in the
    way, measured in ADRs.
  * The stock's own ORB record added at 5 pts, from 8,962 measured
    trades.
  * Hard filters now reject tangled EMAs, low ADR and thin turnover.

THRESHOLDS CAME FROM YOUR DATA, NOT FROM AN OPINION
---------------------------------------------------
Across 8,621 clean trades in orb_backtest:

  ADR%   1.5-2.0 = -0.202 R    2.0-2.5 = -0.024 R
         2.5-3.5 = +0.099 R    >3.5    = +0.334 R   => MIN_ADR 2.5

  Daily 9/20 gap
         <0.25% = +0.008 R     0.25-0.50 = +0.028 R
         >0.50% = +0.078 R and better                => MIN_EMA_SEP 0.50

  Daily trend alignment showed NO measurable edge (with-trend S3/S4
  returned +0.158/+0.168 R against counter-trend S1/S2 +0.176/+0.261 R),
  so w_trend ships at 0. The value is still computed and stored on every
  pick so the Journal can settle it on forward data rather than on 335
  historical trades.

Weights and cutoffs are read from nextday_weights where active = true.
Tuning the model never requires editing this file.

USAGE
-----
  Nightly, after eod_ema / momentum_scan have written today's rows:
      /root/trueflow/bin/python nextday_scorer.py

  Dry run — print everything, write nothing:
      /root/trueflow/bin/python nextday_scorer.py --dry-run

  A specific session:
      /root/trueflow/bin/python nextday_scorer.py --date 2026-08-25

  More picks per side:
      /root/trueflow/bin/python nextday_scorer.py --top 15

  Line-by-line breakdown for one stock:
      /root/trueflow/bin/python nextday_scorer.py --explain RELIANCE

  Why was a stock rejected:
      /root/trueflow/bin/python nextday_scorer.py --why RELIANCE
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
#  TUNABLES  (defaults only — the active nextday_weights row wins)
# ══════════════════════════════════════════════════════════════════════

IST = timezone(timedelta(hours=5, minutes=30))

TOP_N          = 10       # picks per side
SLOPE_DAYS     = 5        # sessions used to measure EMA slope
HISTORY_DAYS   = 21       # calendar days of ema_filter history to load
EARNINGS_WARN  = 5        # badge when results are within N days
EXTENDED_PCT   = 7.0      # badge when price is this far from the 9 EMA
LOW_ADR_BADGE  = 2.5      # badge (not reject) below this ADR
TURNOVER_FLOOR = 10.0     # reject the thinnest N% by traded value
ORBHIST_MIN_N  = 20       # trades before a stock's ORB record counts
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
    """table = [(upper_bound_exclusive, points), ...] ascending."""
    if value is None:
        return default
    for upper, pts in table:
        if value < upper:
            return pts
    return default


def next_trading_day(d):
    """Next weekday. NSE holidays are not in the database, so a pick made
    before a holiday carries that date; the grader finds no candles and
    marks it skipped. Harmless."""
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


def norm_trend(t):
    """ema_filter.trend -> 'bull' / 'bear' / 'neutral'."""
    s = (t or "").strip().lower()
    if s.startswith("bull"):
        return "bull"
    if s.startswith("bear"):
        return "bear"
    return "neutral"


# ══════════════════════════════════════════════════════════════════════
#  CONNECTION + PAGED READS
# ══════════════════════════════════════════════════════════════════════

def connect_sb():
    sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
    print("Supabase connected.")
    return sb


def page_select(builder, size=1000, key="id"):
    """Supabase silently caps reads at 1000 rows. Page on a UNIQUE key and
    de-duplicate — paging on session_date duplicated rows and destroyed a
    20-minute run once already.

    `key` must name a column that actually exists on the table being read.
    Not every table has `id`: orb_backtest is keyed on alert_id, and
    assuming otherwise made both of those loaders fail silently."""
    rows, page = [], 0
    while True:
        chunk = builder(page * size, page * size + size - 1) or []
        rows.extend(chunk)
        if len(chunk) < size:
            break
        page += 1
    seen, uniq = set(), []
    for r in rows:
        k = r.get(key)
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


def load_ema_filter(sb, session_date=None):
    """PRIMARY source. Returns (session_date, current_rows, history)."""
    if session_date is None:
        r = (sb.table("ema_filter").select("session_date")
               .order("session_date", desc=True).limit(1)
               .execute().data or [])
        if not r:
            return None, {}, {}
        session_date = r[0]["session_date"]
    sd = date.fromisoformat(session_date)
    cols = ("id,symbol,session_date,trend,ema9,ema20,ltp,prev_high,"
            "prev_low,prev_close,atr,high_52w,vol_today,vol_avg20,"
            "vol_ratio,del_qty,del_pct,oi_buildup,oi_change_pct,sector")
    start = (sd - timedelta(days=HISTORY_DAYS)).isoformat()
    rows = page_select(lambda a, b: (
        sb.table("ema_filter").select(cols)
          .gte("session_date", start)
          .lte("session_date", session_date)
          .order("id").range(a, b).execute().data))
    hist = {}
    for r in rows:
        hist.setdefault(r["symbol"], []).append(r)
    for sym in hist:
        hist[sym].sort(key=lambda x: x["session_date"], reverse=True)
    cur = {s: h[0] for s, h in hist.items()
           if h and h[0]["session_date"] == session_date}
    print("  ema_filter %s: %d symbols current, %d history rows"
          % (session_date, len(cur), len(rows)))
    return session_date, cur, hist


def load_momentum(sb, session_date):
    """SUPPLEMENTARY. Compression and level features.

    Queried one exact session at a time. A date RANGE across
    momentum_stocks (1,450 rows a day, months of history) makes Postgres
    scan and sort far too much and Supabase kills it with
    'canceling statement due to statement timeout'. An equality match on
    a single date reads ~1,450 rows and returns instantly.

    This is supplementary data — if it is unavailable the scorer still
    runs, and score_compression() falls back to a neutral value rather
    than a zero."""
    cols = ("id,symbol,session_date,adr_pct,is_nr4,is_nr7,nr_range_pct,"
            "consol_days,qm_contraction,qm_pivot_level,breakout_price,"
            "low_52w,ema5_daily,pct_from_ema5_daily,company_name,industry")
    try:
        rows = page_select(lambda a, b: (
            sb.table("momentum_stocks").select(cols)
              .eq("session_date", session_date)
              .order("id").range(a, b).execute().data))
    except Exception as e:
        print("  momentum_stocks read failed (%s)" % str(e)[:70])
        print("  continuing WITHOUT compression/level features")
        return {}
    if not rows:
        print("  momentum_stocks: nothing dated %s" % session_date)
        return {}
    best = {r["symbol"]: r for r in rows}
    print("  momentum_stocks: %d symbols dated %s" % (len(best), session_date))
    return best


def load_sector_pulse(sb, session_date, _retry=True):
    rows = page_select(lambda a, b: (
        sb.table("sector_pulse").select("id,sector,adv,dec,total,avg_chg")
          .eq("session_date", session_date).order("id")
          .range(a, b).execute().data))
    if not rows and _retry:
        r = (sb.table("sector_pulse").select("session_date")
               .order("session_date", desc=True).limit(1)
               .execute().data or [])
        if r and r[0]["session_date"] != session_date:
            print("  sector_pulse: nothing for %s, using %s"
                  % (session_date, r[0]["session_date"]))
            return load_sector_pulse(sb, r[0]["session_date"], False)
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
    # Small table (upcoming results only) and it has no `id` column, so
    # this is a single request rather than a paged read.
    rows = (sb.table("earnings_moves").select("symbol,result_date")
              .gte("result_date", today.isoformat())
              .order("result_date").limit(1000).execute().data) or []
    if len(rows) >= 1000:
        print("  earnings_moves hit the 1000-row cap — badges may be partial")
    out = {}
    for r in rows:
        try:
            d = date.fromisoformat(r["result_date"])
        except Exception:
            continue
        days = (d - today).days
        if out.get(r["symbol"]) is None or days < out[r["symbol"]]:
            out[r["symbol"]] = days
    print("  earnings_moves: %d symbols with upcoming results" % len(out))
    return out


def load_orb_history(sb):
    """Each stock's own ORB record. Exit method A (hold with the BO stop
    to 3:20) is the reference — the only method that made money."""
    try:
        rows = page_select(lambda a, b: (
            sb.table("orb_backtest").select("alert_id,symbol,exit_a_r")
              .eq("status", "ok").order("alert_id").range(a, b)
              .execute().data), key="alert_id")
    except Exception as e:
        print("  orb_backtest read failed (%s)" % str(e)[:70])
        print("  continuing WITHOUT per-stock ORB track record")
        return {}
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
    for a in agg.values():
        a["avg_r"] = a["sum"] / a["n"] if a["n"] else None
        a["win_pct"] = 100.0 * a["wins"] / a["n"] if a["n"] else None
    print("  orb_backtest: track record for %d symbols" % len(agg))
    return agg


# ══════════════════════════════════════════════════════════════════════
#  COMPONENT SCORES  — each returns points scaled to its weight
# ══════════════════════════════════════════════════════════════════════

def score_oi(oi_buildup, oi_rank, direction, wmax):
    label = (oi_buildup or "").strip().lower()
    base = OI_SUPPORT[direction].get(label)
    if base is None:
        base = 0.35              # unrecognised / Unknown: neutral
    mag = 0.0 if oi_rank is None else oi_rank / 100.0
    return wmax * (0.75 * base + 0.25 * base * mag)


def score_ema_struct(slope9, slope20, sep_pct, direction, wmax):
    """Slope (8/15) + separation (7/15)."""
    want_up = direction == "bull"
    s9 = slope9 if want_up else (-slope9 if slope9 is not None else None)
    s20 = slope20 if want_up else (-slope20 if slope20 is not None else None)
    slope_pts = 0.0
    if s9 is not None and s20 is not None and s9 > 0 and s20 > 0:
        slope_pts = 8.0 * min(1.0, (s9 + s20) / 2.0 / 2.0)
        slope_pts = max(slope_pts, 2.0)
    sep_pts = band(sep_pct, [(0.50, 0.0), (1.00, 3.0), (2.00, 5.0),
                             (3.00, 6.0), (float("inf"), 7.0)])
    return wmax * (slope_pts + sep_pts) / 15.0


def score_ema_prox(pct_from_9, wmax):
    d = abs(pct_from_9) if pct_from_9 is not None else None
    pts = band(d, [(1.0, 15.0), (2.0, 12.0), (4.0, 8.0),
                   (7.0, 4.0), (float("inf"), 0.0)])
    return wmax * pts / 15.0


def score_compression(m, wmax):
    """m may be None if momentum_stocks has no row for this symbol."""
    if not m:
        return wmax * 0.2         # unknown, not zero
    pts = 0.0
    if m.get("is_nr7"):
        pts += 8.0
    elif m.get("is_nr4"):
        pts += 5.0
    contr = f(m.get("qm_contraction"))
    if contr is not None:
        pts += band(contr, [(0.4, 4.0), (0.6, 3.0), (0.8, 2.0),
                            (float("inf"), 0.0)])
    cd = f(m.get("consol_days"))
    if cd is not None:
        pts += band(cd, [(3.0, 0.0), (5.0, 1.0), (21.0, 3.0),
                         (40.0, 1.5), (float("inf"), 0.0)])
    return wmax * min(pts, 15.0) / 15.0


def score_volume(vol_ratio, del_pct, wmax):
    """Volume 8 + delivery 7. Delivery comes from ema_filter and covers
    the whole universe, so there is no missing-data path here."""
    v = band(vol_ratio, [(1.0, 0.0), (1.2, 2.0), (1.5, 4.0),
                         (2.0, 6.0), (float("inf"), 8.0)])
    d = band(del_pct, [(30.0, 0.0), (40.0, 1.0), (50.0, 3.0),
                       (60.0, 5.0), (float("inf"), 7.0)], default=1.0)
    return wmax * (v + d) / 15.0


def nearest_level(e, m, ltp, direction):
    """Room to run, measured FROM THE TRIGGER — not from spot.

    The first version measured spot -> PDH and treated PDH as a wall.
    That is backwards for this strategy: PDH is where the trade STARTS,
    not where it stops. It scored a coiled stock sitting just under its
    previous day high as having "no room", which is precisely the setup
    that is about to break out. That single error suppressed scores
    across the whole universe.

    So: trigger = PDH for a long, PDL for a short. Room is the distance
    from that trigger to the next real level BEYOND it. Nothing beyond
    means clear sky and full marks.

    Returns (level, name, trigger). level None = clear."""
    m = m or {}
    if direction == "bull":
        trigger = f(e.get("prev_high")) or ltp
        src = [(f(e.get("high_52w")), "52W high"),
               (f(m.get("qm_pivot_level")), "QM pivot"),
               (f(m.get("breakout_price")), "BO level")]
        cands = [(v, n) for v, n in src if v and v > trigger]
        if not cands:
            return None, None, trigger
        lvl, name = min(cands, key=lambda x: x[0])
        return lvl, name, trigger
    trigger = f(e.get("prev_low")) or ltp
    src = [(f(m.get("low_52w")), "52W low"),
           (f(m.get("qm_pivot_level")), "QM pivot"),
           (f(m.get("breakout_price")), "BO level")]
    cands = [(v, n) for v, n in src if v and v < trigger]
    if not cands:
        return None, None, trigger
    lvl, name = max(cands, key=lambda x: x[0])
    return lvl, name, trigger


def score_room(room_adr, wmax):
    if room_adr is None:          # clear sky, nothing in the way
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
        return wmax * 0.4         # unknown, not bad
    pts = band(rec.get("avg_r"), [(-0.05, 0.0), (0.05, 2.0),
                                  (0.20, 3.5), (float("inf"), 5.0)])
    return wmax * pts / 5.0


def score_trend(daily_trend, direction, wmax):
    """Weight 0 in v1 — the backtest found no edge. Still computed and
    stored so the Journal can decide on forward data."""
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


def evaluate(sym, ehist, m, sectors, earnings, orbhist, w,
             oi_rank, turn_rank, explain=False, near_out=None):
    """Returns (pick_dict, reject_reason). Exactly one is ever set."""
    e   = ehist[0]
    ltp = f(e.get("ltp"))
    e9  = f(e.get("ema9"))
    e20 = f(e.get("ema20"))
    if ltp is None or e9 is None or e20 is None or ltp <= 0 or e9 <= 0:
        return None, "no price/EMA data"

    # ── direction: structure AND engine trend must agree ──────────────
    if ltp > e9 > e20:
        stack = "bull"
    elif ltp < e9 < e20:
        stack = "bear"
    else:
        return None, "not stacked price/9E/20E"
    trend = norm_trend(e.get("trend"))
    if trend != stack:
        return None, "structure %s vs engine trend %s" % (stack, trend)
    direction = daily_trend = stack

    sep_pct    = abs(e9 - e20) / ltp * 100.0
    pct_from_9 = (ltp - e9) / e9 * 100.0

    # ADR: momentum_stocks preferred, ema_filter ATR as fallback
    adr, adr_src = f((m or {}).get("adr_pct")), "momentum"
    if adr is None:
        atr = f(e.get("atr"))
        if atr is not None and ltp:
            adr, adr_src = atr / ltp * 100.0, "atr"
    if adr is None or adr < w["min_adr"]:
        return None, "ADR below %.2f" % w["min_adr"]

    if sep_pct < w["min_ema_sep"]:
        return None, "9/20 gap below %.2f - tangled" % w["min_ema_sep"]

    slope9  = slope_pct(ehist, "ema9",  SLOPE_DAYS)
    slope20 = slope_pct(ehist, "ema20", SLOPE_DAYS)
    if slope9 is None or slope20 is None:
        return None, "not enough history for slope"
    if direction == "bull" and (slope9 <= 0 or slope20 <= 0):
        return None, "EMAs not rising"
    if direction == "bear" and (slope9 >= 0 or slope20 >= 0):
        return None, "EMAs not falling"

    if turn_rank is not None and turn_rank < TURNOVER_FLOOR:
        return None, "turnover in bottom %.0f pct" % TURNOVER_FLOOR

    lvl, lvl_name, trigger = nearest_level(e, m, ltp, direction)
    room_adr = (abs(lvl - trigger) / ltp * 100.0 / adr) \
               if (lvl and adr) else None
    trig_adr = (abs(trigger - ltp) / ltp * 100.0 / adr) if adr else None

    sec     = e.get("sector") or (m or {}).get("industry")
    adv_pct = (sectors.get(sec) or {}).get("adv_pct")
    del_pct = f(e.get("del_pct"))
    rec     = orbhist.get(sym)

    sc = {
        "sc_oi":          score_oi(e.get("oi_buildup"), oi_rank,
                                   direction, w["w_oi"]),
        "sc_ema_struct":  score_ema_struct(slope9, slope20, sep_pct,
                                           direction, w["w_ema_struct"]),
        "sc_ema_prox":    score_ema_prox(pct_from_9, w["w_ema_prox"]),
        "sc_compression": score_compression(m, w["w_compression"]),
        "sc_volume":      score_volume(f(e.get("vol_ratio")), del_pct,
                                       w["w_volume"]),
        "sc_room":        score_room(room_adr, w["w_room"]),
        "sc_sector":      score_sector(adv_pct, direction, w["w_sector"]),
        "sc_orbhist":     score_orbhist(rec, w["w_orbhist"]),
        "sc_trend":       score_trend(daily_trend, direction, w["w_trend"]),
    }
    total = sum(sc.values())

    if explain:
        print("  %-14s %s   TOTAL %.1f" % (sym, direction.upper(), total))
        for k, v in sc.items():
            print("      %-16s %6.2f" % (k, v))
        print("      %-16s %6.2f%%" % ("9/20 gap", sep_pct))
        print("      %-16s %6.2f%% (%s)" % ("ADR", adr, adr_src))
        print("      %-16s %6.2f%% / %.2f%%" % ("slope 9/20", slope9, slope20))
        print("      %-16s %.2f (%.2f ADR from spot)"
              % ("trigger", trigger, trig_adr or 0))
        print("      %-16s %s" % ("room beyond it",
              "clear sky" if lvl is None
              else "%s @ %.2f = %.2f ADR" % (lvl_name, lvl, room_adr or 0)))
        print("      %-16s %s / del %s / vol %s"
              % ("oi/del/vol", e.get("oi_buildup"), del_pct,
                 e.get("vol_ratio")))

    if total < w["min_score"]:
        if near_out is not None:
            near_out[0], near_out[1] = round(total, 1), direction
        return None, "score below %.0f" % w["min_score"]

    badges, reasons = [], []
    ed = earnings.get(sym)
    if ed is not None and ed <= EARNINGS_WARN:
        badges.append("RESULTS_%dD" % ed)
    if (m or {}).get("is_nr7"):
        badges.append("NR7")
        reasons.append("NR7 compression")
    elif (m or {}).get("is_nr4"):
        badges.append("NR4")
        reasons.append("NR4 compression")
    if abs(pct_from_9) > EXTENDED_PCT:
        badges.append("EXTENDED")
    if adr < LOW_ADR_BADGE:
        badges.append("LOW_ADR")
    if trig_adr is not None and trig_adr > 1.5:
        badges.append("FAR_TRIGGER")
    if lvl is None:
        badges.append("CLEAR")
        reasons.append("no level in the way")
    else:
        reasons.append("%s at %.2f, %.1f ADR beyond trigger %.2f"
                       % (lvl_name, lvl, room_adr or 0, trigger))
    reasons.append(e.get("oi_buildup") or "OI n/a")
    reasons.append("9/20 gap %.2f%%, ADR %.2f%%" % (sep_pct, adr))
    if rec and rec["n"] >= ORBHIST_MIN_N:
        reasons.append("own ORB record %.2fR over %d trades"
                       % (rec["avg_r"], rec["n"]))

    pick = {
        "symbol": sym, "direction": direction,
        "score": r2(total, 2),
        "conviction": "HIGH" if total >= 75 else "MEDIUM",
        "weights_version": w["version"],
        "ltp": r2(ltp, 2),
        "ema5_daily": r2(f((m or {}).get("ema5_daily")), 2),
        "ema9_daily": r2(e9, 2), "ema20_daily": r2(e20, 2),
        "pct_from_ema9": r2(pct_from_9, 3),
        "pct_from_ema5": r2(f((m or {}).get("pct_from_ema5_daily")), 3),
        "ema_sep_pct": r2(sep_pct, 3),
        "ema9_slope_5d": r2(slope9, 3), "ema20_slope_5d": r2(slope20, 3),
        "adr_pct": r2(adr, 3),
        "vol_ratio": r2(f(e.get("vol_ratio")), 3),
        "del_pct": r2(del_pct, 2),
        "oi_buildup": e.get("oi_buildup"),
        "opt_liq_pct": r2(turn_rank, 2),
        "is_nr4": bool((m or {}).get("is_nr4")),
        "is_nr7": bool((m or {}).get("is_nr7")),
        "nr_range_pct": r2(f((m or {}).get("nr_range_pct")), 3),
        "consol_days": (m or {}).get("consol_days"),
        "qm_contraction": r2(f((m or {}).get("qm_contraction")), 3),
        "prev_day_high": r2(f(e.get("prev_high")), 2),
        "prev_day_low": r2(f(e.get("prev_low")), 2),
        "high_52w": r2(f(e.get("high_52w")), 2),
        "low_52w": r2(f((m or {}).get("low_52w")), 2),
        "nearest_level": r2(lvl, 2), "nearest_level_type": lvl_name,
        "trigger_level": r2(trigger, 2), "trigger_adr": r2(trig_adr, 3),
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
    ap.add_argument("--date", default=None)
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--explain", default=None)
    ap.add_argument("--why", default=None,
                    help="print the rejection reason for one symbol")
    args = ap.parse_args()

    t0 = time.time()
    print("=" * 62)
    print(" TrueFlow Next Day WL v2 — scorer (rev 2, ema_filter)")
    print("=" * 62)

    sb = connect_sb()
    w  = load_weights(sb)

    session_date, cur, ehist = load_ema_filter(sb, args.date)
    if not session_date or not cur:
        print("FATAL: ema_filter has no usable data.")
        return
    sd     = date.fromisoformat(session_date)
    target = next_trading_day(sd)
    print("  session %s  ->  target %s" % (sd, target))

    mom = load_momentum(sb, session_date)
    try:
        sectors = load_sector_pulse(sb, session_date)
    except Exception as e:
        print("  sector_pulse failed (%s) — sector scores neutral"
              % str(e)[:60])
        sectors = {}
    try:
        earnings = load_earnings(sb, sd)
    except Exception as e:
        print("  earnings_moves failed (%s) — no results badges"
              % str(e)[:60])
        earnings = {}
    orbhist = load_orb_history(sb)

    # percentile scales, computed once across the universe
    oi_chgs = sorted(abs(f(e.get("oi_change_pct"), 0) or 0)
                     for e in cur.values())
    turns = sorted((f(e.get("ltp"), 0) or 0) * (f(e.get("vol_today"), 0) or 0)
                   for e in cur.values())

    picks, rejects, why_hit, near_miss = [], {}, None, []
    for sym in sorted(cur.keys()):
        e = cur[sym]
        oi_rank = pct_rank(abs(f(e.get("oi_change_pct"), 0) or 0), oi_chgs)
        turn_rank = pct_rank((f(e.get("ltp"), 0) or 0) *
                             (f(e.get("vol_today"), 0) or 0), turns)
        explain = (args.explain or "").upper() == sym
        near_score = [None, None]
        pick, why = evaluate(sym, ehist[sym], mom.get(sym), sectors,
                             earnings, orbhist, w, oi_rank, turn_rank,
                             explain, near_score)
        if (args.why or "").upper() == sym:
            why_hit = why if why else "PASSED with score %.1f" % pick["score"]
        if pick:
            picks.append(pick)
        else:
            rejects[why] = rejects.get(why, 0) + 1
            if why.startswith("score below"):
                near_miss.append((sym, near_score[0], near_score[1]))

    if args.why:
        print("\n%s -> %s" % (args.why.upper(),
                              why_hit or "not in the universe"))
        return
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
            p["target_date"]  = target.isoformat()

    print("-" * 62)
    print("PASSED: %d  (%d bull / %d bear before top-N)"
          % (len(picks),
             sum(1 for p in picks if p["direction"] == "bull"),
             sum(1 for p in picks if p["direction"] == "bear")))
    print("REJECTED:")
    for k, v in sorted(rejects.items(), key=lambda x: -x[1]):
        print("   %-44s %d" % (k[:44], v))
    if near_miss:
        near_miss.sort(key=lambda x: -x[1])
        print("NEAR MISSES (best scores that failed min_score %.0f):"
              % w["min_score"])
        for sym, sc, d in near_miss[:8]:
            print("   %-14s %5.1f  %s" % (sym, sc, d))

    print("-" * 62)
    for name, lst in (("BULLISH", bulls), ("BEARISH", bears)):
        print("%s (%d)" % (name, len(lst)))
        for p in lst:
            print("  %2d. %-13s %5.1f %-6s %-16s %s"
                  % (p["rank"], p["symbol"], p["score"], p["conviction"],
                     (p["oi_buildup"] or "")[:16], p["badges"] or ""))

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
        except Exception as ex:
            print("  write failed: %s" % ex)
    print("\nWrote %d pick(s) for %s in %.0fs"
          % (written, target, time.time() - t0))


if __name__ == "__main__":
    main()
