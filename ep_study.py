#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
ep_study.py - which EP / Momentum Ignition / Delayed EP rules actually work? (read-only)

Uses ep_scan.py's own detection, so the study and the dashboard agree.

  * judged as trades: enter next open, stop = event low (EP / ignition) or base low
    (delayed EP), exit at the stop or after 10 sessions; stops kept 0.5-4 ADR away
  * compared with a plain uptrend entry ON THE SAME KIND OF MARKET DAY
  * MARKET REGIME: every signal is tagged with that day's breadth (strong >= 55%
    of stocks above the 20 EMA, weak < 45%, neutral between) - choppy, weak
    stretches like the recent one are judged on their own, not mixed in
  * TIME: "last 12 months" vs "before" - a rule must work in both
  * prints a sanity list of the most recent delayed-EP breakouts to eyeball on charts
Nothing is written to the database. Report + CSV in /root/trueflow/study/.

USAGE  /root/trueflow/bin/python ep_study.py --market IN
       /root/trueflow/bin/python ep_study.py --market US
"""
import os, sys, time, argparse, itertools
from datetime import datetime, timezone, date, timedelta
from concurrent.futures import ThreadPoolExecutor
import requests

sys.path.insert(0, "/root/trueflow")
import tf_config as CFG
import ep_scan as E

OUT = "/root/trueflow/study"
HOLD, MIN_N, MIN_PART = 10, 80, 30


def log(m):
    print(m, flush=True)


def tg(chat, msg):
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG.TF_ALERTS_TOKEN,
                      data={"chat_id": chat, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        log("telegram failed: %s" % e)


def trade(bars, i, stop, adr_pct):
    n = len(bars)
    if i + 1 + HOLD >= n or stop is None:
        return None
    entry = bars[i + 1]["o"] or bars[i + 1]["c"]
    if not entry:
        return None
    adr = (adr_pct or 2.0) / 100.0 * entry
    stop = min(stop, entry - 0.5 * adr)
    if entry - stop > 4 * adr:
        return None
    for j in range(i + 1, i + 1 + HOLD):
        b = bars[j]
        if b["o"] <= stop:
            return (b["o"] / entry - 1) * 100
        if b["l"] <= stop:
            return (stop / entry - 1) * 100
    return (bars[i + HOLD]["c"] / entry - 1) * 100


def mean(x):
    return sum(x) / len(x) if x else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["IN", "US"])
    a = ap.parse_args()
    P = E.PARAMS[a.market]; uni_t, ohlc_t, er_t, _, _ = E.TABLES[a.market]
    t0 = time.time()
    sd = E.get("%s?select=session_date&order=session_date.desc&limit=1" % uni_t)[0]["session_date"]
    syms, off = [], 0
    while True:
        p = E.get("%s?select=symbol&session_date=eq.%s&order=symbol.asc&limit=1000&offset=%d" % (uni_t, sd, off)) or []
        syms += [x["symbol"] for x in p]
        if len(p) < 1000:
            break
        off += 1000
    res, off = {}, 0
    while True:
        p = E.get("%s?select=symbol,result_date&limit=1000&offset=%d" % (er_t, off)) or []
        for x in p:
            if x.get("result_date"):
                res.setdefault(x["symbol"], set()).add(str(x["result_date"])[:10])
        if len(p) < 1000:
            break
        off += 1000
    reg, off = {}, 0
    while True:
        p = E.get("market_history?market=eq.%s&select=d,pct20&order=d.asc&limit=1000&offset=%d" % (a.market, off)) or []
        for x in p:
            if x.get("pct20") is not None:
                v = float(x["pct20"]); reg[str(x["d"])] = "strong" if v >= 55 else ("weak" if v < 45 else "neutral")
        if len(p) < 1000:
            break
        off += 1000

    def load(sym):
        rows = E.get("%s?symbol=eq.%s&select=d,o,h,l,c,v&order=d.asc&limit=1000" % (ohlc_t, requests.utils.quote(sym, safe=""))) or []
        return [r for r in rows if r.get("c") and r.get("h") and r.get("l") and r.get("o")]

    with ThreadPoolExecutor(8) as ex:
        data = dict(zip(syms, ex.map(load, syms)))
    last_day = max((b[-1]["d"] for b in data.values() if b), default=sd)
    cut = (datetime.strptime(last_day, "%Y-%m-%d").date() - timedelta(days=365)).isoformat()
    log("EP study %s | %d stocks · regime days %d · last 12 months from %s (%.0fs)" % (a.market, len(data), len(reg), cut, time.time() - t0))

    S, BASEL, recent = [], [], []
    for sym, bars in data.items():
        try:
            events, _ = E.analyse(bars, P, res.get(sym, set()))
        except Exception:
            continue
        for ev in events:
            d = bars[ev["i"]]["d"]
            r = trade(bars, ev["i"], ev["low"], ev.get("adr"))
            if r is not None:
                S.append(dict(kind=ev["typ"], d=d, r=r, reg=reg.get(d), neg=ev["neglected"], move=ev["move"], rvol=ev["rvol"],
                              trend=ev["trend"], run=ev.get("run", 1), pos=ev.get("pos")))
            for tr in ev.get("_trigs", []):
                j = tr["j"]; dj = bars[j]["d"]
                rr = trade(bars, j, tr["stop"], ev.get("adr"))
                row = dict(kind="delayed", parent=ev["typ"], d=dj, r=rr, reg=reg.get(dj), neg=ev["neglected"], nth=tr["nth"],
                           base_len=tr["base_len"], depth=tr["depth_adr"], rvol=tr["rvol"], trend=tr["trend"])
                if rr is not None:
                    S.append(row)
                if tr["nth"] == 1:
                    recent.append(dict(row, sym=sym, ev_d=d, ev_typ=ev["typ"], ev_move=ev["move"]))
        c = [b["c"] for b in bars]; e50 = E.ema(c, 50)
        for i in range(70, len(bars) - HOLD - 1, 5):
            if c[i] > e50[i] and e50[i] > e50[i - 10]:
                adr = sum((bars[k]["h"] - bars[k]["l"]) / bars[k]["l"] for k in range(i - 19, i + 1)) / 20.0 * 100
                r = trade(bars, i, bars[i + 1]["o"] * (1 - 1.5 * adr / 100) if bars[i + 1]["o"] else None, adr)
                if r is not None:
                    BASEL.append(dict(d=bars[i]["d"], r=r, reg=reg.get(bars[i]["d"])))
    log("%d signals, %d baseline samples (%.0fs)" % (len(S), len(BASEL), time.time() - t0))

    def bmean(filt):
        return mean([b["r"] for b in BASEL if filt(b)]) or 0.0
    BM = {}
    for part in ("old", "new"):
        for rg in ("strong", "neutral", "weak", None):
            BM[(part, rg)] = bmean(lambda b: (b["d"] >= cut) == (part == "new") and b["reg"] == rg)

    def score(rows):
        """edge vs the baseline on the same kind of day, per time part and per regime."""
        out = {}
        for part in ("old", "new"):
            rp = [x for x in rows if (x["d"] >= cut) == (part == "new")]
            out[part] = (len(rp), mean([x["r"] - BM[(part, x["reg"])] for x in rp]) if rp else None)
        for rg in ("strong", "neutral", "weak"):
            rr = [x for x in rows if x["reg"] == rg]
            out[rg] = (len(rr), mean([x["r"] - BM[("new" if x["d"] >= cut else "old", rg)] for x in rr]) if rr else None)
        ex = [x["r"] - BM[("new" if x["d"] >= cut else "old", x["reg"])] for x in rows]
        out["all"] = (len(rows), mean(ex) if rows else None)
        if len(ex) > 2:
            m = mean(ex); sd = (sum((e - m) ** 2 for e in ex) / (len(ex) - 1)) ** 0.5
            out["t"] = m / (sd / len(ex) ** 0.5) if sd > 0 else 0.0
        else:
            out["t"] = 0.0
        out["win"] = 100.0 * sum(1 for x in rows if x["r"] > 0) / len(rows) if rows else None
        return out

    GRIDS = {
        "gap": dict(neg=[None, True], move=[None, 6, 8], rvol=[None, 3], trend=[None, True]),
        "ignition": dict(run=[None, 1, "multi"], neg=[None, True], rvol=[None, 3], trend=[None, True]),
        "delayed": dict(parent=[None, "gap", "ignition"], nth=[None, 1], base_len=[None, 5, 10], depth=[None, 3, 5],
                        rvol=[None, 1.5], trend=[None, True], neg=[None, True]),
    }
    results = []
    for kind, grid in GRIDS.items():
        base_rows = [x for x in S if x["kind"] == kind]
        keys = list(grid)
        for combo in itertools.product(*[grid[k] for k in keys]):
            rows, desc = base_rows, []
            for k, val in zip(keys, combo):
                if val is None:
                    continue
                if k == "neg": rows = [x for x in rows if x["neg"]]; desc.append("from neglect")
                elif k == "move": rows = [x for x in rows if (x["move"] or 0) >= val]; desc.append("gap≥%d%%" % val)
                elif k == "rvol": rows = [x for x in rows if (x["rvol"] or 0) >= val]; desc.append("vol≥%gx" % val)
                elif k == "trend": rows = [x for x in rows if x["trend"]]; desc.append("uptrend")
                elif k == "run": rows = [x for x in rows if (x["run"] == 1) == (val == 1)]; desc.append("1-day" if val == 1 else "2-3 day run")
                elif k == "parent": rows = [x for x in rows if x["parent"] == val]; desc.append("after " + val)
                elif k == "nth": rows = [x for x in rows if x["nth"] == 1]; desc.append("1st breakout only")
                elif k == "base_len": rows = [x for x in rows if (x["base_len"] or 0) >= val]; desc.append("base≥%dd" % val)
                elif k == "depth": rows = [x for x in rows if x["depth"] is not None and x["depth"] <= val]; desc.append("base≤%dxADR" % val)
            sc = score(rows)
            if sc["all"][0] < MIN_N or sc["old"][0] < MIN_PART or sc["new"][0] < MIN_PART:
                continue
            results.append(dict(kind=kind, rule=" · ".join(desc) or "(as built)", sc=sc))
    log("%d versions with enough signals (%.0fs)" % (len(results), time.time() - t0))

    def f(v):
        return "  n/a " if v is None else "%+.2f%%" % v
    L = ["EP STUDY - %s - %s" % (a.market, datetime.now(timezone.utc).strftime("%Y-%m-%d")),
         "Trades: next open entry, stop = event low / base low (0.5-4 ADR), exit at stop or %d sessions. No costs." % HOLD,
         "Edge = return minus a plain uptrend entry on the SAME KIND OF MARKET DAY. Regime = breadth that day.",
         "PASS = edge > 0 before AND in the last 12 months, > 0 on strong+neutral days combined, and not noise (t >= 2).", ""]
    tgl = []
    for kind in GRIDS:
        rs = [r for r in results if r["kind"] == kind]
        if not rs:
            L.append("== %s: not enough signals" % kind); continue
        def ok(r):
            sc = r["sc"]
            sn = [x for x in S if False]
            return sc["old"][1] is not None and sc["new"][1] is not None and sc["old"][1] > 0 and sc["new"][1] > 0 and sc["t"] >= 2.0 and \
                   ((sc["strong"][1] or 0) * sc["strong"][0] + (sc["neutral"][1] or 0) * sc["neutral"][0]) > 0
        good = sorted([r for r in rs if ok(r)], key=lambda r: -min(r["sc"]["old"][1], r["sc"]["new"][1]))
        L.append("== %s: %d versions tested, %d pass" % (kind.upper(), len(rs), len(good)))
        L.append("   %-58s %6s %8s %5s %8s %8s %8s %8s %8s %5s" % ("rule", "n", "all", "t", "before", "last12m", "strong", "neutral", "weak", "win"))
        loose = [r for r in rs if r["rule"] == "(as built)"]
        for r in loose + good[:8]:
            sc = r["sc"]
            L.append("   %-58s %6d %8s %5.1f %8s %8s %8s %8s %8s %4.0f%%" % (("now: " if r in loose else "+ ") + r["rule"], sc["all"][0], f(sc["all"][1]), sc["t"], f(sc["old"][1]),
                     f(sc["new"][1]), f(sc["strong"][1]), f(sc["neutral"][1]), f(sc["weak"][1]), sc["win"] or 0))
        if good:
            g = good[0]["sc"]
            tgl.append("✅ <b>%s</b>: %s — %+.2f%% before, %+.2f%% last 12m, weak days %s" % (kind, good[0]["rule"], g["old"][1], g["new"][1], f(g["weak"][1])))
        else:
            tgl.append("✖ <b>%s</b>: no version passed" % kind)
        L.append("")
    recent.sort(key=lambda x: x["d"], reverse=True)
    L.append("== SANITY LIST: 15 most recent 1st delayed-EP breakouts (check these on charts)")
    L.append("   %-12s %-11s %-9s %7s  %-11s %5s %6s %5s %-6s %-8s %s" % ("symbol", "event", "type", "move", "breakout", "base", "depth", "vol", "trend", "regime", "10d"))
    for x in recent[:15]:
        L.append("   %-12s %-11s %-9s %+6.1f%%  %-11s %4dd %5.1fx %4.1fx %-6s %-8s %s" % (x["sym"], x["ev_d"], x["ev_typ"], x["ev_move"] or 0, x["d"], x["base_len"],
                 x["depth"] or 0, x["rvol"] or 0, "yes" if x["trend"] else "no", x["reg"] or "-", ("%+.1f%%" % x["r"]) if x["r"] is not None else "open"))
    rep = "\n".join(L)
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "ep_study_%s_%s.txt" % (a.market, datetime.now(timezone.utc).strftime("%Y%m%d")))
    open(p, "w").write(rep)
    print("\n" + rep)
    log("report: %s (%.0f min)" % (p, (time.time() - t0) / 60))
    tg(CFG.TF_SYSTEM if a.market == "IN" else CFG.TF_US, "🔬 <b>EP study · %s</b>\nRegime-aware; details in study/%s\n\n" % ("India" if a.market == "IN" else "US", os.path.basename(p)) + "\n".join(tgl))


if __name__ == "__main__":
    main()
