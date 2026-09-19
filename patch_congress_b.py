#!/usr/bin/env python3
"""
TrueFlow US - Patcher B
Adds congressional-trading data to the Screener:
  - 8 new columns in a "Congress" group
  - 6 new filters
  - SCR_FILTER_COL entries so Smart Columns reveals them
  - scrPasses logic
  - a fetch in scrLoad + merge onto every row

Safe by design: asserts every anchor is unique, idempotent, backs up first.
"""
import sys, shutil, datetime

SRC = sys.argv[1] if len(sys.argv) > 1 else "/root/trueflow/us_live.html"
s = open(SRC, encoding="utf-8").read()
orig = len(s)
print(f"read {SRC}: {orig:,} chars")

if "congMap" in s:
    print("ALREADY PATCHED - nothing to do")
    sys.exit(0)

# ------------------------------------------------------------ anchors
A_COLS = """ {k:'aplus',g:'Actions',lbl:'A+',a:'c',tip:'A+ confluence flag',def:1,cell:d=>d.aplus?`<span class="scr-aplus"${SCR_T('A+ Setup — VCP/HTF + Cat A/A+C + base >5d & <10% deep + volume dry-up (base + last-5 avg RVOL <0.8x) + 2x+ volume day in last 20 + BO zone + ADR 3+')}>⭐</span>`:''},
];"""

A_FILT = """ {key:'price',label:'Price',opts:['100','500','1000'],disp:{'100':'≥$100','500':'≥$500','1000':'≥$1000'}},
];"""

A_FCOL = "const SCR_FILTER_COL={rclgrade:'rclgrade',"

A_ROW = """   return {sym:s.symbol,name:(s.company_name||''),sector:s.sector||'—',industry:s.industry||'—',cat,
    rs:_rsr||null, stk:_stk||null,"""

A_STAKE = """   const _stk=SCR.stakeMap?SCR.stakeMap[s.symbol]:null;"""

A_PASS = """ if(S.sector.size&&!S.sector.has(d.sector))return false;"""

A_LOADMAP = """  // Relative strength vs SPY / QQQ (own table, joined on symbol)"""

for name, a in [("SCR_COLS end", A_COLS), ("SCR_FILTERS end", A_FILT),
                ("SCR_FILTER_COL", A_FCOL), ("row builder", A_ROW),
                ("stakeMap line", A_STAKE), ("scrPasses sector", A_PASS),
                ("scrLoad rs comment", A_LOADMAP)]:
    n = s.count(a)
    print(f"  anchor {name:22} count={n}")
    assert n == 1, f"ANCHOR '{name}' found {n} times - expected 1. ABORT."

# ------------------------------------------------------------ 1. columns
NEW_COLS = A_COLS.replace("\n];", """
 {k:'cgbuyers',g:'Congress',lbl:'🏛️ Buyers',a:'r',tip:'How many DIFFERENT members of Congress bought this in the last 6 months. Several separate people buying means more than one person buying repeatedly. Disclosures lag up to 45 days, so treat this as conviction context, not a trigger.',def:0,cell:d=>d.cg&&d.cg.distinct_buyers?`<span class="scr-score ${d.cg.distinct_buyers>=5?'scr-s-hi':d.cg.distinct_buyers>=3?'scr-s-mid':'scr-s-lo'}"${SCR_T(d.cg.distinct_buyers+' different members bought in 6 months')}>${d.cg.distinct_buyers}</span>`:'<span class="scr-dash">—</span>'},
 {k:'cgbuy30',g:'Congress',lbl:'🏛️ 30D',a:'r',tip:'Different members who bought in the last 30 days. Usually 0 or 1 — congressional buying is diffuse and disclosure lags.',def:0,cell:d=>d.cg&&d.cg.distinct_buyers_30d?`<span class="scr-yes">${d.cg.distinct_buyers_30d}</span>`:'<span class="scr-dash">—</span>'},
 {k:'cglast',g:'Congress',lbl:'Last Buy',a:'c',tip:'Date the most recent congressional buy was DISCLOSED — the first day you could have seen it',def:0,cell:d=>d.cg&&d.cg.last_buy_date?`<span class="scr-sub">${d.cg.last_buy_date.slice(5)}</span>`:'<span class="scr-dash">—</span>'},
 {k:'cgnet',g:'Congress',lbl:'Net $',a:'r',tip:'Buys minus sells using the MIDPOINT of each disclosed band. Filings give ranges only, never exact values — for ranking, not a real figure.',def:0,cell:d=>{if(!d.cg||!d.cg.net_amount_mid)return '<span class="scr-dash">—</span>';const v=d.cg.net_amount_mid,a2=Math.abs(v);const t=a2>=1e6?'$'+(a2/1e6).toFixed(1)+'M':a2>=1e3?'$'+(a2/1e3).toFixed(0)+'K':'$'+a2.toFixed(0);return `<span style="color:${v>0?'var(--green)':'var(--red)'};font-weight:600">${v>0?'+':'-'}${t}</span>`;}},
 {k:'cgparty',g:'Congress',lbl:'R / D',a:'c',tip:'Party split of the buyers — Republican / Democrat',def:0,cell:d=>d.cg&&(d.cg.buyers_republican||d.cg.buyers_democrat)?`<span style="font-size:10px"><span style="color:var(--red)">${d.cg.buyers_republican||0}</span><span class="scr-dash">/</span><span style="color:var(--cyan)">${d.cg.buyers_democrat||0}</span></span>`:'<span class="scr-dash">—</span>'},
 {k:'cgret',g:'Congress',lbl:'% since disc',a:'r',tip:'Best return of any congressional buy here, measured from its DISCLOSURE date. Across all data the median congressional buy has slightly UNDERPERFORMED SPY since going public.',def:0,cell:d=>(d.cg&&d.cg.best_ret_since_disclosure!=null)?scrPct(d.cg.best_ret_since_disclosure):'<span class="scr-dash">—</span>'},
 {k:'cgconf',g:'Congress',lbl:'⚖️',a:'c',tip:'Committee conflict — a member who bought sits on a committee whose remit covers the sector of this stock',def:0,cell:d=>(d.cg&&d.cg.has_committee_conflict)?`<span class="scr-yes"${SCR_T('A buyer sits on a committee covering this sector')}>⚖️</span>`:'<span class="scr-dash">—</span>'},
 {k:'cgconv',g:'Congress',lbl:'🔗',a:'c',tip:'Convergence — congressional buying PLUS an insider cluster, activist 13D or Delayed EP. This overlap is the reason the congressional data earns its place; on its own it lags too much to trade.',def:0,cell:d=>{if(!d.cg||!d.cg.has_convergence)return '<span class="scr-dash">—</span>';const src=[d.cg.conv_insider_cluster?'insider cluster':'',d.cg.conv_activist_stake?'activist 13D':'',d.cg.conv_delayed_ep?'Delayed EP':''].filter(Boolean).join(' + ');return `<span class="scr-yes"${SCR_T('Also flagged: '+src)}>🔗</span>`;}},
];""")
s = s.replace(A_COLS, NEW_COLS)

# ------------------------------------------------------------ 2. filters
NEW_FILT = A_FILT.replace("\n];", """
 {key:'cgbuyersmin',label:'🏛️ Congress Buyers',tip:'Stocks bought by several DIFFERENT members of Congress in the last 6 months. Disclosures lag up to 45 days — this is conviction context, not an entry trigger.',opts:['2','3','5'],disp:{'2':'≥2 members','3':'≥3 members','5':'≥5 members'},tips:{'2':'At least two different members bought this.','3':'At least three — a genuine spread of interest.','5':'Five or more different members. Rare.'}},
 {key:'cgrecent',label:'🏛️ Recent',tip:'Congressional buying disclosed recently',opts:['30','21'],disp:{'30':'Bought in 30D','21':'Still actionable'},tips:{'30':'At least one member bought and disclosed within the last 30 days.','21':'Disclosed within the last 21 days — as fresh as this data gets.'}},
 {key:'cgconf',label:'🏛️ Conflict',tip:'A member who bought sits on a committee covering the sector of this stock',opts:['y'],disp:{y:'⚖️ Committee conflict'},tips:{y:'One of the buyers sits on a committee whose remit covers this sector. Note: six members including Pelosi have no committee assignments, so this can never fire for them.'}},
 {key:'cgconv',label:'🏛️ Convergence',tip:'Congressional buying that coincides with another TrueFlow signal',opts:['any','insider','activist','ep'],disp:{any:'🔗 Any overlap',insider:'Insider cluster',activist:'Activist 13D',ep:'Delayed EP'},tips:{any:'Congress bought AND there is an insider cluster, activist 13D or Delayed EP. The overlap worth looking at.',insider:'Congress bought and 3+ company insiders also clustered buys.',activist:'Congress bought and an activist filed a 13D stake.',ep:'Congress bought and the stock has a Delayed Episodic Pivot.'}},
 {key:'cgparty',label:'🏛️ Party',tip:'Which side is doing the buying',opts:['R','D'],disp:{R:'🔴 R-led',D:'🔵 D-led'},tips:{R:'More Republican buyers than Democrat.',D:'More Democrat buyers than Republican.'}},
 {key:'cgretmin',label:'🏛️ Since Disclosure',tip:'Best return since the buy became public',opts:['0','10','25'],disp:{'0':'≥0%','10':'≥10%','25':'≥25%'},tips:{'0':'Still above where it was when disclosed.','10':'Up 10%+ since disclosure.','25':'Up 25%+ since disclosure.'}},
];""")
s = s.replace(A_FILT, NEW_FILT)

# ------------------------------------------------------------ 3. SCR_FILTER_COL
s = s.replace(A_FCOL, "const SCR_FILTER_COL={cgbuyersmin:'cgbuyers',cgrecent:'cgbuy30',cgconf:'cgconf',cgconv:'cgconv',cgparty:'cgparty',cgretmin:'cgret',rclgrade:'rclgrade',")

# ------------------------------------------------------------ 4. fetch in scrLoad
NEW_LOAD = """  // Congressional trades - per-ticker rollup (patcher B)
  const _cg={};
  try{
    const _cgr=await volGet('us_congress_ticker_agg?select=ticker,distinct_buyers,distinct_buyers_30d,buys_6m,sells_6m,buyers_republican,buyers_democrat,net_amount_mid,last_buy_date,best_ret_since_disclosure,has_committee_conflict,has_convergence,conv_insider_cluster,conv_activist_stake,conv_delayed_ep,still_actionable&limit=1000');
    (_cgr||[]).forEach(r=>{ if(r.ticker) _cg[r.ticker]=r; });
  }catch(e){ console.warn('congress load failed',e); }
  SCR.congMap=_cg;

""" + A_LOADMAP
s = s.replace(A_LOADMAP, NEW_LOAD)

# ------------------------------------------------------------ 5. merge onto row
s = s.replace(A_STAKE, A_STAKE + """
   const _cgr2=SCR.congMap?SCR.congMap[s.symbol]:null;""")
s = s.replace(A_ROW, A_ROW + """cg:_cgr2||null,""")

# ------------------------------------------------------------ 6. scrPasses
NEW_PASS = A_PASS + """
 if(S.cgbuyersmin&&S.cgbuyersmin.size){const m=Math.min(...[...S.cgbuyersmin].map(Number));if(!d.cg||!d.cg.distinct_buyers||d.cg.distinct_buyers<m)return false;}
 if(S.cgrecent&&S.cgrecent.size){let ok=false;S.cgrecent.forEach(v=>{if(v==='30'&&d.cg&&d.cg.distinct_buyers_30d>0)ok=true;if(v==='21'&&d.cg&&d.cg.still_actionable)ok=true;});if(!ok)return false;}
 if(S.cgconf&&S.cgconf.size){if(!d.cg||!d.cg.has_committee_conflict)return false;}
 if(S.cgconv&&S.cgconv.size){let ok=false;S.cgconv.forEach(v=>{if(!d.cg)return;if(v==='any'&&d.cg.has_convergence)ok=true;if(v==='insider'&&d.cg.conv_insider_cluster)ok=true;if(v==='activist'&&d.cg.conv_activist_stake)ok=true;if(v==='ep'&&d.cg.conv_delayed_ep)ok=true;});if(!ok)return false;}
 if(S.cgparty&&S.cgparty.size){let ok=false;S.cgparty.forEach(v=>{if(!d.cg)return;const R=d.cg.buyers_republican||0,D=d.cg.buyers_democrat||0;if(v==='R'&&R>D)ok=true;if(v==='D'&&D>R)ok=true;});if(!ok)return false;}
 if(S.cgretmin&&S.cgretmin.size){const m=Math.min(...[...S.cgretmin].map(Number));if(!d.cg||d.cg.best_ret_since_disclosure==null||d.cg.best_ret_since_disclosure<m)return false;}"""
s = s.replace(A_PASS, NEW_PASS)

# ------------------------------------------------------------ write
bak = SRC + ".bakB-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(SRC, bak)
open(SRC, "w", encoding="utf-8").write(s)

print(f"backup : {bak}")
print(f"before : {orig:,}")
print(f"after  : {len(s):,}  (+{len(s)-orig:,})")
print("markers: congMap=%d  cgbuyers=%d  cgconv=%d  cgbuyersmin=%d  cg:_cgr2=%d"
      % (s.count("congMap"), s.count("cgbuyers"), s.count("cgconv"),
         s.count("cgbuyersmin"), s.count("cg:_cgr2")))
print("PATCH B APPLIED OK")
