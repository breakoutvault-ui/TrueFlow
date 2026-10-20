#!/root/trueflow/bin/python
"""
index_breadth.py - daily breadth for NSE indices, their sectors and the F&O universe (India).

For every index (and sub-group) and every session:
  n            members with a price that day
  adv/dec/unch closes up / down / flat vs the previous close
  ab9..ab200   members closing above their daily 9 / 20 / 50 / 200 EMA (n200 = members with 200 sessions of history)
  avg_chg      average % change of the members
  idx_close/idx_chg  the index's own close and % change (Kite), when available
Groups: grp='ALL' for the whole index; Nifty 50, Midcap 100, Smallcap 100 and MidSmallcap 400 also per NSE industry
(SECTOR_IDX); F&O (idx='FNO') per our sector.
Members = TODAY's index lists (niftyindices.com CSVs), so history uses today's members (survivorship).
A list can be supplied by hand as index_lists/<CODE>.csv (same columns as NSE's file) if NSE blocks the server.
Prices: daily_ohlc (split-adjusted on the fly). Writes index_members and index_breadth. Read-only on everything else.

  bin/python index_breadth.py [--backfill] [--days N] [--wait] [--dry-run] [--no-kite]
"""
import argparse, csv, io, json, os, sys, time
from datetime import datetime, timedelta
import requests
import tf_config as C
from hi52_scan import split_adjust, get_all, latest_d, log, H, IST

BASE = os.path.dirname(os.path.abspath(__file__))
NI = 'https://www.niftyindices.com/IndexConstituent/'
# code: (name, kind, [csv file candidates], [Kite index name candidates])
INDICES = [
    ('N50', 'Nifty 50', 'broad', ['ind_nifty50list.csv'], ['NIFTY 50']),
    ('BANK', 'Bank Nifty', 'broad', ['ind_niftybanklist.csv'], ['NIFTY BANK']),
    ('MID100', 'Nifty Midcap 100', 'broad', ['ind_niftymidcap100list.csv'], ['NIFTY MIDCAP 100']),
    ('MIDSML400', 'Nifty MidSmallcap 400', 'broad', ['ind_niftymidsmallcap400list.csv'], ['NIFTY MIDSML 400', 'NIFTY MIDSMALLCAP 400']),
    ('SML100', 'Nifty Smallcap 100', 'broad', ['ind_niftysmallcap100list.csv'], ['NIFTY SMLCAP 100', 'NIFTY SMALLCAP 100']),
    ('PVTBANK', 'Nifty Private Bank', 'sector', ['ind_nifty_privatebanklist.csv', 'ind_niftyprivatebanklist.csv'], ['NIFTY PVT BANK', 'NIFTY PRIVATE BANK']),
    ('PSUBANK', 'Nifty PSU Bank', 'sector', ['ind_niftypsubanklist.csv'], ['NIFTY PSU BANK']),
    ('FIN', 'Nifty Financial Services', 'sector', ['ind_niftyfinancelist.csv'], ['NIFTY FIN SERVICE', 'NIFTY FINANCIAL SERVICES']),
    ('IT', 'Nifty IT', 'sector', ['ind_niftyitlist.csv'], ['NIFTY IT']),
    ('PHARMA', 'Nifty Pharma', 'sector', ['ind_niftypharmalist.csv'], ['NIFTY PHARMA']),
    ('HEALTH', 'Nifty Healthcare', 'sector', ['ind_niftyhealthcarelist.csv'], ['NIFTY HEALTHCARE']),
    ('AUTO', 'Nifty Auto', 'sector', ['ind_niftyautolist.csv'], ['NIFTY AUTO']),
    ('FMCG', 'Nifty FMCG', 'sector', ['ind_niftyfmcglist.csv'], ['NIFTY FMCG']),
    ('METAL', 'Nifty Metal', 'sector', ['ind_niftymetallist.csv'], ['NIFTY METAL']),
    ('REALTY', 'Nifty Realty', 'sector', ['ind_niftyrealtylist.csv'], ['NIFTY REALTY']),
    ('ENERGY', 'Nifty Energy', 'sector', ['ind_niftyenergylist.csv'], ['NIFTY ENERGY']),
    ('OILGAS', 'Nifty Oil & Gas', 'sector', ['ind_niftyoilgaslist.csv', 'ind_niftyoil&gaslist.csv'], ['NIFTY OIL AND GAS', 'NIFTY OIL & GAS']),
    ('CONSDUR', 'Nifty Consumer Durables', 'sector', ['ind_niftyconsumerdurableslist.csv'], ['NIFTY CONSR DURBL', 'NIFTY CONSUMER DURABLES']),
    ('MEDIA', 'Nifty Media', 'sector', ['ind_niftymedialist.csv'], ['NIFTY MEDIA']),
    ('CAPMKT', 'Nifty Capital Markets', 'sector', ['ind_niftycapitalmarkets_list.csv', 'ind_niftycapitalmarketslist.csv'], ['NIFTY CAPITAL MKT', 'NIFTY CAPITAL MARKETS']),
    ('DEFENCE', 'Nifty India Defence', 'sector', ['ind_niftyindiadefence_list.csv', 'ind_niftyindiadefencelist.csv'], ['NIFTY IND DEFENCE', 'NIFTY INDIA DEFENCE']),
    ('INFRA', 'Nifty Infrastructure', 'sector', ['ind_niftyinfralist.csv'], ['NIFTY INFRA', 'NIFTY INFRASTRUCTURE']),
]
EMAS = (9, 20, 50, 200)
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36',
      'Accept': 'text/csv,*/*', 'Referer': 'https://www.niftyindices.com/'}


SECTOR_IDX = ('N50', 'FNO', 'MID100', 'SML100', 'MIDSML400')   # indices that also get per-sector rows

def parse_list(text):
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip('﻿'))):
        k = {x.strip().lower(): (v or '').strip() for x, v in r.items() if x}
        s = k.get('symbol')
        if s and (k.get('series', 'EQ') or 'EQ') in ('EQ', 'BE', ''):
            out.append((s, k.get('industry') or ''))
    return out


def members(code, files):
    local = os.path.join(BASE, 'index_lists', code + '.csv')
    if os.path.exists(local):
        m = parse_list(open(local, encoding='utf-8', errors='ignore').read())
        if m: return m, 'file'
    for f in files:
        for k in range(2):
            try:
                r = requests.get(NI + f, headers=UA, timeout=30)
                if r.status_code == 200 and 'symbol' in r.text[:400].lower():
                    m = parse_list(r.text)
                    if m: return m, f
                break
            except Exception:
                time.sleep(3)
    return [], None


def ema_flags(c, n):
    """for each day: is close above the n-EMA (None until n sessions exist)"""
    out = [None] * len(c)
    if len(c) < n: return out
    e = sum(c[:n]) / n; k = 2.0 / (n + 1); out[n - 1] = c[n - 1] > e
    for j in range(n, len(c)):
        e = c[j] * k + e * (1 - k); out[j] = c[j] > e
    return out


def kite_closes(names_by_code, since, dry):
    try:
        from kiteconnect import KiteConnect
        tok = open(getattr(C, 'ACCESS_TOKEN_PATH', os.path.join(BASE, 'access_token.txt'))).read().strip()
        kite = KiteConnect(api_key=C.KITE_API_KEY); kite.set_access_token(tok)
        ins = [i for i in kite.instruments('NSE') if i.get('segment') == 'INDICES']
    except Exception as e:
        log('Kite unavailable (%s): index prices skipped this run' % str(e)[:120]); return {}
    by = {i['tradingsymbol'].upper(): i['instrument_token'] for i in ins}
    out = {}
    for code, names in names_by_code.items():
        tk = next((by[n] for n in names if n in by), None)
        if not tk:
            near = [n for n in by if names[0].split()[-1] in n][:6]
            log('  %s: no Kite index named %s (similar: %s)' % (code, ' / '.join(names), ', '.join(near) or '-')); continue
        try:
            rows = kite.historical_data(tk, since, datetime.now(IST).date(), 'day')
            out[code] = {str(r['date'])[:10]: float(r['close']) for r in rows}
            time.sleep(0.35)
        except Exception as e:
            log('  %s: Kite history failed %s' % (code, str(e)[:100]))
    log('Kite index closes: %d of %d indices' % (len(out), len(names_by_code)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', action='store_true', help='every session since prices start (one-time)')
    ap.add_argument('--days', type=int, default=5, help='sessions to (re)write on a normal run')
    ap.add_argument('--wait', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-kite', action='store_true')
    a = ap.parse_args()
    if a.wait:
        today = datetime.now(IST).strftime('%Y-%m-%d')
        for k in range(12):
            if (latest_d('daily_ohlc') or '') >= today: break
            log("waiting for today's prices (%d/12)" % (k + 1)); time.sleep(300)
    # 1) members
    mem, got = {}, {}
    for code, name, kind, files, _ in INDICES:
        m, src = members(code, files)
        if m: mem[code] = m; got[code] = src
        log('  %-9s %-26s %3d members%s' % (code, name, len(m), (' (' + src + ')') if src else '  ✗ not loaded'))
    ef = get_all('ema_filter?select=session_date&order=session_date.desc&limit=1')
    fdate = ef[0]['session_date'] if ef else None
    fno = get_all('ema_filter?select=symbol,sector&session_date=eq.%s' % fdate) if fdate else []
    mem['FNO'] = [(r['symbol'], r.get('sector') or 'Other') for r in fno if r.get('symbol')]
    log('  FNO       F&O universe               %3d members (ema_filter %s)' % (len(mem['FNO']), fdate))
    syms = sorted({s for v in mem.values() for s, _ in v})
    # 2) prices
    last = latest_d('daily_ohlc')
    since = '2000-01-01' if a.backfill else (datetime.strptime(last, '%Y-%m-%d') - timedelta(days=430)).strftime('%Y-%m-%d')
    log('%d symbols · prices from %s to %s' % (len(syms), since, last))
    ser = {}
    for j in range(0, len(syms), 40):
        chunk = ','.join('"%s"' % s for s in syms[j:j + 40])
        for r in get_all('daily_ohlc?select=symbol,d,h,l,c,v&symbol=in.(%s)&d=gte.%s&order=d.asc' % (requests.utils.quote(chunk, safe='",'), since)):
            if r.get('c') is None: continue
            ser.setdefault(r['symbol'], []).append(r)
    # per symbol: date -> (chg sign, chg %, above flags)
    info = {}
    for s, rs in ser.items():
        h = [float(r['h'] or r['c']) for r in rs]; l = [float(r['l'] or r['c']) for r in rs]
        c = [float(r['c']) for r in rs]; v = [float(r['v'] or 0) for r in rs]
        split_adjust(h, l, c, v)
        fl = {n: ema_flags(c, n) for n in EMAS}
        d = {}
        for k in range(1, len(rs)):
            ch = (c[k] / c[k - 1] - 1) * 100 if c[k - 1] > 0 else None
            d[rs[k]['d']] = (ch, fl[9][k], fl[20][k], fl[50][k], fl[200][k])
        info[s] = d
    days = sorted({dd for d in info.values() for dd in d})
    if not a.backfill: days = days[-a.days:]
    else: days = days[200:] if len(days) > 260 else days      # the first ~200 sessions have no 200 EMA yet
    log('%d sessions to write' % len(days))
    # 3) index prices
    kc = {} if (a.no_kite or not days) else kite_closes({c0: kn for c0, _, _, _, kn in INDICES if c0 in mem}, (datetime.strptime(days[0], '%Y-%m-%d') - timedelta(days=10)).date() if days else None, a.dry_run)
    # 4) breadth
    out = []
    def agg(code, grp, syms_, d, pd_=None):
        n = adv = dec = un = n200 = 0; ab = {e: 0 for e in EMAS}; chs = []
        for s in syms_:
            x = info.get(s, {}).get(d)
            if not x or x[0] is None: continue
            ch = x[0]; fl = {9: x[1], 20: x[2], 50: x[3], 200: x[4]}; n += 1; chs.append(ch)
            if ch > 0.0001: adv += 1
            elif ch < -0.0001: dec += 1
            else: un += 1
            for e in EMAS:
                if fl[e]: ab[e] += 1
            if fl[200] is not None: n200 += 1
        if not n: return None
        row = {'idx': code, 'grp': grp, 'd': d, 'n': n, 'adv': adv, 'dec': dec, 'unch': un,
               'ab9': ab[9], 'ab20': ab[20], 'ab50': ab[50], 'ab200': ab[200], 'n200': n200,
               'avg_chg': round(sum(chs) / n, 3), 'idx_close': None, 'idx_chg': None}
        if grp == 'ALL' and code in kc:
            cl = kc[code].get(d); pv = kc[code].get(pd_) if pd_ else None
            row['idx_close'] = cl; row['idx_chg'] = round((cl / pv - 1) * 100, 3) if cl and pv else None
        return row
    alld = sorted({dd for d in info.values() for dd in d})
    prev = {alld[k]: alld[k - 1] for k in range(1, len(alld))}
    for code, m in mem.items():
        groups = {'ALL': [s for s, _ in m]}
        if code in SECTOR_IDX:
            for s, ind in m: groups.setdefault(ind or 'Other', []).append(s)
        for d in days:
            for g, ss in groups.items():
                r = agg(code, g, ss, d, prev.get(d))
                if r: out.append(r)
    last_rows = [r for r in out if r['d'] == days[-1] and r['grp'] == 'ALL'] if days else []
    summ = '🇮🇳 🧭 Indices %s · %d indices + F&O · %s' % (days[-1] if days else '-', len(mem) - 1,
        ' · '.join('%s %d/%d above 20E' % (r['idx'], r['ab20'], r['n']) for r in last_rows if r['idx'] in ('N50', 'BANK', 'MID100', 'SML100', 'FNO')))
    log(summ)
    if a.dry_run:
        for r in last_rows: print(json.dumps(r))
        log('dry run: nothing saved'); return
    hh = dict(H, **{'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'})
    mrows = [{'idx': code, 'symbol': s, 'industry': ind, 'updated': last} for code, m in mem.items() for s, ind in m]
    # replace the member lists only for the indices loaded this run (a failed download keeps yesterday's list)
    requests.delete('%s/rest/v1/index_members?idx=in.(%s)' % (C.SUPABASE_URL, ','.join(mem.keys())), headers=H, timeout=60)
    for j in range(0, len(mrows), 500):
        r = requests.post('%s/rest/v1/index_members?on_conflict=idx,symbol' % C.SUPABASE_URL, headers=hh, data=json.dumps(mrows[j:j + 500]), timeout=120)
        if r.status_code >= 300: sys.exit('members write failed %s %s' % (r.status_code, r.text[:300]))
    for j in range(0, len(out), 500):
        r = requests.post('%s/rest/v1/index_breadth?on_conflict=idx,grp,d' % C.SUPABASE_URL, headers=hh, data=json.dumps(out[j:j + 500]), timeout=120)
        if r.status_code >= 300: sys.exit('breadth write failed %s %s' % (r.status_code, r.text[:300]))
    log('saved %d members, %d breadth rows' % (len(mrows), len(out)))
    if not a.backfill:
        try:
            requests.post('https://api.telegram.org/bot%s/sendMessage' % C.TF_ALERTS_TOKEN, data={'chat_id': C.TF_SYSTEM, 'text': summ}, timeout=30)
        except Exception as e:
            log('telegram failed', e)


if __name__ == '__main__':
    main()
