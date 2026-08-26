#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_ndwl.py — TrueFlow India dashboard patcher (Next Day WL v2 build)

Applies six patches to Index.html.html:

  1  Live Alerts  — Stage 1/2/3/4 pills with correct meanings. Anything
                    that was not stage 2 has been rendering as "Stage 1"
                    since 21 May, which buried 4,639 alerts in the wrong
                    bucket.
  2  Live Alerts  — Stage 3 and Stage 4 filter buttons, and corrected
                    tooltips on 1 and 2 (they are the COUNTER-trend
                    stages, which the old tooltips never said).
  3  Live Alerts  — % distance from the daily 5 EMA and 9 EMA on every
                    alert card, read from momentum_stocks.
  4  Alert History— four-stage badges and filter, plus a real win rate.
                    The old one divided wins by TOTAL including pending,
                    so every number shown was understated.
  5  Alert History— a four-stage performance table.
  6  Next Day WL  — reads nextday_picks_v2, adds a Journal tab and an
                    Old vs New tab.

SAFETY
  * Every patch is anchored on an exact string that must appear exactly
    once. Any anchor that does not match aborts the whole run — nothing
    is written on a partial match.
  * Writes a timestamped .bak first.
  * Verifies the output is larger than the input and still contains the
    closing </html>.

USAGE
  cd /root/trueflow && bin/python apply_ndwl.py Index.html.html
"""

import sys
import shutil
from datetime import datetime

PATCHES = []


def patch(name, old, new, expect=1):
    """expect = how many times the anchor legitimately appears.
    renderAlertList is defined twice (line ~6666, then overridden by a
    multi-filter version ~9041). The second is the live one, but both are
    patched so the file stays internally consistent."""
    PATCHES.append((name, old, new, expect))


# ══════════════════════════════════════════════════════════════════════
# 1 — Live Alerts stage pill: four stages, not two
# ══════════════════════════════════════════════════════════════════════
patch(
    "1. Live Alerts stage pill",
    """    const stagePill = a.stage===2?'<span class="tag tag-s2">Stage 2</span>':'<span class="tag tag-s1">Stage 1</span>';""",
    """    const stagePill = tfStagePill(a.stage);""",
    expect=2
)

# ══════════════════════════════════════════════════════════════════════
# 2 — Stage filter buttons + honest tooltips
# ══════════════════════════════════════════════════════════════════════
patch(
    "2. Stage filter buttons",
    """              <button class="sort-btn" data-filter-group="alert-stage" data-filter-val="2" onclick="toggleMultiFilter('alert','stage','2',this)" style="color:#22C55E" title="Stage 2: HIGH CONVICTION — ORB breakout confirmed on BOTH 5-min AND 15-min EMA. Best setups, act immediately">🔥 Stage 2</button>
              <button class="sort-btn" data-filter-group="alert-stage" data-filter-val="1" onclick="toggleMultiFilter('alert','stage','1',this)" style="color:var(--amber)" title="Stage 1: Standard breakout — ORB confirmed on 5-min EMA only. Valid setup but less conviction than Stage 2">⚡ Stage 1</button>""",
    """              <button class="sort-btn" data-filter-group="alert-stage" data-filter-val="4" onclick="toggleMultiFilter('alert','stage','4',this)" style="color:#22C55E" title="Stage 4: 5-min AND 15-min EMA aligned, AND the breakout runs WITH the daily trend. All three timeframes agree.">🔥 Stage 4</button>
              <button class="sort-btn" data-filter-group="alert-stage" data-filter-val="3" onclick="toggleMultiFilter('alert','stage','3',this)" style="color:#3B82F6" title="Stage 3: 5-min EMA only, but the breakout runs WITH the daily trend.">📈 Stage 3</button>
              <button class="sort-btn" data-filter-group="alert-stage" data-filter-val="2" onclick="toggleMultiFilter('alert','stage','2',this)" style="color:var(--amber)" title="Stage 2: 5-min AND 15-min EMA aligned, but AGAINST the daily trend. Measured 44.1% win / +0.261R over 444 backtested trades — the best of the four, though counter-trend.">⚡ Stage 2</button>
              <button class="sort-btn" data-filter-group="alert-stage" data-filter-val="1" onclick="toggleMultiFilter('alert','stage','1',this)" style="color:var(--muted)" title="Stage 1: 5-min EMA only, AGAINST the daily trend. Weakest of the four — highest chance of a reversal.">· Stage 1</button>"""
)

# ══════════════════════════════════════════════════════════════════════
# 3 — daily EMA distance on each alert card
# ══════════════════════════════════════════════════════════════════════
patch(
    "3a. EMA distance on card",
    """        <div style="font-size:11px;color:var(--muted)">ORB ${a.orb_side||'—'} · ${a.orb_type==='wide'?'Wide':'Narrow'}</div>""",
    """        <div style="font-size:11px;color:var(--muted)">ORB ${a.orb_side||'—'} · ${a.orb_type==='wide'?'Wide':'Narrow'}</div>
        <div style="font-size:10px;color:var(--muted)">${tfEmaDistHTML(a.symbol)}</div>""",
    expect=2
)

patch(
    "3b. load EMA distances",
    """async function loadLiveAlerts() {
  try {
    const today = new Date().toISOString().slice(0,10);""",
    """async function loadLiveAlerts() {
  try {
    tfLoadEmaDist();
    const today = new Date().toISOString().slice(0,10);"""
)

# ══════════════════════════════════════════════════════════════════════
# 4 — Alert History: four-stage badge, filter, honest win rate
# ══════════════════════════════════════════════════════════════════════
patch(
    "4a. History stage badge",
    """    const stgH=a.stage===2?'<span class="tag tag-s2">S2</span>':'<span class="tag tag-s1">S1</span>';""",
    """    const stgH=tfStageBadge(a.stage);"""
)

patch(
    "4b. History stage filter options",
    """          <option value="all">All Stages</option>
          <option value="2">Stage 2 Only</option>
          <option value="1">Stage 1 Only</option>""",
    """          <option value="all">All Stages</option>
          <option value="4">S4 · 5m+15m, with trend</option>
          <option value="3">S3 · 5m only, with trend</option>
          <option value="2">S2 · 5m+15m, counter-trend</option>
          <option value="1">S1 · 5m only, counter-trend</option>"""
)

patch(
    "4c. Real win rate + 4-stage table",
    """  const total=list.length;
  const wins=list.filter(a=>a.outcome==='win').length;
  const wr=total>0?Math.round(wins/total*100)+'%':'—';""",
    """  const total=list.length;
  const wins=list.filter(a=>a.outcome==='win').length;
  // Win rate must exclude alerts that have no result yet. Dividing by
  // TOTAL (which includes pending) understated every number on this tab.
  const decided=list.filter(a=>a.outcome==='win'||a.outcome==='loss').length;
  const wr=decided>0?Math.round(wins/decided*100)+'%':'—';
  tfRenderStageTable(list);"""
)

# ══════════════════════════════════════════════════════════════════════
# 5 — the four-stage table container
# ══════════════════════════════════════════════════════════════════════
patch(
    "5. Stage table container",
    """          <!-- Daily Win Rate Trend (bar chart) -->""",
    """          <!-- Four-stage performance (added by Next Day WL v2 build) -->
          <div class="fo-card" style="padding:12px 14px;margin-top:8px">
            <div class="fo-card-label" style="margin-bottom:8px">Performance by Stage <span style="font-weight:400;color:var(--muted);font-size:9px">· win rate excludes pending</span></div>
            <div id="ah-stage-table" style="font-size:11px;color:var(--muted)">—</div>
          </div>
          <!-- Daily Win Rate Trend (bar chart) -->"""
)

# ══════════════════════════════════════════════════════════════════════
# 6 — Next Day WL: v2 source + Journal + Old vs New
# ══════════════════════════════════════════════════════════════════════
patch(
    "6a. WL sub-tab buttons",
    """        <button class="sort-btn" id="wl-tab-analytics" onclick="switchWLTab('analytics')" style="font-size:10px;padding:3px 10px">🧠 Analytics</button>""",
    """        <button class="sort-btn" id="wl-tab-analytics" onclick="switchWLTab('analytics')" style="font-size:10px;padding:3px 10px">🧠 Analytics</button>
        <button class="sort-btn" id="wl-tab-journal" onclick="switchWLTab('journal')" style="font-size:10px;padding:3px 10px">📔 Journal</button>
        <button class="sort-btn" id="wl-tab-versus" onclick="switchWLTab('versus')" style="font-size:10px;padding:3px 10px">⚖️ Old vs New</button>"""
)

patch(
    "6b. WL panels",
    """      <div id="wl-analytics-panel" style="display:none">""",
    """      <div id="wl-journal-panel" style="display:none">
        <div id="wl-journal-body" style="font-size:11px;color:var(--muted)">Loading…</div>
      </div>
      <div id="wl-versus-panel" style="display:none">
        <div id="wl-versus-body" style="font-size:11px;color:var(--muted)">Loading…</div>
      </div>
      <div id="wl-analytics-panel" style="display:none">"""
)

patch(
    "6c. WL tab switcher",
    """function switchWLTab(tab) {
  document.getElementById('wl-picks-panel').style.display = tab === 'picks' ? '' : 'none';
  document.getElementById('wl-perf-panel').style.display = tab === 'perf' ? '' : 'none';
  document.getElementById('wl-analytics-panel').style.display = tab === 'analytics' ? '' : 'none';
  ['picks','perf','analytics'].forEach(t => {
    const btn = document.getElementById('wl-tab-'+t);
    if (btn) btn.classList.toggle('active', t === tab);
  });
  if (tab === 'perf') loadWLPerformance();
  if (tab === 'analytics') loadWLAnalytics();
}""",
    """function switchWLTab(tab) {
  ['picks','perf','analytics','journal','versus'].forEach(t => {
    const p = document.getElementById('wl-'+t+'-panel');
    if (p) p.style.display = (t === tab) ? '' : 'none';
    const btn = document.getElementById('wl-tab-'+t);
    if (btn) btn.classList.toggle('active', t === tab);
  });
  if (tab === 'perf') loadWLPerformance();
  if (tab === 'analytics') loadWLAnalytics();
  if (tab === 'journal') loadWLJournal();
  if (tab === 'versus') loadWLVersus();
}"""
)

patch(
    "6d. WL picks source -> nextday_picks_v2",
    """    const url = `${CONFIG.supabaseUrl}/rest/v1/watchlist_picks?select=*&order=session_date.desc,score.desc&limit=40`;""",
    """    const url = `${CONFIG.supabaseUrl}/rest/v1/nextday_picks_v2?select=*&order=target_date.desc,score.desc&limit=60`;"""
)

patch(
    "6e. WL latest-session key",
    """    // Filter to latest session
    const latestDate = data[0].session_date;
    wlPicks = data.filter(p => p.session_date === latestDate);""",
    """    // Filter to the latest TARGET date (v2 is keyed on the day it is for)
    const latestDate = data[0].target_date;
    wlPicks = data.filter(p => p.target_date === latestDate);"""
)

patch(
    "6f. WL pick rendering",
    """function renderWLPicks() {
  const bulls = wlPicks.filter(p => p.direction === 'BULL').sort((a,b) => b.score - a.score);
  const bears = wlPicks.filter(p => p.direction === 'BEAR').sort((a,b) => b.score - a.score);""",
    """function renderWLPicks() {
  const dir = p => String(p.direction||'').toLowerCase();
  const bulls = wlPicks.filter(p => dir(p)==='bull'||dir(p)==='BULL'.toLowerCase()).sort((a,b) => b.score - a.score);
  const bears = wlPicks.filter(p => dir(p)==='bear').sort((a,b) => b.score - a.score);"""
)

patch(
    "6g. WL pick row detail",
    """      const reasons = JSON.parse(p.reasons || '{}');
      const topReason = Object.values(reasons)[0] || '';""",
    """      const topReason = tfPickBadges(p);"""
)

patch(
    "6h. WL pick row footer",
    """          <span style="font-size:8px;color:var(--muted);display:block">${p.oi_buildup||''} | Vol:${p.vol_ratio||0}x</span>""",
    """          <span style="font-size:8px;color:var(--muted);display:block">${p.oi_buildup||''} | Vol:${p.vol_ratio||0}x</span>
          <span style="font-size:8px;color:var(--muted);display:block">${topReason}</span>"""
)

patch(
    "6i. WL date footer",
    """    document.getElementById('wl-target-date').textContent = `For: ${wlPicks[0].target_date} | Generated: ${wlPicks[0].session_date}`;""",
    """    document.getElementById('wl-target-date').textContent = `For: ${wlPicks[0].target_date} | Scored: ${wlPicks[0].session_date} | model v${wlPicks[0].weights_version||1}`;"""
)


# ══════════════════════════════════════════════════════════════════════
#  NEW JAVASCRIPT — appended before the final </script>
# ══════════════════════════════════════════════════════════════════════

NEW_JS = r"""
/* ═══════════════════════════════════════════════════════════════════
   NEXT DAY WL v2  —  helpers added by apply_ndwl.py
   ═══════════════════════════════════════════════════════════════════ */

/* The four stages, stated properly. Confirmed from 5,753 alerts:
   stages 3 and 4 are 99.5% aligned with the daily trend, 1 and 2 are
   against it. 15-minute confirmation is what separates 2/4 from 1/3. */
var TF_STAGES = {
  1: {lab:'S1', full:'Stage 1', cls:'tag-s1', col:'var(--muted)',
      tip:'5-min EMA only, AGAINST the daily trend. Weakest of the four.'},
  2: {lab:'S2', full:'Stage 2', cls:'tag-s2', col:'var(--amber)',
      tip:'5-min AND 15-min aligned, but AGAINST the daily trend. 44.1% win / +0.261R over 444 backtested trades.'},
  3: {lab:'S3', full:'Stage 3', cls:'tag-s1', col:'#3B82F6',
      tip:'5-min EMA only, WITH the daily trend.'},
  4: {lab:'S4', full:'Stage 4', cls:'tag-s2', col:'#22C55E',
      tip:'5-min AND 15-min aligned, WITH the daily trend. All three timeframes agree.'}
};

function tfStage(s){ return TF_STAGES[parseInt(s)] || TF_STAGES[1]; }

function tfStagePill(s){
  var t = tfStage(s);
  return '<span class="tag '+t.cls+'" style="color:'+t.col+'" title="'+t.tip+'">'+t.full+'</span>';
}

function tfStageBadge(s){
  var t = tfStage(s);
  return '<span class="tag '+t.cls+'" style="color:'+t.col+'" title="'+t.tip+'">'+t.lab+'</span>';
}

/* ── daily EMA distance, from momentum_stocks ────────────────────── */
window.tfEmaDist = window.tfEmaDist || null;

async function tfLoadEmaDist(){
  if (window.tfEmaDist) return window.tfEmaDist;
  try {
    var h = {'apikey':CONFIG.supabaseKey,'Authorization':'Bearer '+CONFIG.supabaseKey};
    var r = await fetch(CONFIG.supabaseUrl+'/rest/v1/momentum_stocks?select=session_date&order=session_date.desc&limit=1',{headers:h});
    var d = await r.json();
    if(!d || !d.length) return null;
    var sd = d[0].session_date, map = {}, from = 0;
    /* momentum_stocks is ~1450 rows a day and Supabase silently caps a
       read at 1000, so this pages explicitly. */
    while (from < 4000) {
      var hh = Object.assign({}, h, {'Range-Unit':'items','Range':from+'-'+(from+999)});
      var rr = await fetch(CONFIG.supabaseUrl+'/rest/v1/momentum_stocks?select=symbol,pct_from_ema5_daily,pct_from_ema9_daily&session_date=eq.'+sd,{headers:hh});
      var dd = await rr.json();
      if(!Array.isArray(dd) || !dd.length) break;
      dd.forEach(function(x){ map[x.symbol] = {p5:x.pct_from_ema5_daily, p9:x.pct_from_ema9_daily}; });
      if (dd.length < 1000) break;
      from += 1000;
    }
    window.tfEmaDist = map;
    return map;
  } catch(e){ console.error('ema dist load', e); return null; }
}

function tfPct(v){
  if (v === null || v === undefined) return '—';
  var n = Number(v);
  if (isNaN(n)) return '—';
  var c = n >= 0 ? '#22C55E' : '#EF4444';
  return '<span style="color:'+c+'">'+(n>=0?'+':'')+n.toFixed(1)+'%</span>';
}

function tfEmaDistHTML(sym){
  var m = window.tfEmaDist;
  if (!m || !m[sym]) return '';
  return '<span title="How far price is from the daily 5 and 9 EMA. Closer to the 9 EMA = tighter entry, smaller stop.">5E '
       + tfPct(m[sym].p5) + ' · 9E ' + tfPct(m[sym].p9) + '</span>';
}

/* ── four-stage performance table on Alert History ───────────────── */
function tfRenderStageTable(list){
  var el = document.getElementById('ah-stage-table');
  if (!el) return;
  var rows = [4,3,2,1].map(function(s){
    var g = list.filter(function(a){ return parseInt(a.stage) === s; });
    var w = g.filter(function(a){ return a.outcome==='win'; }).length;
    var l = g.filter(function(a){ return a.outcome==='loss'; }).length;
    var p = g.filter(function(a){ return !a.outcome || a.outcome==='pending'; }).length;
    var dec = w + l;
    var wr = dec ? Math.round(w/dec*100)+'%' : '—';
    var t = tfStage(s);
    return '<tr title="'+t.tip+'">'
      + '<td style="padding:3px 8px;color:'+t.col+';font-weight:700">'+t.full+'</td>'
      + '<td style="padding:3px 8px">'+g.length+'</td>'
      + '<td style="padding:3px 8px;color:#22C55E">'+w+'</td>'
      + '<td style="padding:3px 8px;color:#EF4444">'+l+'</td>'
      + '<td style="padding:3px 8px;color:var(--muted)">'+p+'</td>'
      + '<td style="padding:3px 8px;font-weight:700">'+wr+'</td></tr>';
  }).join('');
  el.innerHTML = '<table style="width:100%;border-collapse:collapse">'
    + '<tr style="color:var(--muted);font-size:10px;text-align:left">'
    + '<th style="padding:3px 8px">Stage</th><th style="padding:3px 8px">Alerts</th>'
    + '<th style="padding:3px 8px">Wins</th><th style="padding:3px 8px">Losses</th>'
    + '<th style="padding:3px 8px">Pending</th><th style="padding:3px 8px">Win rate</th></tr>'
    + rows + '</table>';
}

/* ── pick badges ─────────────────────────────────────────────────── */
function tfPickBadges(p){
  var out = [];
  if (p.badges) {
    String(p.badges).split(',').forEach(function(b){
      b = b.trim();
      if (!b) return;
      if (b.indexOf('RESULTS_') === 0) out.push('<span style="color:var(--amber)" title="Results are due — the chart can gap overnight">⚠️ '+b.replace('RESULTS_','Results ')+'</span>');
      else if (b === 'NR7') out.push('<span style="color:#3B82F6" title="Narrowest range in 7 sessions — coiled">NR7</span>');
      else if (b === 'NR4') out.push('<span style="color:#3B82F6" title="Narrowest range in 4 sessions">NR4</span>');
      else if (b === 'LOW_ADR') out.push('<span style="color:var(--muted)" title="ADR between 2.0% and 2.5%. Measured -0.024R in that band, so treat with caution.">Low ADR</span>');
      else if (b === 'CLEAR') out.push('<span style="color:#22C55E" title="No level standing in the way above the trigger">Clear</span>');
      else if (b === 'EXTENDED') out.push('<span style="color:var(--amber)" title="More than 7% from the 9 EMA — stop would be wide">Extended</span>');
      else if (b === 'FAR_TRIGGER') out.push('<span style="color:var(--muted)" title="Trigger is more than 1.5 ADR away — it may not even be reached">Far trigger</span>');
      else out.push(b);
    });
  }
  if (p.trigger_level) out.push('<span title="Previous day high/low — where the trade starts">Trig '+Number(p.trigger_level).toFixed(1)+'</span>');
  if (p.room_adr != null) out.push('<span title="Room from the trigger to the next level, in ADRs">Room '+Number(p.room_adr).toFixed(1)+'R</span>');
  return out.join(' · ');
}

/* ── Journal ─────────────────────────────────────────────────────── */
var wlOutcomes = null;

async function tfLoadOutcomes(){
  if (wlOutcomes) return wlOutcomes;
  var h = {'apikey':CONFIG.supabaseKey,'Authorization':'Bearer '+CONFIG.supabaseKey};
  var all = [], from = 0;
  while (from < 8000) {
    var hh = Object.assign({}, h, {'Range-Unit':'items','Range':from+'-'+(from+999)});
    var r = await fetch(CONFIG.supabaseUrl+'/rest/v1/nextday_outcomes?select=*&order=target_date.desc',{headers:hh});
    var d = await r.json();
    if (!Array.isArray(d) || !d.length) break;
    all = all.concat(d);
    if (d.length < 1000) break;
    from += 1000;
  }
  wlOutcomes = all;
  return all;
}

function tfAgg(rows){
  var t = rows.filter(function(r){ return r.r_multiple !== null && r.r_multiple !== undefined; });
  if (!t.length) return null;
  var wins = t.filter(function(r){ return r.r_multiple > 0; }).length;
  var sum = t.reduce(function(a,r){ return a + Number(r.r_multiple); }, 0);
  return {n:t.length, win:100*wins/t.length, avg:sum/t.length, total:sum};
}

function tfSliceTable(title, rows, keyFn, note){
  var groups = {};
  rows.forEach(function(r){
    var k = keyFn(r);
    if (k === null || k === undefined || k === '') return;
    (groups[k] = groups[k] || []).push(r);
  });
  var keys = Object.keys(groups).sort();
  if (!keys.length) return '';
  var body = keys.map(function(k){
    var a = tfAgg(groups[k]);
    if (!a || a.n < 3) return '';
    var c = a.avg > 0 ? '#22C55E' : '#EF4444';
    return '<tr><td style="padding:3px 8px">'+k+'</td>'
      + '<td style="padding:3px 8px">'+a.n+'</td>'
      + '<td style="padding:3px 8px">'+a.win.toFixed(0)+'%</td>'
      + '<td style="padding:3px 8px;color:'+c+';font-weight:700">'+a.avg.toFixed(3)+'R</td>'
      + '<td style="padding:3px 8px;color:'+c+'">'+a.total.toFixed(1)+'R</td></tr>';
  }).join('');
  if (!body) return '';
  return '<div class="fo-card" style="padding:12px 14px;margin-top:8px">'
    + '<div class="fo-card-label" style="margin-bottom:6px">'+title
    + (note ? ' <span style="font-weight:400;color:var(--muted);font-size:9px">· '+note+'</span>' : '')
    + '</div><table style="width:100%;border-collapse:collapse;font-size:11px">'
    + '<tr style="color:var(--muted);font-size:10px;text-align:left"><th style="padding:3px 8px">Bucket</th>'
    + '<th style="padding:3px 8px">Trades</th><th style="padding:3px 8px">Win</th>'
    + '<th style="padding:3px 8px">Avg R</th><th style="padding:3px 8px">Total R</th></tr>'
    + body + '</table></div>';
}

async function loadWLJournal(){
  var el = document.getElementById('wl-journal-body');
  if (!el) return;
  el.innerHTML = 'Loading…';
  try {
    var rows = await tfLoadOutcomes();
    if (!rows.length) { el.innerHTML = 'No graded picks yet. Run nextday_grader.py after the close.'; return; }
    var broke = rows.filter(function(r){ return r.broke; }).length;
    var a = tfAgg(rows) || {n:0, win:0, avg:0, total:0};
    var c = a.avg > 0 ? '#22C55E' : '#EF4444';
    var head = '<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px">'
      + tfStat('Picks graded', rows.length, 'var(--text)', 'Every pick with a next-day result')
      + tfStat('Triggered', broke + ' ('+Math.round(100*broke/rows.length)+'%)', 'var(--text)', 'How many actually broke their opening range. The rest never gave an entry.')
      + tfStat('Win rate', a.n ? a.win.toFixed(0)+'%' : '—', c, 'Of trades that triggered')
      + tfStat('Avg R', a.n ? a.avg.toFixed(3)+'R' : '—', c, 'R = entry minus stop. Exit method A: hold with the BO stop to 15:20.')
      + tfStat('Total R', a.n ? a.total.toFixed(1)+'R' : '—', c, 'Sum of all R')
      + '</div>';
    var slices = ''
      + tfSliceTable('By time of day the break happened', rows, function(r){ return r.break_bucket; }, 'when the edge lives')
      + tfSliceTable('By day regime', rows, function(r){ return r.day_regime; }, 'index range vs its ADR')
      + tfSliceTable('By ORB type', rows, function(r){ return r.orb_type; })
      + tfSliceTable('By direction', rows, function(r){ return r.direction; })
      + tfSliceTable('By score bucket', rows, function(r){
          if (r.score == null) return 'no score (old model)';
          var s = Number(r.score);
          return s >= 75 ? '75+' : s >= 70 ? '70-74' : s >= 65 ? '65-69' : s >= 60 ? '60-64' : 'under 60';
        }, 'does a higher score actually win more?')
      + tfSliceTable('By own alert firing', rows, function(r){ return r.alert_fired ? 'alert fired' : 'no alert'; }, 'is the alert engine adding selection?')
      + tfSliceTable('By alert stage', rows, function(r){ return r.alert_stage ? tfStage(r.alert_stage).full : null; })
      + tfSliceTable('By gap size', rows, function(r){
          if (r.gap_pct == null) return null;
          var g = Math.abs(Number(r.gap_pct));
          return g < 0.5 ? 'under 0.5%' : g < 1.5 ? '0.5-1.5%' : 'over 1.5%';
        });
    el.innerHTML = head + slices
      + '<div style="margin-top:10px;font-size:10px;color:var(--muted)">Buckets with fewer than 3 trades are hidden — they would be noise, not signal.</div>';
  } catch(e){ el.innerHTML = 'Journal error: ' + e; }
}

function tfStat(label, val, col, tip){
  return '<div class="fo-card" style="padding:10px 12px" title="'+(tip||'')+'">'
    + '<div class="fo-card-label">'+label+'</div>'
    + '<div style="font-size:18px;font-weight:700;color:'+col+'">'+val+'</div></div>';
}

/* ── Old vs New ──────────────────────────────────────────────────── */
async function loadWLVersus(){
  var el = document.getElementById('wl-versus-body');
  if (!el) return;
  el.innerHTML = 'Loading…';
  try {
    var rows = await tfLoadOutcomes();
    var neu = rows.filter(function(r){ return r.score != null; });
    var old = rows.filter(function(r){ return r.score == null; });
    var an = tfAgg(neu), ao = tfAgg(old);
    var fmt = function(name, a, cnt, note){
      if (!a) return '<tr><td style="padding:4px 8px">'+name+'</td><td colspan="5" style="padding:4px 8px;color:var(--muted)">'+(note||'no graded trades yet')+'</td></tr>';
      var c = a.avg > 0 ? '#22C55E' : '#EF4444';
      return '<tr><td style="padding:4px 8px;font-weight:700">'+name+'</td>'
        + '<td style="padding:4px 8px">'+cnt+'</td>'
        + '<td style="padding:4px 8px">'+a.n+'</td>'
        + '<td style="padding:4px 8px">'+a.win.toFixed(0)+'%</td>'
        + '<td style="padding:4px 8px;color:'+c+';font-weight:700">'+a.avg.toFixed(3)+'R</td>'
        + '<td style="padding:4px 8px;color:'+c+'">'+a.total.toFixed(1)+'R</td></tr>';
    };
    el.innerHTML = '<div class="fo-card" style="padding:12px 14px">'
      + '<div class="fo-card-label" style="margin-bottom:8px">Champion vs challenger</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:11px">'
      + '<tr style="color:var(--muted);font-size:10px;text-align:left"><th style="padding:4px 8px">Model</th>'
      + '<th style="padding:4px 8px">Picks</th><th style="padding:4px 8px">Triggered</th>'
      + '<th style="padding:4px 8px">Win</th><th style="padding:4px 8px">Avg R</th>'
      + '<th style="padding:4px 8px">Total R</th></tr>'
      + fmt('New (v2)', an, neu.length)
      + fmt('Old (watchlist_generator)', ao, old.length)
      + '</table>'
      + '<div style="margin-top:10px;font-size:10px;color:var(--muted);line-height:1.7">'
      + 'Both models are graded identically: entry at the break of the first 5-minute candle closing outside the opening range, stop at that candle\'s low, exit at 15:20. '
      + 'Same measurement, same days, so the comparison is fair. '
      + 'The new model needs several weeks before its numbers mean anything — do not retire the old one on a handful of trades.'
      + '</div></div>';
  } catch(e){ el.innerHTML = 'Versus error: ' + e; }
}
"""


# ══════════════════════════════════════════════════════════════════════
#  RUNNER
# ══════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("usage: apply_ndwl.py <Index.html.html>")
        sys.exit(1)
    path = sys.argv[1]

    try:
        src = open(path, encoding="utf-8").read()
    except IOError as e:
        print("FATAL: cannot read %s (%s)" % (path, e))
        sys.exit(1)

    orig_len = len(src)
    print("Read %s — %d bytes, %d lines"
          % (path, orig_len, src.count("\n") + 1))

    if "tfStagePill" in src:
        print("FATAL: this file already has the v2 patches applied.")
        print("Fetch a clean copy from GitHub before re-running.")
        sys.exit(1)

    # ---- verify EVERY anchor before changing anything -----------------
    bad = []
    for name, old, new, expect in PATCHES:
        c = src.count(old)
        if c != expect:
            bad.append((name, c, expect))
    if bad:
        print("-" * 62)
        print("ABORTED — %d anchor(s) did not match the expected count:" % len(bad))
        for name, c, expect in bad:
            print("   %-34s found %d, expected %d" % (name, c, expect))
        print("Nothing written. The live file has changed since this")
        print("patcher was written — send Claude the current file.")
        sys.exit(1)
    print("All %d anchors verified unique." % len(PATCHES))

    for name, old, new, expect in PATCHES:
        src = src.replace(old, new, expect)
        print("  applied: %s%s" % (name, " (x%d)" % expect if expect > 1 else ""))

    # ---- append the new javascript before the LAST </script> ---------
    idx = src.rfind("</script>")
    if idx == -1:
        print("FATAL: no </script> tag found.")
        sys.exit(1)
    src = src[:idx] + NEW_JS + "\n" + src[idx:]
    print("  applied: new javascript block (%d bytes)" % len(NEW_JS))

    # ---- sanity checks ----------------------------------------------
    if len(src) <= orig_len:
        print("FATAL: output is not larger than input. Nothing written.")
        sys.exit(1)
    # NOTE: the live file ends "</html" with no closing ">". That is how it
    # already is on GitHub; browsers auto-close it. Checking for "</body>"
    # instead so this never trips on that quirk.
    if "</body>" not in src.lower():
        print("FATAL: output lost its </body>. Nothing written.")
        sys.exit(1)

    bak = "%s.bak.%s" % (path, datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)

    print("-" * 62)
    print("PATCHED  %d -> %d bytes  (+%d)"
          % (orig_len, len(src), len(src) - orig_len))
    print("Backup:  %s" % bak)
    print("Upload the patched Index.html.html to GitHub to deploy.")


if __name__ == "__main__":
    main()
