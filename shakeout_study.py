#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
shakeout_study.py - which shakeout rules actually work? (read-only)

For one market at a time it:
  1. finds every shakeout / breakout event in the stored daily prices, using
     shakeout_scan.py's own detection (so the study and the dashboard agree)
  2. tries tighter versions of each rule (prior rally, box quality, undercut
     depth and speed, volume, market breadth, stock strength)
  3. judges every version as trades: enter next open, stop = shakeout low,
     exit at the stop or after 10 sessions
  4. keeps only versions that work in BOTH halves of the history, with at
     least 100 trades, and compares them with a plain baseline
Nothing is written to the database. Results: a text report + CSV in
/root/trueflow/study/, and a short summary on Telegram
(India -> TrueFlow System, US -> TrueFlow US).

USAGE  /root/trueflow/bin/python shakeout_study.py --market IN
       /root/trueflow/bin/python shakeout_study.py --market US
"""
import os, sys, time, argparse, itertools, json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import requests

sys.path.insert(0, "/root/trueflow")
import tf_config as CFG
import shakeout_scan as SK

OUT = "/root/trueflow/study"
HOLD = 10
MIN_N, MIN_HALF = 100, 40


def log(m):
    print(m, flush=True)


def tg(chat, msg):
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % CFG.TF_ALERTS_TOKEN,
                      data={"chat_id": chat, "text": msg, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        log("telegram failed: %s" % e)


def trade(bars, i, stop):
    """Enter next open; exit at the stop or after HOLD sessions. Returns (R, ret10%) or None.
    The stop is kept between 0.5 and 3 ADR below the entry: a stop hugging the entry
    makes R meaningless, and one miles away is not a shakeout trade."""
    n = len(bars)
    if i + 1 + HOLD >= n or stop is None or i < 20:
        return None
    entry = bars[i + 1]["o"] or bars[i + 1]["c"]
    if not entry:
        return None
    adr = sum((bars[j]["h"] - bars[j]["l"]) / bars[j]["l"] for j in range(i - 19, i + 1)) / 20.0 * entry
    stop = min(stop, entry - 0.5 * adr)
    if entry - stop > 3 * adr:
        return None
    risk = entry - stop
    if risk <= 0:
        return None
    for j in range(i + 1, i + 1 + HOLD):
        b = bars[j]
        if b["o"] <= stop:                       # gapped through the stop
            return ((b["o"] - entry) / risk, (b["o"] / entry - 1) * 100)
        if b["l"] <= stop:
            return (-1.0, (stop / entry - 1) * 100)
    c = bars[i + HOLD]["c"]
    return ((c - entry) / risk, (c / entry - 1) * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=["IN", "US"])
    a = ap.parse_args()
    P = SK.PARAMS[a.market]
    uni_t, ohlc_t, _, _ = SK.TABLES[a.market]
    t0 = time.time()
    sd = SK.get("%s?select=session_date&order=session_date.desc&limit=1" % uni_t)[0]["session_date"]
    syms, off = [], 0
    while True:
        p = SK.get("%s?select=symbol&session_date=eq.%s&order=symbol.asc&limit=1000&offset=%d" % (uni_t, sd, off)) or []
        syms += [x["symbol"] for x in p]
        if len(p) < 1000:
            break
        off += 1000

    def load(sym):
        rows = SK.get("%s?symbol=eq.%s&select=d,o,h,l,c,v&order=d.asc&limit=1000"
                      % (ohlc_t, requests.utils.quote(sym, safe=""))) or []
        return [r for r in rows if r.get("c") and r.get("h") and r.get("l") and r.get("o")]

    with ThreadPoolExecutor(8) as ex:
        data = dict(zip(syms, ex.map(load, syms)))
    log("Study %s | %d symbols loaded (%.0fs)" % (a.market, len(data), time.time() - t0))

    # ── market breadth and stock strength, per date ───────────────────
    above50, total, ret63 = {}, {}, {}
    for sym, bars in data.items():
        if len(bars) < 80:
            continue
        c = [b["c"] for b in bars]
        e50 = SK.ema(c, 50)
        for i in range(60, len(bars)):
            d = bars[i]["d"]
            total[d] = total.get(d, 0) + 1
            if c[i] > e50[i]:
                above50[d] = above50.get(d, 0) + 1
            if i >= 63:
                ret63.setdefault(d, []).append((c[i] / c[i - 63] - 1, sym))
    breadth = {d: 100.0 * above50.get(d, 0) / total[d] for d in total if total[d] >= 50}
    top30 = {}
    for d, lst in ret63.items():
        if len(lst) >= 50:
            lst.sort(reverse=True)
            top30[d] = set(s for _, s in lst[:max(1, int(len(lst) * 0.3))])

    # ── events + baseline ─────────────────────────────────────────────
    rows, base = [], []
    for sym, bars in data.items():
        try:
            events, _ = SK.analyse(bars, P)
        except Exception:
            continue
        for i, typ, ex in events:
            if not ex.get("trend"):
                continue
            t = trade(bars, i, ex.get("low"))
            if not t:
                continue
            d = ex.get("d")
            rows.append(dict(type=typ, d=d, R=t[0], r10=t[1], rv=ex.get("rv"),
                             rally=ex.get("rally"), box_len=ex.get("box_len"),
                             touches=min(ex.get("box_th") or 0, ex.get("box_tl") or 0),
                             depth=ex.get("box_depth_adr"), uc=ex.get("depth"), days=ex.get("days"),
                             breadth=breadth.get(d), strong=(sym in top30.get(d, ()))))
        # baseline: every 5th uptrend day, same trade rules with a 1.5 ADR stop
        c = [b["c"] for b in bars]
        if len(bars) < 80:
            continue
        e50 = SK.ema(c, 50)
        for i in range(70, len(bars) - HOLD - 1, 5):
            if not (c[i] > e50[i] and e50[i] > e50[i - 10]):
                continue
            adr = sum((bars[j]["h"] - bars[j]["l"]) / bars[j]["l"] for j in range(i - 19, i + 1)) / 20.0
            t = trade(bars, i, bars[i + 1]["o"] * (1 - 1.5 * adr) if bars[i + 1]["o"] else None)
            if t:
                base.append((bars[i]["d"], t[0], t[1], breadth.get(bars[i]["d"])))
    log("%d trend events, %d baseline samples (%.0fs)" % (len(rows), len(base), time.time() - t0))
    if not rows:
        log("No events - nothing to study.")
        return
    dates = sorted(set(r["d"] for r in rows))
    mid = dates[len(dates) // 2]
    log("halves split at %s" % mid)

    def arr(k, dflt=np.nan):
        return np.array([(r[k] if r[k] is not None else dflt) for r in rows], dtype=float)
    TY = np.array([r["type"] for r in rows]); R = arr("R"); R10 = arr("r10")
    RV = arr("rv"); RALLY = arr("rally"); BL = arr("box_len"); TCH = arr("touches")
    DEP = arr("depth"); UC = arr("uc"); DAYS = arr("days"); BR = arr("breadth")
    STR = np.array([bool(r["strong"]) for r in rows]); H2 = np.array([r["d"] >= mid for r in rows])

    b = np.array([(x[1], x[2], (x[3] if x[3] is not None else np.nan), x[0] >= mid) for x in base], dtype=object)
    bR = b[:, 0].astype(float); b10 = b[:, 1].astype(float); bBR = b[:, 2].astype(float); bH2 = b[:, 3].astype(bool)
    def bstats(mask):
        return (int(mask.sum()), float(np.mean(bR[mask])) if mask.any() else np.nan,
                float(np.mean(b10[mask] > 0) * 100) if mask.any() else np.nan,
                float(np.mean(b10[mask])) if mask.any() else np.nan)
    BASE = {"all": bstats(np.ones(len(bR), bool)), "breadth50": bstats(bBR > 50)}

    GRID = {
        "rally": [None, 20, 30], "box_len": [None, 15, 20], "touches": [None, 3], "depth": [None, 4],
        "uc": [None, 1.0, 0.5], "days": [None, 1, 0], "vol": [None, 1.0, 1.5],
        "mkt": [None, "breadth50"], "rs": [None, "top30"],
    }
    BOX_TYPES = {"ur", "spring", "top", "fbbo", "shkbo"}
    results = []
    for typ in sorted(set(TY)):
        base_m = TY == typ
        keys = ["rally", "box_len", "touches", "depth", "vol", "mkt", "rs"] + (["uc", "days"] if typ in ("ur", "spring") else [])
        if typ not in BOX_TYPES:
            keys = ["vol", "mkt", "rs"]
        for combo in itertools.product(*[GRID[k] for k in keys]):
            m = base_m.copy(); desc = []
            for k, v in zip(keys, combo):
                if v is None:
                    continue
                if k == "rally": m &= RALLY >= v; desc.append("rally≥%d%%" % v)
                elif k == "box_len": m &= BL >= v; desc.append("box≥%dd" % v)
                elif k == "touches": m &= TCH >= v; desc.append("touches≥%d" % v)
                elif k == "depth": m &= DEP <= v; desc.append("box≤%dxADR" % v)
                elif k == "uc": m &= UC <= v; desc.append("undercut≤%.1fADR" % v)
                elif k == "days": m &= DAYS <= v; desc.append("reclaim≤%dd" % v)
                elif k == "vol": m &= RV >= v; desc.append("vol≥%.1fx" % v)
                elif k == "mkt": m &= BR > 50; desc.append("breadth>50%")
                elif k == "rs": m &= STR; desc.append("top30% RS")
            n = int(m.sum())
            if n < MIN_N:
                continue
            m1, m2 = m & ~H2, m & H2
            if m1.sum() < MIN_HALF or m2.sum() < MIN_HALF:
                continue
            bm = BASE["breadth50"][3] if "breadth>50%" in desc else BASE["all"][3]
            x1, x2 = R10[m1] - bm, R10[m2] - bm
            se = float(np.std(R10[m], ddof=1) / np.sqrt(n)) if n > 1 else 1e9
            edge = float(np.mean(R10[m]) - bm)
            r1, r2 = float(np.mean(x1)), float(np.mean(x2))
            results.append(dict(type=typ, rule=" · ".join(desc) or "(loose default)", n=n, edge=edge,
                                edge_h1=r1, edge_h2=r2, tstat=edge / se if se > 0 else 0.0,
                                mean10=float(np.mean(R10[m])),
                                avgR=float(np.mean(R[m])), avgR_h1=r1, avgR_h2=r2,
                                win=float(np.mean(R10[m] > 0) * 100), stopped=float(np.mean(R[m] <= -0.99) * 100),
                                med10=float(np.median(R10[m])), mkt="breadth50" if "breadth>50%" in desc else "all"))
    log("%d rule versions passed the size checks (%.0fs)" % (len(results), time.time() - t0))

    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    csvp = os.path.join(OUT, "shakeout_study_%s_%s.csv" % (a.market, stamp))
    with open(csvp, "w") as fh:
        fh.write("type,rule,n,avg_pct,edge_pct,edge_first_half,edge_second_half,t_stat,win_pct,stopped_pct,avgR\n")
        for r in sorted(results, key=lambda x: (x["type"], -min(x["edge_h1"], x["edge_h2"]))):
            fh.write('%s,"%s",%d,%.3f,%.3f,%.3f,%.3f,%.2f,%.1f,%.1f,%.3f\n' % (r["type"], r["rule"], r["n"], r["mean10"], r["edge"],
                     r["edge_h1"], r["edge_h2"], r["tstat"], r["win"], r["stopped"], r["avgR"]))
    lines = ["SHAKEOUT STUDY - %s - %s" % (a.market, stamp),
             "Trades: enter next open, stop = shakeout low, exit at stop or after %d sessions. Uptrend events only." % HOLD,
             "Baseline (any uptrend day, 1.5 ADR stop): avg %+.2f%%/trade, %.0f%% up (n=%d); when breadth>50%%: %+.2f%%, %.0f%% up (n=%d)"
             % (BASE["all"][3], BASE["all"][2], BASE["all"][0], BASE["breadth50"][3], BASE["breadth50"][2], BASE["breadth50"][0]),
             "A rule 'works' only if it beats the baseline in BOTH halves (split %s) and the edge is not noise (t >= 2)." % mid,
             "Edge = average % return per trade minus the baseline's. Exits: stop or 10 sessions. No costs.", ""]
    tg_lines = []
    for typ in sorted(set(TY)):
        rs = [r for r in results if r["type"] == typ]
        if not rs:
            continue
        loose = [r for r in rs if r["rule"] == "(loose default)"]
        good = [r for r in rs if r["edge_h1"] > 0 and r["edge_h2"] > 0 and r["tstat"] >= 2.0]
        good.sort(key=lambda r: -min(r["edge_h1"], r["edge_h2"]))
        lines.append("== %s: %d versions tested, %d beat the baseline in both halves" % (SK_NAME(typ), len(rs), len(good)))
        if loose:
            r = loose[0]
            lines.append("   now (loose): n=%d  avg %+.2f%%/trade  edge vs baseline %+.2f%% (%+.2f / %+.2f)  t=%.1f  win %.0f%%  stopped %.0f%%  avg %.2fR"
                         % (r["n"], r["mean10"], r["edge"], r["edge_h1"], r["edge_h2"], r["tstat"], r["win"], r["stopped"], r["avgR"]))
        for r in good[:8]:
            lines.append("   + %-58s n=%-5d avg %+.2f%%  edge %+.2f%% (%+.2f / %+.2f)  t=%.1f  win %.0f%%  stopped %.0f%%  %.2fR"
                         % (r["rule"], r["n"], r["mean10"], r["edge"], r["edge_h1"], r["edge_h2"], r["tstat"], r["win"], r["stopped"], r["avgR"]))
        if good:
            g = good[0]
            tg_lines.append("✅ <b>%s</b>: %s — %+.2f%%/trade vs baseline, n=%d" % (SK_NAME(typ), g["rule"], g["edge"], g["n"]))
        else:
            tg_lines.append("✖ <b>%s</b>: no version beat the baseline" % SK_NAME(typ))
        lines.append("")
    rep = "\n".join(lines)
    txtp = os.path.join(OUT, "shakeout_study_%s_%s.txt" % (a.market, stamp))
    open(txtp, "w").write(rep)
    print("\n" + rep)
    log("files: %s  %s  (%.0f min)" % (txtp, csvp, (time.time() - t0) / 60))
    chat = CFG.TF_SYSTEM if a.market == "IN" else CFG.TF_US
    tg(chat, ("🔬 <b>Shakeout study · %s</b>\nBaseline %+.2f%%/trade · details in study/%s\n\n" % (
        "India" if a.market == "IN" else "US", BASE["all"][3], os.path.basename(txtp))) + "\n".join(tg_lines))


def SK_NAME(t):
    return {"ema9": "9 EMA shakeout", "ema20": "20 EMA shakeout", "ur": "Box U&R", "spring": "Spring",
            "top": "Top shakeout", "fbbo": "Failed breakdown → breakout", "shkbo": "Shakeout → box breakout"}.get(t, t)


if __name__ == "__main__":
    main()
