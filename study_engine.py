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
        "nr_status,rcl_grade,breakout_type,momentum_score,day_high,day_low")

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


def build(rows):
    """price[symbol] = list of (date, close) in date order; plus a date index."""
    px = defaultdict(list)
    for r in rows:
        c = f(r.get("ltp"))
        if c and c > 0:
            px[r["symbol"]].append((r["session_date"], c))
    for s in px:
        px[s].sort()
    idx = {s: {d: i for i, (d, _) in enumerate(v)} for s, v in px.items()}
    return px, idx


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
    ap.add_argument("--horizon", type=int, default=10,
                    help="which horizon the tailwind tables use")
    a = ap.parse_args()

    rows, dates = fetch(a.sessions)
    px, idx = build(rows)

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["session_date"]].append(r)

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
                    v = fwd(px, idx, r["symbol"], date, h)
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
            v = fwd(px, idx, r["symbol"], date, a.horizon)
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
                v = fwd(px, idx, r["symbol"], date, a.horizon)
                if v is not None:
                    vals.append(v)
        res.append((name, vals))
    res = [res[0]] + sorted(res[1:], key=lambda x: -(stats(x[1])["med"] if x[1] else -99))
    show("PLAYBOOK AUDIT - forward %d sessions, ranked by median" % a.horizon, res)

    print("\nNot measurable from this table, so deliberately NOT shown:")
    for s in SKIPPED:
        print("  - " + s)
    print("\nRead these as relative, not absolute. No stops, no costs, no slippage.")
    print("A playbook only earns its place if it beats the baseline row.")


if __name__ == "__main__":
    main()
