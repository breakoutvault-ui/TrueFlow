#!/root/trueflow/bin/python
"""
pb_regime_study.py - which playbooks work in which market regime.

Replays every stored day of the nightly scan through the DASHBOARD'S OWN
playbook rules (headless browser on the local Index.html.html / us.html), then
measures each pick: entry next session's open, exit at the close 10 sessions
later, minus the same day's plain-uptrend baseline. Split by regime
(Strong >=55% / Neutral / Weak <45% of stocks above the 20 EMA, from
market_history). Random-pick playbooks run through the same test as a check.

Read-only on all market data. Writes: playbook_regime table (unless --dry-run),
study/pb_regime_<MKT>_<date>.txt and .json, one Telegram summary.

  bin/python pb_regime_study.py --market IN [--days N] [--dry-run]
"""
import argparse, json, math, os, random, re, sys, time
import statistics as st
from datetime import datetime, timedelta, timezone

import requests
import tf_config as C

BASE = os.path.dirname(os.path.abspath(__file__))
IST = timezone(timedelta(hours=5, minutes=30))
H = {'apikey': C.SUPABASE_KEY, 'Authorization': 'Bearer ' + C.SUPABASE_KEY}

SETTINGS = {
    'IN': {'html': 'Index.html.html', 'scan': 'momentum_stocks', 'ohlc': 'daily_ohlc', 'chat': 'TF_SYSTEM', 'flag': '🇮🇳'},
    'US': {'html': 'us.html', 'scan': 'us_momentum_stocks', 'ohlc': 'us_daily_ohlc', 'chat': 'TF_US', 'flag': '🇺🇸'},
}
BASE_P = dict(hold=10, dedup=10, min_n=30, min_days=10, min_blocks=5, t_pass=2.0, t_weak=1.5,
              strong=55.0, weak=45.0, rand_n=100, ema=50, ema_slope=10, warmup_days=120)
PARAMS = {'IN': dict(BASE_P), 'US': dict(BASE_P)}
REGIMES = ['all', 'strong', 'neutral', 'weak']


def log(*a):
    print(datetime.now(IST).strftime('%H:%M:%S'), *a, flush=True)


# ── Supabase (read) ───────────────────────────────────────────────
def sb_get_all(path, page=1000):
    out, off = [], 0
    sep = '&' if '?' in path else '?'
    while True:
        for attempt in range(4):
            try:
                r = requests.get('%s/rest/v1/%s%slimit=%d&offset=%d' % (C.SUPABASE_URL, path, sep, page, off), headers=H, timeout=120)
                r.raise_for_status()
                b = r.json()
                break
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
        out += b
        if len(b) < page:
            return out
        off += page


def load_regime(mkt):
    rows = sb_get_all('market_history?market=eq.%s&select=d,pct20&order=d.asc' % mkt)
    return {r['d']: float(r['pct20']) for r in rows if r.get('pct20') is not None}


def load_ohlc(mkt, since):
    rows = sb_get_all('%s?select=symbol,d,o,c&d=gte.%s&order=d.asc,symbol.asc' % (SETTINGS[mkt]['ohlc'], since))
    ser = {}
    for r in rows:
        if r.get('o') is None or r.get('c') is None:
            continue
        ser.setdefault(r['symbol'], []).append((r['d'], float(r['o']), float(r['c'])))
    return ser


def scan_day(mkt, d):
    return sb_get_all('%s?select=*&session_date=eq.%s&order=symbol.asc' % (SETTINGS[mkt]['scan'], d))


def scan_first_date(mkt):
    rr = requests.get('%s/rest/v1/%s?select=session_date&order=session_date.asc&limit=1' % (C.SUPABASE_URL, SETTINGS[mkt]['scan']), headers=H, timeout=120)
    rr.raise_for_status()
    j = rr.json()
    return j[0]['session_date'] if j else None


# ── replay through the dashboard's own rules ──────────────────────
START_JS = r"""() => {
 try{switchMode('momentum');}catch(e){}
 var b=document.querySelector('.nav-tab[onclick*="\'screener\'"]');
 try{switchTab('screener',b,'momentum');}catch(e){}
 try{scrLoad();}catch(e){}
}"""
COUNT_JS = r"""() => {
 var s=SCR,keep={};Object.keys(s.state).forEach(function(x){keep[x]=s.state[x];});
 var ap=s.activePreset,qe=document.getElementById('scr-q'),qv=qe?qe.value:'',out={};
 if(qe)qe.value='';
 try{
  SCR_PRESETS.forEach(function(p){
   if(!p.grp||p.id==='all')return;
   SCR_FILTERS.forEach(function(f){s.state[f.key]=new Set();});s.state.aplus=new Set();s.activePreset=p.id;
   if(p.set)for(var k in p.set)s.state[k]=new Set(p.set[k]);
   if(p.dyn==='strongsector'){try{s.state.sector=scrStrongSectorsPx();}catch(e){}}
   var syms=[];
   for(var i=0;i<s.data.length;i++){var d=s.data[i];if(d._ipoRow)continue;try{if(scrPasses(d))syms.push(d.sym);}catch(e){}}
   out[p.id]={grp:p.grp,label:String(p.label||'').replace(/<[^>]+>/g,''),when:p.when||'eod',sep:!!(p._sk||p._ep),syms:syms};
  });
 }finally{
  Object.keys(s.state).forEach(function(x){if(!(x in keep))delete s.state[x];});
  Object.keys(keep).forEach(function(x){s.state[x]=keep[x];});
  s.activePreset=ap;if(qe)qe.value=qv;
 }
 return {n:s.data.length,updated:s.updated||null,pb:out};
}"""


def replay(mkt, days, snapshot):
    """days: list of dates. snapshot(d) -> scan rows. Returns {d: {n, universe, pb:{id:{...,syms}}}}.
    Robust for long runs on a small server: a fresh tab per day, the browser restarted every
    RESTART_EVERY days and after any crash, one retry per day, and every finished day saved to
    study/pb_replay_<MKT>.json so a re-run continues where it stopped (cache is dropped if the
    dashboard file changes)."""
    from playwright.sync_api import sync_playwright
    html = os.path.join(BASE, SETTINGS[mkt]['html'])
    scan_tbl = SETTINGS[mkt]['scan']
    os.makedirs(os.path.join(BASE, 'study'), exist_ok=True)
    cpath = os.path.join(BASE, 'study', 'pb_replay_%s.json' % mkt)
    hsize = os.path.getsize(html)
    cache = {}
    try:
        cj = json.load(open(cpath))
        if cj.get('html_size') == hsize:
            cache = cj.get('days', {})
            log('  resuming: %d days already replayed' % len(cache))
        else:
            log('  dashboard file changed since the last run: starting the replay fresh')
    except Exception:
        pass

    def save_cache():
        tmp = cpath + '.tmp'
        json.dump({'html_size': hsize, 'days': cache}, open(tmp, 'w'))
        os.replace(tmp, cpath)

    cur = {'rows': []}

    def handle(route):
        u = route.request.url
        if u.startswith('file:'):
            return route.continue_()
        if '/rest/v1/' in u:
            tbl = u.split('/rest/v1/', 1)[1].split('?', 1)[0]
            body = []
            if tbl == scan_tbl:
                m1, m2 = re.search(r'[?&]offset=(\d+)', u), re.search(r'[?&]limit=(\d+)', u)
                off = int(m1.group(1)) if m1 else 0
                lim = int(m2.group(1)) if m2 else 1000
                body = cur['rows'][off:off + lim]
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
        return route.abort()

    def one_day(ctx, d):
        errs = []
        page = ctx.new_page()
        try:
            page.on('pageerror', lambda e: errs.append(str(e)[:200]))
            page.route('**/*', handle)
            page.goto('file://' + html, wait_until='load', timeout=120000)
            page.wait_for_function("typeof scrLoad==='function' && typeof switchTab==='function'", timeout=60000)
            page.wait_for_timeout(2500)   # the page finishes its own start-up (login check, tab setup) first
            ok = False
            for attempt in range(3):
                page.evaluate(START_JS)
                try:
                    page.wait_for_function("typeof SCR!=='undefined' && SCR.loaded && SCR.data && SCR.data.length>0", timeout=30000)
                    ok = True
                    break
                except Exception:
                    page.wait_for_timeout(2000)
            if not ok:
                raise RuntimeError('screener did not load · page errors: ' + ' | '.join(errs[-3:]))
            page.wait_for_timeout(1200)   # let late modules (Shakeouts/EP/Focus) install their presets
            return page.evaluate(COUNT_JS)
        finally:
            try:
                page.close()
            except Exception:
                pass

    todo = [d for d in days if d not in cache]
    log('  replay: %d days to do, %d from earlier runs' % (len(todo), len([d for d in days if d in cache])))
    RESTART_EVERY = 20
    t0, done, failed = time.time(), 0, []
    with sync_playwright() as pw:
        br = ctx = None

        def start():
            b = pw.chromium.launch(args=['--disable-dev-shm-usage', '--disable-gpu', '--disable-extensions'])
            c = b.new_context(viewport={'width': 1400, 'height': 900})
            c.add_init_script("try{localStorage.setItem('tf_auth_until',String(Date.now()+864e5));}catch(e){}")
            return b, c

        def stop(b):
            try:
                b.close()
            except Exception:
                pass

        br, ctx = start()
        since_restart = 0
        for k, d in enumerate(todo):
            try:
                rows = snapshot(d)
            except Exception as e:
                log('  %s: could not read the scan (%s)' % (d, str(e)[:120])); failed.append(d); continue
            if not rows:
                log('  %s: no scan rows, skipped' % d); continue
            cur['rows'] = rows
            r = None
            for attempt in range(2):
                if since_restart >= RESTART_EVERY or not br.is_connected():
                    stop(br); br, ctx = start(); since_restart = 0
                try:
                    r = one_day(ctx, d)
                    since_restart += 1
                    break
                except Exception as e:
                    log('  %s: attempt %d failed (%s) · restarting the browser' % (d, attempt + 1, str(e)[:120]))
                    stop(br); br, ctx = start(); since_restart = 0
            if r is None:
                failed.append(d); continue
            if r.get('updated') and r['updated'] != d:
                log('  %s: dashboard read date %s, skipped' % (d, r['updated'])); continue
            r['universe'] = [x.get('symbol') for x in rows if x.get('symbol')]
            cache[d] = r
            done += 1
            if done % 5 == 0:
                save_cache()
            if done % 10 == 0:
                el = time.time() - t0
                log('  replay %d/%d %s · %d stocks · %d playbooks · %.0fs elapsed · ~%.0f min left' % (
                    k + 1, len(todo), d, r['n'], len(r['pb']), el, el / (k + 1) * (len(todo) - k - 1) / 60))
        stop(br)
    save_cache()
    if failed:
        log('  %d days failed: %s' % (len(failed), ', '.join(failed[:12]) + (' …' if len(failed) > 12 else '')))
    return {d: cache[d] for d in days if d in cache}


# ── maths ─────────────────────────────────────────────────────────
def ema(vals, n):
    k, out, e = 2.0 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def prep(ser, P):
    """per symbol: date->index, opens, closes, uptrend flag per index"""
    S = {}
    for sym, rows in ser.items():
        ds = [r[0] for r in rows]
        o = [r[1] for r in rows]
        c = [r[2] for r in rows]
        e = ema(c, P['ema'])
        up = [i >= P['ema'] and c[i] > e[i] and e[i] > e[i - P['ema_slope']] for i in range(len(c))]
        S[sym] = {'ix': {d: i for i, d in enumerate(ds)}, 'o': o, 'c': c, 'up': up}
    return S


def fwd(S, sym, d, hold):
    s = S.get(sym)
    if not s:
        return None, None
    i = s['ix'].get(d)
    if i is None or i + hold >= len(s['c']):
        return None, None
    o = s['o'][i + 1]
    if not o or o <= 0:
        return None, None
    return 100.0 * (s['c'][i + hold] - o) / o, i


def baselines(S, days, universe, hold):
    base = {}
    for d in days:
        v = []
        for sym in universe.get(d, ()):
            s = S.get(sym)
            if not s:
                continue
            i = s['ix'].get(d)
            if i is None or not s['up'][i]:
                continue
            r, _ = fwd(S, sym, d, hold)
            if r is not None:
                v.append(r)
        if len(v) >= 20:
            base[d] = sum(v) / len(v)
    return base


def regime_of(v, P):
    return 'strong' if v >= P['strong'] else ('weak' if v < P['weak'] else 'neutral')


def trades_for(picks_by_day, S, base, P):
    """picks_by_day: {d:[syms]} -> list of (d, raw, edge) with per-symbol dedup"""
    last, out = {}, []
    for d in sorted(picks_by_day):
        if d not in base:
            continue
        for sym in picks_by_day[d]:
            r, i = fwd(S, sym, d, P['hold'])
            if r is None:
                continue
            if sym in last and i - last[sym] < P['dedup']:
                continue
            last[sym] = i
            out.append((d, r, r - base[d]))
    return out


def summarise(trades, P, mid, blk):
    """t-stat over non-overlapping blocks of `hold` sessions: 10-session holds started on
    neighbouring days overlap, so day-by-day statistics would overstate significance."""
    if not trades:
        return {'n': 0, 'days': 0, 'blocks': 0, 'verdict': 'too few'}
    by = {}
    for d, r, e in trades:
        by.setdefault(blk[d], []).append(e)
    bm = [sum(v) / len(v) for v in by.values()]
    nb = len(bm)
    nd = len({d for d, _, _ in trades})
    mean_e = sum(e for _, _, e in trades) / len(trades)
    bmean = sum(bm) / nb
    t = (bmean / (st.stdev(bm) / math.sqrt(nb))) if nb > 2 and st.stdev(bm) > 0 else 0.0
    raw = [r for _, r, _ in trades]
    h1 = [e for d, r, e in trades if d < mid]
    h2 = [e for d, r, e in trades if d >= mid]
    m1 = sum(h1) / len(h1) if h1 else None
    m2 = sum(h2) / len(h2) if h2 else None
    o = {'n': len(trades), 'days': nd, 'blocks': nb, 'mean_pct': sum(raw) / len(raw), 'median_pct': st.median(raw),
         'win_pct': 100.0 * sum(1 for x in raw if x > 0) / len(raw), 'edge_pct': mean_e, 't': t, 'h1': m1, 'h2': m2}
    if o['n'] < P['min_n'] or nd < P['min_days'] or nb < P['min_blocks']:
        o['verdict'] = 'too few'
    elif mean_e > 0 and bmean > 0 and t >= P['t_pass'] and (m1 or 0) > 0 and (m2 or 0) > 0:
        o['verdict'] = 'PASS'
    elif mean_e > 0 and bmean > 0 and t >= P['t_weak']:
        o['verdict'] = 'weak'
    else:
        o['verdict'] = 'no edge'
    return o


def study(rep, S, reg, P, seed=7):
    days = sorted(d for d in rep if d in reg)
    universe = {d: set(rep[d].get('universe') or []) for d in days}
    base = baselines(S, days, universe, P['hold'])
    days = [d for d in days if d in base]
    mid = days[len(days) // 2] if days else None
    blk = {d: i // P['hold'] for i, d in enumerate(days)}
    meta, picks = {}, {}
    for d in days:
        for pid, x in rep[d]['pb'].items():
            meta.setdefault(pid, {k: x[k] for k in ('grp', 'label', 'when', 'sep')})
            picks.setdefault(pid, {})[d] = x['syms']
    out = {}
    daily_counts = []
    for pid, pd in picks.items():
        tot = sum(len(v) for v in pd.values())
        row = {'meta': meta[pid], 'signals': tot}
        if tot == 0:
            row['status'] = 'not testable' if not meta[pid]['sep'] else 'studied separately'
            out[pid] = row
            continue
        daily_counts.append(tot / max(1, len(pd)))
        tr = trades_for(pd, S, base, P)
        row['status'] = 'tested'
        row['res'] = {}
        for rg in REGIMES:
            sub = tr if rg == 'all' else [x for x in tr if regime_of(reg[x[0]], P) == rg]
            row['res'][rg] = summarise(sub, P, mid, blk)
        out[pid] = row
    # random-pick check: same number of picks a day, drawn from that day's universe
    rnd = random.Random(seed)
    k = max(1, int(round(st.median(daily_counts)))) if daily_counts else 5
    rand_pass = {rg: 0 for rg in REGIMES}
    for j in range(P['rand_n']):
        pd = {}
        for d in days:
            u = sorted(universe[d])
            pd[d] = rnd.sample(u, min(k, len(u))) if u else []
        tr = trades_for(pd, S, base, P)
        for rg in REGIMES:
            sub = tr if rg == 'all' else [x for x in tr if regime_of(reg[x[0]], P) == rg]
            if summarise(sub, P, mid, blk).get('verdict') == 'PASS':
                rand_pass[rg] += 1
    reg_days = {rg: sum(1 for d in days if regime_of(reg[d], P) == rg) for rg in REGIMES[1:]}
    return {'days': days, 'from': days[0] if days else None, 'to': days[-1] if days else None, 'regime_days': reg_days,
            'rand_k': k, 'rand_n': P['rand_n'], 'rand_pass': rand_pass, 'pb': out}


# ── output ────────────────────────────────────────────────────────
def fmt(v, dp=2, sign=True):
    return '—' if v is None else (('%+.' if sign else '%.') + str(dp) + 'f') % v


def report(mkt, R, P):
    L = []
    L.append('PLAYBOOK x REGIME STUDY - %s - %s' % (mkt, datetime.now(IST).strftime('%d %b %Y %H:%M IST')))
    L.append('Period %s to %s · %d days · Strong %d / Neutral %d / Weak %d days' % (
        R['from'], R['to'], len(R['days']), R['regime_days']['strong'], R['regime_days']['neutral'], R['regime_days']['weak']))
    L.append('Trade: next open -> close %d sessions later, no stop; edge = minus the same day\'s plain-uptrend average (close > rising 50 EMA).' % P['hold'])
    L.append('One trade per stock per playbook per %d sessions. PASS = >=%d trades on >=%d days, edge > 0, t >= %.1f over non-overlapping %d-session blocks (>=%d blocks), edge > 0 in both halves.' % (
        P['dedup'], P['min_n'], P['min_days'], P['t_pass'], P['hold'], P['min_blocks']))
    L.append('Random check: %d random playbooks of %d picks/day -> PASS count by regime: %s (should be ~0)' % (
        R['rand_n'], R['rand_k'], ', '.join('%s %d' % (k, v) for k, v in R['rand_pass'].items())))
    L.append('')
    tested = [(pid, x) for pid, x in R['pb'].items() if x['status'] == 'tested']
    tested.sort(key=lambda kv: -(kv[1]['res']['all'].get('edge_pct') or -99))
    hdr = '%-34s %-6s' % ('PLAYBOOK', 'REG') + '%7s %5s %8s %8s %6s %6s %8s %8s  %s' % ('TRADES', 'DAYS', 'RAW', 'EDGE', 'T', 'WIN%', 'HALF1', 'HALF2', 'VERDICT')
    L.append(hdr)
    for pid, x in tested:
        for rg in REGIMES:
            o = x['res'][rg]
            nm = (x['meta']['label'][:33] if rg == 'all' else '')
            L.append('%-34s %-6s' % (nm, rg) + '%7s %5s %8s %8s %6s %6s %8s %8s  %s' % (
                o.get('n', 0), o.get('days', 0), fmt(o.get('mean_pct')), fmt(o.get('edge_pct')), fmt(o.get('t'), 1, False),
                fmt(o.get('win_pct'), 0, False), fmt(o.get('h1')), fmt(o.get('h2')), o.get('verdict')))
        L.append('')
    nt = [x['meta']['label'] for x in R['pb'].values() if x['status'] == 'not testable']
    ss = [x['meta']['label'] for x in R['pb'].values() if x['status'] == 'studied separately']
    L.append('NOT TESTABLE on stored scans (need data the scan history does not keep: MTF, smart money, results, sector pulse): ' + ('; '.join(nt) or 'none'))
    L.append('STUDIED SEPARATELY (shakeout / EP studies): ' + ('; '.join(ss) or 'none'))
    return '\n'.join(L)


def tg_summary(mkt, R):
    flag = SETTINGS[mkt]['flag']
    lines = ['%s <b>Playbook × regime study</b> (%s → %s, %d days)' % (flag, R['from'], R['to'], len(R['days'])),
             'Strong %d · Neutral %d · Weak %d days' % (R['regime_days']['strong'], R['regime_days']['neutral'], R['regime_days']['weak'])]
    for rg in ['strong', 'neutral', 'weak', 'all']:
        ps = []
        for pid, x in R['pb'].items():
            if x['status'] != 'tested':
                continue
            o = x['res'][rg]
            if o.get('verdict') == 'PASS':
                ps.append((o['edge_pct'], '%s %+.2f%% (%d)' % (x['meta']['label'], o['edge_pct'], o['n'])))
        ps.sort(reverse=True)
        lines.append('')
        lines.append('<b>%s</b>: %s' % (rg.upper() if rg != 'all' else 'ALL DAYS', ('; '.join(p[1] for p in ps[:8]) or 'none passed')))
    lines.append('')
    lines.append('Random check: %s PASS of %d (should be ~0).' % (sum(R['rand_pass'].values()), R['rand_n'] * 4))
    nt = sum(1 for x in R['pb'].values() if x['status'] == 'not testable')
    lines.append('%d playbooks not testable on stored scans. Full report: study/ folder.' % nt)
    return '\n'.join(lines)[:3900]


def send_tg(mkt, text):
    chat = getattr(C, SETTINGS[mkt]['chat'], None)
    tok = getattr(C, 'TF_ALERTS_TOKEN', None)
    if not chat or not tok:
        log('telegram not configured')
        return
    try:
        requests.post('https://api.telegram.org/bot%s/sendMessage' % tok,
                      data={'chat_id': chat, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': 'true'}, timeout=30)
    except Exception as e:
        log('telegram failed', e)


def save_db(mkt, R):
    today = datetime.now(IST).strftime('%Y-%m-%d')
    rows = []
    for pid, x in R['pb'].items():
        if x['status'] != 'tested':
            rows.append({'market': mkt, 'playbook': pid, 'label': x['meta']['label'], 'grp': x['meta']['grp'], 'regime': 'all',
                         'verdict': x['status'], 'n': 0, 'days': 0, 'blocks': 0, 'period_from': R['from'], 'period_to': R['to'], 'computed_on': today})
            continue
        for rg in REGIMES:
            o = x['res'][rg]
            rows.append({'market': mkt, 'playbook': pid, 'label': x['meta']['label'], 'grp': x['meta']['grp'], 'regime': rg,
                         'n': o.get('n', 0), 'days': o.get('days', 0), 'blocks': o.get('blocks', 0),
                         'mean_pct': round(o['mean_pct'], 3) if o.get('mean_pct') is not None else None,
                         'edge_pct': round(o['edge_pct'], 3) if o.get('edge_pct') is not None else None,
                         'win_pct': round(o['win_pct'], 1) if o.get('win_pct') is not None else None,
                         't': round(o['t'], 2) if o.get('t') is not None else None,
                         'h1': round(o['h1'], 3) if o.get('h1') is not None else None,
                         'h2': round(o['h2'], 3) if o.get('h2') is not None else None,
                         'verdict': o.get('verdict'), 'period_from': R['from'], 'period_to': R['to'], 'computed_on': today})
    hh = dict(H, **{'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'})
    for i in range(0, len(rows), 500):
        r = requests.post('%s/rest/v1/playbook_regime?on_conflict=market,playbook,regime' % C.SUPABASE_URL, headers=hh,
                          data=json.dumps(rows[i:i + 500]), timeout=120)
        if r.status_code >= 300:
            raise RuntimeError('playbook_regime write failed %s %s' % (r.status_code, r.text[:300]))
    log('saved %d rows to playbook_regime' % len(rows))


# ── main ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--market', required=True, choices=['IN', 'US'])
    ap.add_argument('--days', type=int, default=0, help='only the last N study days (quick test)')
    ap.add_argument('--dry-run', action='store_true', help='no database write, no Telegram')
    a = ap.parse_args()
    mkt, P = a.market, PARAMS[a.market]
    log('start', mkt, 'dry-run' if a.dry_run else '')

    reg = load_regime(mkt)
    first = scan_first_date(mkt)
    if not first:
        sys.exit('no scan history')
    since = (datetime.strptime(first, '%Y-%m-%d') - timedelta(days=P['warmup_days'])).strftime('%Y-%m-%d')
    log('scan history from', first, '· loading prices from', since)
    ser = load_ohlc(mkt, since)
    S = prep(ser, P)
    cal = sorted({d for rows in ser.values() for d, _, _ in rows})
    log('prices: %d symbols, %d sessions' % (len(S), len(cal)))
    days = [d for i, d in enumerate(cal) if d >= first and i + P['hold'] < len(cal) and d in reg]
    if a.days:
        days = days[-a.days:]
    log('study days: %d (%s -> %s)' % (len(days), days[0] if days else '-', days[-1] if days else '-'))

    rep = replay(mkt, days, lambda d: scan_day(mkt, d))
    log('replayed %d of %d days' % (len(rep), len(days)))
    if len(rep) < max(20, len(days) // 2):
        log('too few days replayed to judge anything: nothing saved or sent. Run the same command again: it continues where it stopped.')
        sys.exit(1)
    R = study(rep, S, reg, P)
    txt = report(mkt, R, P)
    os.makedirs(os.path.join(BASE, 'study'), exist_ok=True)
    stamp = datetime.now(IST).strftime('%Y%m%d')
    fn = os.path.join(BASE, 'study', 'pb_regime_%s_%s' % (mkt, stamp))
    open(fn + '.txt', 'w').write(txt)
    slim = {k: v for k, v in R.items() if k != 'days'}
    open(fn + '.json', 'w').write(json.dumps(slim, default=str))
    print(txt)
    log('report', fn + '.txt')
    if a.dry_run:
        log('dry run: nothing saved, nothing sent')
        return
    save_db(mkt, R)
    send_tg(mkt, tg_summary(mkt, R))
    log('done')


if __name__ == '__main__':
    main()
