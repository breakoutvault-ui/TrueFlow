#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
ep_scan.py - Episodic Pivots, Momentum Ignitions and Delayed EPs, India + US

Built from stored daily prices, so both markets work the same way and do not
depend on the results trackers.

  EVENTS (the day it happens)
    gap      Episodic Pivot: opens with a real GAP above yesterday's close,
             closes strong, on heavy volume.
    ignition Momentum Ignition (no gap): no big gap, but the stock rallies
             through the day and closes near the high on heavy volume - or does
             it over 2-3 days in a row.
  CONTEXT
    neglected   the stock was quiet for the month before (not already running)
    results     the event lines up with a results date (catalyst confirmed)
  LIFE AFTER THE EVENT (stage, today)
    fresh     event today or yesterday
    drifting  still pushing to new highs after the event
    basing    pulled back, but holding above the event-day low
    coiling   basing AND the last week is tight on drying volume -> delayed-EP watchlist
    resumed   broke above the base high after basing -> DELAYED EP trigger
    faded     closed below the event-day low -> failed

Settings are kept per market (PARAMS) - India and US can differ.
Writes ep_state / us_ep_state (one row per stock) and ep_evidence / us_ep_evidence
(how every past signal did over the next 5/10/20 sessions vs a plain uptrend entry).

USAGE  /root/trueflow/bin/python ep_scan.py --market IN [--dry-run --symbols A,B]
       /root/trueflow/bin/python ep_scan.py --market US
"""
import sys, time, argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from statistics import median
import requests

sys.path.insert(0, "/root/trueflow")
import tf_config as CFG

H = {"apikey": CFG.SUPABASE_KEY, "Authorization": "Bearer " + CFG.SUPABASE_KEY, "Content-Type": "application/json"}
BASE = dict(
    gap_pct=4.0, gap_adr=1.0,            # gap: open >= max(4%, 1x ADR) above yesterday's close
    gap_vol=2.0,                         # ... on >= 2x 20-day average volume
    gap_close_frac=0.5,                  # ... and closes in the upper half of the day
    ign_pct=5.0, ign_adr=2.0,            # ignition (1 day): close-to-close >= max(5%, 2x ADR)
    ign_close_frac=0.75,                 # ... closing in the top quarter of its range
    ign_vol=2.0,                         # ... on >= 2x volume
    run_pct=8.0, run_adr=3.0, run_days=3, run_vol=1.5,   # ignition (2-3 days): >= max(8%, 3x ADR), each day upper half
    neglect_pct=8.0,                     # neglected = moved less than 8% either way in the 20 sessions before
    fresh_days=1, base_min=3, coil_days=5, coil_range=0.5, coil_vol=0.85,
    max_age=60, resume_window=2, horizons=(5, 10, 20), evidence_days=500,
)
PARAMS = {"IN": dict(BASE), "US": dict(BASE)}
TABLES = {"IN": ("momentum_stocks", "daily_ohlc", "earnings_moves", "ep_state", "ep_evidence"),
          "US": ("us_momentum_stocks", "us_daily_ohlc", "us_earnings_moves", "us_ep_state", "us_ep_evidence")}


def log(m):
    print(m, flush=True)


def get(path):
    for a in range(4):
        try:
            r = requests.get(CFG.SUPABASE_URL + "/rest/v1/" + path, headers=H, timeout=90)
            if r.status_code == 200:
                return r.json()
            log("  read %s -> %s" % (path[:60], r.status_code))
        except Exception as e:
            log("  read error: %s" % str(e)[:80])
        time.sleep(2 * (a + 1))
    return None


def ema(v, n):
    k, out, e = 2.0 / (n + 1), [], None
    for x in v:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def analyse(bars, P, results_days):
    """Walk forward through history, no look-ahead. Returns (events, state)."""
    n = len(bars)
    if n < 60:
        return [], None
    o = [b["o"] for b in bars]; h = [b["h"] for b in bars]; l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]; v = [b["v"] or 0 for b in bars]
    e50 = ema(c, 50)
    adr = [None] * n
    for i in range(20, n):
        adr[i] = sum((h[j] - l[j]) / l[j] * 100 for j in range(i - 19, i + 1) if l[j] > 0) / 20.0
    v20 = [None] * n
    for i in range(20, n):
        v20[i] = sum(v[i - 20:i]) / 20.0

    def pos(i):   # where the close sits in the day's range, 0..1
        r = h[i] - l[i]
        return (c[i] - l[i]) / r if r > 0 else 0.5

    events = []
    last_ev = -99
    for i in range(41, n):
        a = adr[i - 1] or 2.0
        vr = (v[i] / v20[i]) if v20[i] else 0
        gap = (o[i] / c[i - 1] - 1) * 100 if c[i - 1] else 0
        chg = (c[i] / c[i - 1] - 1) * 100 if c[i - 1] else 0
        typ = None
        if gap >= max(P["gap_pct"], P["gap_adr"] * a) and vr >= P["gap_vol"] and pos(i) >= P["gap_close_frac"]:
            typ = "gap"
        elif chg >= max(P["ign_pct"], P["ign_adr"] * a) and pos(i) >= P["ign_close_frac"] and vr >= P["ign_vol"] \
                and gap < max(P["gap_pct"], P["gap_adr"] * a):
            typ = "ignition"
        else:
            for k in range(2, P["run_days"] + 1):          # 2-3 strong days in a row
                s = i - k + 1
                if c[s - 1] <= 0:
                    continue
                run = (c[i] / c[s - 1] - 1) * 100
                if run < max(P["run_pct"], P["run_adr"] * a):
                    continue
                if all(pos(j) >= 0.5 and c[j] > c[j - 1] for j in range(s, i + 1)) and \
                        all(v20[j] and v[j] / v20[j] >= P["run_vol"] for j in range(s, i + 1)):
                    typ = "ignition"; chg = run; vr = sum(v[j] / v20[j] for j in range(s, i + 1)) / k
                    break
        if typ and i - last_ev > 5:                         # one event per burst of activity
            pre = (c[i - 1] / c[i - 21] - 1) * 100 if c[i - 21] else 0
            start = i
            while start > i - 3 and typ == "ignition" and c[start - 1] > c[start - 2] and pos(start - 1) >= 0.5:
                start -= 1
            ev = dict(i=i, typ=typ, move=round(gap if typ == "gap" else chg, 2), rvol=round(vr, 2),
                      low=min(l[start:i + 1]), high=h[i], neglected=abs(pre) <= P["neglect_pct"],
                      results=any(d in results_days for d in (bars[i]["d"], bars[i - 1]["d"])),
                      trend=bool(c[i] > e50[i]))
            events.append(ev)
            last_ev = i
    # ── stage of the latest event, and delayed-EP triggers through history ──
    def stage_walk(ev, upto):
        """Stage of an event as of bar `upto`, plus the bar of a delayed trigger (resumed)."""
        i0 = ev["i"]; peak = h[i0]; peak_i = i0; base_hi = None; base_lo = None; based_since = None
        st = "fresh"; trig = None
        for j in range(i0 + 1, upto + 1):
            if c[j] < ev["low"]:
                return "faded", trig, None, None, None
            if h[j] > peak and based_since is None:
                peak = h[j]; peak_i = j
            if based_since is None:
                if j - peak_i >= P["base_min"]:
                    based_since = peak_i
                    base_hi = max(h[peak_i:j + 1]); base_lo = min(l[peak_i + 1:j + 1])
            else:
                if c[j] > base_hi and j - based_since >= P["base_min"]:
                    trig = j; st = "resumed"
                    based_since = None; peak = h[j]; peak_i = j; base_hi = base_lo = None
                    continue
                base_lo = min(base_lo, l[j])
            if j - i0 <= P["fresh_days"]:
                st = "fresh"
            elif based_since is None:
                st = "drifting" if trig is None or j - trig > P["resume_window"] else "resumed"
            else:
                rng = max(h[j - P["coil_days"] + 1:j + 1]) - min(l[j - P["coil_days"] + 1:j + 1])
                full = (base_hi - base_lo) if base_hi and base_lo else rng
                vdry = (sum(v[j - P["coil_days"] + 1:j + 1]) / P["coil_days"]) <= P["coil_vol"] * (v20[j] or 1)
                st = "coiling" if (full > 0 and rng <= P["coil_range"] * full and vdry) else "basing"
        return st, trig, base_hi, base_lo, (upto - based_since) if based_since is not None else None

    # evidence: event days and delayed triggers
    for ev in events:
        st, trig, _, _, _ = stage_walk(ev, min(n - 1, ev["i"] + P["max_age"]))
        ev["trigger"] = trig
    t = n - 1
    live = [e for e in events if t - e["i"] <= P["max_age"]]
    state = {"session_date": bars[t]["d"], "close": round(c[t], 4), "trend_ok": bool(c[t] > e50[t] and e50[t] > e50[t - 10]),
             "ev_type": None, "ev_date": None, "ev_age": None, "ev_move": None, "ev_rvol": None, "ev_low": None,
             "neglected": None, "results": None, "stage": None, "base_high": None, "base_low": None, "base_days": None,
             "trigger_date": None, "trigger_age": None}
    if live:
        ev = live[-1]
        st, trig, bh, bl, bd = stage_walk(ev, t)
        state.update(ev_type=ev["typ"], ev_date=bars[ev["i"]]["d"], ev_age=t - ev["i"], ev_move=ev["move"],
                     ev_rvol=ev["rvol"], ev_low=round(ev["low"], 4), neglected=ev["neglected"], results=ev["results"],
                     stage=st, base_high=round(bh, 4) if bh else None, base_low=round(bl, 4) if bl else None,
                     base_days=bd, trigger_date=bars[trig]["d"] if trig is not None else None,
                     trigger_age=(t - trig) if trig is not None else None)
    return events, state


def evidence(bars, events, P, acc, base_acc):
    n = len(bars); lo = max(0, n - P["evidence_days"])
    def fwd(i, key):
        if i < lo or i + 1 >= n:
            return
        e = bars[i + 1]["o"] or bars[i + 1]["c"]
        for hz in P["horizons"]:
            if i + hz < n and e:
                acc.setdefault((key, hz), []).append((bars[i + hz]["c"] / e - 1) * 100)
    for ev in events:
        k = ev["typ"]
        fwd(ev["i"], k)
        fwd(ev["i"], k + ("_neglected" if ev["neglected"] else "_moving"))
        if ev["results"]:
            fwd(ev["i"], k + "_results")
        if ev.get("trigger") is not None:
            fwd(ev["trigger"], "delayed_" + k)
    c = [b["c"] for b in bars]; e50 = ema(c, 50)
    for i in range(max(lo, 60), n - 21, 5):
        if c[i] > e50[i]:
            e = bars[i + 1]["o"] or bars[i + 1]["c"]
            if e:
                for hz in P["horizons"]:
                    base_acc.setdefault(hz, []).append((bars[i + hz]["c"] / e - 1) * 100)


PB = {   # playbook rules, per market (scan decides who qualifies)
    "IN": [("ep_neglect", lambda s: s["ev_type"] == "gap" and s["neglected"] and s["stage"] in ("fresh",)),
           ("ep_moving", lambda s: s["ev_type"] == "gap" and not s["neglected"] and s["stage"] in ("fresh",)),
           ("ep_ignition", lambda s: s["ev_type"] == "ignition" and s["stage"] in ("fresh",)),
           ("ep_results", lambda s: s["results"] and s["stage"] in ("fresh", "drifting")),
           ("dep_coil", lambda s: s["stage"] == "coiling"),
           ("dep_break", lambda s: s["stage"] == "resumed" and (s["trigger_age"] or 99) <= 2)],
}
PB["US"] = list(PB["IN"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["IN", "US"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    P = PARAMS[a.market]; uni_t, ohlc_t, er_t, st_t, ev_t = TABLES[a.market]
    t0 = time.time()
    sd = get("%s?select=session_date&order=session_date.desc&limit=1" % uni_t)[0]["session_date"]
    syms, off = [], 0
    while True:
        p = get("%s?select=symbol&session_date=eq.%s&order=symbol.asc&limit=1000&offset=%d" % (uni_t, sd, off)) or []
        syms += [x["symbol"] for x in p]
        if len(p) < 1000:
            break
        off += 1000
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    res, off = {}, 0
    while True:
        p = get("%s?select=symbol,result_date&limit=1000&offset=%d" % (er_t, off)) or []
        for x in p:
            if x.get("result_date"):
                res.setdefault(x["symbol"], set()).add(str(x["result_date"])[:10])
        if len(p) < 1000:
            break
        off += 1000

    def load(sym):
        rows = get("%s?symbol=eq.%s&select=d,o,h,l,c,v&order=d.asc&limit=1000" % (ohlc_t, requests.utils.quote(sym, safe=""))) or []
        return [r for r in rows if r.get("c") and r.get("h") and r.get("l") and r.get("o")]

    with ThreadPoolExecutor(8) as ex:
        data = dict(zip(syms, ex.map(load, syms)))
    log("EP scan %s | %d symbols, results dates for %d (%.0fs)" % (a.market, len(data), len(res), time.time() - t0))
    rows, acc, base_acc, cnt = [], {}, {}, {}
    for sym in syms:
        bars = data.get(sym) or []
        try:
            events, st = analyse(bars, P, res.get(sym, set()))
        except Exception as e:
            log("  %s: %s" % (sym, str(e)[:100])); continue
        if not st:
            continue
        evidence(bars, events, P, acc, base_acc)
        st["symbol"] = sym
        st["computed_at"] = datetime.now(timezone.utc).isoformat()
        hits = [k for k, f in PB[a.market] if st["ev_type"] and st["trend_ok"] is not None and f(st)]
        st["pb_hits"] = ",".join(hits) or None
        rows.append(st)
        if st["stage"]:
            cnt[("stage", st["ev_type"] + ":" + st["stage"])] = cnt.get(("stage", st["ev_type"] + ":" + st["stage"]), 0) + 1
        for h_ in hits:
            cnt[("playbook", h_)] = cnt.get(("playbook", h_), 0) + 1
        if a.symbols:
            log("  %s %s" % (sym, {k: st[k] for k in ("ev_type", "ev_date", "ev_move", "neglected", "results", "stage", "base_high", "trigger_date", "pb_hits")}))
    today = datetime.now(timezone.utc).date().isoformat()
    bm = {hz: (sum(v) / len(v)) for hz, v in base_acc.items() if v}
    ev = []
    for (k, hz), vals in sorted(acc.items()):
        if len(vals) < 10:
            continue
        mean = sum(vals) / len(vals)
        ev.append({"type": k, "horizon": hz, "n": len(vals), "median_pct": round(median(vals), 2), "mean_pct": round(mean, 2),
                   "win_pct": round(100.0 * sum(1 for x in vals if x > 0) / len(vals), 1),
                   "edge_pct": round(mean - bm.get(hz, 0), 2), "computed_on": today})
    log("ROWS %d" % len(rows))
    for k in sorted(cnt):
        log("   %-9s %-26s %d" % (k[0], k[1], cnt[k]))
    for r in ev:
        if r["horizon"] == 10:
            log("   evidence %-22s n=%-5d mean %+.2f%%  edge vs uptrend baseline %+.2f%%  up %.0f%%" % (r["type"], r["n"], r["mean_pct"], r["edge_pct"], r["win_pct"]))
    if a.dry_run:
        log("DRY RUN - nothing written (%.0fs)" % (time.time() - t0)); return
    for tbl, data_, conflict in ((st_t, rows, "symbol"), (ev_t, ev, "type,horizon")):
        for i in range(0, len(data_), 500):
            r = requests.post(CFG.SUPABASE_URL + "/rest/v1/%s?on_conflict=%s" % (tbl, conflict),
                              headers=dict(H, Prefer="resolution=merge-duplicates,return=minimal"), json=data_[i:i + 500], timeout=120)
            if r.status_code >= 300:
                raise SystemExit("%s write failed %s: %s" % (tbl, r.status_code, r.text[:200]))
    log("WROTE %s %d · %s %d (%.0fs)" % (st_t, len(rows), ev_t, len(ev), time.time() - t0))


if __name__ == "__main__":
    main()
