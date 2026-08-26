#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════
 nextday_grader.py  —  TrueFlow  (Next Day WL v2, script 3)
════════════════════════════════════════════════════════════════════════

WHAT THIS DOES
--------------
Takes every pick made for a trading day and works out, from the actual
5-minute candles, what the trade would have done. One row per pick into
`nextday_outcomes`.

Recorded per pick: the opening range and whether it was wide or narrow,
the gap, whether the ORB broke and at what time, the entry, the stop, the
R-multiple, the plain % move, MFE, MAE, whether it reached +1R and +2R,
the day's regime, and whether one of your own intraday alerts fired on
that stock.

HOW A TRADE IS MEASURED  (identical to orb_simulate.py on purpose)
------------------------------------------------------------------
  * Opening range = 9:15 to 9:30.
  * A breakout candle is a 5-minute candle that CLOSES outside the range.
  * Entry = that candle's high + 1 tick (long) or low - 1 tick (short),
    filled on a later candle that actually reaches it.
  * Stop = that same candle's low (long) or high (short).
  * R = entry - stop. Everything is reported in R so a Rs.400 stock and a
    Rs.35,000 stock are comparable.
  * Exit method A: hold with the BO stop, exit at 15:20. That was the
    only method of the nine tested that made money.
  * Within one candle we ALWAYS assume the stop executed before the high.
    A candle that both spiked to target and hit the stop is booked a
    loss. Conservative by design — results will understate rather than
    flatter.

  The one deliberate difference from orb_simulate: that script pairs a
  breakout with the alert that fired. A pick has no alert, so this takes
  the FIRST breakout that triggers — which is what you would actually
  trade watching from 9:30.

OUTCOMES ARE MODEL-AGNOSTIC
---------------------------
An outcome is a fact about a stock on a day, not about who picked it. So
`nextday_outcomes` is keyed on (target_date, symbol, direction) and each
is graded once. Use --with-old to also pull in symbols from the OLD
watchlist_picks table; attribution happens later by joining whichever
picks table you care about. That is what lets the Journal compare the two
models honestly on identical measurement.

USAGE
-----
  Grade today's picks (run after 15:30):
      /root/trueflow/bin/python nextday_grader.py

  A specific day:
      /root/trueflow/bin/python nextday_grader.py --date 2026-08-26

  Catch up the last week:
      /root/trueflow/bin/python nextday_grader.py --days 7

  30-day backfill including the old model's picks, for the Journal:
      /root/trueflow/bin/python nextday_grader.py --days 30 --with-old

  Test on a few first:
      /root/trueflow/bin/python nextday_grader.py --days 5 --limit 5 --dry-run
════════════════════════════════════════════════════════════════════════
"""

import sys
import time
import math
import argparse
from datetime import datetime, date, timedelta, timezone

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
#  TUNABLES  — same values as orb_simulate.py so results are comparable
# ══════════════════════════════════════════════════════════════════════

IST = timezone(timedelta(hours=5, minutes=30))

ORB_START     = (9, 15)
ORB_END       = (9, 30)
HARD_EXIT     = (15, 20)
TICK          = 0.05
ATR_PERIOD    = 14
ADR_PERIOD    = 20
WIDE_ORB_ATR  = 0.40      # ORB wider than 40% of daily ATR = WIDE

# Day regime from the index. Measured on range vs ADR rather than EMA
# crossings: the crossing-count method in orb_simulate used a threshold
# of 6 crossings on a 75-bar day, which labelled literally every day
# "Range" and made the whole dimension useless.
REGIME_TREND_MIN = 1.00   # day range >= this x ADR  -> Trend
REGIME_RANGE_MAX = 0.70   # day range <  this x ADR  -> Range

KITE_SLEEP    = 0.35
CHUNK_DAYS_5M = 95
BATCH_WRITE   = 200

INDEX_SYMBOLS = {
    "BANKNIFTY": "NIFTY BANK",
    "NIFTY":     "NIFTY 50",
    "FINNIFTY":  "NIFTY FIN SERVICE",
}
REGIME_INDEX = "NIFTY 50"


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
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, nd)
    except (TypeError, ValueError):
        return None


def as_ist(dt):
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


def atr_series(candles, period):
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


def adr_series(candles, period):
    n = len(candles)
    out = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        w = candles[i - period + 1:i + 1]
        vals = [(c["high"] - c["low"]) for c in w]
        out[i] = sum(vals) / len(vals) if vals else None
    return out


# ══════════════════════════════════════════════════════════════════════
#  CONNECTIONS
# ══════════════════════════════════════════════════════════════════════

def connect_kite():
    kite = KiteConnect(api_key=CFG.KITE_API_KEY)
    token_path = getattr(CFG, "ACCESS_TOKEN_PATH",
                         "/root/trueflow/access_token.txt")
    with open(token_path) as fh:
        kite.set_access_token(fh.read().strip())
    prof = kite.profile()
    print("Kite connected: %s" % prof.get("user_name", "?"))
    return kite


def connect_sb():
    sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
    print("Supabase connected.")
    return sb


def page_select(builder, size=1000, key="id"):
    """Supabase caps reads at 1000 rows. Page on a key that EXISTS on the
    table — not every table has `id`."""
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
#  LOADING WHAT NEEDS GRADING
# ══════════════════════════════════════════════════════════════════════

def load_targets(sb, d_from, d_to, with_old, regrade):
    """Returns {(target_date, symbol, direction): {...}}"""
    out = {}

    rows = page_select(lambda a, b: (
        sb.table("nextday_picks_v2")
          .select("id,target_date,symbol,direction,score,conviction,"
                  "weights_version,trigger_level")
          .gte("target_date", d_from.isoformat())
          .lte("target_date", d_to.isoformat())
          .order("id").range(a, b).execute().data))
    for r in rows:
        out[(r["target_date"], r["symbol"], r["direction"])] = {
            "pick_id": r["id"], "score": r.get("score"),
            "conviction": r.get("conviction"),
            "weights_version": r.get("weights_version"),
        }
    print("  nextday_picks_v2: %d picks" % len(rows))

    if with_old:
        try:
            old = page_select(lambda a, b: (
                sb.table("watchlist_picks")
                  .select("id,target_date,symbol,direction,score")
                  .gte("target_date", d_from.isoformat())
                  .lte("target_date", d_to.isoformat())
                  .order("id").range(a, b).execute().data))
            added = 0
            for r in old:
                d = (r.get("direction") or "").strip().lower()
                if d.startswith("bull") or d in ("long", "buy"):
                    d = "bull"
                elif d.startswith("bear") or d in ("short", "sell"):
                    d = "bear"
                else:
                    continue
                k = (r["target_date"], r["symbol"], d)
                if k not in out:
                    out[k] = {"pick_id": None, "score": None,
                              "conviction": None, "weights_version": None}
                    added += 1
            print("  watchlist_picks (old model): %d rows, %d new keys"
                  % (len(old), added))
        except Exception as e:
            print("  watchlist_picks read failed (%s)" % str(e)[:70])

    if not regrade:
        try:
            done = page_select(lambda a, b: (
                sb.table("nextday_outcomes")
                  .select("id,target_date,symbol,direction")
                  .gte("target_date", d_from.isoformat())
                  .lte("target_date", d_to.isoformat())
                  .order("id").range(a, b).execute().data))
            before = len(out)
            for r in done:
                out.pop((r["target_date"], r["symbol"], r["direction"]), None)
            if before != len(out):
                print("  already graded: %d (use --regrade to redo)"
                      % (before - len(out)))
        except Exception as e:
            print("  outcomes read failed (%s)" % str(e)[:70])

    return out


def load_alerts(sb, d_from, d_to):
    """Did one of your own intraday alerts fire on this stock that day?"""
    try:
        rows = page_select(lambda a, b: (
            sb.table("fo_alerts")
              .select("id,symbol,direction,stage,fired_at,session_date")
              .gte("session_date", d_from.isoformat())
              .lte("session_date", d_to.isoformat())
              .order("id").range(a, b).execute().data))
    except Exception as e:
        print("  fo_alerts read failed (%s)" % str(e)[:70])
        return {}
    out = {}
    for r in rows:
        d = "bull" if (r.get("direction") or "").lower().startswith("bull") \
            else "bear"
        k = (r["session_date"], r["symbol"], d)
        fired = as_ist(r.get("fired_at"))
        prev = out.get(k)
        if prev is None or (fired and prev["t"] and fired < prev["t"]):
            out[k] = {"t": fired, "stage": r.get("stage")}
    print("  fo_alerts: %d symbol-days with an alert" % len(out))
    return out


# ══════════════════════════════════════════════════════════════════════
#  KITE DATA
# ══════════════════════════════════════════════════════════════════════

def build_token_map(kite, wanted):
    print("Fetching NSE instrument list...")
    inst = kite.instruments("NSE")
    lookup = {i["tradingsymbol"]: i["instrument_token"] for i in inst}
    tok, missing = {}, []
    for sym in wanted:
        name = INDEX_SYMBOLS.get(sym, sym)
        if name in lookup:
            tok[sym] = lookup[name]
        else:
            missing.append(sym)
    if missing:
        print("  no NSE instrument for %d symbol(s): %s"
              % (len(missing), ", ".join(sorted(missing)[:12])))
    print("  resolved %d/%d symbols" % (len(tok), len(wanted)))
    return tok, lookup


def fetch_candles(kite, token, d_from, d_to, interval, chunk_days):
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
                          % (interval, cur, end, str(e)[:50]))
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
        if c["date"]:
            g.setdefault(c["date"].date(), []).append(c)
    return g


# ══════════════════════════════════════════════════════════════════════
#  DAY REGIME
# ══════════════════════════════════════════════════════════════════════

def build_regime(kite, lookup, d_from, d_to):
    """Trend / Range / Mixed per day, from the index's own range vs ADR."""
    tok = lookup.get(REGIME_INDEX)
    if not tok:
        print("  no token for %s — day regime unavailable" % REGIME_INDEX)
        return {}
    daily = fetch_candles(kite, tok, d_from - timedelta(days=60), d_to,
                          "day", 1900)
    if not daily:
        return {}
    adr = adr_series(daily, ADR_PERIOD)
    out = {}
    for i, c in enumerate(daily):
        a = adr[i]
        if not a or a <= 0:
            continue
        rng = c["high"] - c["low"]
        ratio = rng / a
        if ratio >= REGIME_TREND_MIN:
            lab = "Trend"
        elif ratio < REGIME_RANGE_MAX:
            lab = "Range"
        else:
            lab = "Mixed"
        chg = None
        if i > 0 and daily[i - 1]["close"]:
            chg = (c["close"] - daily[i - 1]["close"]) \
                / daily[i - 1]["close"] * 100.0
        if chg is not None and lab == "Trend":
            lab = "Trend Up" if chg > 0 else "Trend Down"
        out[c["date"].date()] = {"regime": lab, "chg": chg,
                                 "range_vs_adr": ratio}
    print("  day regime built for %d sessions" % len(out))
    return out


# ══════════════════════════════════════════════════════════════════════
#  THE CORE — grade one pick
# ══════════════════════════════════════════════════════════════════════

def find_first_breakout(bars, level, is_bull, lock):
    """First 5m candle CLOSING outside the range whose trigger price is
    then actually reached by a later candle.

    Returns (bo_index, entry_index) or ("NEVER", None) if it closed
    outside but never triggered, or (None, None) if it never broke."""
    any_break = False
    for i, b in enumerate(bars):
        if b["date"] < lock:
            continue
        outside = (b["close"] > level) if is_bull else (b["close"] < level)
        if not outside:
            continue
        any_break = True
        trig = (b["high"] + TICK) if is_bull else (b["low"] - TICK)
        for j in range(i + 1, len(bars)):
            hit = (bars[j]["high"] >= trig) if is_bull \
                else (bars[j]["low"] <= trig)
            if hit:
                return i, j
    return ("NEVER", None) if any_break else (None, None)


def walk(bars, start_i, entry, stop, is_bull, hard_exit, risk):
    """Exit method A: hold with the BO stop, out at 15:20.
    Stop always assumed to execute before the favourable extreme."""
    sgn = 1.0 if is_bull else -1.0
    best = worst = entry
    last = None
    for i in range(start_i, len(bars)):
        b = bars[i]
        if b["date"] > hard_exit:
            break
        fav = b["high"] if is_bull else b["low"]
        adv = b["low"] if is_bull else b["high"]
        if sgn * (fav - best) > 0:
            best = fav
        if sgn * (adv - worst) < 0:
            worst = adv
        last = b
        if sgn * (adv - stop) <= 0:
            mins = int((b["date"] - bars[start_i]["date"]).total_seconds()
                       / 60) + 5
            return (-1.0, stop, b["date"], "stop", mins,
                    sgn * (best - entry) / risk,
                    sgn * (worst - entry) / risk)
    if last is None:
        return None
    mins = int((last["date"] - bars[start_i]["date"]).total_seconds() / 60) + 5
    r = sgn * (last["close"] - entry) / risk
    return (r, last["close"], last["date"], "time_exit", mins,
            sgn * (best - entry) / risk, sgn * (worst - entry) / risk)


def grade(sym, direction, d, bars5, daily_by_day, meta, regime, alert):
    """Returns a row dict for nextday_outcomes, or (None, reason)."""
    if not bars5:
        return None, "no 5m candles"
    day_bars = [b for b in bars5 if b["date"].date() == d]
    if len(day_bars) < 10:
        return None, "too few candles"

    is_bull = direction == "bull"
    first = day_bars[0]
    orb_bars = [b for b in day_bars
                if hhmm(first["date"], ORB_START) <= b["date"]
                < hhmm(first["date"], ORB_END)]
    if not orb_bars:
        return None, "no opening range"
    orb_high = max(b["high"] for b in orb_bars)
    orb_low = min(b["low"] for b in orb_bars)
    lock = hhmm(first["date"], ORB_END)
    hard = hhmm(first["date"], HARD_EXIT)

    dc = daily_by_day.get(d)
    prev_close = meta.get("prev_close")
    atr_d = meta.get("atr")
    day_open = day_bars[0]["open"]
    gap_pct = ((day_open - prev_close) / prev_close * 100.0) \
        if prev_close else None

    orb_range = orb_high - orb_low
    orb_range_pct = (orb_range / day_open * 100.0) if day_open else None
    orb_vs_atr = (orb_range / atr_d) if atr_d else None
    orb_type = None
    if orb_vs_atr is not None:
        orb_type = "wide" if orb_vs_atr > WIDE_ORB_ATR else "narrow"

    reg = regime.get(d) or {}
    row = {
        "target_date": d.isoformat(), "symbol": sym, "direction": direction,
        "pick_id": meta.get("pick_id"), "score": meta.get("score"),
        "conviction": meta.get("conviction"),
        "weights_version": meta.get("weights_version"),
        "orb_high": r2(orb_high, 2), "orb_low": r2(orb_low, 2),
        "orb_range_pct": r2(orb_range_pct, 3),
        "orb_range_vs_atr": r2(orb_vs_atr, 3), "orb_type": orb_type,
        "gap_pct": r2(gap_pct, 3),
        "day_regime": reg.get("regime"), "nifty_chg_pct": r2(reg.get("chg"), 3),
        "alert_fired": bool(alert), "status": "ok",
    }
    if alert:
        row["alert_stage"] = alert.get("stage")
        row["alert_time"] = alert["t"].isoformat() if alert.get("t") else None

    # plain % move of the underlying, close vs open
    last_close = day_bars[-1]["close"]
    row["move_pct"] = r2((last_close - day_open) / day_open * 100.0
                         * (1 if is_bull else -1), 3) if day_open else None

    level = orb_high if is_bull else orb_low
    bo_i, en_i = find_first_breakout(day_bars, level, is_bull, lock)
    if bo_i is None:
        row["broke"] = False
        return row, None
    if bo_i == "NEVER":
        row["broke"] = False
        row["status"] = "closed_outside_never_triggered"
        return row, None

    bo = day_bars[bo_i]
    entry = bo["high"] + TICK if is_bull else bo["low"] - TICK
    stop = bo["low"] if is_bull else bo["high"]
    risk = abs(entry - stop)
    if risk <= 0:
        row["broke"] = True
        row["status"] = "zero_risk"
        return row, None

    row.update({
        "broke": True,
        "break_time": bo["date"].isoformat(),
        "break_bucket": time_bucket(bo["date"]),
        "bo_high": r2(bo["high"], 2), "bo_low": r2(bo["low"], 2),
        "entry_price": r2(entry, 2),
        "entry_time": day_bars[en_i]["date"].isoformat(),
        "stop_price": r2(stop, 2), "risk_pts": r2(risk, 2),
        "risk_pct": r2(risk / entry * 100.0, 3),
    })

    res = walk(day_bars, en_i, entry, stop, is_bull, hard, risk)
    if res is None:
        row["status"] = "no_walk"
        return row, None
    r, px, t, why, mins, mfe, mae = res
    row.update({
        "r_multiple": r2(r, 4), "exit_price": r2(px, 2),
        "exit_time": t.isoformat(), "exit_reason": why,
        "exit_minutes": mins,
        "mfe_r": r2(mfe, 4), "mae_r": r2(mae, 4),
        "hit_1r": mfe >= 1.0, "hit_2r": mfe >= 2.0,
    })
    return row, None


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--with-old", action="store_true",
                    help="also grade symbols from the old watchlist_picks")
    ap.add_argument("--regrade", action="store_true",
                    help="redo days already present in nextday_outcomes")
    ap.add_argument("--limit", type=int, default=0,
                    help="only N symbols (test runs)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    today = datetime.now(IST).date()
    if args.date:
        d_from = d_to = date.fromisoformat(args.date)
    else:
        d_to = today
        d_from = today - timedelta(days=args.days)

    print("=" * 62)
    print(" TrueFlow Next Day WL v2 — grader   %s to %s" % (d_from, d_to))
    print("=" * 62)

    sb = connect_sb()
    targets = load_targets(sb, d_from, d_to, args.with_old, args.regrade)
    if not targets:
        print("Nothing to grade.")
        return
    print("  %d pick(s) to grade" % len(targets))

    alerts = load_alerts(sb, d_from, d_to)
    kite = connect_kite()

    symbols = sorted({k[1] for k in targets})
    if args.limit:
        symbols = symbols[:args.limit]
        targets = {k: v for k, v in targets.items() if k[1] in symbols}
        print("TEST MODE: %d symbols, %d picks" % (len(symbols), len(targets)))

    tokens, lookup = build_token_map(kite, symbols)
    regime = build_regime(kite, lookup, d_from, d_to)

    by_sym = {}
    for (td, sym, dr), meta in targets.items():
        by_sym.setdefault(sym, []).append((date.fromisoformat(td), dr, meta))

    rows, skipped, reasons = [], 0, {}
    for n, sym in enumerate(sorted(by_sym.keys()), 1):
        if sym not in tokens:
            skipped += len(by_sym[sym])
            reasons["no_token"] = reasons.get("no_token", 0) + len(by_sym[sym])
            continue
        try:
            bars5 = fetch_candles(kite, tokens[sym], d_from, d_to,
                                  "5minute", CHUNK_DAYS_5M)
            daily = fetch_candles(kite, tokens[sym],
                                  d_from - timedelta(days=90), d_to,
                                  "day", 1900)
        except Exception as e:
            skipped += len(by_sym[sym])
            reasons["fetch_error"] = reasons.get("fetch_error", 0) + 1
            print("  %-14s fetch error: %s" % (sym, str(e)[:50]))
            continue

        atr = atr_series(daily, ATR_PERIOD)
        dmap, ctx = {}, {}
        for i, c in enumerate(daily):
            dd = c["date"].date()
            dmap[dd] = c
            ctx[dd] = {"atr": atr[i],
                       "prev_close": daily[i - 1]["close"] if i > 0 else None}
        bars_by_day = group_by_day(bars5)

        for d, direction, meta in by_sym[sym]:
            c = dict(meta)
            c.update(ctx.get(d) or {})
            row, why = grade(sym, direction, d, bars_by_day.get(d, []),
                             dmap, c, regime,
                             alerts.get((d.isoformat(), sym, direction)))
            if row is None:
                skipped += 1
                reasons[why] = reasons.get(why, 0) + 1
            else:
                rows.append(row)

        if n % 20 == 0 or n == len(by_sym):
            print("  [%3d/%3d] graded=%d skipped=%d  %.0fs"
                  % (n, len(by_sym), len(rows), skipped, time.time() - t0))

    print("-" * 62)
    broke = sum(1 for r in rows if r.get("broke"))
    with_r = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    print("GRADED %d   broke %d (%.0f%%)   skipped %d"
          % (len(rows), broke,
             100.0 * broke / len(rows) if rows else 0, skipped))
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("   skip: %-36s %d" % (str(k)[:36], v))
    if with_r:
        wins = sum(1 for r in with_r if r > 0)
        print("TRADES %d   win %.1f%%   avg %.3f R   total %.1f R"
              % (len(with_r), 100.0 * wins / len(with_r),
                 sum(with_r) / len(with_r), sum(with_r)))
        buckets = {}
        for r in rows:
            b = r.get("break_bucket")
            if b and r.get("r_multiple") is not None:
                buckets.setdefault(b, []).append(r["r_multiple"])
        for b in sorted(buckets):
            v = buckets[b]
            print("   %-12s n=%3d  avg %.3f R" % (b, len(v), sum(v) / len(v)))

    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        return
    if not rows:
        print("\nNothing to write.")
        return

    written = 0
    for i in range(0, len(rows), BATCH_WRITE):
        chunk = rows[i:i + BATCH_WRITE]
        try:
            (sb.table("nextday_outcomes")
               .upsert(chunk, on_conflict="target_date,symbol,direction")
               .execute())
            written += len(chunk)
        except Exception as e:
            print("  write failed: %s" % str(e)[:120])
    print("\nWrote %d outcome(s) in %.0fs" % (written, time.time() - t0))


if __name__ == "__main__":
    main()
