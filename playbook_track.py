#!/usr/bin/env python3
# ==============================================================
#  TrueFlow - playbook tracker
#
#  Runs nightly, after momentum_scan. Three jobs in one pass:
#
#   1. RECORD  - every stock each playbook matched tonight
#   2. GRADE   - picks old enough to have a forward return get one
#   3. ROLL UP - playbook_stats, so the dashboard reads one small table
#
#  WHY THIS EXISTS
#  The historical study answered "did this work over the past year".
#  This answers "is it working now", and it accumulates from tonight.
#  Nothing here can be backfilled: a night missed is a night gone.
#
#  AN HONEST LIMITATION, READ IT
#  The real playbook rules live in the dashboard's JavaScript. These are
#  PYTHON APPROXIMATIONS of them. They will drift as the dashboard changes,
#  and a few dashboard playbooks (Alignment, Smart Money, and the weekly /
#  monthly half of the reframed rules) cannot be reproduced here at all,
#  because that data is not in momentum_stocks. Those are left out rather
#  than approximated badly - a wrong number is worse than a missing one.
#
#  Entries are the NEXT session's open: the scan runs at night, so tonight's
#  close was never buyable.
#
#  Run:  cd /root/trueflow && ./bin/python playbook_track.py
#        ./bin/python playbook_track.py --grade-only
#  Cron: 30 13 * * 1-5   (7:00 PM IST, after the 6:30 PM momentum scan)
# ==============================================================

import sys, json, argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from statistics import median

sys.path.insert(0, "/root/trueflow")
import requests
import tf_config as CFG

SB = getattr(CFG, "SUPABASE_URL")
KEY = getattr(CFG, "SUPABASE_KEY")
H = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
IST = timezone(timedelta(hours=5, minutes=30))
HORIZONS = (5, 10, 20)


def log(m):
    print("%s  %s" % (datetime.now(IST).strftime("%Y-%m-%d %H:%M"), m), flush=True)


def get(path):
    r = requests.get("%s/rest/v1/%s" % (SB, path), headers=H, timeout=90)
    r.raise_for_status()
    return r.json()


def post(path, rows, prefer="resolution=merge-duplicates,return=minimal"):
    for i in range(0, len(rows), 500):
        r = requests.post("%s/rest/v1/%s" % (SB, path),
                          headers=dict(H, Prefer=prefer), json=rows[i:i + 500], timeout=120)
        if r.status_code >= 300:
            raise RuntimeError("%s write failed %s: %s" % (path, r.status_code, r.text[:250]))


def patch(path, body):
    r = requests.patch("%s/rest/v1/%s" % (SB, path), headers=H, json=body, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError("patch failed %s: %s" % (r.status_code, r.text[:200]))


def f(x):
    try:
        return None if x in (None, "") else float(x)
    except Exception:
        return None


def near_high(r, pct):
    h, c = f(r.get("high_52w")), f(r.get("ltp"))
    return bool(h and c and h > 0 and (h - c) / h * 100 <= pct)


# ── the playbooks, as closely as momentum_stocks allows ──────────────────
PLAYS = {
 "Coiled & Drying":      lambda r: r.get("qm_pattern") in ("VCP", "HTF")
                                   and r.get("category") in ("A", "AC")
                                   and (f(r.get("qm_contraction")) or 0) >= 0.7,
 "VCP at Pivot":         lambda r: r.get("qm_pattern") == "VCP"
                                   and r.get("category") in ("A", "AC") and near_high(r, 15),
 "Episodic Pivots":      lambda r: r.get("qm_pattern") == "EP",
 "EP + 2x volume":       lambda r: r.get("qm_pattern") == "EP" and (f(r.get("vol_ratio")) or 0) >= 2,
 "Breakout + Volume":    lambda r: bool(r.get("breakout_type")) and (f(r.get("vol_ratio")) or 0) >= 1.5,
 "ATH Breakout":         lambda r: (r.get("breakout_type") or "") == "ATH",
 "High-ADR Breakout":    lambda r: bool(r.get("breakout_type")) and (f(r.get("vol_ratio")) or 0) >= 1.5
                                   and (f(r.get("adr_pct")) or 0) >= 5,
 # qm_pattern never holds "Reclaim" - reclaims are marked by rcl_grade, which
 # is why both of these recorded zero picks on the first run.
 "Reclaim":              lambda r: bool(r.get("rcl_grade")),
 "Reclaim A/B":          lambda r: (r.get("rcl_grade") or "") in ("A", "B"),
 "NR7":                  lambda r: bool(r.get("is_nr7")),
 "NR7 + VCP":            lambda r: bool(r.get("is_nr7")) and r.get("qm_pattern") == "VCP",
 "HTF":                  lambda r: r.get("qm_pattern") == "HTF",
 "Weekend Coils":        lambda r: r.get("qm_pattern") in ("VCP", "HTF")
                                   and r.get("category") in ("A", "AC")
                                   and (f(r.get("adr_pct")) or 0) >= 3
                                   and (r.get("qm_base_days") or 0) >= 5,
 "Cat A only":           lambda r: r.get("category") == "A",
 # shakeouts - the rules that passed the 29 Sep study for this market (from the nightly shakeout scan)
 "20 EMA Shakeout + Volume":          lambda r: "in_ema20v" in (r.get("pb_hits") or "").split(","),
 "20 EMA Shakeout + Volume, strong":  lambda r: "in_ema20v_rs" in (r.get("pb_hits") or "").split(","),
 "Box U&R (tight)":                   lambda r: "in_box_ur" in (r.get("pb_hits") or "").split(","),
 "ALL SETUPS (baseline)":lambda r: (r.get("qm_pattern") or "") in ("VCP", "HTF", "EP", "Reclaim"),
}

COLS = ("symbol,session_date,ltp,qm_pattern,category,adr_pct,vol_ratio,high_52w,"
        "qm_contraction,qm_base_days,is_nr7,rcl_grade,breakout_type")


def latest_session():
    j = get("momentum_stocks?select=session_date&order=session_date.desc&limit=1")
    return j[0]["session_date"] if j else None


def record(day):
    """Write tonight's matches. Re-running the same day is harmless."""
    rows, off = [], 0
    while True:
        b = get("momentum_stocks?select=%s&session_date=eq.%s&order=symbol.asc&limit=1000&offset=%d"
                % (COLS, day, off))
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    log("%d stocks in the %s scan" % (len(rows), day))
    # merge the shakeout scan (one row per stock). Missing table / no rows = those plays record nothing.
    try:
        sk, off2 = {}, 0
        while True:
            b = get("shakeout_state?select=symbol,session_date,trend_ok,box_state,shake_type,shake_age,shk_bo,shk_near,pb_hits"
                    "&limit=1000&offset=%d" % off2)
            for x in b:
                if str(x.get("session_date")) == str(day):
                    sk[x["symbol"]] = x
            if len(b) < 1000:
                break
            off2 += 1000
        for r in rows:
            x = sk.get(r["symbol"])
            if x:
                r.update({k: x.get(k) for k in ("trend_ok", "box_state", "shake_type", "shake_age", "shk_bo", "shk_near", "pb_hits")})
        log("shakeout rows merged: %d" % len(sk))
    except Exception as e:
        log("shakeout rows not available (%s) - shakeout plays skipped tonight" % str(e)[:80])

    picks, counts = [], {}
    for name, test in PLAYS.items():
        n = 0
        for r in rows:
            try:
                if not test(r):
                    continue
            except Exception:
                continue
            picks.append({"session_date": day, "playbook": name, "symbol": r["symbol"]})
            n += 1
        counts[name] = n
    if picks:
        post("playbook_picks", picks)
    for k in sorted(counts, key=lambda x: -counts[x]):
        log("   %-24s %4d" % (k, counts[k]))
    return len(picks)


def grade():
    """Fill in forward returns for picks old enough to have them."""
    todo = get("playbook_picks?select=session_date,playbook,symbol&graded_at=is.null"
               "&order=session_date.asc&limit=20000")
    if not todo:
        log("nothing waiting to be graded")
        return 0
    syms = sorted({t["symbol"] for t in todo})
    oldest = min(t["session_date"] for t in todo)
    log("%d ungraded picks across %d symbols, oldest %s" % (len(todo), len(syms), oldest))

    px = {}
    for i, sym in enumerate(syms):
        bars = get("daily_ohlc?select=d,o,c&symbol=eq.%s&d=gte.%s&order=d.asc&limit=400"
                   % (sym, oldest))
        if bars:
            px[sym] = bars
        if (i + 1) % 200 == 0:
            log("   loaded %d/%d symbols" % (i + 1, len(syms)))

    done, still = 0, 0
    for t in todo:
        bars = px.get(t["symbol"])
        if not bars:
            still += 1
            continue
        # the first session AFTER the signal - tonight's close was never buyable
        nxt = None
        for k, b in enumerate(bars):
            if b["d"] > t["session_date"]:
                nxt = k
                break
        if nxt is None:
            still += 1
            continue
        entry = f(bars[nxt].get("o")) or f(bars[nxt].get("c"))
        if not entry:
            still += 1
            continue
        body = {"entry": entry, "entry_date": bars[nxt]["d"]}
        ready = True
        for h in HORIZONS:
            j = nxt + h
            if j < len(bars):
                c = f(bars[j].get("c"))
                body["r%d" % h] = round((c / entry - 1) * 100, 3) if c else None
            else:
                ready = False           # not enough sessions yet - come back tomorrow
        if ready:
            body["graded_at"] = datetime.now(IST).isoformat()
            done += 1
        else:
            still += 1
        patch("playbook_picks?session_date=eq.%s&playbook=eq.%s&symbol=eq.%s"
              % (t["session_date"], requests.utils.quote(t["playbook"]), t["symbol"]), body)
    log("graded %d, %d still waiting for more sessions" % (done, still))
    return done


def rollup():
    """Recompute the summary the dashboard reads."""
    rows, off = [], 0
    while True:
        b = get("playbook_picks?select=playbook,session_date,r5,r10,r20&limit=1000&offset=%d" % off)
        rows.extend(b)
        if len(b) < 1000:
            break
        off += 1000
    agg = defaultdict(lambda: defaultdict(list))
    dates = defaultdict(list)
    for r in rows:
        dates[r["playbook"]].append(r["session_date"])
        for h in HORIZONS:
            v = f(r.get("r%d" % h))
            if v is not None:
                agg[r["playbook"]][h].append(v)
    out = []
    for pb, byh in agg.items():
        for h, vals in byh.items():
            if len(vals) < 3:
                continue
            wins = [x for x in vals if x > 0]
            out.append({"playbook": pb, "horizon": h, "n": len(vals),
                        "median_pct": round(median(vals), 3),
                        "mean_pct": round(sum(vals) / len(vals), 3),
                        "win_pct": round(100.0 * len(wins) / len(vals), 1),
                        "best_pct": round(max(vals), 2), "worst_pct": round(min(vals), 2),
                        "first_date": min(dates[pb]), "last_date": max(dates[pb]),
                        "updated_at": datetime.now(IST).isoformat()})
    if out:
        post("playbook_stats", out)
        log("rolled up %d playbook/horizon rows" % len(out))
    else:
        log("nothing graded yet - the table fills once picks are 5+ sessions old")
    return len(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grade-only", action="store_true")
    a = ap.parse_args()

    if not a.grade_only:
        day = latest_session()
        if not day:
            log("momentum_stocks is empty - has the scan run?")
            sys.exit(1)
        record(day)
    grade()
    rollup()
    log("done")


if __name__ == "__main__":
    main()
