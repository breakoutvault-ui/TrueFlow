#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════════════
 nextday_learner.py  —  TrueFlow  (Next Day WL v2, script 4)
════════════════════════════════════════════════════════════════════════

WHAT THIS DOES
--------------
Reads graded picks, works out which parts of the score actually separate
winners from losers, and — once there is enough evidence — proposes a new
set of weights.

WHY THE RAILS EXIST
-------------------
Twenty picks a day is roughly 400 graded picks a month, and only about
half of those ever trigger. That is enough to nudge weights and nowhere
near enough to trust a big swing. Tune too eagerly on a small sample and
the model learns last month's market instead of a real edge. So:

  * nothing is proposed below MIN_SAMPLE graded TRADES
  * no single weight moves more than MAX_MOVE_PCT in one cycle
  * the total always renormalises back to 100
  * a new weight set is written INACTIVE. You activate it yourself.
  * every version is kept, so rolling back is one UPDATE

It never edits the active row. It cannot silently change how you trade.

WHAT IT MEASURES
----------------
For each scoring component, the Pearson correlation between the points
that component awarded and the R-multiple the trade returned. A component
that correlates positively is doing work. One sitting near zero is adding
noise and a constant.

It also writes the slice tables (time bucket, regime, ORB type, score
bucket) into nextday_learning so the Journal tab has them without
recomputing.

USAGE
-----
  Look, change nothing (safe, run any time):
      /root/trueflow/bin/python nextday_learner.py

  Write the slice snapshot but no weights:
      /root/trueflow/bin/python nextday_learner.py --snapshot

  Propose a new INACTIVE weight version:
      /root/trueflow/bin/python nextday_learner.py --propose

  Then, if you agree with it, activate by hand:
      update nextday_weights set active=false where active;
      update nextday_weights set active=true where version=<N>;
════════════════════════════════════════════════════════════════════════
"""

import sys
import math
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


IST = timezone(timedelta(hours=5, minutes=30))

MIN_SAMPLE   = 200     # graded TRADES before any weight is proposed
MAX_MOVE_PCT = 20.0    # no weight moves more than this in one cycle
MIN_WEIGHT   = 2.0     # never zero a component out entirely
MAX_WEIGHT   = 30.0    # never let one component dominate

COMPONENTS = [
    ("sc_oi",          "w_oi",          "OI buildup"),
    ("sc_ema_struct",  "w_ema_struct",  "EMA structure"),
    ("sc_ema_prox",    "w_ema_prox",    "Proximity to 9 EMA"),
    ("sc_compression", "w_compression", "Range compression"),
    ("sc_volume",      "w_volume",      "Volume + delivery"),
    ("sc_room",        "w_room",        "Room to run"),
    ("sc_sector",      "w_sector",      "Sector strength"),
    ("sc_orbhist",     "w_orbhist",     "Own ORB record"),
    ("sc_trend",       "w_trend",       "Daily trend align"),
]


def f(x, d=None):
    try:
        return d if x is None else float(x)
    except (TypeError, ValueError):
        return d


def connect_sb():
    sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
    print("Supabase connected.")
    return sb


def page_select(builder, size=1000, key="id"):
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


def corr(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def load_joined(sb):
    """Picks joined to their outcomes, in python (PostgREST has no join)."""
    picks = page_select(lambda a, b: (
        sb.table("nextday_picks_v2").select("*")
          .order("id").range(a, b).execute().data))
    outs = page_select(lambda a, b: (
        sb.table("nextday_outcomes").select("*")
          .order("id").range(a, b).execute().data))
    omap = {(o["target_date"], o["symbol"], o["direction"]): o for o in outs}
    joined = []
    for p in picks:
        o = omap.get((p["target_date"], p["symbol"], p["direction"]))
        if o:
            joined.append((p, o))
    print("  %d v2 picks, %d outcomes, %d joined"
          % (len(picks), len(outs), len(joined)))
    traded = [(p, o) for p, o in joined if o.get("r_multiple") is not None]
    print("  %d of those actually triggered" % len(traded))
    return joined, traded


def load_weights(sb):
    rows = (sb.table("nextday_weights").select("*")
              .eq("active", True).order("version", desc=True)
              .limit(1).execute().data or [])
    if not rows:
        print("FATAL: no active row in nextday_weights.")
        sys.exit(1)
    return rows[0]


def slice_stats(rows, keyfn):
    g = {}
    for p, o in rows:
        k = keyfn(p, o)
        if k in (None, ""):
            continue
        g.setdefault(str(k), []).append(f(o.get("r_multiple")))
    out = []
    for k, v in sorted(g.items()):
        v = [x for x in v if x is not None]
        if len(v) < 3:
            continue
        wins = sum(1 for x in v if x > 0)
        out.append({"bucket": k, "n": len(v),
                    "win_pct": round(100.0 * wins / len(v), 1),
                    "avg_r": round(sum(v) / len(v), 4),
                    "total_r": round(sum(v), 2)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true",
                    help="write slice tables to nextday_learning")
    ap.add_argument("--propose", action="store_true",
                    help="write a new INACTIVE weight version")
    args = ap.parse_args()

    print("=" * 62)
    print(" TrueFlow Next Day WL v2 — learner")
    print("=" * 62)
    sb = connect_sb()
    w = load_weights(sb)
    print("  active weights: v%s" % w.get("version"))

    joined, traded = load_joined(sb)
    if not joined:
        print("\nNothing to learn from yet. Run the grader first.")
        return

    # ── headline ─────────────────────────────────────────────────────
    rs = [f(o.get("r_multiple")) for p, o in traded]
    rs = [x for x in rs if x is not None]
    if rs:
        wins = sum(1 for x in rs if x > 0)
        print("-" * 62)
        print("V2 PICKS SO FAR:  %d triggered of %d  |  win %.1f%%  "
              "avg %.3f R  total %.1f R"
              % (len(rs), len(joined), 100.0 * wins / len(rs),
                 sum(rs) / len(rs), sum(rs)))

    # ── component correlations ───────────────────────────────────────
    print("-" * 62)
    print("COMPONENT CORRELATION WITH R  (does this part earn its weight?)")
    print("  %-22s %8s %8s %s" % ("component", "weight", "corr", "reading"))
    cors = {}
    for sc, wk, label in COMPONENTS:
        xs, ys = [], []
        for p, o in traded:
            a, b = f(p.get(sc)), f(o.get("r_multiple"))
            if a is not None and b is not None:
                xs.append(a)
                ys.append(b)
        c = corr(xs, ys)
        cors[sc] = c
        spread = (max(xs) - min(xs)) if xs else 0
        if c is None:
            reading = "not enough data"
        elif spread < 0.5:
            reading = "FLAT — scores everyone the same, adds nothing"
        elif c > 0.10:
            reading = "helping"
        elif c < -0.10:
            reading = "HURTING — inverted?"
        else:
            reading = "no signal yet"
        print("  %-22s %8.1f %8s %s"
              % (label, f(w.get(wk), 0),
                 "—" if c is None else "%+.3f" % c, reading))

    # ── slices ───────────────────────────────────────────────────────
    slices = {
        "time_bucket": lambda p, o: o.get("break_bucket"),
        "day_regime":  lambda p, o: o.get("day_regime"),
        "orb_type":    lambda p, o: o.get("orb_type"),
        "direction":   lambda p, o: p.get("direction"),
        "score_band":  lambda p, o: ("75+" if f(p.get("score"), 0) >= 75
                                     else "70-74" if f(p.get("score"), 0) >= 70
                                     else "65-69" if f(p.get("score"), 0) >= 65
                                     else "60-64"),
        "alert_fired": lambda p, o: "yes" if o.get("alert_fired") else "no",
        "badges":      lambda p, o: (p.get("badges") or "none").split(",")[0],
    }
    print("-" * 62)
    snapshot_rows = []
    today = datetime.now(IST).date().isoformat()
    for dim, fn in slices.items():
        st = slice_stats(traded, fn)
        if not st:
            continue
        print("%s:" % dim)
        for s in st:
            print("   %-22s n=%3d  win %5.1f%%  avg %+.3f R"
                  % (s["bucket"][:22], s["n"], s["win_pct"], s["avg_r"]))
        for s in st:
            snapshot_rows.append({
                "run_date": today, "scope": "v2", "dim": dim,
                "bucket": s["bucket"], "n": s["n"],
                "win_pct": s["win_pct"], "avg_r": s["avg_r"],
                "total_r": s["total_r"],
                "weights_version": w.get("version"),
            })

    if args.snapshot and snapshot_rows:
        try:
            sb.table("nextday_learning").insert(snapshot_rows).execute()
            print("\nWrote %d snapshot row(s)." % len(snapshot_rows))
        except Exception as e:
            print("\nSnapshot write failed: %s" % str(e)[:100])

    # ── weight proposal ──────────────────────────────────────────────
    print("-" * 62)
    if len(rs) < MIN_SAMPLE:
        print("WEIGHTS UNCHANGED — %d triggered trades, need %d."
              % (len(rs), MIN_SAMPLE))
        print("Tuning on a smaller sample fits noise, not edge.")
        return
    if not args.propose:
        print("Sample is large enough. Re-run with --propose to write a")
        print("new INACTIVE weight version for you to review.")
        return

    usable = {k: v for k, v in cors.items() if v is not None}
    if not usable:
        print("No usable correlations. Nothing proposed.")
        return
    mean_c = sum(usable.values()) / len(usable)

    new_w, changes = {}, []
    for sc, wk, label in COMPONENTS:
        cur = f(w.get(wk), 0.0)
        c = cors.get(sc)
        if c is None or cur <= 0:
            new_w[wk] = cur
            continue
        # move toward components that beat the average correlation
        adj = 1.0 + max(-MAX_MOVE_PCT, min(MAX_MOVE_PCT,
                                           (c - mean_c) * 200.0)) / 100.0
        prop = max(MIN_WEIGHT, min(MAX_WEIGHT, cur * adj))
        new_w[wk] = prop
        if abs(prop - cur) > 0.05:
            changes.append((label, cur, prop, c))

    total = sum(new_w.values())
    if total <= 0:
        print("Degenerate proposal. Nothing written.")
        return
    for k in new_w:
        new_w[k] = round(new_w[k] * 100.0 / total, 2)

    print("PROPOSED (renormalised to 100):")
    for label, cur, prop, c in changes:
        print("   %-22s %5.1f -> %5.1f   (corr %+.3f)"
              % (label, cur, prop, c))
    if not changes:
        print("   no component moved enough to matter")
        return

    nxt = (sb.table("nextday_weights").select("version")
             .order("version", desc=True).limit(1).execute().data or [])
    version = (nxt[0]["version"] if nxt else 1) + 1
    row = dict(new_w)
    row.update({
        "version": version, "active": False, "source": "auto",
        "min_score": f(w.get("min_score")), "min_adr": f(w.get("min_adr")),
        "min_ema_sep": f(w.get("min_ema_sep")),
        "notes": "auto-proposed from %d triggered trades on %s; "
                 "INACTIVE until activated by hand" % (len(rs), today),
    })
    try:
        sb.table("nextday_weights").insert(row).execute()
        print("\nWrote version %d as INACTIVE. Nothing has changed yet."
              % version)
        print("To use it:")
        print("   update nextday_weights set active=false where active;")
        print("   update nextday_weights set active=true where version=%d;"
              % version)
    except Exception as e:
        print("\nWrite failed: %s" % str(e)[:120])


if __name__ == "__main__":
    main()
