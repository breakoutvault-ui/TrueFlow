#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
shakeout_scan.py - every kind of shakeout, for India and US, every night

  EMA shakeouts   ema9  - lose the daily 9 EMA for 1-3 closes, then close back above
                  ema20 - the same against the 20 EMA
  Box shakeouts   ur     - Undercut & Rally: low breaks the box low, close back inside in 1-3 sessions
                  spring - a U&R whose undercut day had heavy volume and closed in its upper half
                  fbbo   - failed breakdown -> breakout: after a U&R, a close above the box high
                  top    - shakeout at the top: close above the box high, back inside within
                           a few sessions, then a new close above the high
  The pattern this exists for:  ANY shakeout in the last N sessions, then the box breaks.

Writes one row per stock (today's state) and an evidence table that says how
every past signal of each type did over the next 5 / 10 / 20 sessions.

India and US use SEPARATE settings (PARAMS below). They start from the same
values; every threshold that depends on how much a stock moves is scaled by
that stock's own average daily range, so a quiet large-cap and a volatile
small-cap are judged fairly. Tune each market from its own evidence table.

USAGE
  /root/trueflow/bin/python shakeout_scan.py --market IN
  /root/trueflow/bin/python shakeout_scan.py --market US
  add --dry-run --symbols A,B  to test without writing
"""
import sys, time, math, argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from statistics import median
import requests

sys.path.insert(0, "/root/trueflow")
import tf_config as CFG

H = {"apikey": CFG.SUPABASE_KEY, "Authorization": "Bearer " + CFG.SUPABASE_KEY,
     "Content-Type": "application/json"}

# ── settings, one set per market ───────────────────────────────────────────
BASE = dict(
    box_min=10, box_max=60,        # box length in sessions
    box_depth_max=15.0,            # box top-to-bottom, % of the low ...
    box_depth_adr=6.0,             # ... and never deeper than 6x the stock's ADR
    touch_tol_adr=0.5,             # a "touch" = within 0.5 ADR of the edge
    touches=2,                     # touches needed on the top AND the bottom
    flat_frac=0.5,
    expand_frac=1.35,              # going back in time may widen the box by at most 35%                 # start vs end of the box within half its height (sideways, not a trend)
    uc_max_adr=1.5,                # undercut no deeper than 1.5x ADR below the box low
    reclaim_days=3,                # back inside the box within this many sessions
    spring_vol=1.5,                # spring: undercut-day volume vs 20-day average
    fbbo_days=10,                  # failed breakdown -> breakout within this many sessions
    top_fail_days=5,               # top shakeout: back inside within this many sessions
    top_rebreak_days=10,           # ... and a new close above the high within this many
    ema_reclaim_days=3,            # EMA shakeout: at most this many closes below
    ema9_above_before=5,           # ... after at least this many closes above the 9 EMA
    ema20_above_before=10,         # ... or above the 20 EMA
    lookback=15,                   # "a shakeout in the last N sessions" for the breakout playbook
    near_hi_pct=3.0,               # "near the box high" = within this % below it
    horizons=(5, 10, 20),
    evidence_days=500,             # evidence uses roughly the last two years
)
PARAMS = {"IN": dict(BASE), "US": dict(BASE)}

# ── the playbooks that passed the study (29 Sep 2026), one list per market ──
# Each rule: event type, conditions on that event, and whether the stock must be
# in the top 30% of its market by 3-month return. A stock qualifies if such an
# event happened today or in the last SIGNAL_AGE sessions. Rules that showed no
# edge are simply not listed for that market.
SIGNAL_AGE = 2
PLAYBOOKS = {
    "IN": [
        dict(id="in_ema20v", type="ema20", rv=1.5),
        dict(id="in_ema20v_rs", type="ema20", rv=1.5, rs=True),
        dict(id="in_box_ur", type="ur", rally=20, uc=0.5, days=0, rv=1.0),
    ],
    "US": [
        dict(id="us_ema9v", type="ema9", rv=1.5),
        dict(id="us_ema9v_rs", type="ema9", rv=1.5, rs=True),
        dict(id="us_fbbo", type="fbbo", rally=30),
        dict(id="us_shkbo", type="shkbo", rally=30, box_len=15, rv=1.0),
        dict(id="us_box_ur", type="ur", rally=30, box_len=15, touches=3, uc=1.0, days=1),
        dict(id="us_spring", type="spring"),
    ],
}


def rule_hits(market, events, n, rs_top):
    """Which of this market's playbooks the stock qualifies for today."""
    hits = []
    for rule in PLAYBOOKS[market]:
        for i, typ, ex in reversed(events):
            if n - 1 - i > SIGNAL_AGE:
                break
            if typ != rule["type"] or not ex.get("trend"):
                continue
            if rule.get("rv") and (ex.get("rv") or 0) < rule["rv"]:
                continue
            if rule.get("rally") and (ex.get("rally") or 0) < rule["rally"]:
                continue
            if rule.get("box_len") and (ex.get("box_len") or 0) < rule["box_len"]:
                continue
            if rule.get("touches") and min(ex.get("box_th") or 0, ex.get("box_tl") or 0) < rule["touches"]:
                continue
            if rule.get("uc") is not None and (ex.get("depth") is None or ex["depth"] > rule["uc"]):
                continue
            if rule.get("days") is not None and (ex.get("days") is None or ex["days"] > rule["days"]):
                continue
            if rule.get("rs") and not rs_top:
                continue
            hits.append(rule["id"])
            break
    return hits
TABLES = {"IN": ("momentum_stocks", "daily_ohlc", "shakeout_state", "shakeout_evidence"),
          "US": ("us_momentum_stocks", "us_daily_ohlc", "us_shakeout_state", "us_shakeout_evidence")}


def log(m):
    print(m, flush=True)


def get(path):
    for a in range(4):
        try:
            r = requests.get(CFG.SUPABASE_URL + "/rest/v1/" + path, headers=H, timeout=90)
            if r.status_code == 200:
                return r.json()
            log("  read %s -> %s %s" % (path[:50], r.status_code, r.text[:100]))
        except Exception as e:
            log("  read error: %s" % str(e)[:100])
        time.sleep(2 * (a + 1))
    return None


def upsert(table, rows, conflict):
    n = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(CFG.SUPABASE_URL + "/rest/v1/%s?on_conflict=%s" % (table, conflict),
                          headers=dict(H, Prefer="resolution=merge-duplicates,return=minimal"),
                          json=chunk, timeout=120)
        if r.status_code >= 300:
            raise SystemExit("%s write failed %s: %s" % (table, r.status_code, r.text[:200]))
        n += len(chunk)
    return n


def ema(vals, n):
    k, out, e = 2.0 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def analyse(bars, P):
    """bars: list of dicts d,o,h,l,c,v (oldest first). Returns (events, state).
    Walks forward one day at a time and only ever looks at the past."""
    n = len(bars)
    if n < 80:
        return [], None
    o = [b["o"] for b in bars]; h = [b["h"] for b in bars]; l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]; v = [b["v"] or 0 for b in bars]
    e9, e20, e50 = ema(c, 9), ema(c, 20), ema(c, 50)
    adr = [None] * n
    for i in range(20, n):
        adr[i] = sum((h[j] - l[j]) / l[j] * 100 for j in range(i - 19, i + 1) if l[j] > 0) / 20.0
    v20 = [None] * n
    for i in range(20, n):
        v20[i] = sum(v[i - 20:i]) / 20.0

    def trend_ok(i):
        return i >= 60 and c[i] > e50[i] and e50[i] > e50[i - 10]

    def find_box(end):
        """Longest valid box ending at `end` (inclusive), or None."""
        a = adr[end]
        if a is None:
            return None
        lim = min(P["box_depth_max"], P["box_depth_adr"] * a)
        # the box keeps the shape of its most recent stretch: going further back
        # may widen it only a little (stops the approach into the box being counted)
        cs = max(0, end - P["box_min"] + 1)
        core_hi, core_lo = max(h[cs:end + 1]), min(l[cs:end + 1])
        if core_lo > 0:
            lim = min(lim, (core_hi - core_lo) / core_lo * 100 * P["expand_frac"])
        hi, lo, best = -1e18, 1e18, None
        for s in range(end, max(-1, end - P["box_max"]), -1):
            hi = max(hi, h[s]); lo = min(lo, l[s])
            if lo <= 0 or (hi - lo) / lo * 100 > lim:
                break
            # a box is sideways: the start and the end of the window sit at a
            # similar level (otherwise it is the tail of a trend, not a box)
            if end - s + 1 >= P["box_min"] and \
                    abs(sum(c[s:s + 3]) / 3.0 - sum(c[end - 2:end + 1]) / 3.0) <= P["flat_frac"] * (hi - lo):
                best = (s, hi, lo)
        if not best:
            return None
        s, hi, lo = best
        tol = P["touch_tol_adr"] * a / 100.0 * c[end]
        def touches(vals, edge, up):
            t, last = 0, -99
            for j in range(s, end + 1):
                near = (vals[j] >= edge - tol) if up else (vals[j] <= edge + tol)
                if near and j - last >= 3:
                    t += 1; last = j
            return t
        th, tl = touches(h, hi, True), touches(l, lo, False)
        if th < P["touches"] or tl < P["touches"]:
            return None
        return {"s": s, "e": end, "hi": hi, "lo": lo, "th": th, "tl": tl}

    events = []                  # (i, type, extra)
    last_shake = [None, None]    # day and low of the latest shakeout of any kind

    def add(i, typ, extra):
        extra["trend"] = bool(trend_ok(i))
        # context for the rule study (ignored by the nightly state)
        extra["rv"] = (v[i] / v20[i]) if v20[i] else None
        extra["d"] = bars[i]["d"]
        bx = box
        if bx:
            a0 = adr[bx["e"]] or 1
            s0 = bx["s"]
            pre_lo = min(l[max(0, s0 - 60):s0 + 1])
            extra.update(box_len=bx["e"] - bx["s"] + 1, box_th=bx["th"], box_tl=bx["tl"],
                         box_depth_adr=(bx["hi"] - bx["lo"]) / bx["lo"] * 100 / a0,
                         rally=(bx["hi"] / pre_lo - 1) * 100 if pre_lo > 0 else None)
        if typ in ("shkbo",) and last_shake[1] is not None:
            extra["low"] = last_shake[1]
        events.append((i, typ, extra))
        if typ in ("ema9", "ema20", "ur", "spring", "top"):
            last_shake[0] = i
            last_shake[1] = extra.get("low")
    box = None                   # the box as it stood before anything broke it
    uc = None                    # active undercut: {start, low}
    brk = None                   # active top breakout: {day}
    fail = None                  # top: fell back inside {day}
    last_ur = None               # last U&R day (for failed breakdown -> breakout)
    ref_box = None               # the last box seen, kept after it breaks (for today's state)
    hold_until = -1              # after a U&R keep the ORIGINAL box (don't let the undercut widen it)
    broke_down = None            # day the box failed for real
    below9 = below20 = 0
    for i in range(60, n):
        # ── EMA shakeouts ─────────────────────────────
        if c[i] < e9[i]:
            below9 += 1
        else:
            if 1 <= below9 <= P["ema_reclaim_days"]:
                s0 = i - below9
                if s0 - P["ema9_above_before"] >= 0 and all(c[j] > e9[j] for j in range(s0 - P["ema9_above_before"], s0)) \
                        and e9[s0 - 1] > e20[s0 - 1]:
                    add(i, "ema9", {"low": min(l[s0:i + 1])})
            below9 = 0
        if c[i] < e20[i]:
            below20 += 1
        else:
            if 1 <= below20 <= P["ema_reclaim_days"]:
                s0 = i - below20
                if s0 - P["ema20_above_before"] >= 0 and all(c[j] > e20[j] for j in range(s0 - P["ema20_above_before"], s0)):
                    add(i, "ema20", {"low": min(l[s0:i + 1])})
            below20 = 0
        # ── box shakeouts ─────────────────────────────
        if uc is None and brk is None and fail is None:
            nb = find_box(i - 1) if i > hold_until or box is None else None
            if nb or box is None:          # nothing in progress: keep the box up to date
                box = nb
                if nb:
                    broke_down = None
            if box:
                ref_box = box
        if box is None:
            continue
        a = adr[i] or 1
        if uc is None and brk is None and fail is None:
            if l[i] < box["lo"] and (last_ur is None or i - last_ur > P["reclaim_days"]):
                uc = {"start": i, "low": l[i], "vol": v[i], "upper": (c[i] - l[i]) >= 0.5 * (h[i] - l[i])}
            elif c[i] > box["hi"]:
                brk = {"day": i}
                if last_ur is not None and i - last_ur <= P["fbbo_days"]:
                    add(i, "fbbo", {"low": box["lo"]})
                    last_ur = None
                # THE pattern: any shakeout in the last N sessions, then the box breaks
                if last_shake[0] is not None and i - last_shake[0] <= P["lookback"]:
                    add(i, "shkbo", {"low": box["lo"]})
                continue
            else:
                continue
        if uc is not None:
            uc["low"] = min(uc["low"], l[i])
            depth_adr = (box["lo"] - uc["low"]) / box["lo"] * 100 / a
            if depth_adr > P["uc_max_adr"]:
                uc = None; box = None; last_ur = None          # a real breakdown, not a shakeout
                broke_down = i
                continue
            if c[i] >= box["lo"]:
                vr = (uc["vol"] / v20[uc["start"]]) if v20[uc["start"]] else None
                spring = vr is not None and vr >= P["spring_vol"] and uc["upper"]
                add(i, "spring" if spring else "ur",
                               {"low": uc["low"], "depth": depth_adr, "days": i - uc["start"],
                                "rvol": (v[i] / v20[i]) if v20[i] else None})
                last_ur = i; uc = None
                hold_until = i + P["lookback"]
            elif i - uc["start"] >= P["reclaim_days"]:
                uc = None; box = None; last_ur = None
                broke_down = i
            continue
        if brk is not None:
            if c[i] <= box["hi"]:
                if c[i] >= box["lo"] and i - brk["day"] <= P["top_fail_days"]:
                    fail = {"day": i, "low": l[i]}
                else:
                    box = None
                brk = None
            elif i - brk["day"] > P["top_fail_days"]:
                brk = None; box = None                          # breakout held - the box is done
            continue
        if fail is not None:
            fail["low"] = min(fail["low"], l[i])
            if c[i] > box["hi"]:
                add(i, "top", {"low": fail["low"]})
                fail = None; box = None
            elif c[i] < box["lo"] or i - fail["day"] > P["top_rebreak_days"]:
                fail = None; box = None
    # ── the pattern this is for: any shakeout, then the box breaks ─────
    # fbbo is the breakout itself - the shakeout behind it is the U&R before it
    types = {"ema9", "ema20", "ur", "spring", "top"}   # shkbo / fbbo are breakouts, not shakeouts
    ev_days = [e for e in events if e[1] in types]
    # today's state
    t = n - 1
    cur = ref_box if (ref_box and t - ref_box["e"] <= P["box_max"]) else find_box(t - 1)
    last = ev_days[-1] if ev_days else None
    age = (t - last[0]) if last else None
    st = {"session_date": bars[t]["d"], "close": round(c[t], 4),
          "adr_pct": round(adr[t], 2) if adr[t] else None, "trend_ok": bool(trend_ok(t)),
          "box_hi": None, "box_lo": None, "box_days": None, "box_touch_hi": None, "box_touch_lo": None,
          "box_state": None, "shake_type": last[1] if last else None,
          "shake_date": bars[last[0]]["d"] if last else None, "shake_age": age,
          "shake_low": round(last[2].get("low"), 4) if last and last[2].get("low") else None,
          "uc_depth_adr": round(last[2]["depth"], 2) if last and last[2].get("depth") is not None else None,
          "reclaim_rvol": round(last[2]["rvol"], 2) if last and last[2].get("rvol") else None,
          "shk_bo": False, "shk_near": False}
    # the box the stock is in now: before today's bar (so today's break can be seen)
    bx = cur
    if bx:
        st.update(box_hi=round(bx["hi"], 4), box_lo=round(bx["lo"], 4), box_days=bx["e"] - bx["s"] + 1,
                  box_touch_hi=bx["th"], box_touch_lo=bx["tl"])
        a = adr[t] or 1
        recent_shake = last is not None and age <= P["lookback"]
        if broke_down is not None and ref_box is bx and c[t] < bx["lo"]:
            st["box_state"] = "breakdown"
        elif c[t] > bx["hi"]:
            st["box_state"] = "broke"
            st["shk_bo"] = bool(recent_shake and c[t - 1] <= bx["hi"])
        elif l[t] < bx["lo"] and c[t] < bx["lo"]:
            st["box_state"] = "undercut"
        elif c[t] >= bx["hi"] * (1 - P["near_hi_pct"] / 100.0):
            st["box_state"] = "nearhi"
        elif last and last[1] in ("ur", "spring") and age <= 5:
            st["box_state"] = "reclaimed"
        else:
            st["box_state"] = "inbox"
        st["shk_near"] = bool(recent_shake and st["box_state"] in ("nearhi",))
    elif last and last[1] in ("ur", "spring") and age == 0:
        st["box_state"] = "reclaimed"
    # "broke" also covers a break in the last 3 sessions after a shakeout
    return events, st


def evidence(bars, events, P, acc):
    n = len(bars)
    lo_i = max(0, n - P["evidence_days"])
    for i, typ, ex in events:
        if i < lo_i or i + 1 >= n:
            continue
        keys = [typ] + ([typ + "_trend"] if ex.get("trend") else [])
        entry = bars[i + 1]["o"] or bars[i + 1]["c"]
        if not entry:
            continue
        for hz in P["horizons"]:
            j = i + hz
            if j < n:
                for k in keys:
                    acc.setdefault((k, hz), []).append((bars[j]["c"] / entry - 1) * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["IN", "US"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", default="")
    a = ap.parse_args()
    P = PARAMS[a.market]
    uni_t, ohlc_t, state_t, ev_t = TABLES[a.market]
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
    log("Shakeout scan %s | %d symbols | universe %s" % (a.market, len(syms), sd))

    def load(sym):
        rows = get("%s?symbol=eq.%s&select=d,o,h,l,c,v&order=d.asc&limit=1000"
                   % (ohlc_t, requests.utils.quote(sym, safe=""))) or []
        return [r for r in rows if r.get("c") and r.get("h") and r.get("l") and r.get("o")]

    with ThreadPoolExecutor(8) as ex:
        data = dict(zip(syms, ex.map(load, syms)))
    log("prices loaded in %.0fs" % (time.time() - t0))

    # stock strength today: top 30% of the market by 3-month (63-session) return
    r63 = []
    for sym in syms:
        b = data.get(sym) or []
        if len(b) > 63 and b[-64]["c"]:
            r63.append((b[-1]["c"] / b[-64]["c"] - 1, sym))
    r63.sort(reverse=True)
    rs_top = set(s for _, s in r63[:max(1, int(len(r63) * 0.3))])

    rows, acc, cnt = [], {}, {}
    for sym in syms:
        bars = data.get(sym) or []
        try:
            events, st = analyse(bars, P)
        except Exception as e:
            log("  %s: %s" % (sym, str(e)[:100]))
            continue
        if not st:
            continue
        st["symbol"] = sym
        st["computed_at"] = datetime.now(timezone.utc).isoformat()
        st["rs_top30"] = sym in rs_top
        hits = rule_hits(a.market, events, len(bars), sym in rs_top)
        st["pb_hits"] = ",".join(hits) if hits else None
        for h in hits:
            cnt[("playbook", h)] = cnt.get(("playbook", h), 0) + 1
        rows.append(st)
        evidence(bars, events, P, acc)
        for k in ("box_state", "shake_type"):
            if st.get(k):
                cnt[(k, st[k])] = cnt.get((k, st[k]), 0) + 1
        if st["shk_bo"]:
            cnt[("playbook", "shakeout->breakout")] = cnt.get(("playbook", "shakeout->breakout"), 0) + 1
        if a.symbols:
            log("  %s %s" % (sym, {k: st[k] for k in ("box_state", "box_hi", "box_lo", "box_days", "shake_type",
                                                    "shake_age", "shake_low", "shk_bo", "shk_near", "trend_ok")}))
    today = datetime.now(timezone.utc).date().isoformat()
    ev = []
    for (typ, hz), vals in sorted(acc.items()):
        if len(vals) < 5:
            continue
        ev.append({"type": typ, "horizon": hz, "n": len(vals), "median_pct": round(median(vals), 2),
                   "mean_pct": round(sum(vals) / len(vals), 2),
                   "win_pct": round(100.0 * sum(1 for x in vals if x > 0) / len(vals), 1),
                   "computed_on": today})
    log("ROWS %d" % len(rows))
    for k in sorted(cnt):
        log("   %-12s %-22s %d" % (k[0], k[1], cnt[k]))
    for r in ev:
        if r["horizon"] == 10:
            log("   evidence %-7s n=%-5d 10d median %+.2f%%  up %.0f%%" % (r["type"], r["n"], r["median_pct"], r["win_pct"]))
    if a.dry_run:
        log("DRY RUN - nothing written (%.0fs)" % (time.time() - t0))
        return
    w1 = upsert(state_t, rows, "symbol")
    w2 = upsert(ev_t, ev, "type,horizon") if ev else 0
    log("WROTE %s %d · %s %d (%.0fs)" % (state_t, w1, ev_t, w2, time.time() - t0))


if __name__ == "__main__":
    main()
