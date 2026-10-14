#!/root/trueflow/bin/python
"""
picks_score.py - scores Mahesh's 🎯 Next-Day Picks (India) from stored daily prices.

For every pick in next_picks that is not fully scored:
  session      the first trading session on/after for_date (from market_history, market IN)
  ref_close    close of the session before it
  gap_pct      open vs ref_close
  d1_ret       open -> close of the session (the day-trade view)
  d1_up/d1_dn  highest high / lowest low of the session vs its open
  d1_cc        close vs ref_close (what holding from the pick evening gave)
  d5_ret       close of the 5th session (session included) vs ref_close; filled once it exists
  mkt_*        the same numbers averaged over every stock in daily_ohlc that day (the market)
  ndwl         the Next Day WL (nextday_picks_v2) also picked it for that session, and its score
  orb / orb_r  the first ORB trade that day: orb_backtest (simulated, exit A, R after costs) if it is there,
               else the fo_alerts alert (fired, not simulated), else 'no ORB signal'
Read-only on everything except next_picks.

  bin/python picks_score.py [--wait] [--weekly] [--dry-run]
"""
import argparse, json, sys, time
from datetime import datetime, timedelta, timezone
import requests
import tf_config as C
from hi52_scan import get_all, latest_d, log, H, IST

TBL = 'next_picks'


def sb(path):
    r = requests.get('%s/rest/v1/%s' % (C.SUPABASE_URL, path), headers=H, timeout=120)
    r.raise_for_status()
    return r.json()


def patch(pid, body):
    hh = dict(H, **{'Content-Type': 'application/json', 'Prefer': 'return=minimal'})
    r = requests.patch('%s/rest/v1/%s?id=eq.%s' % (C.SUPABASE_URL, TBL, pid), headers=hh, data=json.dumps(body), timeout=60)
    if r.status_code >= 300:
        log('  write failed %s %s' % (r.status_code, r.text[:200]))


def pct(a, b):
    return None if a is None or b in (None, 0) else round((a / b - 1) * 100, 2)


_mkt = {}
def market(d, ref, d5):
    """average over all stocks: open->close on d, close(d) vs close(ref), close(d5) vs close(ref)"""
    k = (d, ref, d5)
    if k in _mkt: return _mkt[k]
    day = {r['symbol']: r for r in get_all('daily_ohlc?select=symbol,o,c&d=eq.%s' % d)}
    prv = {r['symbol']: r for r in get_all('daily_ohlc?select=symbol,c&d=eq.%s' % ref)}
    oc = [(float(r['c']) / float(r['o']) - 1) * 100 for r in day.values() if r.get('o') and r.get('c') and float(r['o']) > 0]
    cc = [(float(day[s]['c']) / float(prv[s]['c']) - 1) * 100 for s in day if s in prv and day[s].get('c') and prv[s].get('c') and float(prv[s]['c']) > 0]
    f5 = None
    if d5:
        fut = {r['symbol']: r for r in get_all('daily_ohlc?select=symbol,c&d=eq.%s' % d5)}
        x = [(float(fut[s]['c']) / float(prv[s]['c']) - 1) * 100 for s in fut if s in prv and fut[s].get('c') and prv[s].get('c') and float(prv[s]['c']) > 0]
        f5 = round(sum(x) / len(x), 2) if x else None
    avg = lambda a: round(sum(a) / len(a), 2) if a else None
    _mkt[k] = (avg(oc), avg(cc), f5)
    return _mkt[k]


def orb_of(sym, d):
    try:
        bt = sb('orb_backtest?select=direction,status,exit_a_r,net_a_r&symbol=eq.%s&session_date=eq.%s&order=fired_at.asc&limit=1' % (sym, d))
        if bt:
            b = bt[0]; dr = (b.get('direction') or '').upper()
            if b.get('net_a_r') is not None:
                return ('%s · exit A %+.2fR · after costs %+.2fR' % (dr, float(b['exit_a_r'] or 0), float(b['net_a_r'])), round(float(b['net_a_r']), 3))
            return ('%s · %s' % (dr, b.get('status') or 'no trade'), None)
        fa = sb('fo_alerts?select=direction,stage,outcome,points_captured&symbol=eq.%s&session_date=eq.%s&order=fired_at.asc&limit=1' % (sym, d))
        if fa:
            a = fa[0]
            return ('%s alert (stage %s)%s · not simulated' % ((a.get('direction') or '').upper(), a.get('stage'),
                    (' · ' + a['outcome']) if a.get('outcome') else ''), None)
        return ('no ORB signal', None)
    except Exception as e:
        log('  orb lookup failed %s %s: %s' % (sym, d, e)); return (None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--wait', action='store_true', help="wait (up to 60 min) for today's prices")
    ap.add_argument('--weekly', action='store_true', help='send the weekly Telegram summary')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    if a.wait:
        today = datetime.now(IST).strftime('%Y-%m-%d')
        for k in range(12):
            if (latest_d('daily_ohlc') or '') >= today: break
            log("waiting for today's prices (%d/12)" % (k + 1)); time.sleep(300)
    since = (datetime.now(IST) - timedelta(days=60)).strftime('%Y-%m-%d')
    days = [r['d'] for r in get_all('market_history?select=d&market=eq.IN&d=gte.%s&order=d.asc' % since)]
    last = latest_d('daily_ohlc')
    days = [d for d in days if d <= last]
    if last not in days: days.append(last)
    picks = get_all('%s?select=*&for_date=gte.%s&d5_ret=is.null&order=for_date.asc' % (TBL, since))
    log('%d picks to score · latest prices %s' % (len(picks), last))
    done = 0
    for p in picks:
        s_d = next((d for d in days if d >= p['for_date']), None)
        if not s_d:
            continue                                   # its session has not happened yet
        i = days.index(s_d)
        ref = days[i - 1] if i > 0 else None
        d5 = days[i + 4] if i + 4 < len(days) else None
        rows = get_all('daily_ohlc?select=d,o,h,l,c&symbol=eq.%s&d=gte.%s&order=d.asc' % (p['symbol'], ref or s_d))
        by = {r['d']: r for r in rows}
        r1, r0 = by.get(s_d), by.get(ref)
        if not r1 or not r0:
            log('  %s %s: no prices for %s' % (p['symbol'], p['for_date'], s_d)); continue
        f = lambda r, k: float(r[k]) if r and r.get(k) is not None else None
        o, h, l, c, rc = f(r1, 'o'), f(r1, 'h'), f(r1, 'l'), f(r1, 'c'), f(r0, 'c')
        c5 = f(by.get(d5), 'c') if d5 else None
        m1, mcc, m5 = market(s_d, ref, d5)
        nd = sb('nextday_picks_v2?select=score&target_date=eq.%s&symbol=eq.%s&limit=1' % (s_d, p['symbol']))
        orb, orb_r = orb_of(p['symbol'], s_d)
        body = {'orb': orb, 'orb_r': orb_r, 'session_date': s_d, 'ref_close': rc, 'd1_open': o, 'd1_high': h, 'd1_low': l, 'd1_close': c,
                'gap_pct': pct(o, rc), 'd1_ret': pct(c, o), 'd1_up': pct(h, o), 'd1_dn': pct(l, o), 'd1_cc': pct(c, rc),
                'd5_ret': pct(c5, rc), 'mkt_d1': m1, 'mkt_cc': mcc, 'mkt_d5': m5,
                'ndwl': bool(nd), 'ndwl_score': (nd[0].get('score') if nd else None),
                'scored_at': datetime.now(IST).isoformat()}
        if a.dry_run:
            print(p['symbol'], json.dumps(body)); continue
        patch(p['id'], body); done += 1
    log('scored %d picks' % done)
    if a.weekly:
        weekly(a.dry_run)


def weekly(dry):
    since = (datetime.now(IST) - timedelta(days=8)).strftime('%Y-%m-%d')
    rows = [r for r in get_all('%s?select=*&session_date=gte.%s' % (TBL, since)) if r.get('d1_ret') is not None]
    if not rows:
        msg = '🎯 Next-Day Picks · this week: no scored picks'
    else:
        n = len(rows)
        avg = lambda k: sum(float(r[k]) for r in rows if r.get(k) is not None) / max(1, sum(1 for r in rows if r.get(k) is not None))
        win = sum(1 for r in rows if float(r['d1_ret']) > 0)
        beat = sum(1 for r in rows if r.get('mkt_d1') is not None and float(r['d1_ret']) > float(r['mkt_d1']))
        best = max(rows, key=lambda r: float(r['d1_ret'])); worst = min(rows, key=lambda r: float(r['d1_ret']))
        msg = ('🇮🇳 🎯 Next-Day Picks · last 7 days · %d picks\n'
               'Open→close: avg %+.2f%% (market %+.2f%%) · %d/%d up · beat the market %d/%d\n'
               'Biggest rise from open: avg %+.2f%% · close vs previous close: avg %+.2f%%\n'
               'Best %s %+.2f%% · worst %s %+.2f%%'
               % (n, avg('d1_ret'), avg('mkt_d1'), win, n, beat, n, avg('d1_up'), avg('d1_cc'),
                  best['symbol'], float(best['d1_ret']), worst['symbol'], float(worst['d1_ret'])))
    log(msg)
    if dry: return
    try:
        requests.post('https://api.telegram.org/bot%s/sendMessage' % C.TF_ALERTS_TOKEN,
                      data={'chat_id': C.TF_SYSTEM, 'text': msg}, timeout=30)
    except Exception as e:
        log('telegram failed', e)


if __name__ == '__main__':
    main()
