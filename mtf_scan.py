#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mtf_scan.py — TrueFlow multi-timeframe engine (India)
════════════════════════════════════════════════════════════════════════

WHAT IT DOES (nightly, 8:30 PM IST)
  For every stock in stock_universe, plus mainboard stocks listed since
  it was built, it pulls ~3 years of daily candles from Kite and works out:

    * Daily / weekly / monthly trend state (Up, Pullback, Pullback to 50,
      Flat, Down) from the 9 and 20 EMA on each timeframe
    * The combined state: All Timeframes Up, HTF Pullback, Weekly Turn,
      Early Turnaround, HTF Stretched, Counter-Trend Bounce, Rolling Over,
      All Timeframes Down, Mixed
    * Daily 50 and 200 EMA
    * 20 EMA Base / 50 EMA Base (box after a 30%+ rally, sitting on the EMA)
    * New-listing data: listing-day high, post-listing high, reclaim and
      breakout flags
    * Evidence: 5/10/20-day forward returns for each state over the last
      ~250 sessions, so the states can be judged on data

STORAGE
  momentum_mtf holds ONE row per stock, overwritten each night. It never
  grows with history. mtf_evidence holds ~40 rows. The database is near the
  free-plan limit, so nothing here accumulates.

USAGE
  Test on a few stocks, writes nothing:
      /root/trueflow/bin/python mtf_scan.py --dry-run --limit 5
  Full run:
      /root/trueflow/bin/python mtf_scan.py
  One stock, explained:
      /root/trueflow/bin/python mtf_scan.py --dry-run --symbols RELIANCE
════════════════════════════════════════════════════════════════════════
"""

import sys
import os
import json
import time
import math
import argparse
from datetime import datetime, date, timedelta, timezone

CFG = None
KiteConnect = None
create_client = None

# ══════════════════════════════════════════════════════════════════════
#  TUNABLES — starting defaults; the evidence table is how they get judged
# ══════════════════════════════════════════════════════════════════════

IST = timezone(timedelta(hours=5, minutes=30))

HISTORY_DAYS        = 1100   # ~3 years of daily candles (monthly 20 EMA needs 20+ months)
KITE_CHUNK_DAYS     = 1900   # Kite allows long spans for the day interval
KITE_SLEEP          = 0.35   # same pacing as nextday_grader.py
BATCH_WRITE         = 200

EMA_FLAT_PCT        = 0.50   # 9/20 gap under this = Flat (same cut as Next Day WL)
STRETCH_W9_PCT      = 15.0   # above weekly 9 EMA by more than this = HTF Stretched
WEEKLY_TURN_WEEKS   = 3      # a weekly 9>20 cross this recent = Weekly Turn
EMA_SLOPE_BARS      = 10     # "rising" = EMA now above its value this many bars ago

BASE_LOOKBACK       = 126    # ~6 months to find the high the base hangs from
PRIOR_RALLY_MIN     = 30.0   # the rally before the base, % (matches "solid pole")
BASE_MAX_DEPTH      = 25.0   # deepest allowed base, %
BASE20_MIN_DAYS     = 15
BASE50_MIN_DAYS     = 20
BASE_NEAR_ADR       = 1.0    # "on the EMA" = within this many ADRs of it
BASE_FAIL_SESSIONS  = 5      # closes below the box low in a row = Base Failed

LISTINGS_FILE       = "/root/trueflow/mtf_listings.json"
IPO_SEED_DAYS       = 365    # first run: pick up mainboard listings from the last year
MIN_LISTED_SESSIONS = 5      # wait a week after listing
RECENT_SESSIONS     = 22     # "Recently listed" = the first month after that week
IPO_MIN_TURNOVER_CR = 5.0    # avg daily traded value floor (market cap not available)
MAX_DISCOVERY       = 1500   # new-symbol checks per run (first run is the big one)

EVIDENCE_SESSIONS   = 250
EVIDENCE_HORIZONS   = (5, 10, 20)
EVIDENCE_BASE_EVERY = 5      # EMA-base evidence sampled every Nth day (speed)

STATES = ["allup", "htfpb", "wkturn", "earlyturn", "stretched", "ctbounce",
          "rolling", "alldown", "mixed"]
STATE_NAMES = {
    "allup": "All Timeframes Up", "htfpb": "HTF Pullback",
    "wkturn": "Weekly Turn", "earlyturn": "Early Turnaround",
    "stretched": "HTF Stretched",
    "ctbounce": "Counter-Trend Bounce", "rolling": "Rolling Over",
    "alldown": "All Timeframes Down", "mixed": "Mixed",
    "base20": "20 EMA Base", "base50": "50 EMA Base", "ALL": "All stocks",
}


# ══════════════════════════════════════════════════════════════════════
#  MATHS
# ══════════════════════════════════════════════════════════════════════

def ema_series(values, period):
    """Same formula as momentum_scan.py's calc_ema (SMA seed, then k=2/(n+1)),
    returned as a full series so any bar can be read back."""
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out[period - 1] = e
    for i in range(period, n):
        e = values[i] * k + e * (1 - k)
        out[i] = e
    return out


def rnd(x, p=2):
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return round(float(x), p)
    except Exception:
        return None


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def to_date(d):
    if isinstance(d, datetime):
        return d.astimezone(IST).date() if d.tzinfo else d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def week_complete(d):
    return d.weekday() == 4          # judged on Friday's close


def month_complete(d):
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != d.month      # last weekday of the month


def group_periods(dates, closes, highs, lows, kind):
    """Weekly (ISO week) or monthly candles, each remembering which daily
    bars it covers. Returns list of dicts, oldest first."""
    out, idx = [], [0] * len(dates)
    key_prev = None
    for i, d in enumerate(dates):
        key = d.isocalendar()[:2] if kind == "w" else (d.year, d.month)
        if key != key_prev:
            out.append({"first": i, "last": i, "close": closes[i],
                        "high": highs[i], "low": lows[i], "date": d})
            key_prev = key
        else:
            p = out[-1]
            p["last"] = i
            p["close"] = closes[i]
            p["date"] = d
            p["high"] = max(p["high"], highs[i])
            p["low"] = min(p["low"], lows[i])
        idx[i] = len(out) - 1
    return out, idx


def tf_state(c, e9, e20, e50=None, e50_rising=False, daily=False):
    if c is None or e9 is None or e20 is None or e20 == 0:
        return None
    gap = abs(e9 - e20) / e20 * 100.0
    if gap < EMA_FLAT_PCT:
        return "Flat"
    if c > e9 and e9 > e20:
        return "Up"
    if e9 > e20 and e20 <= c <= e9:
        return "Pullback"
    if daily and e50 is not None and e50_rising and e20 > e50 and e50 <= c < e20:
        return "Pullback50"
    if c < e9 and e9 < e20:
        return "Down"
    return "Flat"


def short_month_state(c, e9_now, e9_prev):
    """9–19 months of history: no monthly 20 EMA yet, so judge on the 9 alone."""
    if c is None or e9_now is None:
        return None
    rising = e9_prev is not None and e9_now > e9_prev
    falling = e9_prev is not None and e9_now < e9_prev
    if c > e9_now and rising:
        return "Up"
    if c < e9_now and falling:
        return "Down"
    return "Flat"


def combine(d, w, m, pct_w9, w_turn):
    if w == "Up" and m == "Up" and d == "Up":
        if pct_w9 is not None and pct_w9 > STRETCH_W9_PCT:
            return "stretched"
        return "allup"
    if w == "Up" and m == "Up" and d in ("Pullback", "Pullback50", "Flat"):
        return "htfpb"
    if w_turn and m in ("Up", "Flat", None) and d in ("Up", "Pullback"):
        return "wkturn"
    if m == "Up" and w == "Pullback" and d in ("Down", "Pullback", "Pullback50", "Flat"):
        return "rolling"
    if d == "Up" and w_turn and m == "Down":
        return "earlyturn"   # weekly turned up, monthly still falling
    if d == "Up" and w == "Down" and m in ("Down", None):
        return "ctbounce"
    if d == "Down" and w == "Down" and m == "Down":
        return "alldown"
    return "mixed"


# ══════════════════════════════════════════════════════════════════════
#  PER-STOCK SERIES
# ══════════════════════════════════════════════════════════════════════

class Series:
    def __init__(self, candles):
        self.c = candles
        self.dates = [to_date(x["date"]) for x in candles]
        self.o = [float(x["open"]) for x in candles]
        self.h = [float(x["high"]) for x in candles]
        self.l = [float(x["low"]) for x in candles]
        self.cl = [float(x["close"]) for x in candles]
        self.v = [float(x.get("volume") or 0) for x in candles]
        self.n = len(candles)
        self.e9 = ema_series(self.cl, 9)
        self.e20 = ema_series(self.cl, 20)
        self.e50 = ema_series(self.cl, 50)
        self.e200 = ema_series(self.cl, 200)
        rng = [((self.h[i] / self.l[i]) - 1.0) * 100.0 if self.l[i] > 0 else 0.0
               for i in range(self.n)]
        self.adr_pre = [0.0]
        for r in rng:
            self.adr_pre.append(self.adr_pre[-1] + r)
        self.wk, self.widx = group_periods(self.dates, self.cl, self.h, self.l, "w")
        self.mo, self.midx = group_periods(self.dates, self.cl, self.h, self.l, "m")
        wc = [p["close"] for p in self.wk]
        mc = [p["close"] for p in self.mo]
        self.e9w, self.e20w = ema_series(wc, 9), ema_series(wc, 20)
        self.e9m, self.e20m = ema_series(mc, 9), ema_series(mc, 20)

    def adr(self, i, n=20):
        if i + 1 < n:
            n = i + 1
        if n <= 0:
            return None
        return (self.adr_pre[i + 1] - self.adr_pre[i + 1 - n]) / n

    def rising(self, arr, i, bars=EMA_SLOPE_BARS):
        if i - bars < 0 or arr[i] is None or arr[i - bars] is None:
            return False
        return arr[i] > arr[i - bars]

    # ── state at a completed weekly / monthly bar ────────────────────
    def w_state_at(self, j):
        if j < 0:
            return None
        return tf_state(self.wk[j]["close"], self.e9w[j], self.e20w[j])

    def m_state_at(self, j):
        if j < 0:
            return None, False
        c = self.mo[j]["close"]
        if self.e20m[j] is not None:
            return tf_state(c, self.e9m[j], self.e20m[j]), False
        if self.e9m[j] is not None:
            prev = self.e9m[j - 1] if j - 1 >= 0 else None
            return short_month_state(c, self.e9m[j], prev), True
        return None, True

    def w_cross_at(self, j):
        """Most recent weekly 9-over-20 cross at or before completed week j."""
        k = j
        while k >= 1:
            a9, a20, b9, b20 = self.e9w[k - 1], self.e20w[k - 1], self.e9w[k], self.e20w[k]
            if None not in (a9, a20, b9, b20) and a9 <= a20 and b9 > b20:
                return k
            k -= 1
        return None

    def d_state_at(self, i):
        return tf_state(self.cl[i], self.e9[i], self.e20[i], self.e50[i],
                        self.rising(self.e50, i), daily=True)

    # ── full state as of daily bar i (weeks/months completed before it) ─
    def states_as_of(self, i, live=False):
        wj = self.widx[i]
        mj = self.midx[i]
        if not (live and week_complete(self.dates[i]) and self.wk[wj]["last"] == i):
            wj -= 1
        if not (live and month_complete(self.dates[i]) and self.mo[mj]["last"] == i):
            mj -= 1
        d = self.d_state_at(i)
        w = self.w_state_at(wj)
        m, short = self.m_state_at(mj)
        e9w = self.e9w[wj] if wj >= 0 else None
        pw9 = pct(self.cl[i], e9w)
        xj = self.w_cross_at(wj) if wj >= 1 else None
        w_turn = (xj is not None and (wj - xj) < WEEKLY_TURN_WEEKS
                  and self.e9w[wj] is not None and self.e20w[wj] is not None
                  and self.e9w[wj] > self.e20w[wj])
        return {"d": d, "w": w, "m": m, "m_short": short, "wj": wj, "mj": mj,
                "pw9": pw9, "xj": xj, "w_turn": w_turn,
                "mtf": combine(d, w, m, pw9, w_turn)}

    # ── 20 / 50 EMA base, using bars up to i ────────────────────────
    def ema_base_at(self, i):
        start = max(0, i - BASE_LOOKBACK + 1)
        if i - start < BASE20_MIN_DAYS:
            return None
        iH, H = start, self.h[start]
        for k in range(start, i + 1):
            if self.h[k] >= H:
                iH, H = k, self.h[k]
        base_days = i - iH
        if base_days < BASE20_MIN_DAYS:
            return None
        lo0 = min(self.l[max(0, iH - BASE_LOOKBACK):iH + 1])
        if lo0 <= 0 or (H / lo0 - 1) * 100.0 < PRIOR_RALLY_MIN:
            return None
        box_low, streak, streak_low = None, 0, None
        for k in range(iH, i + 1):
            if box_low is None:
                box_low = self.l[k]
                continue
            if self.cl[k] < box_low:
                streak += 1
                streak_low = self.l[k] if streak_low is None else min(streak_low, self.l[k])
                if streak >= BASE_FAIL_SESSIONS:
                    return None                         # Base Failed: off the list
            else:
                if streak:
                    box_low = min(box_low, streak_low)  # shakeout undercut joins the box
                    streak, streak_low = 0, None
                box_low = min(box_low, self.l[k])
        depth = (H - box_low) / H * 100.0
        if depth > BASE_MAX_DEPTH:
            return None
        c = self.cl[i]
        e20, e50 = self.e20[i], self.e50[i]
        a = self.adr(i)
        if e20 is None or e50 is None or a is None:
            return None
        band = a / 100.0 * c * BASE_NEAR_ADR
        e50_up = self.rising(self.e50, i)
        e20_ok = (self.e20[i - EMA_SLOPE_BARS] is not None and
                  e20 >= self.e20[i - EMA_SLOPE_BARS] * 0.995) if i >= EMA_SLOPE_BARS else False
        near20, near50 = abs(c - e20) <= band, abs(c - e50) <= band
        kind = None
        if streak:
            # below the box but not failed yet: keep the label, show the count
            if e50_up:
                kind = "20" if abs(c - e20) <= abs(c - e50) else "50"
        elif near20 and near50 and e50_up:
            kind = "20+50"
        elif near20 and e20_ok and e50_up:
            kind = "20"
        elif near50 and e50_up and base_days >= BASE50_MIN_DAYS:
            kind = "50"
        if kind is None:
            return None
        return {"ema_base": kind, "base_days": base_days,
                "base_depth_pct": rnd(depth, 1), "box_high": rnd(H),
                "box_low": rnd(box_low), "below_box_days": streak,
                "base_status": "below box" if streak else "forming"}


# ══════════════════════════════════════════════════════════════════════
#  ONE STOCK -> ONE ROW
# ══════════════════════════════════════════════════════════════════════

def build_row(sym, meta, S, fetch_from):
    i = S.n - 1
    if i < 1:
        return None
    st = S.states_as_of(i, live=True)
    c = S.cl[i]
    wj, mj = st["wj"], st["mj"]
    # developing (current, unfinished) week / month
    w_dev = tf_state(c, S.e9w[S.widx[i]], S.e20w[S.widx[i]])
    m_dev = None
    if S.e20m[S.midx[i]] is not None:
        m_dev = tf_state(c, S.e9m[S.midx[i]], S.e20m[S.midx[i]])
    xj = st["xj"]
    tf_up = sum(1 for x in (st["d"], st["w"], st["m"]) if x == "Up")
    m_bo = False
    if mj >= 6:
        prev = S.mo[mj - 6:mj]
        hi_c = max(p["close"] for p in prev)
        rng_pct = pct(max(p["high"] for p in prev), min(p["low"] for p in prev))
        m_bo = S.mo[mj]["close"] > hi_c and rng_pct is not None and rng_pct <= 40.0
    row = {
        "symbol": sym,
        "session_date": str(S.dates[i]),
        "exchange": meta.get("exchange") or "NSE",
        "company_name": meta.get("name"),
        "is_ipo": bool(meta.get("is_ipo")),
        "ltp": rnd(c),
        "day_chg_pct": rnd(pct(c, S.cl[i - 1])),
        "adr_pct": rnd(S.adr(i)),
        "ema9_d": rnd(S.e9[i]), "ema20_d": rnd(S.e20[i]),
        "ema50_d": rnd(S.e50[i]), "ema200_d": rnd(S.e200[i]),
        "pct_from_ema9_d": rnd(pct(c, S.e9[i])),
        "pct_from_ema20_d": rnd(pct(c, S.e20[i])),
        "pct_from_ema50": rnd(pct(c, S.e50[i])),
        "pct_from_ema200": rnd(pct(c, S.e200[i])),
        "ema50_rising": S.rising(S.e50, i),
        "ema9_w": rnd(S.e9w[wj]) if wj >= 0 else None,
        "ema20_w": rnd(S.e20w[wj]) if wj >= 0 else None,
        "pct_from_ema9_w": rnd(st["pw9"]),
        "ema9_m": rnd(S.e9m[mj]) if mj >= 0 else None,
        "ema20_m": rnd(S.e20m[mj]) if mj >= 0 else None,
        "pct_from_ema9_m": rnd(pct(c, S.e9m[mj])) if mj >= 0 else None,
        "d_state": st["d"], "w_state": st["w"], "w_state_dev": w_dev,
        "m_state": st["m"], "m_state_dev": m_dev,
        "m_short_history": bool(st["m_short"]),
        "w_cross_date": str(S.wk[xj]["date"]) if xj is not None else None,
        "w_cross_weeks": (wj - xj) if xj is not None else None,
        "m_breakout": bool(m_bo),
        "mtf_state": st["mtf"], "tf_up": tf_up,
        "ema_base": None, "base_days": None, "base_depth_pct": None,
        "box_high": None, "box_low": None, "below_box_days": None,
        "base_status": None,
        "listing_date": None, "days_listed": None, "listing_open": None,
        "listing_day_high": None, "listing_day_low": None,
        "post_listing_high": None, "pct_from_plh": None,
        "ldh_reclaim": False, "plh_breakout": False,
        "ipo_base_days": None, "ipo_base_depth": None,
        "recently_listed": False, "avg_turnover_cr": None,
    }
    b = S.ema_base_at(i)
    if b:
        row.update(b)
    # listing data: when the first candle is clearly after our fetch start
    first = S.dates[0]
    if meta.get("is_ipo") or first > fetch_from + timedelta(days=10):
        ldh = S.h[0]
        plh = max(S.h)
        prior_plh = max(S.h[:i]) if i >= 1 else ldh
        iP = max(k for k in range(S.n) if S.h[k] == plh)
        ldh_rc = False
        for k in (i, i - 1):
            if k >= 2 and S.cl[k] > ldh and S.cl[k - 1] <= ldh:
                ldh_rc = True
        plh_bo = False
        for k in (i, i - 1):
            if k >= MIN_LISTED_SESSIONS and S.cl[k] > max(S.h[:k]):
                plh_bo = True
        after = S.l[iP + 1:] if iP + 1 <= i else []
        tv = [S.cl[k] * S.v[k] for k in range(S.n)]
        row.update({
            "listing_date": str(first), "days_listed": S.n,
            "listing_open": rnd(S.o[0]), "listing_day_high": rnd(ldh),
            "listing_day_low": rnd(S.l[0]), "post_listing_high": rnd(plh),
            "pct_from_plh": rnd(pct(c, plh)),
            "ldh_reclaim": ldh_rc, "plh_breakout": plh_bo,
            "ipo_base_days": i - iP,
            "ipo_base_depth": rnd((plh - min(after)) / plh * 100.0, 1) if after else 0.0,
            "recently_listed": MIN_LISTED_SESSIONS <= S.n <= MIN_LISTED_SESSIONS + RECENT_SESSIONS,
            "avg_turnover_cr": rnd(sum(tv) / len(tv) / 1e7),
        })
    return row


def evidence_for(S, acc):
    """Forward returns by state, as of each of the last EVIDENCE_SESSIONS bars."""
    maxh = max(EVIDENCE_HORIZONS)
    end = S.n - 1 - maxh
    start = max(210, end - EVIDENCE_SESSIONS)     # need the 200 EMA and some months
    for i in range(start, end + 1):
        st = S.states_as_of(i)
        c0 = S.cl[i]
        tags = ["ALL", st["mtf"]]
        if (i - start) % EVIDENCE_BASE_EVERY == 0:
            b = S.ema_base_at(i)
            if b and b["base_status"] == "forming":
                tags.append("base50" if b["ema_base"] == "50" else "base20")
        for h in EVIDENCE_HORIZONS:
            r = (S.cl[i + h] / c0 - 1.0) * 100.0
            for t in tags:
                acc.setdefault((t, h), []).append(r)


def summarise_evidence(acc, today):
    rows = []
    for (state, h), vals in acc.items():
        if not vals:
            continue
        s = sorted(vals)
        n = len(s)
        med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        rows.append({"state": state, "horizon": h, "n": n,
                     "avg_ret": rnd(sum(s) / n), "median_ret": rnd(med),
                     "win_pct": rnd(100.0 * sum(1 for x in s if x > 0) / n, 1),
                     "computed_on": str(today)})
    return rows


# ══════════════════════════════════════════════════════════════════════
#  CONNECTIONS + DATA
# ══════════════════════════════════════════════════════════════════════

def connect_kite():
    kite = KiteConnect(api_key=CFG.KITE_API_KEY)
    tp = getattr(CFG, "ACCESS_TOKEN_PATH", "/root/trueflow/access_token.txt")
    with open(tp) as fh:
        kite.set_access_token(fh.read().strip())
    print("Kite connected: %s" % kite.profile().get("user_name", "?"))
    return kite


def fetch_daily(kite, token, d_from, d_to):
    out, cur = [], d_from
    while cur <= d_to:
        end = min(cur + timedelta(days=KITE_CHUNK_DAYS), d_to)
        for attempt in range(3):
            try:
                out.extend(kite.historical_data(token, cur, end, "day") or [])
                break
            except Exception as e:
                if attempt == 2:
                    print("    fetch failed %s..%s: %s" % (cur, end, str(e)[:60]))
                else:
                    time.sleep(1.5)
            finally:
                time.sleep(KITE_SLEEP)
        cur = end + timedelta(days=1)
    seen, uniq = set(), []
    for c in out:
        d = to_date(c["date"])
        if d not in seen and c.get("close"):
            seen.add(d)
            uniq.append(c)
    uniq.sort(key=lambda c: to_date(c["date"]))
    return uniq


def load_universe(sb):
    rows, page = [], 0
    while True:
        chunk = (sb.table("stock_universe")
                 .select("symbol,company_name,exchange,tv_symbol")
                 .order("symbol").range(page * 1000, page * 1000 + 999)
                 .execute().data) or []
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        page += 1
    seen, out = set(), []
    for r in rows:
        s = r.get("symbol")
        if s and s not in seen:
            seen.add(s)
            out.append(r)
    return out


def is_mainboard_eq(inst):
    ts = inst.get("tradingsymbol") or ""
    name = (inst.get("name") or "").upper()
    if inst.get("instrument_type") != "EQ" or inst.get("segment") != "NSE":
        return False
    if "-" in ts:                      # -SM / -ST (SME), -BE, -RR, -GB, bonds...
        return False
    if (inst.get("lot_size") or 1) != 1:
        return False
    if "ETF" in name or ts.endswith("BEES") or ts.endswith("ETF") or "LIQUID" in ts:
        return False
    return True


def load_listings():
    try:
        with open(LISTINGS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_listings(d):
    tmp = LISTINGS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh)
    os.replace(tmp, LISTINGS_FILE)


def discover_listings(kite, nse_inst, universe_syms, today, dry_run, limit):
    cache = load_listings()
    cands = [x for x in nse_inst if is_mainboard_eq(x)
             and x["tradingsymbol"] not in universe_syms
             and x["tradingsymbol"] not in cache]
    seed_from = today - timedelta(days=IPO_SEED_DAYS + 30)
    print("New-listing check: %d unchecked symbol(s) not in stock_universe"
          % len(cands))
    if limit:
        cands = cands[:limit]
    cands = cands[:MAX_DISCOVERY]
    found = 0
    for n, x in enumerate(cands, 1):
        cs = fetch_daily(kite, x["instrument_token"], seed_from, today)
        if not cs:
            cache[x["tradingsymbol"]] = {"old": True, "first": None}
            continue
        first = to_date(cs[0]["date"])
        new = first > seed_from + timedelta(days=7)
        cache[x["tradingsymbol"]] = {"old": not new, "first": str(first),
                                     "name": x.get("name")}
        if new:
            found += 1
        if n % 200 == 0:
            print("   checked %d/%d (%d new listings so far)" % (n, len(cands), found))
            sys.stdout.flush()
            if not dry_run:
                save_listings(cache)
    if not dry_run:
        save_listings(cache)
    return cache


def send_telegram(msg):
    tok = getattr(CFG, "TELEGRAM_TOKEN", None)
    chat = getattr(CFG, "TELEGRAM_CHAT_ID", None)
    if not tok or not chat:
        return
    try:
        import requests
        requests.post("https://api.telegram.org/bot%s/sendMessage" % tok,
                      data={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
                      timeout=15)
    except Exception:
        pass


def write_rows(sb, table, rows, conflict):
    if not rows:
        return 0
    keys = set()
    for r in rows:
        keys.update(r.keys())
    rows = [{k: r.get(k) for k in keys} for r in rows]   # PGRST102 guard
    done = 0
    for i in range(0, len(rows), BATCH_WRITE):
        chunk = rows[i:i + BATCH_WRITE]
        try:
            sb.table(table).upsert(chunk, on_conflict=conflict).execute()
            done += len(chunk)
        except Exception as e:
            print("  write failed (%s): %s" % (table, str(e)[:160]))
    return done


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    global CFG, KiteConnect, create_client
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="only the first N stocks")
    ap.add_argument("--symbols", default="", help="comma list, e.g. RELIANCE,TCS")
    ap.add_argument("--no-evidence", action="store_true")
    ap.add_argument("--skip-listings", action="store_true")
    args = ap.parse_args()

    try:
        import tf_config as _c
        CFG = _c
    except ImportError:
        print("FATAL: tf_config.py not found in /root/trueflow.")
        sys.exit(1)
    try:
        from kiteconnect import KiteConnect as _K
        KiteConnect = _K
        from supabase import create_client as _cc
        create_client = _cc
    except ImportError as e:
        print("FATAL: missing package in this venv: %s" % e)
        sys.exit(1)

    t0 = time.time()
    print("=" * 66)
    print(" TrueFlow MTF scan  %s" % datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"))
    print("=" * 66)
    today = datetime.now(IST).date()
    kite = connect_kite()
    sb = create_client(CFG.SUPABASE_URL, CFG.SUPABASE_KEY)
    print("Supabase connected.")

    uni = load_universe(sb)
    print("stock_universe: %d symbols" % len(uni))
    nse = kite.instruments("NSE")
    bse = kite.instruments("BSE")
    nse_map = {x["tradingsymbol"]: x for x in nse if x.get("instrument_type") == "EQ"}
    bse_map = {x["tradingsymbol"]: x for x in bse if x.get("instrument_type") == "EQ"}

    want = set(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    jobs, missing = [], []
    for r in uni:
        sym, ex = r["symbol"], (r.get("exchange") or "NSE").upper()
        if want and sym not in want:
            continue
        inst = None
        if ex == "BSE":
            alt = (r.get("tv_symbol") or "").split(":")[-1]
            inst = bse_map.get(sym) or bse_map.get(alt) or nse_map.get(sym)
        else:
            inst = nse_map.get(sym) or bse_map.get(sym)
        if not inst:
            missing.append(sym)
            continue
        jobs.append((sym, inst["instrument_token"],
                     {"exchange": ex, "name": r.get("company_name"), "is_ipo": False}))
    if missing:
        print("  no Kite instrument for %d symbol(s): %s"
              % (len(missing), ", ".join(sorted(missing)[:15])))

    new_listings = []
    if not args.skip_listings and not want:
        uni_syms = set(r["symbol"] for r in uni)
        cache = discover_listings(kite, nse, uni_syms, today, args.dry_run, args.limit)
        for sym, info in cache.items():
            if info.get("old") or not info.get("first") or sym in uni_syms:
                continue
            inst = nse_map.get(sym)
            if not inst:
                continue
            new_listings.append(sym)
            jobs.append((sym, inst["instrument_token"],
                         {"exchange": "NSE", "name": info.get("name"),
                          "is_ipo": True, "first": info["first"]}))
        print("Tracking %d new listing(s) since %s"
              % (len(new_listings), today - timedelta(days=IPO_SEED_DAYS)))
    elif want:
        for sym in want:
            info = load_listings().get(sym)
            if info and not info.get("old") and sym in nse_map and \
                    not any(j[0] == sym for j in jobs):
                jobs.append((sym, nse_map[sym]["instrument_token"],
                             {"exchange": "NSE", "name": info.get("name"),
                              "is_ipo": True, "first": info.get("first")}))

    if args.limit and not want:
        jobs = jobs[:args.limit]
    print("Scanning %d stock(s)...\n" % len(jobs))
    sys.stdout.flush()

    rows, acc, skipped = [], {}, {}
    fetch_from = today - timedelta(days=HISTORY_DAYS)
    for n, (sym, tok, meta) in enumerate(jobs, 1):
        f_from = fetch_from
        if meta.get("is_ipo") and meta.get("first"):
            f_from = min(fetch_from, date.fromisoformat(meta["first"]) - timedelta(days=3))
        cs = fetch_daily(kite, tok, f_from, today)
        if len(cs) < 2:
            skipped["no candles"] = skipped.get("no candles", 0) + 1
            continue
        if (today - to_date(cs[-1]["date"])).days > 7:
            skipped["stale (no trade in 7 days)"] = skipped.get("stale (no trade in 7 days)", 0) + 1
            continue
        S = Series(cs)
        if meta.get("is_ipo"):
            if S.n < MIN_LISTED_SESSIONS:
                skipped["listed < 5 sessions"] = skipped.get("listed < 5 sessions", 0) + 1
                continue
            tv = sum(S.cl[k] * S.v[k] for k in range(S.n)) / S.n / 1e7
            if tv < IPO_MIN_TURNOVER_CR:
                skipped["new listing below turnover floor"] = \
                    skipped.get("new listing below turnover floor", 0) + 1
                continue
        try:
            row = build_row(sym, meta, S, f_from)
            if row:
                rows.append(row)
            if not args.no_evidence and not meta.get("is_ipo") and S.n > 260:
                evidence_for(S, acc)
        except Exception as e:
            skipped["error"] = skipped.get("error", 0) + 1
            print("  %s: %s" % (sym, str(e)[:100]))
        if n % 100 == 0:
            print("  %d/%d  (%.0fs)" % (n, len(jobs), time.time() - t0))
            sys.stdout.flush()

    # ── report ────────────────────────────────────────────────────────
    print("\n" + "-" * 66)
    print("ROWS %d   skipped %s" % (len(rows), dict(skipped) if skipped else "none"))
    cnt = {}
    for r in rows:
        cnt[r["mtf_state"]] = cnt.get(r["mtf_state"], 0) + 1
    for s in STATES:
        print("   %-22s %d" % (STATE_NAMES[s], cnt.get(s, 0)))
    bases = [r for r in rows if r.get("ema_base")]
    print("   EMA bases: %d  (20: %d · 50: %d · 20+50: %d · below box: %d)" % (
        len(bases), sum(1 for r in bases if r["ema_base"] == "20"),
        sum(1 for r in bases if r["ema_base"] == "50"),
        sum(1 for r in bases if r["ema_base"] == "20+50"),
        sum(1 for r in bases if r["base_status"] == "below box")))
    ipos = [r for r in rows if r.get("is_ipo")]
    print("   New listings tracked: %d  (recently listed: %d)"
          % (len(ipos), sum(1 for r in ipos if r.get("recently_listed"))))
    for r in sorted(ipos, key=lambda x: x.get("listing_date") or "")[-12:]:
        print("      %-14s listed %s  %3s sessions  %s" % (
            r["symbol"], r["listing_date"], r["days_listed"],
            "RECENT" if r["recently_listed"] else ""))
    if want or (args.dry_run and args.limit and args.limit <= 10):
        for r in rows:
            print("\n  %s  %s" % (r["symbol"], STATE_NAMES.get(r["mtf_state"], r["mtf_state"])))
            print("     daily %-10s weekly %-9s (dev %s)  monthly %-6s (dev %s)%s" % (
                r["d_state"], r["w_state"], r["w_state_dev"], r["m_state"],
                r["m_state_dev"], "  short history" if r["m_short_history"] else ""))
            print("     %%9E %s  %%50E %s  %%200E %s  %%Wk9E %s  ADR %s%%" % (
                r["pct_from_ema9_d"], r["pct_from_ema50"], r["pct_from_ema200"],
                r["pct_from_ema9_w"], r["adr_pct"]))
            if r.get("ema_base"):
                print("     EMA base %s · %sd · depth %s%% · box %s-%s · %s" % (
                    r["ema_base"], r["base_days"], r["base_depth_pct"],
                    r["box_low"], r["box_high"], r["base_status"]))

    ev = summarise_evidence(acc, today) if acc else []
    if ev:
        print("\nEVIDENCE (last ~%d sessions, forward return %%):" % EVIDENCE_SESSIONS)
        print("   %-22s %8s %8s %8s %8s" % ("state", "n(10d)", "5d", "10d", "20d"))
        by = {(r["state"], r["horizon"]): r for r in ev}
        for s in ["ALL"] + STATES + ["base20", "base50"]:
            if (s, 10) not in by:
                continue
            print("   %-22s %8d %8s %8s %8s" % (
                STATE_NAMES.get(s, s), by[(s, 10)]["n"],
                *["%+.2f" % by[(s, h)]["avg_ret"] if (s, h) in by else "—"
                  for h in EVIDENCE_HORIZONS]))

    if args.dry_run:
        print("\nDRY RUN — nothing written.  (%.0fs)" % (time.time() - t0))
        return

    w1 = write_rows(sb, "momentum_mtf", rows, "symbol")
    w2 = write_rows(sb, "mtf_evidence", ev, "state,horizon") if ev else 0
    print("\nWROTE momentum_mtf %d · mtf_evidence %d   (%.0fs)" % (w1, w2, time.time() - t0))
    send_telegram("🧭 <b>MTF scan done</b>\n%d stocks · %d All Timeframes Up · %d HTF Pullback"
                  " · %d EMA bases · %d new listings tracked" % (
                      w1, cnt.get("allup", 0), cnt.get("htfpb", 0), len(bases), len(ipos)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print("FATAL: %s" % e)
        try:
            send_telegram("⚠️ <b>MTF scan failed</b>\n%s" % str(e)[:300])
        except Exception:
            pass
        raise
