"""Read-only web dashboard for Wallee.

Serves a single-page dashboard showing whiteboard state, temperature charts,
print status, camera feeds, and safety status. Updates via websocket.

No controls — all control goes through CLI or Telegram.
Uses only stdlib + redis. No Flask, no socket.io.

Note: This dashboard only displays data from the Wallee whiteboard (trusted
internal source). No user input is rendered as HTML. All dynamic content
is set via textContent or safe DOM APIs to prevent XSS.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler

logger = logging.getLogger(__name__)


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wallee Dashboard</title>
<style>
:root { --bg: #0d1117; --card: rgba(22,27,34,0.9); --border: #30363d; --text: #e6edf3;
        --muted: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922;
        --blue: #58a6ff; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono', 'Fira Code', monospace; background: var(--bg);
       color: var(--text); font-size: 12px; height: 100vh; overflow: hidden; }
.shell { display: grid; grid-template-rows: auto auto 1fr auto; height: 100vh; padding: 10px; gap: 8px; }
h2 { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px; overflow: hidden; }
table { width: 100%; border-collapse: collapse; }
td { padding: 2px 6px; border-bottom: 1px solid rgba(48,54,61,0.5); vertical-align: top; font-size: 11px; }
td:first-child { color: var(--muted); white-space: nowrap; }
.dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.dot.green { background: var(--green); } .dot.red { background: var(--red); animation: pulse 1s infinite; } .dot.yellow { background: var(--yellow); }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
.indicator { display: inline-flex; align-items: center; gap: 5px; padding: 3px 8px; border-radius: 999px;
             background: rgba(13,17,23,0.55); border: 1px solid var(--border); font-size: 11px; }
.progress-bar { height: 6px; background: rgba(48,54,61,0.9); border-radius: 999px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, var(--green), #5ee37b); border-radius: 999px; transition: width 0.5s; }
canvas { width: 100%; height: 90px; }
img.cam { display: block; width: 100%; height: 160px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border); }
.cam-shell { position: relative; }
.cam-empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
             color: var(--muted); font-size: 11px; text-transform: uppercase; border-radius: 6px; background: rgba(13,17,23,0.7); }
.cam-status { position: absolute; top: 6px; right: 6px; font-size: 9px; font-weight: 700;
              text-transform: uppercase; padding: 2px 6px; border-radius: 999px;
              border: 1px solid var(--border); background: rgba(13,17,23,0.7); color: var(--muted); }
.cam-status.live { color: var(--green); } .cam-status.stale { color: var(--yellow); } .cam-status.offline { color: var(--red); }
/* Hero row */
.hero { display: flex; gap: 10px; align-items: stretch; }
.hero-card { flex: 1; padding: 10px 14px; border-radius: 8px; background: rgba(13,17,23,0.52);
             border: 1px solid rgba(88,166,255,0.12); }
.hero-label { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 1px; }
.hero-value { font-size: 20px; font-weight: 700; margin-top: 4px; }
.hero-meta { font-size: 11px; color: var(--muted); margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* Key metrics row */
.metrics-row { display: flex; gap: 6px; flex-wrap: wrap; padding: 6px 10px; background: var(--card);
               border: 1px solid var(--border); border-radius: 8px; }
.metric { display: flex; gap: 4px; align-items: baseline; padding: 2px 8px; font-size: 11px; }
.metric-label { color: var(--muted); }
.metric-value { font-weight: 700; }
.metric-adjusted { color: var(--yellow); }
/* Main grid */
.main { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; min-height: 0; overflow: hidden; }
.col { display: flex; flex-direction: column; gap: 8px; min-height: 0; overflow: hidden; }
.feed { flex: 1; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--border) transparent; min-height: 0; }
.feed-entry { padding: 4px 8px; border-left: 3px solid transparent; font-size: 11px; }
.feed-entry:not(:last-child) { border-bottom: 1px solid rgba(48,54,61,0.4); }
.feed-entry.active { border-left-color: var(--blue); }
.feed-entry.stale { opacity: 0.55; }
.feed-head { display: flex; align-items: center; gap: 6px; }
.feed-ts { color: var(--muted); font-size: 10px; min-width: 50px; font-variant-numeric: tabular-nums; }
.feed-badge { font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 600; text-transform: uppercase; }
.feed-badge.wait { background: #21262d; color: var(--muted); }
.feed-badge.action { background: rgba(88,166,255,0.15); color: var(--blue); }
.feed-badge.done { background: rgba(63,185,80,0.18); color: var(--green); }
.feed-badge.failed, .feed-badge.rejected { background: rgba(248,81,73,0.18); color: var(--red); }
.feed-badge.call-human { background: rgba(248,81,73,0.15); color: var(--red); }
.feed-text { font-size: 11px; color: var(--text); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.feed-empty { color: var(--muted); padding: 8px 0; font-style: italic; }
/* Bottom bar */
.bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.urgent-flag { display: inline-block; background: var(--red); color: #fff; font-size: 9px;
               font-weight: 700; padding: 1px 6px; border-radius: 3px; animation: pulse 1s infinite; }
@media (max-width: 1200px) { .main { grid-template-columns: 1fr 1fr; } }
@media (max-width: 800px) { .main { grid-template-columns: 1fr; } .hero { flex-wrap: wrap; } .bottom { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="shell">
  <!-- HEADER -->
  <div style="display:flex;justify-content:space-between;align-items:center;padding:0 4px;">
    <div style="display:flex;align-items:center;gap:12px;">
      <span style="font-size:20px;font-weight:700;color:var(--blue);letter-spacing:1px;">WALLEE</span>
      <span id="conn" class="indicator"></span>
      <span id="safety" class="indicator"></span>
    </div>
    <span id="clock" style="color:var(--muted);font-size:11px;"></span>
  </div>

  <!-- HERO ROW: Phase, Progress, Agent, Vision -->
  <div class="hero">
    <div class="hero-card">
      <div class="hero-label">Phase</div>
      <div id="summary-state" class="hero-value">--</div>
      <div id="summary-state-meta" class="hero-meta"></div>
    </div>
    <div class="hero-card">
      <div class="hero-label">Progress</div>
      <div id="summary-progress" class="hero-value">--</div>
      <div class="progress-bar" style="margin-top:6px;"><div id="progress-fill" class="progress-fill" style="width:0%"></div></div>
      <div id="summary-progress-meta" class="hero-meta"></div>
    </div>
    <div class="hero-card">
      <div class="hero-label">Agent</div>
      <div id="summary-agent" class="hero-value">--</div>
      <div id="summary-agent-meta" class="hero-meta"></div>
    </div>
    <div class="hero-card">
      <div class="hero-label">Vision</div>
      <div id="summary-vision" class="hero-value">--</div>
      <div id="summary-vision-meta" class="hero-meta"></div>
    </div>
  </div>

  <!-- MAIN 3-COLUMN GRID -->
  <div class="main">
    <!-- LEFT: Cameras + Key Metrics -->
    <div class="col">
      <div class="card">
        <h2>Key Metrics</h2>
        <table id="key-metrics"></table>
      </div>
      <div class="card" style="flex:1;">
        <h2>Cameras</h2>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">
          <div class="cam-shell"><img id="cam-nozzle" class="cam" alt=""><div id="cam-nozzle-empty" class="cam-empty">Nozzle offline</div><span id="cam-nozzle-status" class="cam-status offline">offline</span></div>
          <div class="cam-shell"><img id="cam-buddy1" class="cam" alt=""><div id="cam-buddy1-empty" class="cam-empty">Buddy offline</div><span id="cam-buddy1-status" class="cam-status offline">offline</span></div>
        </div>
        <div id="cam-buddy2-wrap" style="margin-top:6px;display:none;">
          <div class="cam-shell"><img id="cam-buddy2" class="cam" alt=""><div id="cam-buddy2-empty" class="cam-empty">Buddy 2 offline</div><span id="cam-buddy2-status" class="cam-status offline">offline</span></div>
        </div>
        <canvas id="temp-chart" style="margin-top:6px;"></canvas>
      </div>
    </div>

    <!-- CENTER: Agent Log -->
    <div class="col">
      <div class="card" style="flex:1;display:flex;flex-direction:column;">
        <h2>Agent Log</h2>
        <div id="agent-log" class="feed"></div>
      </div>
    </div>

    <!-- RIGHT: Vision + Human + Adjustments -->
    <div class="col">
      <div class="card">
        <h2>Vision Analysis</h2>
        <div id="vision-panel"></div>
      </div>
      <div class="card">
        <h2>Human</h2>
        <div id="intent"></div>
      </div>
      <div class="card" style="flex:1;display:flex;flex-direction:column;">
        <h2>Adjustments</h2>
        <div id="adjustments" class="feed"></div>
      </div>
    </div>
  </div>

  <!-- BOTTOM: All Keys (collapsible) -->
  <details style="margin-top:4px;">
    <summary style="color:var(--muted);font-size:11px;cursor:pointer;padding:4px;">All Whiteboard Keys</summary>
    <div class="card" style="margin-top:4px;max-height:200px;overflow-y:auto;"><table id="all-keys"></table></div>
  </details>
</div>

<script>
"use strict";
let ws;
const tempHistory = {nozzle:[], bed:[], chamber:[], heatbreak:[]};
const MAX_HISTORY = 60;
var lastActivityLog = null;
var lastRecentActions = null;

function clearEl(el) { while (el.firstChild) el.removeChild(el.firstChild); }
function makeRow(label, value) {
  var tr = document.createElement('tr');
  var td1 = document.createElement('td'); td1.textContent = label;
  var td2 = document.createElement('td'); td2.textContent = String(value);
  tr.appendChild(td1); tr.appendChild(td2); return tr;
}
function setIndicator(el, dotClass, text) {
  clearEl(el); var dot = document.createElement('span'); dot.className = 'dot ' + dotClass;
  el.appendChild(dot); el.appendChild(document.createTextNode(' ' + text));
}
function setStat(id, value, meta) {
  var v = document.getElementById(id), m = document.getElementById(id + '-meta');
  if (v) v.textContent = value; if (m) m.textContent = meta || '';
}

function connect() {
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.hostname + ':WS_PORT');
  ws.onopen = function() { setIndicator(document.getElementById('conn'), 'green', 'live'); };
  ws.onclose = function() { setIndicator(document.getElementById('conn'), 'red', 'disconnected'); setTimeout(connect, 3000); };
  ws.onmessage = function(e) { try { update(JSON.parse(e.data)); } catch(err) { console.error(err); } };
}

function update(s) {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
  updateSummary(s); updateKeyMetrics(s); updateVision(s);
  updateCameras(s); updateTempChart(s); updateAgentLog(s);
  updateIntent(s); updateAdjustments(s); updateSafety(s); updateAllKeys(s);
}

/* --- HERO ROW --- */
function updateSummary(s) {
  var phase = s['job.phase'] || s['printer.state'] || 'UNKNOWN';
  var detail = s['job.phase_detail'] || '';
  setStat('summary-state', phase, detail);

  var progress = s['printer.job_progress'];
  var remaining = s['printer.job_time_remaining_s'];
  var pv = progress != null ? Math.round(progress) + '%' : '--';
  var pm = remaining != null ? Math.round(remaining / 60) + 'm left' : (s['printer.print_filename'] || '');
  setStat('summary-progress', pv, pm);
  var fill = document.getElementById('progress-fill');
  if (fill) fill.style.width = (progress != null ? Math.round(progress) : 0) + '%';

  var dec = s['agent.last_decision'] || '';
  var dl = dec.split(':')[0] || '--'; if (dl.length > 14) dl = 'Decision';
  var dm = dec.indexOf(': ') > 0 ? dec.substring(dec.indexOf(': ') + 2) : dec;
  setStat('summary-agent', dl.toUpperCase(), dm);

  /* Vision hero */
  var vs = s['vision.status'] || 'NO_DATA';
  var vc = s['vision.confidence'] || '';
  var vd = s['vision.description'] || '';
  var vts = s['vision.last_analysis_ts'];
  var vage = vts ? Math.round(Date.now()/1000 - parseFloat(vts)) : 999;
  if (vage > 30) { vs = 'OFFLINE'; vd = 'No vision data'; }
  setStat('summary-vision', vs + (vc ? ' (' + vc + ')' : ''), vd);
  var vel = document.getElementById('summary-vision');
  if (vel) {
    vel.style.color = vs.startsWith('DEFECT') ? 'var(--red)' :
                      vs.startsWith('POSSIBLE') ? 'var(--yellow)' :
                      vs === 'OFFLINE' ? 'var(--red)' : 'var(--text)';
  }
}

/* --- KEY METRICS --- */
function updateKeyMetrics(s) {
  var el = document.getElementById('key-metrics'); clearEl(el);
  var items = [
    ['Nozzle', s['printer.temp_nozzle'], s['printer.target_nozzle'], '\u00B0C'],
    ['Bed', s['printer.temp_bed'], s['printer.target_bed'], '\u00B0C'],
    ['Heatbreak', s['printer.temp_heatbreak'], null, '\u00B0C'],
    ['Speed', s['printer.speed'], 100, '%'],
    ['Flow', s['printer.flow'], 100, '%'],
    ['Fan HB', s['printer.fan_heatbreak_rpm'], null, 'rpm'],
    ['Fan Print', s['printer.fan_print_rpm'], null, 'rpm'],
    ['Nozzle A', s['printer.curr_nozzle'], null, 'A'],
    ['Bed V', s['printer.volt_bed'], null, 'V'],
  ];
  for (var i = 0; i < items.length; i++) {
    var name = items[i][0], val = items[i][1], ref = items[i][2], unit = items[i][3];
    if (val == null) continue;
    var display = val + (ref != null ? '/' + ref : '') + unit;
    var row = makeRow(name, display);
    /* Highlight adjusted values (speed/flow differ from 100) */
    if (ref != null && val !== ref) row.lastChild.style.cssText = 'color:var(--yellow);font-weight:700';
    el.appendChild(row);
  }
}

/* --- VISION ANALYSIS --- */
function updateVision(s) {
  var el = document.getElementById('vision-panel'); clearEl(el);
  var tbl = document.createElement('table');
  var vts = s['vision.last_analysis_ts'];
  var age = vts ? Math.round(Date.now()/1000 - parseFloat(vts)) : null;
  if (!vts || age > 30) {
    var r = makeRow('Status', 'VISION OFFLINE');
    r.lastChild.style.cssText = 'color:var(--red);font-weight:700';
    tbl.appendChild(r); el.appendChild(tbl); return;
  }
  var status = s['vision.status'] || 'NO_DATA';
  var sr = makeRow('Status', status);
  if (status.startsWith('DEFECT')) sr.lastChild.style.cssText = 'color:var(--red);font-weight:700';
  else if (status.startsWith('POSSIBLE') || status.startsWith('INCONSISTENT')) sr.lastChild.style.cssText = 'color:var(--yellow);font-weight:700';
  tbl.appendChild(sr);
  var conf = s['vision.confidence']; if (conf != null) tbl.appendChild(makeRow('Conf', conf));
  var desc = s['vision.description']; if (desc) tbl.appendChild(makeRow('Desc', desc));
  var defects = ['stringing','spaghetti','blob','warping','layer_shift','underextrusion','overextrusion','burn_marks','bed_adhesion_ok','normal'];
  for (var i = 0; i < defects.length; i++) {
    var k = defects[i], v = s['vision.nozzle.'+k] || s['vision.'+k]; if (v == null) continue;
    var dr = makeRow(k.replace(/_/g,' '), v);
    if (v > 0.7 && k !== 'normal' && k !== 'bed_adhesion_ok') dr.lastChild.style.cssText = 'color:var(--red);font-weight:700';
    else if (v > 0.4 && k !== 'normal' && k !== 'bed_adhesion_ok') dr.lastChild.style.cssText = 'color:var(--yellow)';
    tbl.appendChild(dr);
  }
  var bs = s['vision.buddy.status']; if (bs) { var br = makeRow('Buddy', bs); if (bs.startsWith('DEFECT')) br.lastChild.style.cssText='color:var(--red);font-weight:700'; tbl.appendChild(br); }
  tbl.appendChild(makeRow('Updated', age + 's ago'));
  el.appendChild(tbl);
}

/* --- CAMERAS --- */
function updateCamCard(imgId, statusKey, frameKey, s) {
  var img = document.getElementById(imgId), empty = document.getElementById(imgId+'-empty'), badge = document.getElementById(imgId+'-status');
  var status = s[statusKey] || 'offline';
  if (badge) { badge.className = 'cam-status ' + status; badge.textContent = status; }
  if (status === 'live' && s[frameKey]) { img.src = 'data:image/jpeg;base64,' + s[frameKey]; img.alt = ''; if (empty) empty.style.display = 'none'; }
  else { img.removeAttribute('src'); if (empty) { empty.style.display = 'flex'; empty.textContent = status === 'stale' ? 'Stale' : 'Offline'; } }
}
function updateCameras(s) {
  updateCamCard('cam-nozzle','camera.nozzle_status','camera.nozzle_frame',s);
  updateCamCard('cam-buddy1','camera.buddy1_status','camera.buddy1_frame',s);
  var b2s = s['camera.buddy2_status'];
  var b2wrap = document.getElementById('cam-buddy2-wrap');
  if (b2s && b2s !== 'offline') { if (b2wrap) b2wrap.style.display = 'block'; updateCamCard('cam-buddy2','camera.buddy2_status','camera.buddy2_frame',s); }
  else { if (b2wrap) b2wrap.style.display = 'none'; }
}

/* --- TEMP CHART --- */
function pushHistory(key, val) { tempHistory[key].push(val); if (tempHistory[key].length > MAX_HISTORY) tempHistory[key].shift(); }
function updateTempChart(s) {
  if (s['printer.temp_nozzle'] != null) pushHistory('nozzle', s['printer.temp_nozzle']);
  if (s['printer.temp_bed'] != null) pushHistory('bed', s['printer.temp_bed']);
  if (s['printer.temp_chamber'] != null) pushHistory('chamber', s['printer.temp_chamber']);
  if (s['printer.temp_heatbreak'] != null) pushHistory('heatbreak', s['printer.temp_heatbreak']);
  var canvas = document.getElementById('temp-chart'); if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var W = canvas.offsetWidth * 2, H = 180; canvas.width = W; canvas.height = H;
  ctx.clearRect(0,0,W,H);
  var colors = {nozzle:'#f85149',bed:'#d29922',chamber:'#58a6ff',heatbreak:'#8b949e'};
  var PL=40,PR=6,PT=6,PB=20; var pW=W-PL-PR, pH=H-PT-PB;
  var mx=50; for (var k in tempHistory) for (var i=0;i<tempHistory[k].length;i++) mx=Math.max(mx,tempHistory[k][i]);
  mx=Math.ceil(mx/10)*10+10;
  ctx.strokeStyle='#30363d';ctx.lineWidth=1;ctx.fillStyle='#8b949e';ctx.font='14px monospace';ctx.textAlign='right';ctx.textBaseline='middle';
  for (var t=0;t<=mx;t+=Math.max(10,Math.round(mx/4/10)*10)) { var y=PT+pH-(t/mx)*pH; ctx.beginPath();ctx.moveTo(PL,y);ctx.lineTo(W-PR,y);ctx.stroke();ctx.fillText(t+'\u00B0',PL-4,y); }
  for (var key in tempHistory) { var d=tempHistory[key]; if(d.length<2) continue; ctx.beginPath();ctx.strokeStyle=colors[key];ctx.lineWidth=2;
    for(var j=0;j<d.length;j++){var x=PL+(j/(MAX_HISTORY-1))*pW,y=PT+pH-(d[j]/mx)*pH;if(j===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}ctx.stroke();}
  var lx=PL+4,ly=PT+2; ctx.font='12px monospace';ctx.textAlign='left';
  for(var key in colors){if(tempHistory[key].length<1)continue;ctx.fillStyle=colors[key];ctx.fillRect(lx,ly,10,7);ctx.fillStyle='#e6edf3';ctx.fillText(key,lx+14,ly+7);lx+=ctx.measureText(key).width+24;}
}

/* --- AGENT LOG --- */
function parseLogEntries(raw) {
  var entries = [];
  if (Array.isArray(raw)) {
    for (var i = 0; i < raw.length; i++) {
      try { entries.push(typeof raw[i] === 'string' ? JSON.parse(raw[i]) : raw[i]); } catch (e) {}
    }
  }
  return entries;
}

function entrySortValue(e) {
  if (typeof e.ts_epoch === 'number') return e.ts_epoch;
  var ts = e.ts || '';
  if (/^\d{2}:\d{2}:\d{2}$/.test(ts)) {
    var parts = ts.split(':');
    return parseInt(parts[0], 10) * 3600 + parseInt(parts[1], 10) * 60 + parseInt(parts[2], 10);
  }
  return 0;
}

function badgeClassForEntry(e) {
  var status = String(e.status || e.type || '').toUpperCase();
  if (status === 'DONE') return 'done';
  if (status === 'FAILED') return 'failed';
  if (status === 'REJECTED') return 'rejected';
  if (status === 'CALL_HUMAN') return 'call-human';
  if (status === 'WAIT') return 'wait';
  return 'action';
}

function badgeLabelForEntry(e) {
  var status = String(e.status || e.type || '').toUpperCase();
  return status || 'ACTION';
}

function updateAgentLog(s) {
  var el = document.getElementById('agent-log');
  var logData = s['agent.activity_log'];
  var recentActions = s['agent.recent_actions'];
  var logKey = JSON.stringify(logData);
  var recentKey = JSON.stringify(recentActions);
  if (logKey === lastActivityLog && recentKey === lastRecentActions) return;
  lastActivityLog = logKey;
  lastRecentActions = recentKey;
  clearEl(el);
  var entries = parseLogEntries(logData);
  entries = entries.concat(parseLogEntries(recentActions));
  entries.sort(function(a, b) { return entrySortValue(b) - entrySortValue(a); });
  entries = entries.slice(0, 20);
  if (entries.length === 0) { var last = s['agent.last_decision']; if (last) entries.push({ts:'now',status:'WAIT',text:last}); }
  if (entries.length === 0) { var em=document.createElement('div');em.className='feed-empty';em.textContent='Waiting for agent...';el.appendChild(em);return; }
  for (var i=0;i<entries.length;i++) {
    var e=entries[i], row=document.createElement('div'); row.className='feed-entry'+(i===0?' active':' stale');
    var head=document.createElement('div');head.className='feed-head';
    var ts=document.createElement('span');ts.className='feed-ts';ts.textContent=e.ts||'';head.appendChild(ts);
    var badge=document.createElement('span');badge.className='feed-badge';
    badge.className+=' ' + badgeClassForEntry(e);
    badge.textContent=badgeLabelForEntry(e).replace('_',' ');head.appendChild(badge);row.appendChild(head);
    var text=document.createElement('div');text.className='feed-text';
    var ft=e.text||'';if(ft.indexOf(': ')>0&&ft.indexOf(': ')<30)ft=ft.substring(ft.indexOf(': ')+2);
    text.textContent=ft;row.appendChild(text);el.appendChild(row);
  }
}

/* --- HUMAN --- */
function updateIntent(s) {
  var el = document.getElementById('intent'); clearEl(el);
  var intent = s['human.intent'], urgent = s['human.urgent'], pending = s['human.pending_callout'];
  var d = document.createElement('div');
  if (intent) { d.style.cssText='font-size:12px;color:var(--text);'; d.textContent = (urgent?'URGENT: ':'')+intent; }
  else { d.style.cssText='font-size:11px;color:var(--muted);'; d.textContent = 'No active intent'; }
  el.appendChild(d);
  if (pending) {
    try { var pd = typeof pending === 'string' ? JSON.parse(pending) : pending;
      var ps = document.createElement('div'); ps.style.cssText='margin-top:6px;font-size:11px;';
      ps.textContent = 'Pending: ' + (pd.status||'?') + ' — ' + (pd.message||'').substring(0,60);
      if (pd.status === 'PENDING') ps.style.color = 'var(--yellow)';
      el.appendChild(ps);
    } catch(e){}
  }
}

/* --- ADJUSTMENTS (track agent changes) --- */
function updateAdjustments(s) {
  var el = document.getElementById('adjustments'); clearEl(el);
  var logData = s['agent.activity_log']; if (!Array.isArray(logData)) { var em=document.createElement('div');em.className='feed-empty';em.textContent='No adjustments yet';el.appendChild(em);return; }
  var found = 0;
  for (var i=0;i<logData.length;i++) {
    try { var e = typeof logData[i]==='string'?JSON.parse(logData[i]):logData[i];
      var changes = Array.isArray(e.changes) ? e.changes : [];
      if (changes.length === 0) continue;
      for (var j=0;j<changes.length;j++) {
        var change = changes[j];
        var row=document.createElement('div');row.className='feed-entry'+(found===0?' active':' stale');
        var head=document.createElement('div');head.className='feed-head';
        var ts=document.createElement('span');ts.className='feed-ts';ts.textContent=e.ts||'';head.appendChild(ts);
        var badge=document.createElement('span');badge.className='feed-badge action';badge.textContent='ADJ';head.appendChild(badge);
        row.appendChild(head);
        var text=document.createElement('div');text.className='feed-text';
        var unit = change.unit || '';
        var fromVal = change.from == null ? '?' : change.from;
        var toVal = change.to == null ? '?' : change.to;
        text.textContent = (change.label || change.tool || 'Adjustment') + ': ' + fromVal + unit + ' -> ' + toVal + unit;
        row.appendChild(text);
        el.appendChild(row);
        found++;
      }
    } catch(e){}
  }
  if (found===0) { var em=document.createElement('div');em.className='feed-empty';em.textContent='No adjustments this session';el.appendChild(em); }
}

/* --- SAFETY --- */
function updateSafety(s) {
  var el = document.getElementById('safety');
  if (s['safety.estop']) setIndicator(el,'red','ESTOP');
  else if (s['printer.oc_nozzle'] && s['printer.oc_nozzle']!==0) setIndicator(el,'red','OC');
  else if (s['printer.oc_input'] && s['printer.oc_input']!==0) setIndicator(el,'red','OC');
  else setIndicator(el,'green','safe');
}

/* --- ALL KEYS --- */
function formatValue(key, v) {
  if (v==null) return '-'; if (typeof v==='string'&&v.length>200) return '['+v.length+' chars]';
  if (Array.isArray(v)) return v.length+' items'; if (typeof v==='object') return JSON.stringify(v).substring(0,60);
  if (typeof v==='boolean') return v?'true':'false'; return String(v);
}
function updateAllKeys(s) {
  var el = document.getElementById('all-keys'); clearEl(el);
  var keys = Object.keys(s).sort();
  for (var i=0;i<keys.length;i++) el.appendChild(makeRow(keys[i], formatValue(keys[i], s[keys[i]])));
}

setIndicator(document.getElementById('conn'), 'yellow', 'connecting...');
connect();
</script>
</body>
</html>"""


class DashboardServer:
    """Read-only web dashboard with websocket live updates."""

    def __init__(self, whiteboard, host: str = "0.0.0.0", port: int = 8080):
        self.wb = whiteboard
        self.host = host
        self.port = port
        self._ws_port = port + 1
        self._clients: set = set()
        self._thread: threading.Thread | None = None
        self._http_thread: threading.Thread | None = None
        self._http_server: ReusableHTTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._ledger_path = os.path.join(os.environ.get("WALLEE_DATA_DIR", "/var/lib/wallee"), "ledger.db")

    def _recent_action_entries(self, limit: int = 10) -> list[dict]:
        """Read recent ledger actions for status-aware dashboard rendering."""
        if not os.path.exists(self._ledger_path):
            return []
        try:
            con = sqlite3.connect(self._ledger_path)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """SELECT status, tool, params_json, error_json, result_json, updated_ts
                   FROM actions
                   ORDER BY updated_ts DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            con.close()
        except Exception:
            return []

        entries = []
        for row in rows:
            status = str(row["status"] or "").upper()
            if status not in {"DONE", "FAILED", "REJECTED"}:
                continue
            params = row["params_json"] or "{}"
            detail = row["result_json"] if status == "DONE" else row["error_json"]
            text = f"{row['tool']}({params})"
            if detail:
                text += f" — {detail}"
            ts_epoch = float(row["updated_ts"] or 0)
            entries.append({
                "ts": time.strftime("%H:%M:%S", time.localtime(ts_epoch)) if ts_epoch else "",
                "ts_epoch": ts_epoch,
                "status": status,
                "text": text[:300],
            })
        return entries

    def start(self):
        """Start dashboard in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="dashboard")
        self._thread.start()
        logger.info(f"Dashboard starting on http://{self.host}:{self.port}")

    def _run(self):
        """Run HTTP server + websocket updater."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed — dashboard will serve static page only")
            self._run_http_only()
            return

        async def ws_handler(websocket):
            self._clients.add(websocket)
            try:
                async for _ in websocket:
                    pass
            finally:
                self._clients.discard(websocket)

        async def main():
            ws_server = await websockets.serve(ws_handler, self.host, self._ws_port)

            self._http_thread = threading.Thread(target=self._run_http, daemon=True, name="dashboard-http")
            self._http_thread.start()

            while self._running:
                try:
                    state = self.wb.read_all()
                    # Include Redis list data (not string keys — skipped by read_all)
                    for list_key in ("agent.activity_log", "human.intent_log"):
                        try:
                            raw = self.wb.r.lrange(list_key, 0, 19)
                            if raw:
                                state[list_key] = list(raw)
                        except Exception:
                            pass
                    recent_actions = self._recent_action_entries()
                    if recent_actions:
                        state["agent.recent_actions"] = recent_actions
                    payload = json.dumps(state, default=str)
                    dead = set()
                    for client in list(self._clients):
                        try:
                            await client.send(payload)
                        except Exception:
                            dead.add(client)
                    self._clients -= dead
                except Exception as e:
                    logger.error(f"Dashboard push error: {e}")
                await asyncio.sleep(1)

            ws_server.close()
            await ws_server.wait_closed()

        try:
            loop.run_until_complete(main())
        finally:
            self._loop = None
            loop.close()

    def _run_http(self):
        """Simple HTTP server for the dashboard page."""
        html = DASHBOARD_HTML.replace("WS_PORT", str(self._ws_port))

        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())

            def log_message(self, format, *args):
                pass

        server = ReusableHTTPServer((self.host, self.port), Handler)
        server.timeout = 0.5
        self._http_server = server
        logger.info(f"Dashboard HTTP on port {self.port}, WebSocket on port {self._ws_port}")
        try:
            while outer._running:
                try:
                    server.handle_request()
                except OSError:
                    if not outer._running:
                        break
                    raise
        finally:
            self._http_server = None
            try:
                server.server_close()
            except Exception:
                pass

    def _run_http_only(self):
        """Fallback HTTP-only server (no live updates)."""
        html = DASHBOARD_HTML

        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode())

            def log_message(self, format, *args):
                pass

        server = ReusableHTTPServer((self.host, self.port), Handler)
        server.timeout = 0.5
        self._http_server = server
        logger.info(f"Dashboard HTTP-only on port {self.port}")
        try:
            while outer._running:
                try:
                    server.handle_request()
                except OSError:
                    if not outer._running:
                        break
                    raise
        finally:
            self._http_server = None
            try:
                server.server_close()
            except Exception:
                pass

    def stop(self):
        self._running = False
        if self._http_server is not None:
            try:
                self._http_server.server_close()
            except Exception:
                pass
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(lambda: None)
            except Exception:
                pass
        if self._http_thread and self._http_thread.is_alive() and self._http_thread is not threading.current_thread():
            self._http_thread.join(timeout=2)
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
