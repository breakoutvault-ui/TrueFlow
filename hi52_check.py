import tf_config as C, requests, sys
from datetime import datetime, timedelta
H = {'apikey': C.SUPABASE_KEY, 'Authorization': 'Bearer ' + C.SUPABASE_KEY}
U = C.SUPABASE_URL + '/rest/v1/'
def g(p):
    o, off = [], 0
    while 1:
        b = requests.get(U + p + '&limit=1000&offset=%d' % off, headers=H, timeout=120).json(); o += b
        if len(b) < 1000: return o
        off += 1000
sd = requests.get(U + 'momentum_stocks?select=session_date&order=session_date.desc&limit=1', headers=H, timeout=60).json()[0]['session_date']
ms = g('momentum_stocks?select=symbol,ltp,high_52w,breakout_type,breakout_price&session_date=eq.%s&order=symbol.asc' % sd)
since = (datetime.strptime(sd, '%Y-%m-%d') - timedelta(days=380)).strftime('%Y-%m-%d')
oh = g('daily_ohlc?select=symbol,d,h,c&d=gte.%s&d=lte.%s&order=d.asc,symbol.asc' % (since, sd))
S = {}
for r in oh: S.setdefault(r['symbol'], []).append(r)
n = mis = new_hi = near1 = dash_zone = dash_excl = noh = 0; ex = []
for m in ms:
    s = S.get(m['symbol'])
    if not s or s[-1]['d'] != sd or m.get('ltp') is None: noh += 1; continue
    w = s[-252:]; t52 = max(float(x['h']) for x in w); c = float(s[-1]['c']); h = float(s[-1]['h']); n += 1
    if h >= t52: new_hi += 1
    if c >= t52 * 0.99: near1 += 1
    hd = m.get('high_52w')
    if hd:
        hd = float(hd)
        if abs(hd - t52) / t52 > 0.01:
            mis += 1
            if len(ex) < 8: ex.append('%s dash %.1f vs prices %.1f' % (m['symbol'], hd, t52))
        dist = (hd - float(m['ltp'])) / hd * 100
        if not (m.get('breakout_type') and m.get('breakout_price')):
            if 0 < dist <= 15: dash_zone += 1
            elif dist <= 0: dash_excl += 1
print('scan date', sd, '| stocks checked', n, '| no price row', noh)
print('52W high in scan differs from prices by >1%:', mis, ex)
print('made a new 52W high today (prices):', new_hi, '| closed within 1% of 52W high:', near1)
print('dashboard BO Zone (0-15% below high):', dash_zone, '| AT/ABOVE high but left out of the 52W filter:', dash_excl)
