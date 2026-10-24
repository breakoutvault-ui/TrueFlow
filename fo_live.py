#!/root/trueflow/bin/python
"""
fo_live.py - live snapshot of every F&O stock for the Live Alerts tab (India).

Morning (once): F&O list + sector from the latest ema_filter session; from daily_ohlc (days before today,
split-adjusted): yesterday's high / low / close, daily ATR(14, Wilder), daily 9 / 20 EMA, trend:
  Neutral = 9 and 20 EMA crossed 2+ times in the last 10 sessions
  Up      = 9 EMA > 20 EMA and both higher than 3 sessions ago
  Down    = 9 EMA < 20 EMA and both lower than 3 sessions ago
  else Neutral
Futures: near + next month futures for each stock (Kite NFO); yesterday's OI = sum of both from Kite daily history.

Every minute 09:15-15:30 IST: one Kite quote for all stocks + their futures + NIFTY 50 -> table fo_live
(one row per symbol, replaced each minute): price, day open/high/low, % change, volume, live 9/20 EMA
(yesterday's EMA moved by today's price), futures OI now vs yesterday and the OI stage:
  price up + OI up = Long Buildup · price up + OI down = Short Covering
  price down + OI up = Short Buildup · price down + OI down = Long Unwinding   (|OI change| < 0.5% = Flat)
Opening ranges from Kite candles: or15 = 9:15-9:30 (first 15-minute candle, after 9:30),
or60 = 9:15-10:15 (first 60-minute candle, after 10:15).
The dashboard does all comparisons (near / broken PDH-PDL, ORB status, NARROW / WIDE, strength vs Nifty).

  bin/python fo_live.py [--loop] [--once] [--dry-run]
  cron (UTC): 44 3 * * 1-5  cd /root/trueflow && bin/python fo_live.py --loop >> fo_live.log 2>&1
"""
import argparse, json, os, sys, time
from datetime import datetime, timedelta, date
import requests
import tf_config as C
from hi52_scan import split_adjust, get_all, log, H, IST

BASE = os.path.dirname(os.path.abspath(__file__))
NIFTY = 'NSE:NIFTY 50'


def tg(msg):
    try:
        requests.post('https://api.telegram.org/bot%s/sendMessage' % C.TF_ALERTS_TOKEN,
                      data={'chat_id': C.TF_SYSTEM, 'text': msg}, timeout=20)
    except Exception:
        pass


def kite_login():
    from kiteconnect import KiteConnect
    tok = open(getattr(C, 'ACCESS_TOKEN_PATH', os.path.join(BASE, 'access_token.txt'))).read().strip()
    k = KiteConnect(api_key=C.KITE_API_KEY)
    k.set_access_token(tok)
    return k


def ema_series(c, n):
    k, out, e = 2.0 / (n + 1), [], None
    for x in c:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def daily_context(syms, today):
    """yesterday's levels, ATR, EMAs and trend per symbol from daily_ohlc (days before today)."""
    since = (today - timedelta(days=200)).isoformat()
    rows = {}
    lst = sorted(syms)
    for i in range(0, len(lst), 40):
        chunk = ','.join('"%s"' % s for s in lst[i:i + 40])
        for r in get_all('daily_ohlc?select=symbol,d,h,l,c,v&symbol=in.(%s)&d=gte.%s&d=lt.%s&order=d.asc'
                         % (requests.utils.quote(chunk, safe='",'), since, today.isoformat())):
            rows.setdefault(r['symbol'], []).append(r)
    out = {}
    for s, rr in rows.items():
        h = [float(x['h']) for x in rr]; l = [float(x['l']) for x in rr]
        c = [float(x['c']) for x in rr]; v = [float(x['v'] or 0) for x in rr]
        if len(c) < 30: continue
        split_adjust(h, l, c, v)
        e9, e20 = ema_series(c, 9), ema_series(c, 20)
        tr = [h[0] - l[0]] + [max(h[j] - l[j], abs(h[j] - c[j - 1]), abs(l[j] - c[j - 1])) for j in range(1, len(c))]
        atr = sum(tr[1:15]) / 14
        for j in range(15, len(tr)): atr = (atr * 13 + tr[j]) / 14
        sign = [1 if e9[j] > e20[j] else -1 for j in range(len(c))]
        cross10 = sum(1 for j in range(len(c) - 10, len(c)) if sign[j] != sign[j - 1])
        j = len(c) - 1
        if cross10 >= 2: trend = 'neutral'
        elif e9[j] > e20[j] and e9[j] > e9[j - 3] and e20[j] > e20[j - 3]: trend = 'up'
        elif e9[j] < e20[j] and e9[j] < e9[j - 3] and e20[j] < e20[j - 3]: trend = 'down'
        else: trend = 'neutral'
        out[s] = dict(pdh=round(h[j], 2), pdl=round(l[j], 2), pdc=round(c[j], 2), pd=rr[-1]['d'],
                      atr=round(atr, 2), e9=round(e9[j], 2), e20=round(e20[j], 2), trend=trend, cross10=cross10)
    return out


def setup(kite, today, dry):
    t = get_all('ema_filter?select=session_date&order=session_date.desc&limit=1')
    sd = t[0]['session_date']
    ef = get_all('ema_filter?select=symbol,sector&session_date=eq.%s' % sd)
    sector = {r['symbol']: r.get('sector') or 'Unknown' for r in ef}
    syms = sorted(sector)
    log('F&O list: %d symbols (ema_filter %s)' % (len(syms), sd))
    ctx = daily_context(syms, today)
    log('Daily context: %d of %d symbols' % (len(ctx), len(syms)))
    nse = {i['tradingsymbol']: i['instrument_token'] for i in kite.instruments('NSE') if i.get('segment') == 'NSE'}
    futs = {}
    for i in kite.instruments('NFO'):
        if i.get('instrument_type') == 'FUT' and i['name'] in sector and i['expiry'] and i['expiry'] >= today:
            futs.setdefault(i['name'], []).append(i)
    fut = {}
    for s, lst in futs.items():
        lst.sort(key=lambda x: x['expiry'])
        fut[s] = lst[:2]                                    # near + next month
    log('Futures found: %d symbols' % len(fut))
    # yesterday's OI (sum of near + next) from Kite daily history
    prev_oi, k = {}, 0
    d0 = today - timedelta(days=10)
    for s, lst in fut.items():
        tot, ok = 0, True
        for f in lst:
            try:
                hist = kite.historical_data(f['instrument_token'], d0, today - timedelta(days=1), 'day', oi=True)
                tot += int(hist[-1].get('oi') or 0) if hist else 0
            except Exception as e:
                ok = False
            k += 1
            if k % 3 == 0: time.sleep(1.05)                 # Kite history: 3 requests a second
        if ok and tot > 0: prev_oi[s] = tot
    log('Yesterday OI: %d symbols' % len(prev_oi))
    return dict(syms=syms, sector=sector, ctx=ctx, nse=nse, fut=fut, prev_oi=prev_oi, or15={}, or60={})


def first_candle(kite, S, today, minutes, interval):
    """high/low of the first candle of `interval` starting 9:15 (needs 9:15 + minutes to have passed)."""
    out, k = {}, 0
    t0 = datetime(today.year, today.month, today.day, 9, 15)
    for s in S['syms']:
        tk = S['nse'].get(s)
        if not tk: continue
        try:
            c = kite.historical_data(tk, t0, t0 + timedelta(minutes=minutes), interval)
            if c:
                f = c[0]
                out[s] = (round(float(f['high']), 2), round(float(f['low']), 2))
        except Exception:
            pass
        k += 1
        if k % 3 == 0: time.sleep(1.05)
    return out


def oi_stage(pchg, ochg):
    if ochg is None or pchg is None: return None
    if abs(ochg) < 0.5: return 'Flat OI'
    if pchg >= 0: return 'Long Buildup' if ochg > 0 else 'Short Covering'
    return 'Short Buildup' if ochg > 0 else 'Long Unwinding'


def snapshot(kite, S, today, dry):
    keys = ['NSE:' + s for s in S['syms']] + [NIFTY]
    for s, lst in S['fut'].items():
        keys += ['NFO:' + f['tradingsymbol'] for f in lst]
    q = {}
    for i in range(0, len(keys), 450):
        q.update(kite.quote(keys[i:i + 450]))
    now = datetime.now(IST)
    rows = []
    nq = q.get(NIFTY)
    if nq:
        pc = nq['ohlc']['close'] or 0
        rows.append(dict(symbol='NIFTY 50', d=today.isoformat(), ts=now.isoformat(), sector='Index',
                         ltp=nq['last_price'], open=nq['ohlc']['open'], high=nq['ohlc']['high'], low=nq['ohlc']['low'],
                         prev_close=pc, chg_pct=round((nq['last_price'] / pc - 1) * 100, 2) if pc else None))
    for s in S['syms']:
        x = q.get('NSE:' + s)
        if not x: continue
        o = x['ohlc']; ltp = x['last_price']; pc = o['close'] or None
        c = S['ctx'].get(s, {})
        r = dict(symbol=s, d=today.isoformat(), ts=now.isoformat(), sector=S['sector'].get(s),
                 ltp=ltp, open=o['open'], high=o['high'], low=o['low'], prev_close=pc,
                 chg_pct=round((ltp / pc - 1) * 100, 2) if pc else None, vol=x.get('volume'),
                 pdh=c.get('pdh'), pdl=c.get('pdl'), atr=c.get('atr'), e9=c.get('e9'), e20=c.get('e20'),
                 trend=c.get('trend'), cross10=c.get('cross10'))
        if c.get('e9'):
            r['e9_live'] = round(c['e9'] + (2.0 / 10) * (ltp - c['e9']), 2)
            r['e20_live'] = round(c['e20'] + (2.0 / 21) * (ltp - c['e20']), 2)
        lst = S['fut'].get(s)
        if lst:
            oi = sum(int((q.get('NFO:' + f['tradingsymbol']) or {}).get('oi') or 0) for f in lst)
            r['fut_oi'] = oi or None
            po = S['prev_oi'].get(s)
            if oi and po:
                r['fut_oi_prev'] = po
                r['oi_chg_pct'] = round((oi / po - 1) * 100, 2)
                r['oi_stage'] = oi_stage(r['chg_pct'], r['oi_chg_pct'])
        if s in S['or15']: r['or15_h'], r['or15_l'] = S['or15'][s]
        if s in S['or60']: r['or60_h'], r['or60_l'] = S['or60'][s]
        rows.append(r)
    # every row carries the same keys (PostgREST bulk upsert)
    allk = set(); [allk.update(r) for r in rows]
    rows = [{k: r.get(k) for k in allk} for r in rows]
    if dry:
        log('dry run: %d rows; sample %s' % (len(rows), json.dumps(rows[1] if len(rows) > 1 else rows[:1], default=str)[:600]))
        return len(rows)
    hh = dict(H, **{'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'})
    for i in range(0, len(rows), 500):
        rq = requests.post('%s/rest/v1/fo_live?on_conflict=symbol' % C.SUPABASE_URL, headers=hh,
                           data=json.dumps(rows[i:i + 500], default=str), timeout=60)
        if rq.status_code >= 300: log('write failed %s %s' % (rq.status_code, rq.text[:200]))
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    today = datetime.now(IST).date()
    if today.weekday() >= 5 and a.loop:
        log('weekend - nothing to do'); return
    kite, S = None, None
    for att in range(50):                                   # wait for the morning Kite login (up to ~50 min)
        try:
            kite = kite_login(); kite.profile()
            S = setup(kite, today, a.dry_run)
            break
        except Exception as e:
            log('waiting for Kite (%s)' % str(e)[:120])
            if not a.loop: raise
            time.sleep(60)
    if S is None:
        tg('⚠️ fo_live: no Kite login by %s, live F&O table not updating today' % datetime.now(IST).strftime('%H:%M'))
        return
    if not a.loop:
        n = snapshot(kite, S, today, a.dry_run); log('one snapshot: %d rows' % n); return
    tg('📡 fo_live started: %d F&O stocks, %d with futures OI' % (len(S['syms']), len(S['prev_oi'])))
    t_or15 = datetime(today.year, today.month, today.day, 9, 30, 30, tzinfo=IST)
    t_or60 = datetime(today.year, today.month, today.day, 10, 15, 30, tzinfo=IST)
    t_end = datetime(today.year, today.month, today.day, 15, 31, tzinfo=IST)
    n_ok, n_err, last = 0, 0, 0
    S['open_checked'] = False
    while datetime.now(IST) < t_end:
        now = datetime.now(IST)
        if now.hour < 9 or (now.hour == 9 and now.minute < 15):
            time.sleep(20); continue
        try:
            if not S['or15'] and now >= t_or15:
                S['or15'] = first_candle(kite, S, today, 15, '15minute'); log('9:15-9:30 range: %d' % len(S['or15']))
            if not S['or60'] and now >= t_or60:
                S['or60'] = first_candle(kite, S, today, 60, '60minute'); log('9:15-10:15 range: %d' % len(S['or60']))
            if not S.get('open_checked') and (now.hour > 9 or now.minute >= 20):
                S['open_checked'] = True
                lt = (kite.quote([NIFTY]).get(NIFTY) or {}).get('last_trade_time')
                if lt and lt.date() != today:
                    log('market closed today (last Nifty trade %s) - stopping' % lt); tg('📡 fo_live: market closed today, not running'); return
            last = snapshot(kite, S, today, a.dry_run); n_ok += 1
        except Exception as e:
            n_err += 1; log('snapshot failed: %s' % str(e)[:200])
            if n_err in (3, 30): tg('⚠️ fo_live: %d failed snapshots today, last error: %s' % (n_err, str(e)[:150]))
        nxt = (datetime.now(IST) + timedelta(minutes=1)).replace(second=5, microsecond=0)
        time.sleep(max(1, (nxt - datetime.now(IST)).total_seconds()))
    tg('📡 fo_live done: %d snapshots, %d failed, %d rows each' % (n_ok, n_err, last))


if __name__ == '__main__':
    main()
