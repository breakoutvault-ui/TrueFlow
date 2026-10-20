#!/root/trueflow/bin/python
"""
us_index_breadth.py - daily breadth for US indices and the 11 S&P 500 sectors (the US twin of index_breadth.py).

Indices (code: name)
  SPX  S&P 500        members: Wikipedia "List of S&P 500 companies" (with GICS sector)   price ^GSPC
  NDX  Nasdaq 100     members: Wikipedia "Nasdaq-100" (with GICS sector)                    price ^NDX
  DJI  Dow 30         members: Wikipedia "Dow Jones Industrial Average"                     price ^DJI
  SML  US Small Caps  members: our us_universe with market cap $300M-$2B (with Yahoo sector)  no index price
  S_*  the 11 S&P 500 sectors = S&P 500 members by GICS sector                             price = SPDR sector ETF
A list can be supplied by hand as us_index_lists/<CODE>.csv (columns: symbol, industry) if a source blocks the server.

For every index (and, for SPX and NDX, every GICS sector inside it) and every session:
  n, adv/dec/unch, ab9..ab200 (n200), avg_chg, idx_close/idx_chg   - same columns as India's index_breadth
Only members we hold prices for (us_daily_ohlc) are counted: `n` says how many; us_index_members keeps the full list
(tracked = true/false) so the dashboard can show "X of Y members tracked".
Members = TODAY's lists, so history uses today's members (survivorship). Prices are split-adjusted on the fly.
Index / ETF closes from Yahoo (yfinance).
Writes us_index_members and us_index_breadth. Read-only on everything else.

  bin/python us_index_breadth.py [--backfill] [--days N] [--dry-run] [--no-prices]
"""
import argparse, csv, io, json, os, sys, time
from datetime import datetime, timedelta
from html.parser import HTMLParser
import requests
import tf_config as C
from hi52_scan import split_adjust, get_all, latest_d, log, H
from index_breadth import ema_flags

BASE = os.path.dirname(os.path.abspath(__file__))
EMAS = (9, 20, 50, 200)
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36',
      'Accept': 'text/html,text/csv,application/xhtml+xml,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9'}
WIKI = 'https://en.wikipedia.org/wiki/'
BROAD = [('SPX', 'S&P 500', WIKI + 'List_of_S%26P_500_companies', '^GSPC'),
         ('NDX', 'Nasdaq 100', WIKI + 'Nasdaq-100', '^NDX'),
         ('DJI', 'Dow 30', WIKI + 'Dow_Jones_Industrial_Average', '^DJI')]
# US Small Caps = OUR universe (us_universe) with market cap $300M-$2B. The official Russell 2000 list (iShares IWM file)
# answers 403 to the server, so this is our own, clearly-labelled small-cap list. No index price (avg stock instead).
SMALL = ('SML', 'US Small Caps ($300M–$2B, our list)', 300e6, 2e9)
# GICS sector -> (code, ETF)
SECTORS = {'Information Technology': ('S_IT', 'XLK'), 'Financials': ('S_FIN', 'XLF'), 'Health Care': ('S_HC', 'XLV'),
           'Consumer Discretionary': ('S_CD', 'XLY'), 'Consumer Staples': ('S_CS', 'XLP'), 'Industrials': ('S_IND', 'XLI'),
           'Energy': ('S_EN', 'XLE'), 'Materials': ('S_MAT', 'XLB'), 'Utilities': ('S_UT', 'XLU'),
           'Real Estate': ('S_RE', 'XLRE'), 'Communication Services': ('S_COM', 'XLC')}
SECTOR_IDX = ('SPX', 'NDX', 'SML')   # indices that also get per-sector rows
SUMMARY = ('SPX', 'NDX', 'DJI', 'SML')


class Tables(HTMLParser):
    """every <table> as rows of cell texts, with the table's id"""
    def __init__(self):
        super().__init__(); self.tables = []; self.stack = []; self.cell = None; self.row = None
    def handle_starttag(self, tag, attrs):
        if tag == 'table': self.stack.append({'id': dict(attrs).get('id') or '', 'rows': []})
        elif tag == 'tr' and self.stack: self.row = []
        elif tag in ('td', 'th') and self.row is not None: self.cell = []
    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None and self.row is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell = None
        elif tag == 'tr' and self.row is not None and self.stack:
            if self.row: self.stack[-1]['rows'].append(self.row)
            self.row = None
        elif tag == 'table' and self.stack:
            self.tables.append(self.stack.pop())
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)


def norm(s):
    return s.strip().upper().replace('.', '-').replace('/', '-')


def wiki_members(url, debug=False):
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    p = Tables(); p.feed(r.text)
    best = None
    for t in p.tables:
        rows = t['rows']
        if debug and rows: log('    table id=%r rows=%d first=%s' % (t['id'], len(rows), rows[0][:6]))
        # the header row is the first row with a Symbol / Ticker cell (footnote marks ignored)
        hi = next((i for i, x in enumerate(rows[:3]) if any(c.lower().split('[')[0].strip() in ('symbol', 'ticker', 'ticker symbol') for c in x)), None)
        if hi is None: continue
        hd = [c.lower().split('[')[0].strip() for c in rows[hi]]
        si = next(k for k, h in enumerate(hd) if h in ('symbol', 'ticker', 'ticker symbol'))
        gi = next((k for k, h in enumerate(hd) if 'gics sector' in h), None)
        if gi is None: gi = next((k for k, h in enumerate(hd) if h in ('sector', 'industry', 'icb industry')), None)
        got = []
        for x in rows[hi + 1:]:
            if len(x) <= si: continue
            tk = x[si].split('[')[0].strip()
            if not tk or len(tk) > 7 or ' ' in tk: continue
            got.append((norm(tk), x[gi].strip() if gi is not None and gi < len(x) else ''))
        if t['id'] == 'constituents' and got:
            return got
        if got and (best is None or len(got) > len(best)): best = got
    return best or []


def ishares_members(url):
    r = requests.get(url, headers=UA, timeout=60); r.raise_for_status()
    lines = r.text.splitlines()
    k = next((i for i, x in enumerate(lines) if x.startswith('Ticker,')), None)
    if k is None: return []
    out = []
    for x in csv.DictReader(io.StringIO('\n'.join(lines[k:]))):
        t = (x.get('Ticker') or '').strip()
        if t and (x.get('Asset Class') or 'Equity').strip() == 'Equity' and t not in ('-', 'USD'):
            out.append((norm(t), (x.get('Sector') or '').strip()))
    return out


def members(code, url, debug=False):
    local = os.path.join(BASE, 'us_index_lists', code + '.csv')
    if os.path.exists(local):
        rows = [(norm(x.get('symbol') or x.get('Symbol') or ''), (x.get('industry') or x.get('sector') or '').strip())
                for x in csv.DictReader(open(local, encoding='utf-8', errors='ignore'))]
        rows = [x for x in rows if x[0]]
        if rows: return rows, 'file'
    for k in range(2):
        try:
            m = ishares_members(url) if 'ishares' in url else wiki_members(url, debug)
            if m: return m, url.split('/')[2]
        except Exception as e:
            log('  %s: %s' % (code, str(e)[:120])); time.sleep(3)
    return [], None


def yahoo_closes(tickers, since):
    try:
        import yfinance as yf
    except Exception as e:
        log('yfinance unavailable (%s): index prices skipped' % str(e)[:80]); return {}
    out = {}
    for code, t in tickers.items():
        try:
            df = yf.download(t, start=since, interval='1d', auto_adjust=False, progress=False, threads=False)
            if df is None or df.empty: log('  %s (%s): no Yahoo data' % (code, t)); continue
            col = df['Close']
            if hasattr(col, 'columns'): col = col.iloc[:, 0]
            out[code] = {str(i)[:10]: float(v) for i, v in col.items() if v == v}
            time.sleep(0.5)
        except Exception as e:
            log('  %s (%s): Yahoo failed %s' % (code, t, str(e)[:100]))
    log('Yahoo index closes: %d of %d' % (len(out), len(tickers)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--backfill', action='store_true', help='every session since prices start (one-time)')
    ap.add_argument('--days', type=int, default=5, help='sessions to (re)write on a normal run')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-prices', action='store_true', help='skip the Yahoo index closes')
    ap.add_argument('--debug-lists', action='store_true', help='print every table found on the member pages')
    a = ap.parse_args()
    # 1) members
    mem, names, ticker = {}, {}, {}
    for code, name, url, tk in BROAD:
        m, src = members(code, url, a.debug_lists)
        if m: mem[code] = m
        names[code] = name; ticker[code] = tk
        log('  %-5s %-14s %4d members%s' % (code, name, len(m), (' (' + src + ')') if src else '  ✗ not loaded'))
    uni = get_all('us_universe?select=symbol,sector,market_cap')
    sm = [(norm(u['symbol']), u.get('sector') or 'Other') for u in uni if u.get('market_cap') and SMALL[2] <= float(u['market_cap']) < SMALL[3]]
    if sm: mem[SMALL[0]] = sm
    names[SMALL[0]] = SMALL[1]
    log('  %-5s %-14s %4d members (us_universe, %d stocks in the universe)' % (SMALL[0], 'US Small Caps', len(sm), len(uni)))
    for sec, (code, etf) in SECTORS.items():
        names[code] = 'S&P 500 ' + sec; ticker[code] = etf
        if 'SPX' in mem:
            mem[code] = [(s, sec) for s, g in mem['SPX'] if g == sec]
            log('  %-5s %-34s %3d members (%s)' % (code, names[code], len(mem[code]), etf))
    if not mem: sys.exit('no member lists loaded')
    # 2) prices for the members we track
    last = latest_d('us_daily_ohlc')
    since = '2000-01-01' if a.backfill else (datetime.strptime(last, '%Y-%m-%d') - timedelta(days=430)).strftime('%Y-%m-%d')
    want = sorted({s for v in mem.values() for s, _ in v})
    log('%d member symbols · prices from %s to %s' % (len(want), since, last))
    ser = {}
    for j in range(0, len(want), 40):
        chunk = ','.join('"%s"' % s for s in want[j:j + 40])
        for r in get_all('us_daily_ohlc?select=symbol,d,h,l,c,v&symbol=in.(%s)&d=gte.%s&order=d.asc' % (requests.utils.quote(chunk, safe='",'), since)):
            if r.get('c') is None: continue
            ser.setdefault(r['symbol'], []).append(r)
    tracked = set(ser)
    log('%d of %d member symbols have prices in us_daily_ohlc' % (len(tracked), len(want)))
    info = {}
    for s, rs in ser.items():
        h = [float(r['h'] or r['c']) for r in rs]; l = [float(r['l'] or r['c']) for r in rs]
        c = [float(r['c']) for r in rs]; v = [float(r['v'] or 0) for r in rs]
        split_adjust(h, l, c, v)
        fl = {n: ema_flags(c, n) for n in EMAS}
        info[s] = {rs[k]['d']: ((c[k] / c[k - 1] - 1) * 100 if c[k - 1] > 0 else None, fl[9][k], fl[20][k], fl[50][k], fl[200][k]) for k in range(1, len(rs))}
    days = sorted({dd for d in info.values() for dd in d})
    if not a.backfill: days = days[-a.days:]
    else: days = days[200:] if len(days) > 260 else days
    log('%d sessions to write' % len(days))
    # 3) index closes
    yc = {} if (a.no_prices or not days) else yahoo_closes({c0: ticker[c0] for c0 in mem if c0 in ticker}, (datetime.strptime(days[0], '%Y-%m-%d') - timedelta(days=10)).strftime('%Y-%m-%d'))
    # 4) breadth
    alld = sorted({dd for d in info.values() for dd in d})
    prev = {alld[k]: alld[k - 1] for k in range(1, len(alld))}
    out = []
    def agg(code, grp, syms_, d):
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
        if grp == 'ALL' and code in yc:
            cl = yc[code].get(d); pd_ = prev.get(d); pv = yc[code].get(pd_) if pd_ else None
            row['idx_close'] = round(cl, 4) if cl else None
            row['idx_chg'] = round((cl / pv - 1) * 100, 3) if cl and pv else None
        return row
    for code, m in mem.items():
        groups = {'ALL': [s for s, _ in m]}
        if code in SECTOR_IDX:
            for s, ind in m: groups.setdefault(ind or 'Other', []).append(s)
        for d in days:
            for g, ss in groups.items():
                r = agg(code, g, ss, d)
                if r: out.append(r)
    last_rows = [r for r in out if r['d'] == days[-1] and r['grp'] == 'ALL'] if days else []
    summ = '🇺🇸 🧭 Indices %s · %d indices + 11 sectors · %s' % (days[-1] if days else '-', len([c for c in mem if not c.startswith('S_')]),
        ' · '.join('%s %d/%d above 20E' % (r['idx'], r['ab20'], r['n']) for r in last_rows if r['idx'] in SUMMARY))
    log(summ)
    if a.dry_run:
        for r in last_rows: print(json.dumps(r))
        log('dry run: nothing saved'); return
    hh = dict(H, **{'Content-Type': 'application/json', 'Prefer': 'resolution=merge-duplicates,return=minimal'})
    mrows = [{'idx': code, 'symbol': s, 'industry': ind, 'tracked': s in tracked, 'updated': last} for code, m in mem.items() for s, ind in m]
    requests.delete('%s/rest/v1/us_index_members?idx=in.(%s)' % (C.SUPABASE_URL, ','.join(mem.keys())), headers=H, timeout=60)
    for j in range(0, len(mrows), 500):
        r = requests.post('%s/rest/v1/us_index_members?on_conflict=idx,symbol' % C.SUPABASE_URL, headers=hh, data=json.dumps(mrows[j:j + 500]), timeout=120)
        if r.status_code >= 300: sys.exit('members write failed %s %s' % (r.status_code, r.text[:300]))
    for j in range(0, len(out), 500):
        r = requests.post('%s/rest/v1/us_index_breadth?on_conflict=idx,grp,d' % C.SUPABASE_URL, headers=hh, data=json.dumps(out[j:j + 500]), timeout=120)
        if r.status_code >= 300: sys.exit('breadth write failed %s %s' % (r.status_code, r.text[:300]))
    log('saved %d members, %d breadth rows' % (len(mrows), len(out)))
    if not a.backfill:
        try:
            requests.post('https://api.telegram.org/bot%s/sendMessage' % C.TF_ALERTS_TOKEN, data={'chat_id': C.TF_US, 'text': summ}, timeout=30)
        except Exception as e:
            log('telegram failed', e)


if __name__ == '__main__':
    main()
