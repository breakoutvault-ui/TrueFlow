#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════
 daily_levels.py  —  TrueFlow  (Next Day WL v2, script 1 of 4)
════════════════════════════════════════════════════════════════════════

WHAT THIS DOES
--------------
Fills `day_high` and `day_low` on `momentum_stocks` from Kite daily
candles.

WHY IT EXISTS
-------------
The strategy doc says to mark liquidity levels before the market opens —
previous day's high and low being the most important. Nothing in the
database stored them. Without them, "room to run" can only measure
distance to the 52-week high, which is useless for a stock in the middle
of its range.

Today's high/low becomes tomorrow's PDH/PDL. Because this writes a row
per day, it also quietly builds a real daily high/low history that swing
levels can be derived from later.

This is a separate script rather than a patch to momentum_scan.py on
purpose: momentum_scan.py is not in the GitHub repo, so it cannot be
anchor-patched safely. This touches only two columns and never writes a
row that momentum_scan did not already create.

USAGE
-----
  Nightly (run AFTER momentum_scan.py has written today's rows):
      /root/trueflow/bin/python daily_levels.py

  Backfill 60 days of history (run once, ~3 minutes):
      /root/trueflow/bin/python daily_levels.py --backfill

  Wider or narrower window:
      /root/trueflow/bin/python daily_levels.py --days 10
      /root/trueflow/bin/python daily_levels.py --backfill --days 120

  Whole momentum universe instead of just F&O (slower, ~1450 symbols):
      /root/trueflow/bin/python daily_levels.py --all

  Test on a few symbols first:
      /root/trueflow/bin/python daily_levels.py --limit-symbols 5

NOTES
-----
  * Default universe is the F&O list taken from the most recent
    fo_bhav_oi session. That is the only universe the Next Day WL
    scorer cares about, and it keeps this to ~213 Kite calls.
  * Safe to re-run. It only ever updates existing rows.
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
#  TUNABLES
# ══════════════════════════════════════════════════════════════════════

IST            = timezone(timedelta(hours=5, minutes=30))
KITE_SLEEP     = 0.35      # ~3 requests/sec, Kite's historical limit
DEFAULT_DAYS   = 3         # incremental lookback
BACKFILL_DAYS  = 60        # matches the 30-day Journal backfill + padding
CHUNK_DAYS_DAY = 1900      # daily candles: Kite allows a long window

INDEX_SYMBOLS = {
    "BANKNIFTY": "NIFTY BANK",
    "NIFTY":     "NIFTY 50",
    "FINNIFTY":  "NIFTY FIN SERVICE",
}


# ══════════════════════════════════════════════════════════════════════
#  CONNECTIONS  (same pattern as orb_simulate.py)
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


# ══════════════════════════════════════════════════════════════════════
#  UNIVERSE
# ══════════════════════════════════════════════════════════════════════

def page_select(query_builder, size=1000):
    """Supabase silently caps reads at 1000 rows. Always page, and always
    page on a unique key — paging on a non-unique key duplicated rows and
    cost a 20-minute run once already."""
    rows, page = [], 0
    while True:
        chunk = query_builder(page * size, page * size + size - 1) or []
        rows.extend(chunk)
        if len(chunk) < size:
            break
        page += 1
    return rows


def fo_universe(sb):
    """The F&O list, taken from the latest fo_bhav_oi session."""
    r = (sb.table("fo_bhav_oi").select("session_date")
           .order("session_date", desc=True).limit(1).execute().data or [])
    if not r:
        print("  fo_bhav_oi is empty — falling back to full universe.")
        return None
    latest = r[0]["session_date"]
    rows = page_select(lambda a, b: (
        sb.table("fo_bhav_oi").select("id,symbol")
          .eq("session_date", latest).order("id").range(a, b)
          .execute().data))
    syms = sorted({x["symbol"] for x in rows if x.get("symbol")})
    print("  F&O universe from %s: %d symbols" % (latest, len(syms)))
    return syms


def rows_needing_levels(sb, d_from, d_to, symbols):
    """momentum_stocks rows in range that still have no day_high."""
    rows = page_select(lambda a, b: (
        sb.table("momentum_stocks")
          .select("id,symbol,session_date,day_high,day_low")
          .gte("session_date", d_from.isoformat())
          .lte("session_date", d_to.isoformat())
          .order("id").range(a, b).execute().data))
    seen, uniq = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    if symbols is not None:
        want = set(symbols)
        uniq = [r for r in uniq if r["symbol"] in want]
    return uniq


# ══════════════════════════════════════════════════════════════════════
#  KITE
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
              % (len(missing), ", ".join(sorted(missing)[:15])))
    print("  resolved %d/%d symbols" % (len(tok), len(wanted)))
    return tok


def fetch_daily(kite, token, d_from, d_to):
    out, cur = [], d_from
    while cur <= d_to:
        end = min(cur + timedelta(days=CHUNK_DAYS_DAY), d_to)
        for attempt in range(3):
            try:
                part = kite.historical_data(token, cur, end, "day")
                out.extend(part or [])
                break
            except Exception as e:
                if attempt == 2:
                    print("    fetch failed (%s..%s): %s" % (cur, end, e))
                else:
                    time.sleep(1.5)
            finally:
                time.sleep(KITE_SLEEP)
        cur = end + timedelta(days=1)
    by_day = {}
    for c in out:
        d = as_ist(c["date"])
        if d:
            by_day[d.date()] = c
    return by_day


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="go back %d days instead of %d"
                         % (BACKFILL_DAYS, DEFAULT_DAYS))
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--all", action="store_true",
                    help="whole momentum universe, not just F&O")
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument("--force", action="store_true",
                    help="rewrite rows that already have day_high")
    args = ap.parse_args()

    today = datetime.now(IST).date()
    days = args.days if args.days else (BACKFILL_DAYS if args.backfill
                                        else DEFAULT_DAYS)
    d_from, d_to = today - timedelta(days=days), today

    print("=" * 62)
    print(" TrueFlow daily_levels — %s to %s" % (d_from, d_to))
    print("=" * 62)

    kite = connect_kite()
    sb   = connect_sb()

    symbols = None if args.all else fo_universe(sb)

    rows = rows_needing_levels(sb, d_from, d_to, symbols)
    if not args.force:
        rows = [r for r in rows if r.get("day_high") is None]
    if not rows:
        print("Nothing to fill. All rows in range already have levels.")
        return

    by_sym = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)

    syms = sorted(by_sym.keys())
    if args.limit_symbols:
        syms = syms[:args.limit_symbols]
        print("TEST MODE: only %d symbols" % len(syms))

    print("%d row(s) to fill across %d symbol(s)"
          % (sum(len(by_sym[s]) for s in syms), len(syms)))

    tokens = build_token_map(kite, syms)

    filled, missed, no_token = 0, 0, 0
    t0 = time.time()

    for n, sym in enumerate(syms, 1):
        if sym not in tokens:
            no_token += len(by_sym[sym])
            continue
        try:
            # pad a few days so a stale session_date still resolves
            candles = fetch_daily(kite, tokens[sym],
                                  d_from - timedelta(days=7), d_to)
        except Exception as e:
            print("  %-14s fetch error: %s" % (sym, e))
            missed += len(by_sym[sym])
            continue

        for r in by_sym[sym]:
            sd = date.fromisoformat(r["session_date"])
            c = candles.get(sd)
            if not c:
                missed += 1
                continue
            try:
                (sb.table("momentum_stocks")
                   .update({"day_high": float(c["high"]),
                            "day_low":  float(c["low"])})
                   .eq("id", r["id"]).execute())
                filled += 1
            except Exception as e:
                print("  %-14s %s write error: %s" % (sym, sd, e))
                missed += 1

        if n % 25 == 0 or n == len(syms):
            print("  [%3d/%3d] filled=%d missed=%d  %.0fs"
                  % (n, len(syms), filled, missed, time.time() - t0))

    print("-" * 62)
    print("DONE  filled=%d  missed=%d  no_token=%d  in %.0fs"
          % (filled, missed, no_token, time.time() - t0))
    if missed:
        print("Note: 'missed' is normal for holidays, suspensions and")
        print("BSE-only names that have no NSE daily candle.")


if __name__ == "__main__":
    main()
