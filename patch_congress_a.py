#!/usr/bin/env python3
"""
TrueFlow US - Patcher A
Adds two Congress sub-tabs to the Smart Money tab in us.html:
  - Congress        (per-ticker rollup from us_congress_ticker_agg)
  - Congress Trades (per-trade rows from us_congress_trades)

Safe by design:
  - asserts every anchor appears exactly once before writing
  - idempotent: refuses to run twice
  - writes a timestamped backup first
"""
import sys, os, shutil, datetime

SRC = sys.argv[1] if len(sys.argv) > 1 else "/root/trueflow/us_live.html"

s = open(SRC, encoding="utf-8").read()
orig_len = len(s)
print(f"read {SRC}: {orig_len:,} bytes")

if "sm-sub-congress" in s:
    print("ALREADY PATCHED - nothing to do")
    sys.exit(0)

# ---------------------------------------------------------------- anchors
A_BTN = '''      <button class="deals-subtab" id="sm-sub-deals" onclick="smSetView('deals',this)">📋 All Deals</button>'''
A_SETVIEW = '''function smSetView(v,btn){ SM.view=v;'''
A_RENDER = '''function smRender(){
  document.getElementById('sm-filters').innerHTML=smFiltersBar();
  document.getElementById('sm-colbtn').style.display = SM.view==='leaderboard'?'inline-block':'none';'''

for name, a in [("subtab button", A_BTN), ("smSetView", A_SETVIEW), ("smRender", A_RENDER)]:
    n = s.count(a)
    print(f"  anchor {name:18} count={n}")
    assert n == 1, f"ANCHOR '{name}' found {n} times - expected exactly 1. ABORT."

# ---------------------------------------------------------------- 1. buttons
NEW_BTNS = A_BTN + '''
      <button class="deals-subtab" id="sm-sub-congress" onclick="smSetView('congress',this)" title="Stocks members of Congress are buying, from STOCK Act disclosures">🏛️ Congress</button>
      <button class="deals-subtab" id="sm-sub-congtrades" onclick="smSetView('congtrades',this)" title="Every individual disclosed congressional trade">📋 Congress Trades</button>'''
s = s.replace(A_BTN, NEW_BTNS)

# ---------------------------------------------------------------- 2. sort key
OLD_SORT = """  SM.sortKey = (v==='leaderboard')?'cooking_score':((v==='sector'||v==='netflow'||v==='star')?'net':'value'); SM.sortDir=-1;"""
assert s.count(OLD_SORT) == 1, "ABORT: sortKey line not unique"
NEW_SORT = """  SM.sortKey = (v==='leaderboard')?'cooking_score':(v==='congress')?'distinct_buyers':(v==='congtrades')?'disclosure_date':((v==='sector'||v==='netflow'||v==='star')?'net':'value'); SM.sortDir=-1;
  if((v==='congress'||v==='congtrades') && !SM.congLoaded){ smLoadCongress(); return; }"""
s = s.replace(OLD_SORT, NEW_SORT)

# ---------------------------------------------------------------- 3. render hook
OLD_HOOK = """  if(SM.view==='leaderboard'){
    const rows=smVisBoard(); const cols=SM_COLS.filter(c=>!SM.hidden[c.key]);"""
assert s.count(OLD_HOOK) == 1, "ABORT: render hook not unique"
NEW_HOOK = """  if(SM.view==='congress'||SM.view==='congtrades'){ smRenderCongress(); return; }

  if(SM.view==='leaderboard'){
    const rows=smVisBoard(); const cols=SM_COLS.filter(c=>!SM.hidden[c.key]);"""
s = s.replace(OLD_HOOK, NEW_HOOK)

# ---------------------------------------------------------------- 4. the block
BLOCK = r'''
/* ============ CONGRESS TRADES (TrueFlow patcher A) ============ */
const CG = { agg:[], trades:[], loaded:false, err:'',
  side:'buy', party:'all', chamber:'all', owner:'all', asset:'all',
  sector:'all', conflict:false, conv:false, actionable:false, univ:false,
  late:false, amend:false, ruling:'all', q:'', days:'all' };

const CG_AGG_COLS = [
 {key:'ticker', label:'Ticker', tip:'US ticker - click to open the chart', al:'left', always:true},
 {key:'company', label:'Company', tip:'Company name as written on the filing', al:'left'},
 {key:'sector', label:'Sector', tip:'Sector (Yahoo)', al:'left'},
 {key:'industry', label:'Industry', tip:'Industry (Yahoo)', al:'left', defHide:true},
 {key:'distinct_buyers', label:'Buyers 6M', tip:'How many DIFFERENT members of Congress bought this in 6 months. This is the number that matters - congressional buying is diffuse, so a stock several separate people bought is more meaningful than one bought repeatedly by one person.', al:'right'},
 {key:'distinct_buyers_30d', label:'Buyers 30D', tip:'Different members who bought in the last 30 days. Often 0 or 1 - disclosures lag up to 45 days.', al:'right'},
 {key:'buys_6m', label:'Buys', tip:'Total buy transactions disclosed in 6 months (one member can appear several times, and the same trade in two accounts counts twice - that is how it is filed)', al:'right'},
 {key:'sells_6m', label:'Sells', tip:'Total sell transactions disclosed in 6 months', al:'right'},
 {key:'party', label:'R / D', tip:'Party split of the buyers. Red = Republican, blue = Democrat, grey = Independent.', al:'center'},
 {key:'net_amount_mid', label:'Net $', tip:'Buy value minus sell value, using the MIDPOINT of each disclosed range. Filings only give bands ($1,001-$15,000 etc), never exact values - so this is for ranking only, not a real dollar figure.', al:'right'},
 {key:'last_buy_date', label:'Last Buy', tip:'Most recent DISCLOSURE date of a buy', al:'left'},
 {key:'best_ret_since_disclosure', label:'Best %', tip:'Best return of any buy in this stock measured from its disclosure date - i.e. from the day you could first have seen it', al:'right'},
 {key:'flags', label:'Flags', tip:'Conflict / convergence / actionable markers - hover each badge', al:'center'},
 {key:'in_universe', label:'Univ', tip:'Is this ticker inside your 1,663-stock US universe?', al:'center', defHide:true}
];

const CG_TRD_COLS = [
 {key:'member_name', label:'Member', tip:'Who filed it', al:'left', always:true},
 {key:'party', label:'Party', tip:'Party. The "ruling side" filter uses whether this matches the President\'s party.', al:'center'},
 {key:'chamber', label:'Ch', tip:'House or Senate', al:'center'},
 {key:'state_dst', label:'State', tip:'State and district', al:'center', defHide:true},
 {key:'ticker', label:'Ticker', tip:'Ticker - blank for bonds, funds and anything without a symbol', al:'left'},
 {key:'company', label:'Company', tip:'Asset as written on the filing', al:'left'},
 {key:'sector', label:'Sector', tip:'Sector (Yahoo)', al:'left', defHide:true},
 {key:'industry', label:'Industry', tip:'Industry (Yahoo)', al:'left', defHide:true},
 {key:'txn_type', label:'B/S', tip:'Buy or sell. "Exchange" means shares swapped in a merger.', al:'center'},
 {key:'asset_type', label:'Type', tip:'Stock, option, bond, fund or other', al:'center'},
 {key:'owner', label:'Owner', tip:'Whose account - self, spouse, dependent child or joint. Spouse trades are included deliberately; a lot of the volume is spouses.', al:'center'},
 {key:'amount', label:'Amount', tip:'The disclosed RANGE. Congress never reports exact values, only bands.', al:'left'},
 {key:'transaction_date', label:'Traded', tip:'When the trade actually happened', al:'left'},
 {key:'disclosure_date', label:'Disclosed', tip:'When it became public - this is the first day you could have acted on it', al:'left'},
 {key:'filing_lag_days', label:'Lag', tip:'Days between trading and disclosing. The STOCK Act allows 45.', al:'right'},
 {key:'ret_since_txn', label:'% since trade', tip:'Return from the transaction date to now. Includes the move that happened before anyone could see it.', al:'right'},
 {key:'ret_since_disclosure', label:'% since disc', tip:'Return from the DISCLOSURE date to now - the only one you could actually have captured.', al:'right'},
 {key:'ret_vs_spy', label:'vs SPY', tip:'Return since disclosure minus SPY over the same period. Across all data the median is slightly NEGATIVE - the typical congressional buy has not beaten the index since going public.', al:'right'},
 {key:'account', label:'Account', tip:'Which brokerage or trust account', al:'left', defHide:true},
 {key:'flags2', label:'Flags', tip:'Late filing / amendment markers', al:'center'},
 {key:'link', label:'Filing', tip:'Open the original government filing', al:'center'}
];

async function smLoadCongress(){
  if(CG.loaded){ smRender(); return; }
  const tb=document.getElementById('sm-tbody');
  if(tb) tb.innerHTML='<tr><td colspan="14" style="padding:24px;text-align:center;color:var(--muted)">Loading congressional filings...</td></tr>';
  try{
    CG.agg = await smGet('us_congress_ticker_agg','select=*&order=distinct_buyers.desc&limit=1000');
    let all=[], off=0;
    while(off < 12000){
      const b = await smGet('us_congress_trades',
        'select=member_name,party,ruling_side,chamber,state_dst,ticker,company,sector,industry,asset_type,txn_type,owner,account,amount_low,amount_high,transaction_date,disclosure_date,filing_lag_days,is_late,is_amendment,ret_since_txn,ret_since_disclosure,ret_vs_spy,filing_url'
        + '&order=disclosure_date.desc&limit=1000&offset='+off);
      if(!Array.isArray(b) || !b.length) break;
      all = all.concat(b); off += 1000;
      if(b.length < 1000) break;
    }
    CG.trades = all;
    if(!CG.agg.length && !CG.trades.length) CG.err='No congressional data returned.';
  }catch(e){ CG.err='Could not load: '+e; }
  CG.loaded=true; SM.congLoaded=true;
  smRender();
}

function cgNum(v,d){ return (v==null||isNaN(v))?'—':(+v).toFixed(d==null?1:d); }
function cgPct(v){ if(v==null) return '<span style="color:var(--muted)">—</span>';
  const c = v>0?'var(--green)':(v<0?'var(--red)':'var(--muted)');
  return `<span style="color:${c};font-weight:600">${v>0?'+':''}${(+v).toFixed(1)}%</span>`; }
function cgMoney(v){ if(v==null||!v) return '<span style="color:var(--muted)">—</span>';
  const a=Math.abs(v), c=v>0?'var(--green)':'var(--red)';
  const t = a>=1e6 ? '$'+(a/1e6).toFixed(1)+'M' : a>=1e3 ? '$'+(a/1e3).toFixed(0)+'K' : '$'+a.toFixed(0);
  return `<span style="color:${c};font-weight:600" title="Midpoint of disclosed ranges - ranking only, not a real figure">${v>0?'+':'-'}${t}</span>`; }
function cgBand(lo,hi){ if(lo==null) return '—';
  const f=n=> n>=1e6?'$'+(n/1e6).toFixed(1)+'M' : n>=1e3?'$'+(n/1e3).toFixed(0)+'K' : '$'+n;
  return `<span style="font-size:10px;color:var(--text2)">${f(lo)} – ${f(hi)}</span>`; }
function cgParty(p){ if(!p) return '<span style="color:var(--muted)">—</span>';
  const c = p==='Republican'?'var(--red)':(p==='Democrat'?'var(--cyan)':'var(--muted)');
  return `<span style="color:${c};font-weight:600" title="${p}">${p[0]}</span>`; }

function cgFlags(r){
  let h='';
  if(r.has_committee_conflict) h+='<span title="This member sits on a committee whose remit covers this stock&#39;s sector" style="background:rgba(255,196,0,.15);color:var(--gold);border-radius:9px;padding:1px 6px;margin:0 2px;font-size:10px">⚖️</span>';
  if(r.has_convergence){
    const src=[r.conv_insider_cluster?'insider cluster':'',r.conv_activist_stake?'activist 13D':'',r.conv_delayed_ep?'Delayed EP':''].filter(Boolean).join(' + ');
    h+=`<span title="Also flagged elsewhere in TrueFlow: ${src}. This overlap is the reason the tab exists - congressional data alone lags too much to trade." style="background:rgba(0,255,136,.15);color:var(--green);border-radius:9px;padding:1px 6px;margin:0 2px;font-size:10px">🔗</span>`;
  }
  if(r.still_actionable) h+='<span title="A buy was disclosed within the last 21 days" style="background:rgba(0,212,255,.15);color:var(--cyan);border-radius:9px;padding:1px 6px;margin:0 2px;font-size:10px">⏱</span>';
  return h||'<span style="color:var(--muted)">—</span>';
}

function cgVisAgg(){
  let r = CG.agg.slice();
  if(CG.q) r=r.filter(x=>(x.ticker||'').includes(CG.q)||(x.company||'').toUpperCase().includes(CG.q));
  if(CG.sector!=='all') r=r.filter(x=>x.sector===CG.sector);
  if(CG.conflict) r=r.filter(x=>x.has_committee_conflict);
  if(CG.conv) r=r.filter(x=>x.has_convergence);
  if(CG.actionable) r=r.filter(x=>x.still_actionable);
  if(CG.univ) r=r.filter(x=>x.in_universe);
  if(CG.party==='R') r=r.filter(x=>(x.buyers_republican||0)>(x.buyers_democrat||0));
  if(CG.party==='D') r=r.filter(x=>(x.buyers_democrat||0)>(x.buyers_republican||0));
  const k=SM.sortKey, d=SM.sortDir;
  r.sort((a,b)=>{ let x=a[k], y=b[k];
    if(x==null)x=-1e18; if(y==null)y=-1e18;
    if(typeof x==='string') return d*((y>x)?-1:(y<x)?1:0);
    return d*(y-x); });
  return r;
}

function cgVisTrd(){
  let r = CG.trades.slice();
  if(CG.side==='buy') r=r.filter(x=>x.txn_type==='buy');
  else if(CG.side==='sell') r=r.filter(x=>x.txn_type==='sell');
  if(!CG.amend) r=r.filter(x=>!x.is_amendment);
  if(CG.q) r=r.filter(x=>(x.ticker||'').includes(CG.q)||(x.member_name||'').toUpperCase().includes(CG.q)||(x.company||'').toUpperCase().includes(CG.q));
  if(CG.party!=='all') r=r.filter(x=>x.party===(CG.party==='R'?'Republican':CG.party==='D'?'Democrat':'Independent'));
  if(CG.ruling==='yes') r=r.filter(x=>x.ruling_side===true);
  if(CG.ruling==='no') r=r.filter(x=>x.ruling_side===false);
  if(CG.chamber!=='all') r=r.filter(x=>x.chamber===CG.chamber);
  if(CG.owner!=='all') r=r.filter(x=>x.owner===CG.owner);
  if(CG.asset!=='all') r=r.filter(x=>x.asset_type===CG.asset);
  if(CG.sector!=='all') r=r.filter(x=>x.sector===CG.sector);
  if(CG.late) r=r.filter(x=>x.is_late);
  if(CG.univ) r=r.filter(x=>x.ticker);
  if(CG.days!=='all'){ const n=+CG.days, cut=new Date(Date.now()-n*864e5).toISOString().slice(0,10);
    r=r.filter(x=>x.disclosure_date && x.disclosure_date>=cut); }
  const k=SM.sortKey, d=SM.sortDir;
  r.sort((a,b)=>{ let x=a[k], y=b[k];
    if(x==null)x=-1e18; if(y==null)y=-1e18;
    if(typeof x==='string'||typeof y==='string'){ x=x||''; y=y||''; return d*((y>x)?-1:(y<x)?1:0); }
    return d*(y-x); });
  return r;
}

function cgSectors(){ const src = SM.view==='congress'?CG.agg:CG.trades;
  return [...new Set(src.map(r=>r.sector).filter(Boolean))].sort(); }

function cgFiltersBar(){
  const up=document.getElementById('sm-updated'); if(up) up.textContent='';
  const T=(k,lbl,tip)=>`<button class="sort-btn ${CG[k]?'multi-active':''}" onclick="cgToggle('${k}')" title="${tip}">${lbl}</button>`;
  const C=(k,v,lbl,tip)=>`<button class="sort-btn ${CG[k]===v?'multi-active':''}" onclick="cgSet('${k}','${v}')" title="${tip}">${lbl}</button>`;
  const secOpts=['<option value="all">All Sectors</option>'].concat(cgSectors().map(x=>`<option value="${x}"${CG.sector===x?' selected':''}>${x}</option>`)).join('');
  const search=`<input type="text" placeholder="🔎 ticker / member" value="${CG.q}" oninput="cgSearch(this.value)" style="background:var(--bg2);border:1px solid var(--border2);border-radius:20px;padding:4px 12px;color:var(--text);font-size:11px;outline:none;width:150px">`;
  const sel=`<select class="sort-sel" onchange="cgSet('sector',this.value)" style="background:var(--bg2);border:1px solid var(--border2);border-radius:20px;padding:4px 10px;color:var(--text);font-size:11px">${secOpts}</select>`;

  if(SM.view==='congress'){
    return `<div class="mom-filter-group">${C('party','all','All')}${C('party','R','🔴 R-led','Stocks bought by more Republicans than Democrats')}${C('party','D','🔵 D-led','Stocks bought by more Democrats than Republicans')}</div>
      <div class="mom-filter-group">${T('conflict','⚖️ Conflict','Member sits on a committee covering this stock&#39;s sector')}${T('conv','🔗 Convergence','Also has an insider cluster, activist 13D or Delayed EP - the overlap worth looking at')}${T('actionable','⏱ Recent','Disclosed in the last 21 days')}${T('univ','In universe','Only tickers inside your 1,663-stock US universe')}</div>
      ${sel}${search}
      <span style="font-size:11px;color:var(--muted)">Sorted by how many DIFFERENT members bought. Disclosures lag up to 45 days - treat this as conviction context, not an entry trigger.</span>`;
  }
  return `<div class="mom-filter-group">${C('side','buy','Buys','Purchases only - the default')}${C('side','sell','Sells','Sales only')}${C('side','all','Both','Everything including exchanges')}</div>
    <div class="mom-filter-group">${C('party','all','All')}${C('party','R','🔴 R','Republican')}${C('party','D','🔵 D','Democrat')}${C('party','I','⚪ I','Independent')}</div>
    <div class="mom-filter-group">${C('ruling','all','Any side')}${C('ruling','yes','Ruling side','Same party as the President')}${C('ruling','no','Opposition','Not the President&#39;s party')}</div>
    <div class="mom-filter-group">${C('chamber','all','Both')}${C('chamber','rep','House')}${C('chamber','sen','Senate')}</div>
    <div class="mom-filter-group">${C('owner','all','Any owner')}${C('owner','self','Self')}${C('owner','spouse','Spouse')}${C('owner','child','Child')}${C('owner','joint','Joint')}</div>
    <div class="mom-filter-group">${C('asset','all','Any type')}${C('asset','stock','Stock')}${C('asset','option','Option')}${C('asset','bond','Bond')}${C('asset','fund','Fund')}</div>
    <div class="mom-filter-group">${C('days','all','All time')}${C('days','30','30D','Disclosed in the last 30 days')}${C('days','90','90D')}</div>
    <div class="mom-filter-group">${T('late','⏰ Late','Filed more than the 45 days the STOCK Act allows')}${T('amend','Show amendments','Amendments restate an earlier filing. Hidden by default so trades are not counted twice.')}${T('univ','Has ticker','Hide bonds and funds with no symbol')}</div>
    ${sel}${search}`;
}

function cgToggle(k){ CG[k]=!CG[k]; smRender(); }
function cgSet(k,v){ CG[k]=v; smRender(); }
function cgSearch(v){ CG.q=(v||'').trim().toUpperCase(); smRender(); }

function cgCell(c,r,i){
  const pad='padding:6px 8px;text-align:'+(c.al||'left');
  const v=r[c.key];
  switch(c.key){
    case 'ticker': return r.ticker
      ? `<td style="${pad}"><a href="https://www.tradingview.com/chart/?symbol=${r.ticker}" target="_blank" style="color:var(--cyan);font-weight:700;text-decoration:none">${r.ticker}</a></td>`
      : `<td style="${pad};color:var(--muted)">—</td>`;
    case 'company': return `<td style="${pad};font-size:10px;color:var(--text2)" title="${(r.company||'').replace(/"/g,'')}">${(r.company||'—').slice(0,42)}</td>`;
    case 'party': return `<td style="${pad}">${cgParty(r.party!=null?r.party:null)||''}${
      (r.buyers_republican!=null)?`<span style="font-size:10px"><span style="color:var(--red)">${r.buyers_republican||0}</span> / <span style="color:var(--cyan)">${r.buyers_democrat||0}</span>${r.buyers_independent?' / <span style="color:var(--muted)">'+r.buyers_independent+'</span>':''}</span>`:''}</td>`;
    case 'net_amount_mid': return `<td style="${pad}">${cgMoney(v)}</td>`;
    case 'best_ret_since_disclosure':
    case 'ret_since_txn':
    case 'ret_since_disclosure':
    case 'ret_vs_spy': return `<td style="${pad}">${cgPct(v)}</td>`;
    case 'amount': return `<td style="${pad}">${cgBand(r.amount_low,r.amount_high)}</td>`;
    case 'flags': return `<td style="${pad}">${cgFlags(r)}</td>`;
    case 'flags2': { let h='';
      if(r.is_late) h+='<span title="Filed later than the 45 days the STOCK Act allows" style="color:var(--gold);margin:0 2px">⏰</span>';
      if(r.is_amendment) h+='<span title="Amendment - restates an earlier filing. Excluded from all totals." style="color:var(--muted);margin:0 2px">✎</span>';
      return `<td style="${pad}">${h||'<span style="color:var(--muted)">—</span>'}</td>`; }
    case 'link': return r.filing_url
      ? `<td style="${pad}"><a href="${r.filing_url}" target="_blank" title="Open the original government filing" style="color:var(--cyan);text-decoration:none">↗</a></td>`
      : `<td style="${pad};color:var(--muted)">—</td>`;
    case 'in_universe': return `<td style="${pad}">${v?'<span style="color:var(--green)" title="Inside your US universe">✓</span>':'<span style="color:var(--muted)">—</span>'}</td>`;
    case 'chamber': return `<td style="${pad};font-size:10px" title="${v==='sen'?'Senate':'House'}">${v==='sen'?'S':'H'}</td>`;
    case 'txn_type': { const c2=v==='buy'?'var(--green)':(v==='sell'?'var(--red)':'var(--muted)');
      return `<td style="${pad};color:${c2};font-weight:600">${v==='buy'?'BUY':v==='sell'?'SELL':'EXCH'}</td>`; }
    case 'filing_lag_days': { const c3=(v!=null&&v>45)?'var(--gold)':'var(--text2)';
      return `<td style="${pad};color:${c3}">${v==null?'—':v}</td>`; }
    case 'distinct_buyers': return `<td style="${pad};font-weight:700;color:var(--text)">${v||0}</td>`;
    case 'member_name': return `<td style="${pad};font-weight:600;color:var(--text)" title="${(r.member_name||'')}">${(r.member_name||'—').slice(0,26)}</td>`;
    case 'account': return `<td style="${pad};font-size:10px;color:var(--muted)" title="${(r.account||'').replace(/"/g,'')}">${(r.account||'—').slice(0,24)}</td>`;
    default: return `<td style="${pad};color:var(--text2)">${(v==null||v==='')?'—':v}</td>`;
  }
}

function smRenderCongress(){
  const fb=document.getElementById('sm-filters'); if(fb) fb.innerHTML=cgFiltersBar();
  const cb=document.getElementById('sm-colbtn'); if(cb) cb.style.display='none';
  const thead=document.getElementById('sm-thead'), tbody=document.getElementById('sm-tbody');
  const isAgg = SM.view==='congress';
  const cols = isAgg?CG_AGG_COLS:CG_TRD_COLS;
  const rows = isAgg?cgVisAgg():cgVisTrd();
  const arrow=(k)=>SM.sortKey===k?(SM.sortDir<0?' ▼':' ▲'):'';
  const sortable=new Set(isAgg
    ? ['ticker','sector','distinct_buyers','distinct_buyers_30d','buys_6m','sells_6m','net_amount_mid','last_buy_date','best_ret_since_disclosure']
    : ['member_name','ticker','sector','txn_type','transaction_date','disclosure_date','filing_lag_days','ret_since_txn','ret_since_disclosure','ret_vs_spy']);

  thead.innerHTML='<tr style="position:sticky;top:0;background:var(--bg1);z-index:2">'+cols.map(c=>{
    const sb=sortable.has(c.key);
    return `<th title="${(c.tip||'').replace(/"/g,'&quot;')}" style="text-align:${c.al};padding:6px 8px;${sb?'cursor:pointer':''}" ${sb?`onclick="smSort('${c.key}')"`:''}>${c.label}${sb?arrow(c.key):''}</th>`;
  }).join('')+'</tr>';

  if(CG.err){ tbody.innerHTML=`<tr><td colspan="${cols.length}"><div class="mom-empty"><div class="mom-empty-text">${CG.err}</div></div></td></tr>`; }
  else if(!rows.length){ tbody.innerHTML=`<tr><td colspan="${cols.length}"><div class="mom-empty"><div class="mom-empty-icon">🏛️</div><div class="mom-empty-text">No congressional trades match these filters</div></div></td></tr>`; }
  else tbody.innerHTML=rows.slice(0,1500).map((r,i)=>'<tr style="border-top:1px solid var(--border)">'+cols.map(c=>cgCell(c,r,i)).join('')+'</tr>').join('');

  const cnt=document.getElementById('sm-count');
  if(cnt) cnt.textContent = isAgg
    ? `${rows.length} stocks`
    : `${rows.length} trades${rows.length>1500?' (showing 1500)':''}`;
}
/* ============ END CONGRESS (patcher A) ============ */

'''

# insert the block immediately before smSetView
s = s.replace(A_SETVIEW, BLOCK + A_SETVIEW)

# ---------------------------------------------------------------- write
bak = SRC + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(SRC, bak)
open(SRC, "w", encoding="utf-8").write(s)

print(f"backup    : {bak}")
print(f"before    : {orig_len:,} bytes")
print(f"after     : {len(s):,} bytes  (+{len(s)-orig_len:,})")
print("markers   : sm-sub-congress=%d  smRenderCongress=%d  smLoadCongress=%d  CG_AGG_COLS=%d"
      % (s.count("sm-sub-congress"), s.count("smRenderCongress"),
         s.count("smLoadCongress"), s.count("CG_AGG_COLS")))
print("PATCH A APPLIED OK")
