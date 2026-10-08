#!/root/trueflow/bin/python
"""
hi52_scan.py - 52-week-high conviction signals, per market, from stored daily prices.

For every stock (latest session):
  new_hi        today's high is a new 52-week high (above the previous 252 sessions)
  close_above   today's close is above the previous 52-week high
  strong        close_above AND close in the top 25% of the day's range AND volume >= 1.5x the 20-day average
  wk_state      'this'  = this week's close (so far) is above the 52W high as it stood before the week
                'last'  = last completed week did that
  wk_vr         that week's average daily volume vs the 10 weeks before it (x); wk_ok = wk_vr >= 1.5,
                or the week before it was >= 1.5x
  fresh         a new 52W high in the last 5 sessions that is the first one after >= 8 weeks without one
  gap_wk        weeks between this new high and the previous one
  ext_pct       close vs the 52W high level it broke (%); not extended = <= 5%
  rs3m          3-month return minus the index's 3-month return (pct points); leader = > 0
Prices are split/bonus-adjusted on the fly (split_adjust) before anything is measured.
Writes hi52_state (IN) / us_hi52_state (US). Read-only on everything else.

  bin/python hi52_scan.py --market IN [--dry-run] [--symbols A,B] [--wait]
"""
import argparse, json, sys, time
from datetime import datetime, timedelta, timezone
import requests
import tf_config as C

IST = timezone(timedelta(hours=5, minutes=30))
H = {'apikey': C.SUPABASE_KEY, 'Authorization': 'Bearer ' + C.SUPABASE_KEY}
SETTINGS = {
    'IN': {'ohlc': 'daily_ohlc', 'out': 'hi52_state', 'mh': 'IN', 'chat': 'TF_SYSTEM', 'flag': '🇮🇳'},
    'US': {'ohlc': 'us_daily_ohlc', 'out': 'us_hi52_state', 'mh': 'US', 'chat': 'TF_US', 'flag': '🇺🇸'},
}
BASE = dict(look=252, vol_avg=20, strong_vol=1.5, strong_clv=0.75, wk_vol=1.5, wk_avg=10,
            fresh_within=5, fresh_gap=40, not_ext=5.0, rs_len=63, cal_days=470)
PARAMS = {'IN': dict(BASE), 'US': dict(BASE)}


def log(*a):
    print(datetime.now(IST).strftime('%H:%M:%S'), *a, flush=True)


def get_all(path):
    out, off = [], 0
    sep = '&' if '?' in path else '?'
    while True:
        for k in range(4):
            try:
                r = requests.get('%s/rest/v1/%s%slimit=1000&offset=%d' % (C.SUPABASE_URL, path, sep, off), headers=H, timeout=120)
                r.raise_for_status(); b = r.json(); break
            except Exception:
                if k == 3: raise
                time.sleep(3 * (k + 1))
        out += b
        if len(b) < 1000: return out
        off += 1000


def latest_d(tbl):
    r = requests.get('%s/rest/v1/%s?select=d&order=d.desc&limit=1' % (C.SUPABASE_URL, tbl), headers=H, timeout=60)
    r.raise_for_status(); j = r.json()
    return j[0]['d'] if j else None


def wkey(d):
    dt = datetime.strptime(d, '%Y-%m-%d')
    return (dt - timedelta(days=dt.weekday())).strftime('%Y-%m-%d')


def analyse(rows, idx, P):
    """rows: list of dicts d,h,l,c,v ascending. idx: {d: index close}. Returns dict or None."""
    n = len(rows)
    if n < 60: return None
    h = [float(r['h']) for r in rows]; l = [float(r['l']) for r in rows]
    c = [float(r['c']) for r in rows]; v = [float(r['v'] or 0) for r in rows]; ds = [r['d'] for r in rows]
    split_adjust(h, l, c, v)
    i = n - 1
    pm = prevmax(h, P['look'])                 # pm[j] = highest high of the `look` sessions before j
    prev_hi = pm[i]
    if not prev_hi: return None
    hi52 = max(prev_hi, h[i])
    rng = h[i] - l[i]
    clv = (c[i] - l[i]) / rng if rng > 0 else 1.0
    va = v[max(0, i - P['vol_avg']):i]
    rvol = v[i] / (sum(va) / len(va)) if va and sum(va) > 0 else None
    new_hi = h[i] > prev_hi
    close_above = c[i] > prev_hi
    strong = bool(close_above and clv >= P['strong_clv'] - 1e-9 and rvol is not None and rvol >= P['strong_vol'])

    # new-high days over the window we have (each compared with its own previous 252 sessions)
    nh = [j for j in range(1, n) if pm[j] is not None and h[j] > pm[j]]
    recent = [j for j in nh if j >= i - P['fresh_within'] + 1]
    fresh, gap_wk, lvl = False, None, None
    if recent:
        j = recent[0]
        before = [k for k in nh if k < j]
        gap = (j - before[-1]) if before else None
        gap_wk = round(gap / 5.0, 1) if gap is not None else None
        fresh = gap is not None and gap >= P['fresh_gap']
        lvl = pm[j]                                   # the level broken at that new high
    # extension: vs the 52W high level that the latest breakout cleared (or the previous high)
    base_lvl = lvl if lvl else prev_hi
    ext_pct = (c[i] / base_lvl - 1) * 100 if base_lvl else None

    # weekly
    weeks, order = {}, []
    for k in range(n):
        w = wkey(ds[k])
        if w not in weeks: weeks[w] = []; order.append(w)
        weeks[w].append(k)
    def wk_eval(wi):
        if wi < 0 or wi >= len(order): return None
        ks = weeks[order[wi]]
        first = ks[0]
        lvl_w = pm[first]
        wclose = c[ks[-1]]
        av = sum(v[k] for k in ks) / len(ks)
        prev = []
        for pw in order[max(0, wi - P['wk_avg']):wi]:
            pk = weeks[pw]; prev.append(sum(v[k] for k in pk) / len(pk))
        vr = av / (sum(prev) / len(prev)) if prev and sum(prev) > 0 else None
        return {'above': bool(lvl_w and wclose > lvl_w), 'vr': vr, 'lvl': lvl_w}
    cur, last, last2 = wk_eval(len(order) - 1), wk_eval(len(order) - 2), wk_eval(len(order) - 3)
    wk_state, wk_vr, wk_vr_prev = None, None, None
    if cur and cur['above']:
        wk_state, wk_vr, wk_vr_prev = 'this', cur['vr'], (last or {}).get('vr')
    elif last and last['above']:
        wk_state, wk_vr, wk_vr_prev = 'last', last['vr'], (last2 or {}).get('vr')
    wk_ok = bool(wk_state and ((wk_vr or 0) >= P['wk_vol'] or (wk_vr_prev or 0) >= P['wk_vol']))

    # relative strength vs the index
    rs3m = None
    if i >= P['rs_len'] and c[i - P['rs_len']] > 0:
        r_s = (c[i] / c[i - P['rs_len']] - 1) * 100
        d0, d1 = ds[i - P['rs_len']], ds[i]
        i0 = idx.get(d0) or _near(idx, d0); i1 = idx.get(d1) or _near(idx, d1)
        if i0 and i1: rs3m = r_s - (i1 / i0 - 1) * 100

    rd = lambda x, p=2: None if x is None else round(x, p)
    return {'session_date': ds[i], 'close': rd(c[i]), 'hi52': rd(hi52), 'prev_hi': rd(prev_hi),
            'pct_from_hi': rd((c[i] / hi52 - 1) * 100), 'new_hi': bool(new_hi), 'close_above': bool(close_above),
            'clv': rd(clv), 'rvol': rd(rvol), 'strong': strong, 'wk_state': wk_state, 'wk_vr': rd(wk_vr),
            'wk_vr_prev': rd(wk_vr_prev), 'wk_ok': wk_ok, 'fresh': bool(fresh), 'gap_wk': gap_wk,
            'ext_pct': rd(ext_pct), 'rs3m': rd(rs3m)}


def split_adjust(h, l, c, v):
    """Stored prices may not be adjusted for splits / bonuses. A day whose WHOLE range sits about
    1/2, 1/3, 1/4, 1/5 or 1/10 of the previous close (or that many times above it, for a reverse split)
    is treated as a split, and every earlier price is divided by that factor (volume multiplied)."""
    for j in range(len(c) - 1, 0, -1):
        p = c[j - 1]
        if p <= 0 or c[j] <= 0: continue
        f = None
        if h[j] < p * 0.62:                       # whole day far below yesterday's close
            r = p / c[j]
            for k in (2, 3, 4, 5, 10):
                if abs(r / k - 1) < 0.08: f = k; break
        elif l[j] > p * 1.6:                      # whole day far above: reverse split
            r = c[j] / p
            for k in (2, 3, 4, 5, 10):
                if abs(r / k - 1) < 0.08: f = 1.0 / k; break
        if f:
            for i in range(j):
                h[i] /= f; l[i] /= f; c[i] /= f; v[i] *= f


def prevmax(h, look):
    from collections import deque
    out, dq = [None] * len(h), deque()
    for j in range(len(h)):
        while dq and dq[0] < j - look: dq.popleft()
        out[j] = h[dq[0]] if dq else None
        while dq and h[dq[-1]] <= h[j]: dq.pop()
        dq.append(j)
    return out


def _near(idx, d):
    ks = [k for k in idx if k <= d]
    return idx[max(ks)] if ks else None


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
    rows = get_all(q)
    ser = {}
    for r in rows:
        if None in (r.get('h'), r.get('l'), r.get('c')): continue
        ser.setdefault(r['symbol'], []).append(r)
    mh = get_all('market_history?select=d,idx_close&market=eq.%s&d=gte.%s&order=d.asc' % (S['mh'], since))
    idx = {r['d']: float(r['idx_close']) for r in mh if r.get('idx_close')}
    log('%d symbols · index closes %d' % (len(ser), len(idx)))
    out, now = [], datetime.now(IST).isoformat()
    for sym, rs in ser.items():
        if rs[-1]['d'] != last: continue          # not traded on the latest session
        try:
            x = analyse(rs, idx, P)
        except Exception as e:
            log('  %s: %s' % (sym, e)); continue
        if x:
            x['symbol'] = sym; x['computed_at'] = now; out.append(x)
    c = lambda k: sum(1 for x in out if x.get(k))
    summ = ('%s 52W highs %s · %d stocks · new high %d · close above %d · strong close+vol %d · weekly close above %d (on volume %d) · fresh %d'
            % (S['flag'], last, len(out), c('new_hi'), c('close_above'), c('strong'), c('wk_state'), c('wk_ok'), c('fresh')))
    log(summ)
    if a.dry_run or a.symbols:
        for x in out[:15]: print(json.dumps(x))
        log('dry run: nothing saved'); return
    hh = dict(H, **{'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'})
    # keep the table to today's universe: drop rows for stocks that no longer trade
    for i in range(0, len(out), 500):
        r = requests.post('%s/rest/v1/%s?on_conflict=symbol' % (C.SUPABASE_URL, S['out']), headers=hh, data=json.dumps(out[i:i + 500]), timeout=120)
        if r.status_code >= 300: sys.exit('write failed %s %s' % (r.status_code, r.text[:300]))
    requests.delete('%s/rest/v1/%s?session_date=lt.%s' % (C.SUPABASE_URL, S['out'], last), headers=H, timeout=60)
    log('saved %d rows to %s' % (len(out), S['out']))
    try:
        requests.post('https://api.telegram.org/bot%s/sendMessage' % C.TF_ALERTS_TOKEN,
                      data={'chat_id': getattr(C, S['chat']), 'text': summ}, timeout=30)
    except Exception as e:
        log('telegram failed', e)


if __name__ == '__main__':
    main()
