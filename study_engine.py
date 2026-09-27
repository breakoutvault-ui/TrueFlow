#!/usr/bin/env python3
# ==============================================================
#  TrueFlow - Measurement Engine
#
#  Answers two questions from momentum_stocks history, nothing else:
#
#   1. TAILWINDS - do setups in leading sectors / industries do better
#      than setups in lagging ones?
#   2. PLAYBOOKS - which playbooks actually produce forward returns?
#
#  It writes NOTHING to the database. Read-only, safe to re-run.
#
#  Run:  cd /root/trueflow && ./bin/python study_engine.py
#        ./bin/python study_engine.py --sessions 250     (deeper, slower)
#
#  HONEST LIMITS, read before trusting any number below:
#   * momentum_mtf is overwritten nightly, so weekly/monthly state has NO
#     history. Playbooks whose rules depend on it are skipped, not faked.
#   * Smart-money and alignment fields are not in this table either.
#   * 'ltp' is taken as the session close. It is the price the nightly scan
#     recorded, which is the close on a normal day.
#   * Forward returns are close-to-close with no stop, no slippage and no
#     costs. They measure whether the SETUP leads anywhere, not what you
#     would have earned trading it.
# ==============================================================

import sys, json, argparse
from collections import defaultdict
from statistics import median

sys.path.insert(0, "/root/trueflow")
import requests
import tf_config as CFG

SB_URL = getattr(CFG, "SUPABASE_URL")
SB_KEY = getattr(CFG, "SUPABASE_KEY")
H = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY}

COLS = ("symbol,session_date,ltp,sector,industry,qm_pattern,category,adr_pct,"
        "vol_ratio,high_52w,qm_contraction,qm_base_days,qm_vol_dryup,is_nr7,"
        "nr_status,rcl_grade,breakout_type,momentum_score,day_high,day_low,"
        "qm_pivot_level,above_ema9_daily")

HORIZONS = (5, 10, 20)
MIN_GROUP = 5          # a group needs this many members to be ranked
PAGE = 1000


def sessions_list(n):
    """
    Walk BACK one distinct date at a time. The obvious version - ask for
    n*5 rows and dedupe - fails badly: one session is ~1,450 rows, so a
    750-row page contains exactly one date.
    """
    dates, cur = [], None
    while len(dates) < n:
        q = "select=session_date&order=session_date.desc&limit=1"
        if cur:
            q += "&session_date=lt.%s" % cur
        r = requests.get("%s/rest/v1/momentum_stocks?%s" % (SB_URL, q),
                         headers=H, timeout=60)
        r.raise_for_status()
        j = r.json()
        if not j:
            break
        cur = j[0]["session_date"]
        dates.append(cur)
    return dates


def fetch(sessions):
    """One session at a time - keeps every OFFSET small, which matters on a
    267 MB table where a deep offset makes Postgres crawl."""
    dates = sessions_list(sessions)
    if not dates:
        print("No rows found in momentum_stocks."); sys.exit(1)
    need = 63 + max(HORIZONS)
    if len(dates) < need:
        print("\n!! Only %d sessions available. The group ranking needs 63 sessions"
              "\n   of history and the longest forward window is %d, so most rows"
              "\n   will have nothing to measure. Want at least %d for a real answer.\n"
              % (len(dates), max(HORIZONS), need))
    print("Pulling %d sessions, %s to %s ..." % (len(dates), dates[-1], dates[0]))

    rows = []
    for i, d in enumerate(dates):
        off = 0
        while True:
            u = ("%s/rest/v1/momentum_stocks?select=%s&session_date=eq.%s"
                 "&order=symbol.asc&limit=%d&offset=%d" % (SB_URL, COLS, d, PAGE, off))
            rr = requests.get(u, headers=H, timeout=120)
            rr.raise_for_status()
            b = rr.json()
            rows.extend(b)
            if len(b) < PAGE:
                break
            off += PAGE
        if (i + 1) % 25 == 0:
            print("  %d/%d sessions, %d rows..." % (i + 1, len(dates), len(rows)), flush=True)
    print("  %d rows total\n" % len(rows))
    return rows, dates


def f(x):
    try:
        return None if x is None or x == "" else float(x)
    except Exception:
        return None


COST_PCT = 0.35        # round-trip: STT both sides, stamp, exchange, GST, DP


def fetch_ohlc(symbols):   # returns (px, idx, breadth)
    """
    The panel now comes from daily_ohlc - REAL highs and lows from Kite.
    momentum_stocks.day_high / day_low are empty on all 264,077 rows, which is
    why every earlier stop result was close-to-close and the wick count was 0.

    The 9 EMA is computed here from these closes rather than read from
    above_ema9_daily, because that column only exists for the ~260 scanned
    sessions while this table holds three years. Exits can now run past the
    last scan date instead of being cut short by it.

    Shape is unchanged - (date, close, high, above9, low) - so nothing
    downstream needs to know where the numbers came from.
    """
    px, idx = {}, {}
    breadth = defaultdict(lambda: [0, 0])     # date -> [above 50 EMA, counted]
    done = 0
    for sym in symbols:
        rows, cur = [], None
        while True:
            q = ("select=d,o,h,l,c&symbol=eq.%s&order=d.asc&limit=1000" % sym)
            if cur:
                q += "&d=gt.%s" % cur
            r = requests.get("%s/rest/v1/daily_ohlc?%s" % (SB_URL, q),
                             headers=H, timeout=60)
            r.raise_for_status()
            j = r.json()
            if not j:
                break
            rows.extend(j)
            cur = j[-1]["d"]
            if len(j) < 1000:
                break
        if len(rows) < 30:
            continue

        k9, k20, k50 = 2.0 / (9 + 1), 2.0 / (20 + 1), 2.0 / (50 + 1)
        e9 = e20 = e50 = None
        panel = []
        for n, b in enumerate(rows):
            c = f(b.get("c"))
            hi = f(b.get("h")) or c
            lo = f(b.get("l")) or c
            if not c or c <= 0:
                continue
            e9 = c if e9 is None else (c - e9) * k9 + e9
            e20 = c if e20 is None else (c - e20) * k20 + e20
            e50 = c if e50 is None else (c - e50) * k50 + e50
            panel.append((b["d"], c, hi, c > e9, lo, f(b.get("o")) or c, c > e20))
            if n >= 50:                        # only once the 50 EMA is settled
                breadth[b["d"]][1] += 1
                if c > e50:
                    breadth[b["d"]][0] += 1
        if len(panel) < 30:
            continue
        px[sym] = panel
        idx[sym] = {b[0]: i for i, b in enumerate(panel)}
        done += 1
        if done % 200 == 0:
            print("  %d/%d symbols loaded..." % (done, len(symbols)), flush=True)
    return px, idx, dict(breadth)


def f(x):
    try:
        return None if x is None or x == "" else float(x)
    except Exception:
        return None


def build(rows):
    """px[symbol] = [(date, close, high)] in date order, plus a date index."""
    px = defaultdict(list)
    for r in rows:
        c = f(r.get("ltp"))
        if c and c > 0:
            px[r["symbol"]].append((r["session_date"], c, f(r.get("day_high")) or c,
                                    r.get("above_ema9_daily"), f(r.get("day_low")) or c))
    for s in px:
        px[s].sort()
    idx = {s: {b[0]: i for i, b in enumerate(v)} for s, v in px.items()}
    return px, idx, dict(breadth)


def fwd(px, idx, sym, date, n):
    """Return over the next n sessions, in %, or None if not enough history."""
    v = px.get(sym)
    if not v:
        return None
    i = idx[sym].get(date)
    if i is None or i + n >= len(v):
        return None
    a, b = v[i][1], v[i + n][1]
    return (b / a - 1) * 100 if a else None


def fwd_tr(px, idx, sym, date, n):
    """Forward return you could actually have captured: in at the next open,
    out at the close n sessions later."""
    v = px.get(sym)
    if not v:
        return None
    i = idx[sym].get(date)
    if i is None:
        return None
    entry, ei = entry_after(px, sym, i)
    if entry is None or ei + n >= len(v):
        return None
    return (v[ei + n][1] / entry - 1) * 100 if entry else None


def back(px, idx, sym, date, n):
    """Return over the PREVIOUS n sessions - used to rank groups."""
    v = px.get(sym)
    if not v:
        return None
    i = idx[sym].get(date)
    if i is None or i - n < 0:
        return None
    a, b = v[i - n][1], v[i][1]
    return (b / a - 1) * 100 if a else None


def trigger_fwd(px, idx, sym, date, pivot, wait, h):
    """
    The honest test for a PRE-BREAKOUT setup. A coil playbook does not say
    "buy today" - it says "wait for the break". So: from the day after the
    signal, look up to `wait` sessions for the first session whose HIGH clears
    the pivot. Enter AT the pivot, then measure h sessions on from there.

    Returns (return_pct, days_to_trigger) or (None, None) if it never broke out
    inside the window - which is itself a result worth counting.
    """
    v = px.get(sym)
    if not v or not pivot or pivot <= 0:
        return None, None
    i = idx[sym].get(date)
    if i is None:
        return None, None
    for j in range(i + 1, min(i + 1 + wait, len(v))):
        if v[j][2] >= pivot:                       # the high cleared the pivot
            if j + h >= len(v):
                return None, None                  # not enough history after it
            return (v[j + h][1] / pivot - 1) * 100, j - i
    return None, None


def exit_9ema(px, idx, sym, start_i, entry_px, need=2, cap=120):
    """
    Mahesh's rule: hold until the stock closes below the daily 9 EMA on `need`
    consecutive sessions, then exit at that close. No fixed horizon, so a slide
    that is reclaimed does not get marked at the bottom of the hole - which is
    exactly what a 10-day mark does to anything signalled before 15 Sep 2026.

    above_ema9_daily is stored per session, so this is the real rule, not a
    reconstruction of it. Returns (return_pct, days_held) or (None, None) if the
    position was still open when the data ran out.
    """
    v = px[sym]
    run = 0
    for j in range(start_i, min(start_i + cap, len(v))):
        ab = v[j][3]
        if ab is False:
            run += 1
            if run >= need:
                return (v[j][1] / entry_px - 1) * 100, j - start_i + 1
        elif ab is True:
            run = 0
        # None (not recorded that day) neither confirms nor breaks the run
    return None, None


def entry_then_exit(px, idx, sym, date, pivot, wait, need):
    """Wait for the pivot break, enter there, then exit on the 9 EMA rule."""
    v = px.get(sym)
    if not v or not pivot or pivot <= 0:
        return None, None
    i = idx[sym].get(date)
    if i is None:
        return None, None
    for j in range(i + 1, min(i + 1 + wait, len(v))):
        if v[j][2] >= pivot:
            # if it gapped straight past the pivot you fill at the open, not
            # at your level - taking the pivot there would be fiction
            fill = max(pivot, v[j][5] or pivot)
            return exit_9ema(px, idx, sym, j, fill, need)
    return None, None


def entry_after(px, sym, i):
    """
    The scan runs at night, so the signal-day close is a price that had already
    happened before the signal existed. You could not have bought it. The first
    fill you could actually get is the NEXT session's open.

    Returns (entry_price, entry_index) or (None, None) at the end of the data.
    """
    v = px.get(sym)
    if not v or i + 1 >= len(v):
        return None, None
    b = v[i + 1]
    return (b[5] or b[1]), i + 1


def market_regime(breadth, universe=1000, thresh=50.0):
    """
    Was the market rising or falling when the signal fired?

    BREADTH: the share of the universe trading above its own 50 EMA. Above
    half the market is an up market.

    The obvious alternative - an index - failed here: NIFTY 50 is not in
    daily_ohlc, and the first fallback compounded the MEDIAN daily move of
    every stock, which drifts down forever because the median stock
    underperforms. That produced 660 DOWN sessions against 82 UP and made the
    whole split meaningless. Breadth cannot fail that way: it is a ratio
    recomputed each day, with nothing to accumulate.
    """
    # a day needs a decent slice of the universe before its breadth means
    # anything - scaled, so this does not silently blank out on a small run
    floor = max(20, universe // 5)
    out, series = {}, []
    for d in sorted(breadth):
        up, tot = breadth[d]
        if tot < floor:
            continue
        pc = 100.0 * up / tot
        out[d] = "UP" if pc >= thresh else "DOWN"
        series.append((d, pc))
    return out, series


def run_trade(px, sym, i, entry, stop, basis, need=2, cap=120,
              ema_ix=3, trail_adr=None, adr=None):
    """
    One trade, start to finish.

    basis='touch'  - the stop fires the moment the day's LOW reaches it.
    basis='close'  - the stop only fires if the day CLOSES at or below it.

    That single switch is the whole question: a touch stop takes you out on the
    wick, a close stop rides through the wick but wears the whole down-day when
    the break is real.

    Exits either way on `need` consecutive closes below the daily 9 EMA.
    Returns (R, days, reason, wicks) - wicks counts days the low pierced the
    stop but the close recovered above it, i.e. times a touch stop would have
    thrown you out and a close stop would not.
    """
    v = px[sym]
    risk = entry - stop
    if risk <= 0:
        return None
    run = 0
    wicks = 0
    peak = entry
    for j in range(i, min(i + cap, len(v))):
        c, hi, ab, lo = v[j][1], v[j][2], v[j][3], v[j][4]
        if len(v[j]) > ema_ix:
            ab = v[j][ema_ix]
        if trail_adr and adr:
            # a trailing stop only ever ratchets up, never down
            # R is ALWAYS measured against the risk taken at entry. Letting the
            # denominator shrink as the trail rises turns a normal trade into a
            # 27R fantasy - which is exactly what it did before this line went.
            stop = max(stop, peak * (1 - trail_adr * adr / 100.0))
        if lo <= stop:
            if basis == "touch":
                # filled at the stop; a gap through it would fill worse, and
                # there is no open price stored, so reality is a bit worse
                return (stop - entry) / risk, j - i + 1, "stop", wicks
            if c <= stop:
                return (c - entry) / risk, j - i + 1, "stop", wicks
            wicks += 1                      # pierced and recovered same day
        if ab is False:
            run += 1
            if run >= need:
                return (c - entry) / risk, j - i + 1, "ema", wicks
        elif ab is True:
            run = 0
        if c > peak:
            peak = c
    return None                              # still open when the data ran out


def excursions(px, sym, i, entry, hold):
    """How far it went against you, and for you, in % - before any exit rule."""
    v = px[sym]
    lo = hi = None
    for j in range(i, min(i + hold, len(v))):
        a = (v[j][4] / entry - 1) * 100
        b = (v[j][2] / entry - 1) * 100
        lo = a if lo is None else min(lo, a)
        hi = b if hi is None else max(hi, b)
    return lo, hi


def pct_at(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * p / 100.0))]


def group_ranks(rows_by_date, px, idx, key, lookback=63):
    """
    For each session, rank every group by the MEDIAN trailing return of its
    members. Median, not mean, so one exploding stock cannot carry a group.
    Returns rank[date][group] = (rank, total, median_return).
    """
    out = {}
    for date, rows in rows_by_date.items():
        agg = defaultdict(list)
        for r in rows:
            g = (r.get(key) or "").strip()
            if not g or g == "-":
                continue
            v = back(px, idx, r["symbol"], date, lookback)
            if v is not None:
                agg[g].append(v)
        scored = [(median(v), g) for g, v in agg.items() if len(v) >= MIN_GROUP]
        if len(scored) < 4:
            continue
        scored.sort(reverse=True)
        out[date] = {g: (i + 1, len(scored), m) for i, (m, g) in enumerate(scored)}
    return out


def stats(vals):
    if not vals:
        return None
    n = len(vals)
    return {"n": n, "med": median(vals), "avg": sum(vals) / n,
            "win": 100.0 * sum(1 for v in vals if v > 0) / n}


def show(title, buckets):
    print("\n" + title)
    print("  %-22s %6s %8s %8s %8s" % ("", "n", "median", "mean", "win%"))
    for name, vals in buckets:
        s = stats(vals)
        if not s:
            print("  %-22s %6s" % (name, "-")); continue
        print("  %-22s %6d %7.2f%% %7.2f%% %7.1f%%"
              % (name, s["n"], s["med"], s["avg"], s["win"]))


# ---------- playbooks reproducible from THIS table only ----------
def is_setup(r):
    return (r.get("qm_pattern") or "") in ("VCP", "HTF", "EP", "Reclaim")


PLAYS = {
 "Coiled & Drying (VCP, Cat A)":
    lambda r: r.get("qm_pattern") == "VCP" and r.get("category") in ("A", "AC"),
 "Tight Contraction (>=0.7)":
    lambda r: r.get("qm_pattern") in ("VCP", "HTF") and (f(r.get("qm_contraction")) or 0) >= 0.7
              and r.get("category") in ("A", "AC"),
 "VCP at Pivot (within 15% of 52wH)":
    lambda r: r.get("qm_pattern") == "VCP" and r.get("category") in ("A", "AC")
              and near_high(r, 15),
 "EP":
    lambda r: r.get("qm_pattern") == "EP",
 "EP + 2x volume":
    lambda r: r.get("qm_pattern") == "EP" and (f(r.get("vol_ratio")) or 0) >= 2,
 "Reclaim":
    lambda r: r.get("qm_pattern") == "Reclaim",
 "Reclaim grade A/B":
    lambda r: r.get("qm_pattern") == "Reclaim" and (r.get("rcl_grade") or "") in ("A", "B"),
 "NR7":
    lambda r: bool(r.get("is_nr7")),
 "NR7 + VCP":
    lambda r: bool(r.get("is_nr7")) and r.get("qm_pattern") == "VCP",
 "NR broken up":
    lambda r: (r.get("nr_status") or "") == "brokenUp",
 "Breakout + 1.5x volume":
    lambda r: bool(r.get("breakout_type")) and (f(r.get("vol_ratio")) or 0) >= 1.5,
 "High-ADR breakout (ADR>=5)":
    lambda r: bool(r.get("breakout_type")) and (f(r.get("vol_ratio")) or 0) >= 1.5
              and (f(r.get("adr_pct")) or 0) >= 5,
 "Weekend Coils (VCP/HTF, ADR3+, base5+)":
    lambda r: r.get("qm_pattern") in ("VCP", "HTF") and r.get("category") in ("A", "AC")
              and (f(r.get("adr_pct")) or 0) >= 3 and (r.get("qm_base_days") or 0) >= 5,
}

SKIPPED = ["every Alignment playbook (market/sector state is not stored per session)",
           "every Smart Money playbook (bulk/block data is not in this table)",
           "the higher-timeframe half of every reframed rule (momentum_mtf keeps no history)"]


def near_high(r, pct):
    h, c = f(r.get("high_52w")), f(r.get("ltp"))
    return bool(h and c and h > 0 and (h - c) / h * 100 <= pct)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=150)
    ap.add_argument("--wait", type=int, default=10,
                    help="sessions to wait for a pre-breakout setup to trigger")
    ap.add_argument("--horizon", type=int, default=10,
                    help="which horizon the tailwind tables use")
    a = ap.parse_args()

    rows, dates = fetch(a.sessions)
    syms = sorted({r["symbol"] for r in rows})
    print("Loading real daily OHLC for %d symbols from daily_ohlc..." % len(syms))
    px, idx, breadth = fetch_ohlc(syms)
    print("  %d symbols have usable price history\n" % len(px))
    if not px:
        print("daily_ohlc is empty - run ohlc_backfill.py first."); sys.exit(1)

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["session_date"]].append(r)

    # ---------------- 0. WHAT IS ACTUALLY IN THE COLUMNS ----------------
    # Three playbooks returned n=0 on the first run because the literal values
    # were guessed from the dashboard code. Print the truth instead.
    print("COLUMN VALUES actually present (so no filter is written on a guess):")
    for col in ("qm_pattern", "nr_status", "rcl_grade", "breakout_type",
                "category", "qm_vol_dryup", "vol_class"):
        cnt = defaultdict(int)
        for r in rows:
            v = r.get(col)
            cnt["(null)" if v is None or v == "" else str(v)] += 1
        if len(cnt) <= 1 and "(null)" in cnt:
            print("  %-14s ALL NULL" % col); continue
        top = sorted(cnt.items(), key=lambda kv: -kv[1])[:8]
        print("  %-14s %s" % (col, "  ".join("%s=%d" % (k, v) for k, v in top)))
    print()

    # ---------------- 0b. WHAT EACH VALUE IS ACTUALLY WORTH ----------------
    # Instead of me guessing that a reclaim is stored as "Reclaim", measure
    # EVERY value that appears. No filter can be written on a wrong literal.
    print("FORWARD RETURN BY COLUMN VALUE - %d sessions ahead" % a.horizon)
    for col in ("qm_pattern", "nr_status", "rcl_grade", "breakout_type",
                "category", "vol_class"):
        buck = defaultdict(list)
        for date, rws in by_date.items():
            for r in rws:
                v = r.get(col)
                if v is None or v == "":
                    continue
                fv = fwd_tr(px, idx, r["symbol"], date, a.horizon)
                if fv is not None:
                    buck[str(v)].append(fv)
        rowsout = [(k, v) for k, v in buck.items() if len(v) >= 50]
        if not rowsout:
            print("\n  %s: nothing with 50+ samples" % col); continue
        rowsout.sort(key=lambda kv: -median(kv[1]))
        show("  " + col, rowsout)
    print()

    print("Ranking groups by 3-month median member return (min %d members)..."
          % MIN_GROUP)
    sec_rank = group_ranks(by_date, px, idx, "sector")
    ind_rank = group_ranks(by_date, px, idx, "industry")
    if sec_rank:
        any_d = list(sec_rank)[len(sec_rank) // 2]
        print("  sectors ranked on a typical day:   %d" % len(sec_rank[any_d]))
    if ind_rank:
        any_d = list(ind_rank)[len(ind_rank) // 2]
        print("  industries ranked on a typical day: %d" % len(ind_rank[any_d]))

    # ---------------- 1. TAILWINDS ----------------
    for label, ranks, key in (("SECTOR", sec_rank, "sector"),
                              ("INDUSTRY", ind_rank, "industry")):
        for h in HORIZONS:
            q = defaultdict(list)
            for date, rws in by_date.items():
                rk = ranks.get(date)
                if not rk:
                    continue
                for r in rws:
                    if not is_setup(r):
                        continue
                    g = (r.get(key) or "").strip()
                    if g not in rk:
                        continue
                    pos, tot, _ = rk[g]
                    v = fwd_tr(px, idx, r["symbol"], date, h)
                    if v is None:
                        continue
                    frac = (pos - 1) / max(1, tot - 1)
                    q["Q1 (leading)" if frac <= .25 else
                      "Q2" if frac <= .50 else
                      "Q3" if frac <= .75 else "Q4 (lagging)"].append(v)
            show("TAILWIND by %s - forward %d sessions, QM setups only"
                 % (label, h),
                 [(k, q[k]) for k in ("Q1 (leading)", "Q2", "Q3", "Q4 (lagging)")])

    # ---------------- 2. PLAYBOOKS ----------------
    base = defaultdict(list)
    for date, rws in by_date.items():
        for r in rws:
            v = fwd_tr(px, idx, r["symbol"], date, a.horizon)
            if v is not None:
                base["ALL STOCKS (baseline)"].append(v)
    res = [("ALL STOCKS (baseline)", base["ALL STOCKS (baseline)"])]
    for name, test in PLAYS.items():
        vals = []
        for date, rws in by_date.items():
            for r in rws:
                try:
                    if not test(r):
                        continue
                except Exception:
                    continue
                v = fwd_tr(px, idx, r["symbol"], date, a.horizon)
                if v is not None:
                    vals.append(v)
        res.append((name, vals))
    res = [res[0]] + sorted(res[1:], key=lambda x: -(stats(x[1])["med"] if x[1] else -99))
    show("PLAYBOOK AUDIT - forward %d sessions, ranked by median" % a.horizon, res)

    # ---------------- 3. PRE-BREAKOUT SETUPS, MEASURED PROPERLY ----------
    # Coil playbooks say "wait for the break", so holding from the signal date
    # marks them down for something they never told you to do. This enters at
    # the pivot instead, and counts how often the break even happens.
    print("\n\nPRE-BREAKOUT SETUPS - entered AT the pivot, not on the signal day")
    print("  (waits up to %d sessions for the break, then holds %d)"
          % (a.wait, a.horizon))
    print("  %-34s %7s %7s %8s %8s %7s"
          % ("", "signals", "broke%", "median", "mean", "win%"))
    for name in ("Coiled & Drying (VCP, Cat A)", "Tight Contraction (>=0.7)",
                 "VCP at Pivot (within 15% of 52wH)", "NR7 + VCP",
                 "Weekend Coils (VCP/HTF, ADR3+, base5+)"):
        test = PLAYS[name]
        sig = 0; hit = []
        for date, rws in by_date.items():
            for r in rws:
                try:
                    if not test(r):
                        continue
                except Exception:
                    continue
                pv = f(r.get("qm_pivot_level"))
                if not pv:
                    continue
                sig += 1
                v, _d = trigger_fwd(px, idx, r["symbol"], date, pv, a.wait, a.horizon)
                if v is not None:
                    hit.append(v)
        st = stats(hit)
        if not st or sig == 0:
            print("  %-34s %7d %7s" % (name, sig, "-")); continue
        print("  %-34s %7d %6.1f%% %7.2f%% %7.2f%% %6.1f%%"
              % (name, sig, 100.0 * len(hit) / sig, st["med"], st["avg"], st["win"]))
    print("  Signals with no stored pivot are skipped, so 'signals' can be")
    print("  lower than the count in the table above.")

    # ---------------- 4. HELD TO YOUR ACTUAL EXIT RULE -------------------
    print("\n\nHELD TO THE 9 EMA RULE - no fixed horizon, no mark-to-market")
    print("  Pre-breakout setups enter at the pivot; the rest enter on the signal close.")
    for need in (1, 2):
        print("\n  Exit after %d consecutive close%s below the daily 9 EMA:"
              % (need, "" if need == 1 else "s"))
        print("    %-34s %7s %8s %8s %7s %7s"
              % ("", "trades", "median", "mean", "win%", "days"))
        rows_out = []
        for name, test in PLAYS.items():
            pre = "VCP" in name or "Contraction" in name or "Coil" in name or "NR7" in name
            rets, days = [], []
            for date, rws in by_date.items():
                for r in rws:
                    try:
                        if not test(r):
                            continue
                    except Exception:
                        continue
                    sym = r["symbol"]
                    i = idx.get(sym, {}).get(date)
                    if i is None:
                        continue
                    if pre:
                        pv = f(r.get("qm_pivot_level"))
                        if not pv:
                            continue
                        ret, d = entry_then_exit(px, idx, sym, date, pv, a.wait, need)
                    else:
                        en, ei = entry_after(px, sym, i)
                        if en is None:
                            continue
                        ret, d = exit_9ema(px, idx, sym, ei, en, need)
                    if ret is not None:
                        rets.append(ret); days.append(d)
            st = stats(rets)
            if st:
                rows_out.append((st["med"], name, st, median(days)))
        rows_out.sort(reverse=True)
        for _m, name, st, d in rows_out:
            print("    %-34s %7d %7.2f%% %7.2f%% %6.1f%% %6.0f"
                  % (name, st["n"], st["med"], st["avg"], st["win"], d))

    # ---------------- 5. STOPS: TOUCH vs CLOSE ---------------------------
    print("\n\n" + "=" * 74)
    print("STOP STUDY - how far trades go against you, and touch vs close stops")
    print("=" * 74)

    # the population: every QM setup with an ADR, entered on the signal close
    sigs = []
    for date, rws in by_date.items():
        for r in rws:
            if not is_setup(r):
                continue
            adr = f(r.get("adr_pct"))
            sym = r["symbol"]
            i = idx.get(sym, {}).get(date)
            if not adr or adr <= 0 or i is None:
                continue
            entry, ei = entry_after(px, sym, i)     # next session's open
            if entry is None:
                continue
            sigs.append((sym, ei, entry, adr, r.get("qm_pattern"), date, r))
    print("\n%d setups with an ADR and a tradable entry.\n" % len(sigs))

    # ---- 5a. where the stop BELONGS, from the trades themselves ----
    print("MAXIMUM ADVERSE EXCURSION - how deep it dug before it worked")
    print("  (measured over %d sessions, in ADR units, no stop applied)\n" % a.horizon)
    win_mae, lose_mae, win_mfe = [], [], []
    for sym, i, entry, adr, _p, _dt, _r in sigs:
        lo, hi = excursions(px, sym, i, entry, a.horizon)
        if lo is None:
            continue
        out = fwd_tr(px, idx, sym, px[sym][i][0], a.horizon)
        if out is None:
            continue
        (win_mae if out > 0 else lose_mae).append(-lo / adr)
        if out > 0:
            win_mfe.append(hi / adr)
    print("    %-26s %8s %8s %8s %8s %8s" % ("", "50th", "70th", "80th", "90th", "95th"))
    for lbl, vals in (("WINNERS went against you", win_mae),
                      ("LOSERS went against you", lose_mae),
                      ("WINNERS ran in your favour", win_mfe)):
        if not vals:
            continue
        print("    %-26s %7.2f  %7.2f  %7.2f  %7.2f  %7.2f"
              % (lbl, pct_at(vals, 50), pct_at(vals, 70), pct_at(vals, 80),
                 pct_at(vals, 90), pct_at(vals, 95)))
    print("\n    Read it like this: a stop placed just past the 80-90th percentile")
    print("    of the WINNERS row keeps nearly every trade that was going to work.")
    print("    Anything wider than that is room you are paying for and not using.")

    # ---- 5b. the actual question: touch or close ----
    print("\n\nTOUCH vs CLOSE - same stops, same trades, only the trigger differs")
    print("  Stop = N x ADR below entry. Exit otherwise on 2 closes under the 9 EMA.")
    print("  Expectancy is in R, so it already accounts for the wider stop costing more.\n")
    print("  Costs of %.2f%% round trip are deducted from every trade." % COST_PCT)
    print("  medianR is shown next to expectancy: a big mean with a poor median")
    print("  means a handful of outliers are carrying the whole result.\n")
    print("  %-6s %-7s %7s %8s %9s %8s %8s %8s %7s %6s"
          % ("stop", "basis", "trades", "stopped%", "expect", "medianR",
             "win%", "avgWin", "avgLoss", "days"))
    for mult in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
        for basis in ("touch", "close"):
            Rs, days, stopped, wicks = [], [], 0, 0
            for sym, i, entry, adr, _p, _dt, _r in sigs:
                stop = entry * (1 - mult * adr / 100.0)
                res = run_trade(px, sym, i, entry, stop, basis)
                if not res:
                    continue
                R, d, why, wk = res
                R -= (entry * COST_PCT / 100.0) / (entry - stop)   # costs, in R
                Rs.append(R); days.append(d); wicks += wk
                if why == "stop":
                    stopped += 1
            if not Rs:
                continue
            wins = [r for r in Rs if r > 0]
            loss = [r for r in Rs if r <= 0]
            print("  %-6s %-7s %7d %7.1f%% %8.3fR %7.2fR %7.1f%% %7.2fR %7.2fR %5.0f"
                  % ("%.2fx" % mult, basis, len(Rs), 100.0 * stopped / len(Rs),
                     sum(Rs) / len(Rs), median(Rs), 100.0 * len(wins) / len(Rs),
                     (sum(wins) / len(wins)) if wins else 0,
                     (sum(loss) / len(loss)) if loss else 0, median(days)))
        if mult == 0.5:
            print()

    # ---- 5c. how often a touch stop was simply a wick ----
    print("\n  WICK-OUTS - days the low pierced the stop but the close recovered")
    print("  (every one of these is a trade a touch stop closed and a close stop kept)\n")
    print("    %-8s %10s %12s" % ("stop", "wick days", "per 100 trades"))
    for mult in (0.5, 0.75, 1.0, 1.5):
        tot, wk = 0, 0
        for sym, i, entry, adr, _p, _dt, _r in sigs:
            stop = entry * (1 - mult * adr / 100.0)
            res = run_trade(px, sym, i, entry, stop, "close")
            if not res:
                continue
            tot += 1; wk += res[3]
        if tot:
            print("    %-8s %10d %12.1f" % ("%.2fx" % mult, wk, 100.0 * wk / tot))

    # ---------------- 6. WAS IT THE METHOD, OR THE YEAR? -----------------
    print("\n\n" + "=" * 74)
    print("MARKET REGIME - the same setups, split by what the market was doing")
    print("=" * 74)
    reg, series = market_regime(breadth, universe=len(px))
    up_d = sum(1 for v in reg.values() if v == "UP")
    print("\n  Regime: share of the universe above its own 50 EMA, 50% is the line.")
    print("  %d sessions UP, %d sessions DOWN across %d dated sessions."
          % (up_d, len(reg) - up_d, len(reg)))
    if series:
        pcs = [p for _d, p in series]
        print("  breadth ranged %.0f%% to %.0f%%, median %.0f%%"
              % (min(pcs), max(pcs), median(pcs)))
        step = max(1, len(series) // 8)
        print("  sample: " + "  ".join("%s %.0f%%" % (d[5:], p)
                                       for d, p in series[::step][-8:]))
    print()

    def split(items, getval):
        out = {"UP": [], "DOWN": []}
        for it in items:
            d = it[-1]
            g = reg.get(d)
            if g not in out:
                continue
            v = getval(it)
            if v is not None:
                out[g].append(v)
        return out

    # 6a. every playbook, by regime, fixed horizon
    print("  PLAYBOOKS by regime - forward %d sessions, entered at the next open"
          % a.horizon)
    print("  %-34s %8s %8s %8s %8s"
          % ("", "UP n", "UP med", "DOWN n", "DOWN med"))
    pb_items = []
    for date, rws in by_date.items():
        for r in rws:
            pb_items.append((r, date))
    for name, test in [("ALL STOCKS (baseline)", lambda r: True)] + list(PLAYS.items()):
        got = {"UP": [], "DOWN": []}
        for r, date in pb_items:
            g = reg.get(date)
            if g not in got:
                continue
            try:
                if not test(r):
                    continue
            except Exception:
                continue
            v = fwd_tr(px, idx, r["symbol"], date, a.horizon)
            if v is not None:
                got[g].append(v)
        u, dn = stats(got["UP"]), stats(got["DOWN"])
        print("  %-34s %8s %8s %8s %8s"
              % (name,
                 u["n"] if u else "-", ("%.2f%%" % u["med"]) if u else "-",
                 dn["n"] if dn else "-", ("%.2f%%" % dn["med"]) if dn else "-"))

    # 6b. the stop study, by regime - close basis only, touch vs close settled
    print("\n  STOPS by regime - close basis, after %.2f%% costs, expectancy in R"
          % COST_PCT)
    print("  %-8s %8s %10s %9s %8s %10s %9s"
          % ("stop", "UP n", "UP expect", "UP win%", "DOWN n", "DOWN expect", "DOWN win%"))
    for mult in (0.75, 1.0, 1.25, 1.5, 2.0):
        got = {"UP": [], "DOWN": []}
        for sym, i, entry, adr, _p, dt, _r in sigs:
            g = reg.get(dt)
            if g not in got:
                continue
            stop = entry * (1 - mult * adr / 100.0)
            res = run_trade(px, sym, i, entry, stop, "close")
            if not res:
                continue
            R = res[0] - (entry * COST_PCT / 100.0) / (entry - stop)
            got[g].append(R)
        u, dn = got["UP"], got["DOWN"]
        if not u and not dn:
            print("  %-8s %8s" % ("%.2fx" % mult, "no trades in either regime"))
            continue
        if not u or not dn:
            side = "UP" if u else "DOWN"
            vals = u or dn
            print("  %-8s only %s has trades: n=%d expectancy %.3fR"
                  % ("%.2fx" % mult, side, len(vals), sum(vals) / len(vals)))
            continue
        print("  %-8s %8d %9.3fR %8.1f%% %8d %10.3fR %8.1f%%"
              % ("%.2fx" % mult, len(u), sum(u) / len(u),
                 100.0 * len([x for x in u if x > 0]) / len(u),
                 len(dn), sum(dn) / len(dn),
                 100.0 * len([x for x in dn if x > 0]) / len(dn)))

    # 6c. tailwind, by regime
    print("\n  TAILWIND by regime - industry quartile, forward 20 sessions")
    print("  %-16s %8s %9s %8s %9s" % ("", "UP n", "UP med", "DOWN n", "DOWN med"))
    qr = {}
    for date, rws in by_date.items():
        rk = ind_rank.get(date)
        g = reg.get(date)
        if not rk or g not in ("UP", "DOWN"):
            continue
        for r in rws:
            if not is_setup(r):
                continue
            gr = (r.get("industry") or "").strip()
            if gr not in rk:
                continue
            pos, tot, _ = rk[gr]
            v = fwd_tr(px, idx, r["symbol"], date, 20)
            if v is None:
                continue
            frac = (pos - 1) / max(1, tot - 1)
            q = "Q1 (leading)" if frac <= .25 else ("Q4 (lagging)" if frac > .75 else "Q2-Q3")
            qr.setdefault(q, {"UP": [], "DOWN": []})[g].append(v)
    for q in ("Q1 (leading)", "Q2-Q3", "Q4 (lagging)"):
        if q not in qr:
            continue
        u, dn = stats(qr[q]["UP"]), stats(qr[q]["DOWN"])
        print("  %-16s %8s %9s %8s %9s"
              % (q, u["n"] if u else "-", ("%.2f%%" % u["med"]) if u else "-",
                 dn["n"] if dn else "-", ("%.2f%%" % dn["med"]) if dn else "-"))

    print("\n  If the UP columns are positive and the DOWN columns negative, the")
    print("  setups are fine and the missing rule is WHEN to trade them.")
    print("  If UP is also negative, the problem is the setups themselves.")

    # ---------------- 7. THE FILTER LADDER -------------------------------
    print("\n\n" + "=" * 74)
    print("FILTER LADDER - conditions stacked one at a time")
    print("=" * 74)
    print("\n  Stop 1.5x ADR close basis, exit on 2 closes under the 9 EMA,")
    print("  costs deducted. Every earlier table tested filters ONE AT A TIME on")
    print("  the whole pool. This is the question that was never asked: what")
    print("  happens when you apply them together?\n")

    def q_of(r, date):
        rk = ind_rank.get(date)
        if not rk:
            return None
        g = (r.get("industry") or "").strip()
        if g not in rk:
            return None
        pos, tot, _ = rk[g]
        frac = (pos - 1) / max(1, tot - 1)
        return 1 if frac <= .25 else (4 if frac > .75 else 23)

    RUNGS = [
      ("all QM setups",              lambda r, d: True),
      ("+ UP breadth only",          lambda r, d: reg.get(d) == "UP"),
      ("+ not a Q4 industry",        lambda r, d: q_of(r, d) not in (4, None)),
      ("+ leading (Q1) industry",    lambda r, d: q_of(r, d) == 1),
      ("+ category A or A+C",        lambda r, d: r.get("category") in ("A", "AC")),
      ("+ ADR 3% or more",           lambda r, d: (f(r.get("adr_pct")) or 0) >= 3),
      ("+ VCP or HTF only",          lambda r, d: r.get("qm_pattern") in ("VCP", "HTF")),
      ("+ within 15% of 52w high",   lambda r, d: near_high(r, 15)),
    ]
    print("  %-28s %7s %9s %8s %7s %7s %7s"
          % ("", "trades", "expect", "medianR", "win%", "avgWin", "avgLoss"))
    active = []
    for label, test in RUNGS:
        active.append(test)
        Rs = []
        for sym, i, entry, adr, _p, dt, r in sigs:
            ok = True
            for t in active:
                try:
                    if not t(r, dt):
                        ok = False; break
                except Exception:
                    ok = False; break
            if not ok:
                continue
            stop = entry * (1 - 1.5 * adr / 100.0)
            res = run_trade(px, sym, i, entry, stop, "close")
            if not res:
                continue
            Rs.append(res[0] - (entry * COST_PCT / 100.0) / (entry - stop))
        if len(Rs) < 20:
            print("  %-28s %7d   sample too small to read" % (label, len(Rs)))
            continue
        w = [x for x in Rs if x > 0]; l = [x for x in Rs if x <= 0]
        print("  %-28s %7d %8.3fR %7.2fR %6.1f%% %6.2fR %6.2fR"
              % (label, len(Rs), sum(Rs) / len(Rs), median(Rs),
                 100.0 * len(w) / len(Rs),
                 (sum(w) / len(w)) if w else 0, (sum(l) / len(l)) if l else 0))
    print("\n  Watch the trades column as much as the expectancy one. A rung that")
    print("  improves the number while collapsing the sample has not proved")
    print("  anything - it has just found a smaller group to be lucky in.")

    # ---------------- 8. DOES A SLOWER EXIT PAY? -------------------------
    print("\n\n" + "=" * 74)
    print("EXIT TEST - same trades, same 1.5x ADR stop, different trend exits")
    print("=" * 74)
    print("\n  A 9 EMA exit holds a median 4-6 days. Winners reached 2.16 ADR")
    print("  inside ten sessions, so the move is there - the question is whether")
    print("  the exit is cutting it off.\n")
    filt = [t for _l, t in RUNGS[:3]]          # UP breadth, not Q4 - the two that held up
    pool = []
    for sym, i, entry, adr, _p, dt, r in sigs:
        if all((lambda t: t(r, dt))(t) for t in filt):
            pool.append((sym, i, entry, adr))
    print("  Pool: %d setups in UP breadth, outside Q4 industries.\n" % len(pool))
    print("  %-34s %7s %9s %8s %7s %6s"
          % ("exit rule", "trades", "expect", "medianR", "win%", "days"))
    EXITS = [("1 close under the 9 EMA",  dict(ema_ix=3, need=1)),
             ("2 closes under the 9 EMA", dict(ema_ix=3, need=2)),
             ("1 close under the 20 EMA", dict(ema_ix=6, need=1)),
             ("2 closes under the 20 EMA",dict(ema_ix=6, need=2)),
             ("trail 1.5 ADR off the high", dict(ema_ix=3, need=99, trail_adr=1.5)),
             ("trail 2.5 ADR off the high", dict(ema_ix=3, need=99, trail_adr=2.5))]
    for label, kw in EXITS:
        Rs, dys = [], []
        for sym, i, entry, adr in pool:
            stop = entry * (1 - 1.5 * adr / 100.0)
            res = run_trade(px, sym, i, entry, stop, "close", adr=adr, **kw)
            if not res:
                continue
            Rs.append(res[0] - (entry * COST_PCT / 100.0) / (entry - stop))
            dys.append(res[1])
        if len(Rs) < 20:
            print("  %-34s %7d  too few" % (label, len(Rs))); continue
        w = [x for x in Rs if x > 0]
        print("  %-34s %7d %8.3fR %7.2fR %6.1f%% %5.0f"
              % (label, len(Rs), sum(Rs) / len(Rs), median(Rs),
                 100.0 * len(w) / len(Rs), median(dys)))

    print("\nNot measurable from this table, so deliberately NOT shown:")
    for s in SKIPPED:
        print("  - " + s)
    print("\nEntries are the NEXT session's open throughout - the signal-day")
    print("close had already happened before the nightly scan produced the signal.")
    print("The stop study deducts costs; the signal-quality tables do not.")
    print("Highs and lows are real (daily_ohlc). No slippage or gap modelling:")
    print("a gap through your stop fills worse than this assumes.")
    print("A playbook only earns its place if it beats the baseline row.")


if __name__ == "__main__":
    main()
