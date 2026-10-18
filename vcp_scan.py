#!/root/trueflow/bin/python
"""
vcp_scan.py - the classic Volatility Contraction Pattern (True VCP), per market, from stored daily prices.

A stock is a True VCP when ALL of these hold on the latest session:
  trend       close above the 50-day EMA, 50 EMA above the 200 EMA, and the 200 EMA higher than 20 sessions ago
  prior run   the base starts at its highest high of the last ~6 months (H0); H0 is at least 25% above
              the lowest low of the 60 sessions before it
  base        at least 15 sessions old
  pullbacks   2 to 6 pullbacks (peak -> trough, measured with a swing filter of max(2.5%, 0.8 x ADR%)),
              each one shallower than the one before; the first no deeper than 35%, the last 10% or less
  pivot       the high of the last peak, no lower than 90% of H0
  volume      average volume from the last peak to today is below the average volume of the whole base
States:
  breakout    first close above the pivot was today or yesterday, on >= 1.5x the 50-day average volume
  near        close within 3% below the pivot (or above it without the volume)
  forming     everything else that passes
Every stock is written with is_vcp and, when it fails, the first rule it failed (reason).
legs = JSON [[peak_date, peak_high, trough_date, trough_low, depth%], ...] for drawing on charts; base_start = date of H0.
Also for EVERY stock: momentum burst / pause-after-burst fields (exp_*), see expansion() - shown on the
dashboard as 'Fast expansion' and 'Tight contraction'.
And the ONE cycle model for EVERY stock (cyc_*), see cycle():
  Expansion -> Contraction -> Shakeout -> Reclaimed -> Breakout (-> next Expansion = next cycle) or Failed.
Prices are split/bonus-adjusted on the fly (split_adjust, shared with hi52_scan.py).
Writes vcp_state (IN) / us_vcp_state (US). Read-only on everything else.

  bin/python vcp_scan.py --market IN [--dry-run] [--symbols A,B] [--wait]
"""
import argparse, json, sys, time
from datetime import datetime, timedelta, timezone
import requests
import tf_config as C
from hi52_scan import split_adjust, get_all, latest_d, log, H, IST

SETTINGS = {
    'IN': {'ohlc': 'daily_ohlc', 'out': 'vcp_state', 'chat': 'TF_SYSTEM', 'flag': '🇮🇳'},
    'US': {'ohlc': 'us_daily_ohlc', 'out': 'us_vcp_state', 'chat': 'TF_US', 'flag': '🇺🇸'},
}
BASE = dict(cal_days=520, look=130, min_base=15, prior_len=60, prior_adv=25.0, t_min=2, t_max=6,
            first_max=35.0, last_max=10.0, swing_min=2.5, swing_adr=0.8, adr_len=20, pivot_min=0.90,
            vol_ratio_max=1.0, near_pct=3.0, bo_vol=1.5, bo_days=2, vol_avg=50, e200_rise=20,
            exp_look=25, exp_days=10, exp_min=20.0, burst_age=1, burst_keep=0.8,
            pause_min=3, pause_max=20, pause_hold=0.618, pause_tight=0.5, pause_vr=1.0,
            cyc_min=20.0, cyc_days=20, cyc_look=140, cyc_ctr_min=3, cyc_fail_ret=0.5, cyc_fail_e20=2,
            cyc_bo_hold=10, cyc_rcl_show=3, cyc_fail_show=5)
PARAMS = {'IN': dict(BASE), 'US': dict(BASE)}


def ema(x, n):
    out = [None] * len(x)
    if len(x) < n: return out
    e = sum(x[:n]) / n; out[n - 1] = e; k = 2.0 / (n + 1)
    for j in range(n, len(x)):
        e = x[j] * k + e * (1 - k); out[j] = e
    return out


def zigzag(h, l, a, b, r):
    """Swings from index a (the base high) to b with a reversal filter of r %.
    Returns (pairs, live): pairs = [(peak_idx, trough_idx)] completed pullbacks;
    live = (peak_idx, low_idx) when price is still inside a pullback that began at a confirmed peak, else None;
    plus the index of the highest high since the last confirmed trough (up-leg) or None."""
    f = r / 100.0
    pairs = []
    P, L = a, None            # current peak / lowest low since it
    mode = 'down'
    T, X = None, None         # confirmed trough / highest high since it
    for j in range(a + 1, b + 1):
        if mode == 'down':
            if h[j] > h[P] and (L is None or l[L] > h[P] * (1 - f)):
                P, L = j, None; continue                   # peak not yet confirmed: it moves up
            if L is None or l[j] < l[L]: L = j
            confirmed = l[L] <= h[P] * (1 - f)
            if confirmed and L < j and h[j] >= l[L] * (1 + f):
                pairs.append((P, L)); T, X, mode = L, j, 'up'
        else:
            if l[j] < l[T]:                                # lower low before the next peak is confirmed: trough moves
                P, L = pairs[-1][0], j; pairs.pop(); mode = 'down'; T = X = None; continue
            if h[j] > h[X]: X = j
            if X < j and l[j] <= h[X] * (1 - f):
                P, L, mode = X, j, 'down'; T = X = None
    if mode == 'down':
        live = (P, L) if (L is not None and l[L] <= h[P] * (1 - f)) else None
        return pairs, live, None
    return pairs, None, X


def expansion(h, l, c, v, ds, i, P):
    """Momentum burst (expansion) and the pause (contraction) after it.
    H = highest high of the last exp_look sessions; L = lowest low of the exp_days sessions up to H.
    Expansion = H/L - 1 >= exp_min %, built in 1..exp_days sessions.
      burst : H is today or yesterday and the close keeps >= burst_keep of the move
      pause : pause_min..pause_max sessions since H; the lowest low since H kept >= pause_hold of the move
              (gave back <= 38.2%); the last up-to-5 sessions span <= pause_tight of the move; average volume
              since H < pause_vr x the average volume of the expansion days"""
    e = {'exp_state': None, 'exp_pct': None, 'exp_days': None, 'exp_age': None, 'exp_kept': None,
         'exp_tight': None, 'exp_vr': None, 'exp_high': None, 'exp_start': None, 'exp_peak': None}
    if i < P['exp_look'] + P['exp_days'] + 2: return e
    H = max(range(i - P['exp_look'] + 1, i + 1), key=lambda j: (h[j], j))
    L = min(range(max(0, H - P['exp_days']), H + 1), key=lambda j: (l[j], -j))
    if L >= H or l[L] <= 0: return e
    mv = h[H] - l[L]; pct = (h[H] / l[L] - 1) * 100
    if pct < P['exp_min']: return e
    age = i - H
    kept = (c[i] - l[L]) / mv
    e.update(exp_pct=round(pct, 1), exp_days=H - L, exp_age=age, exp_kept=round(kept, 2), exp_high=round(h[H], 2),
             exp_start=ds[L], exp_peak=ds[H])
    ve = v[L + 1:H + 1]
    if age <= P['burst_age']:
        if kept >= P['burst_keep']: e['exp_state'] = 'burst'
        return e
    if not (P['pause_min'] <= age <= P['pause_max']): return e
    low_since = min(l[H + 1:i + 1])
    hold = (low_since - l[L]) / mv
    w0 = max(H + 1, i - 4)
    tight = (max(h[w0:i + 1]) - min(l[w0:i + 1])) / mv
    vp = v[H + 1:i + 1]
    vr = (sum(vp) / len(vp)) / (sum(ve) / len(ve)) if ve and vp and sum(ve) > 0 else None
    e.update(exp_tight=round(tight, 2), exp_vr=round(vr, 2) if vr is not None else None)
    if hold >= P['pause_hold'] and kept >= P['pause_hold'] and tight <= P['pause_tight'] and vr is not None and vr < P['pause_vr']:
        e['exp_state'] = 'pause'
    return e


def cycle(h, l, c, v, ds, i, P):
    """The one cycle model, walked day by day over the last cyc_look sessions.
    Expansion : a rise of >= cyc_min % from the lowest low of the previous cyc_days sessions to today's high,
                with today's high the highest since that low. New highs within cyc_ctr_min-1 sessions extend it.
    Contraction: from cyc_ctr_min sessions after the expansion high, no time limit, until it breaks either way.
    Shakeout  : during contraction, a close below the 9 EMA but above the 20 EMA.
    Reclaimed : after a shakeout, a close back above the 9 EMA (shown for cyc_rcl_show sessions, then Contraction).
    Breakout  : a close above the contraction high (the expansion high) in the upper half of the day's range.
                A new >= cyc_min % rise from the contraction low within cyc_days sessions = the next cycle (Expansion).
                No new leg within cyc_bo_hold sessions -> back to Contraction under the new high.
    Failed    : cyc_fail_e20 closes in a row below the 20 EMA, or a close giving back > cyc_fail_ret of the expansion.
                Shown for cyc_fail_show sessions; the next expansion starts again at cycle 1."""
    out = {k: None for k in ('cyc_stage', 'cyc_no', 'cyc_pct', 'cyc_days', 'cyc_speed', 'cyc_lo', 'cyc_hi', 'cyc_start',
                             'cyc_peak', 'cyc_age', 'cyc_kept', 'cyc_tight', 'cyc_vr', 'cyc_clow', 'cyc_shk_low',
                             'cyc_shk_n', 'cyc_since')}
    if i < 60: return out
    e9, e20 = ema(c, 9), ema(c, 20)
    st = None; cyc = 0; lo = hi = None; lo_i = hi_i = None; st_i = 0; b20 = 0
    shk_low = None; shk_n = 0; rcl_i = None; bo_i = None; fail_i = None; clow = None

    def leg(j, after):
        a0 = max(after, j - P['cyc_days'])
        if a0 >= j: return None
        L = min(range(a0, j), key=lambda k: (l[k], -k))
        if l[L] <= 0 or max(h[L:j + 1]) > h[j]: return None
        return L if (h[j] / l[L] - 1) * 100 >= P['cyc_min'] else None

    def start(L, j, n):
        return 'expansion', n, l[L], h[j], L, j, j

    for j in range(max(P['cyc_days'] + 1, i - P['cyc_look']), i + 1):
        b20 = b20 + 1 if (e20[j] is not None and c[j] < e20[j]) else 0
        if st in (None, 'failed'):
            if st == 'failed' and j - fail_i > P['cyc_fail_show']: st = None
            L = leg(j, 0)
            if L is not None:
                st, cyc, lo, hi, lo_i, hi_i, st_i = start(L, j, 1); shk_low = None; shk_n = 0
            continue
        if st == 'breakout':
            L = leg(j, clow_i + 0)
            if L is not None and L >= clow_i:
                st, cyc, lo, hi, lo_i, hi_i, st_i = start(L, j, cyc + 1); shk_low = None; shk_n = 0
                continue
            if h[j] > hi: hi, hi_i = h[j], j
            if b20 >= P['cyc_fail_e20'] or c[j] < clow:
                st, fail_i, st_i = 'failed', j, j; continue
            if j - bo_i >= P['cyc_bo_hold']: st, st_i = 'contraction', j
            continue
        # expansion or contraction family
        if h[j] > hi and j - hi_i < P['cyc_ctr_min']:
            hi, hi_i = h[j], j
            if st != 'expansion': st, st_i = 'expansion', j
            continue
        mv = hi - lo
        if j - hi_i < P['cyc_ctr_min']:
            continue  # still counted as the expansion
        if st == 'expansion': st, st_i = 'contraction', j
        if c[j] > hi and (h[j] - l[j] <= 0 or (c[j] - l[j]) >= 0.5 * (h[j] - l[j])):
            clow_i = min(range(hi_i + 1, j + 1), key=lambda k: (l[k], -k)); clow = l[clow_i]
            bo_i = j; st, st_i = 'breakout', j
            L = leg(j, clow_i)
            if L is not None and L >= clow_i:
                st, cyc, lo, hi, lo_i, hi_i, st_i = start(L, j, cyc + 1); shk_low = None; shk_n = 0
            else:
                hi, hi_i = h[j], j
            continue
        if b20 >= P['cyc_fail_e20'] or c[j] < lo + P['cyc_fail_ret'] * mv:
            st, fail_i, st_i = 'failed', j, j; continue
        below9 = e9[j] is not None and c[j] < e9[j]
        above20 = e20[j] is not None and c[j] > e20[j]
        if below9 and above20:
            if st != 'shakeout': st, st_i = 'shakeout', j; shk_n += 1; shk_low = l[j]
            else: shk_low = min(shk_low, l[j])
        elif st == 'shakeout' and not below9:
            st, st_i, rcl_i = 'reclaimed', j, j
        elif st == 'reclaimed' and j - rcl_i >= P['cyc_rcl_show']:
            st, st_i = 'contraction', j
    if st is None: return out
    rd = lambda x, p=2: None if x is None else round(x, p)
    out.update(cyc_stage=st, cyc_no=cyc, cyc_since=i - st_i)
    if lo is not None and hi is not None and lo_i is not None and lo > 0:
        mv = hi - lo; pct = (hi / lo - 1) * 100; days = max(1, hi_i - lo_i)
        out.update(cyc_pct=rd(pct, 1), cyc_days=days, cyc_speed=rd(pct / days, 2), cyc_lo=rd(lo), cyc_hi=rd(hi),
                   cyc_start=ds[lo_i], cyc_peak=ds[hi_i], cyc_age=i - hi_i, cyc_kept=rd((c[i] - lo) / mv if mv else None))
        if i > hi_i:
            w0 = max(hi_i + 1, i - 4)
            out['cyc_tight'] = rd((max(h[w0:i + 1]) - min(l[w0:i + 1])) / mv if mv else None)
            ve = v[lo_i + 1:hi_i + 1]; vp = v[hi_i + 1:i + 1]
            if ve and vp and sum(ve) > 0: out['cyc_vr'] = rd((sum(vp) / len(vp)) / (sum(ve) / len(ve)))
            out['cyc_clow'] = rd(min(l[hi_i + 1:i + 1]))
    if st in ('shakeout', 'reclaimed', 'contraction') and shk_low is not None:
        out.update(cyc_shk_low=rd(shk_low), cyc_shk_n=shk_n)
    if st == 'breakout' and clow is not None: out['cyc_clow'] = rd(clow)
    return out


def analyse(rows, P):
    n = len(rows)
    rd = lambda x, p=2: None if x is None else round(x, p)
    h = [float(r['h']) for r in rows]; l = [float(r['l']) for r in rows]
    c = [float(r['c']) for r in rows]; v = [float(r['v'] or 0) for r in rows]; ds = [r['d'] for r in rows]
    split_adjust(h, l, c, v)
    i = n - 1
    out = {'session_date': ds[i], 'close': rd(c[i]), 'is_vcp': False, 'state': None, 'reason': None}
    out.update(expansion(h, l, c, v, ds, i, P))
    out.update(cycle(h, l, c, v, ds, i, P))
    def fail(why, **k):
        out.update(k); out['reason'] = why; return out
    if n < 200 + P['e200_rise'] + 1:
        return fail('not enough history')
    e50, e200 = ema(c, 50), ema(c, 200)
    trend = c[i] > e50[i] > e200[i] and e200[i] > e200[i - P['e200_rise']]
    out['trend_ok'] = bool(trend)
    # base high H0: the highest high from `look` sessions ago to 3 sessions ago
    lo = max(0, i - P['look'])
    h0 = max(range(lo, i - 2), key=lambda j: (h[j], j))
    base_days = i - h0
    pl = l[max(0, h0 - P['prior_len']):h0]
    prior = (h[h0] / min(pl) - 1) * 100 if pl and min(pl) > 0 else None
    adr = sum((h[j] - l[j]) / c[j] * 100 for j in range(i - P['adr_len'] + 1, i + 1)) / P['adr_len']
    r = max(P['swing_min'], P['swing_adr'] * adr)
    pairs, live, upx = zigzag(h, l, h0, i, r)
    ts = [(pk, tr, (h[pk] - l[tr]) / h[pk] * 100) for pk, tr in pairs]
    if live: ts.append((live[0], live[1], (h[live[0]] - l[live[1]]) / h[live[0]] * 100))
    depths = [t[2] for t in ts]
    out['base_start'] = ds[h0]
    out['legs'] = json.dumps([[ds[pk], round(h[pk], 2), ds[tr], round(l[tr], 2), round(dp, 1)] for pk, tr, dp in ts]) if ts else None
    out.update(base_days=base_days, prior_adv=rd(prior, 1), adr=rd(adr), swing=rd(r),
               t_count=len(ts), depths='/'.join('%.0f' % d for d in depths) or None,
               first_depth=rd(depths[0], 1) if depths else None, last_depth=rd(depths[-1], 1) if depths else None)
    if ts:
        pk = ts[-1][0]; piv = h[pk]
        out['pivot'] = rd(piv); out['pct_to_pivot'] = rd((piv / c[i] - 1) * 100)
        vb = v[h0:i + 1]; vl = v[pk:i + 1]
        vr = (sum(vl) / len(vl)) / (sum(vb) / len(vb)) if vb and sum(vb) > 0 else None
        out['vol_ratio'] = rd(vr)
    if not trend: return fail('trend: needs close > 50 EMA > 200 EMA with the 200 rising')
    if prior is None or prior < P['prior_adv']: return fail('no 25% run-up before the base')
    if base_days < P['min_base']: return fail('base shorter than %d sessions' % P['min_base'])
    if len(ts) < P['t_min']: return fail('fewer than 2 pullbacks')
    if len(ts) > P['t_max']: return fail('more than 6 pullbacks')
    if any(depths[k] >= depths[k - 1] for k in range(1, len(depths))): return fail('pullbacks not getting smaller')
    if depths[0] > P['first_max']: return fail('first pullback deeper than 35%')
    if depths[-1] > P['last_max']: return fail('last pullback deeper than 10%')
    if piv < h[h0] * P['pivot_min']: return fail('pivot more than 10% under the base high')
    if vr is None or vr >= P['vol_ratio_max']: return fail('volume not drying up')
    # state
    va = v[max(0, i - P['vol_avg']):i]
    state = 'forming'
    above = [] if live else [j for j in range(ts[-1][1] + 1, i + 1) if c[j] > piv]
    if above:
        jb = above[0]
        if jb < i - P['bo_days'] + 1:
            return fail('broke out more than %d sessions ago' % P['bo_days'])
        vb50 = v[max(0, jb - P['vol_avg']):jb]
        bv = v[jb] / (sum(vb50) / len(vb50)) if vb50 and sum(vb50) > 0 else None
        out['bo_vol'] = rd(bv)
        state = 'breakout' if bv is not None and bv >= P['bo_vol'] else 'near'
    elif c[i] >= piv * (1 - P['near_pct'] / 100):
        state = 'near'
    out.update(is_vcp=True, state=state, reason=None)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--market', required=True, choices=['IN', 'US'])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--symbols', default='')
    ap.add_argument('--wait', action='store_true', help='wait (up to 60 min) for today\'s prices to be stored')
    a = ap.parse_args()
    mkt, P, S = a.market, PARAMS[a.market], SETTINGS[a.market]
    if a.wait and mkt == 'IN':
        today = datetime.now(IST).strftime('%Y-%m-%d')
        for k in range(12):
            if (latest_d(S['ohlc']) or '') >= today: break
            log('waiting for today\'s prices (%d/12)' % (k + 1)); time.sleep(300)
    last = latest_d(S['ohlc'])
    since = (datetime.strptime(last, '%Y-%m-%d') - timedelta(days=P['cal_days'])).strftime('%Y-%m-%d')
    q = '%s?select=symbol,d,h,l,c,v&d=gte.%s&order=d.asc,symbol.asc' % (S['ohlc'], since)
    if a.symbols: q += '&symbol=in.(%s)' % a.symbols
    log(mkt, 'loading prices from', since, 'to', last)
    ser = {}
    for r in get_all(q):
        if None in (r.get('h'), r.get('l'), r.get('c')): continue
        ser.setdefault(r['symbol'], []).append(r)
    log('%d symbols' % len(ser))
    out, now = [], datetime.now(IST).isoformat()
    for sym, rs in ser.items():
        if rs[-1]['d'] != last: continue
        try:
            x = analyse(rs, P)
        except Exception as e:
            log('  %s: %s' % (sym, e)); continue
        x['symbol'] = sym; x['computed_at'] = now; out.append(x)
    v = [x for x in out if x['is_vcp']]
    cnt = lambda s: sum(1 for x in v if x['state'] == s)
    trend = sum(1 for x in out if x.get('trend_ok'))
    top = sorted([x for x in v if x['state'] in ('breakout', 'near')], key=lambda x: (x['state'] != 'breakout', x['pct_to_pivot']))[:10]
    summ = ('%s 📐 True VCP %s · %d stocks · %d in an uptrend · %d VCPs: 🚀 breakout %d · 🎯 near pivot %d · ⏳ forming %d'
            % (S['flag'], last, len(out), trend, len(v), cnt('breakout'), cnt('near'), cnt('forming')))
    if top:
        summ += '\n' + '\n'.join('%s %s · %dT %s · pivot %s (%+.1f%%)' % ('🚀' if x['state'] == 'breakout' else '🎯', x['symbol'], x['t_count'], x['depths'], x['pivot'], -x['pct_to_pivot']) for x in top)
    eb = [x for x in out if x.get('exp_state') == 'burst']; ep = [x for x in out if x.get('exp_state') == 'pause']
    cc = {}
    for x in out:
        if x.get('cyc_stage'): cc[x['cyc_stage']] = cc.get(x['cyc_stage'], 0) + 1
    summ += ('\n🔄 Cycle: expansion %d · contraction %d · shakeout %d · reclaimed %d · breakout %d · failed %d'
             % tuple(cc.get(k, 0) for k in ('expansion', 'contraction', 'shakeout', 'reclaimed', 'breakout', 'failed')))
    shk = sorted([x for x in out if x.get('cyc_stage') == 'shakeout'], key=lambda x: -(x.get('cyc_pct') or 0))[:8]
    if shk:
        summ += '\n' + '\n'.join('⚠️ %s cycle %d · +%.0f%% in %dd · contraction day %d · below 9 EMA, above 20' % (x['symbol'], x['cyc_no'], x['cyc_pct'], x['cyc_days'], x['cyc_age']) for x in shk)
    summ += '\n🚀 Fast expansions %d · ⏸ tight contractions %d' % (len(eb), len(ep))
    if ep:
        summ += '\n' + '\n'.join('⏸ %s +%.0f%% in %dd · day %d of tight contraction · kept %.0f%% · vol %.1f×' % (x['symbol'], x['exp_pct'], x['exp_days'], x['exp_age'], x['exp_kept'] * 100, x['exp_vr']) for x in sorted(ep, key=lambda x: -x['exp_pct'])[:8])
    log(summ)
    if a.dry_run or a.symbols:
        for x in [y for y in out if y.get('cyc_stage')][:15]: print(json.dumps({k: x.get(k) for k in ('symbol', 'cyc_stage', 'cyc_no', 'cyc_pct', 'cyc_days', 'cyc_age', 'cyc_kept', 'cyc_since', 'cyc_shk_low')}))
        for x in (eb + ep)[:10]: print(json.dumps({k: x.get(k) for k in ('symbol', 'exp_state', 'exp_pct', 'exp_days', 'exp_age', 'exp_kept', 'exp_tight', 'exp_vr', 'exp_start', 'exp_peak')}))
        for x in (v or out)[:15]: print(json.dumps(x))
        why = {}
        for x in out:
            if x['reason']: why[x['reason']] = why.get(x['reason'], 0) + 1
        for k, nn in sorted(why.items(), key=lambda t: -t[1]): print('  %5d  %s' % (nn, k))
        log('dry run: nothing saved'); return
    hh = dict(H, **{'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'})
    keys = ['symbol', 'session_date', 'close', 'is_vcp', 'state', 'reason', 'trend_ok', 'base_days', 'prior_adv', 'adr', 'swing',
            't_count', 'depths', 'first_depth', 'last_depth', 'pivot', 'pct_to_pivot', 'vol_ratio', 'bo_vol', 'base_start', 'legs', 'exp_state', 'exp_pct', 'exp_days', 'exp_age',
            'exp_kept', 'exp_tight', 'exp_vr', 'exp_high', 'exp_start', 'exp_peak', 'cyc_stage', 'cyc_no', 'cyc_pct', 'cyc_days',
            'cyc_speed', 'cyc_lo', 'cyc_hi', 'cyc_start', 'cyc_peak', 'cyc_age', 'cyc_kept', 'cyc_tight', 'cyc_vr', 'cyc_clow',
            'cyc_shk_low', 'cyc_shk_n', 'cyc_since', 'computed_at']
    rows = [{k: x.get(k) for k in keys} for x in out]
    for j in range(0, len(rows), 500):
        r = requests.post('%s/rest/v1/%s?on_conflict=symbol' % (C.SUPABASE_URL, S['out']), headers=hh, data=json.dumps(rows[j:j + 500]), timeout=120)
        if r.status_code >= 300: sys.exit('write failed %s %s' % (r.status_code, r.text[:300]))
    requests.delete('%s/rest/v1/%s?session_date=lt.%s' % (C.SUPABASE_URL, S['out'], last), headers=H, timeout=60)
    log('saved %d rows to %s' % (len(rows), S['out']))
    try:
        requests.post('https://api.telegram.org/bot%s/sendMessage' % C.TF_ALERTS_TOKEN,
                      data={'chat_id': getattr(C, S['chat']), 'text': summ}, timeout=30)
    except Exception as e:
        log('telegram failed', e)


if __name__ == '__main__':
    main()
