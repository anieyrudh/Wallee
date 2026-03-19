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
        --blue: #58a6ff; --shadow: 0 18px 60px rgba(0,0,0,0.32); --card-glow: rgba(88,166,255,0.12); }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono', 'Fira Code', monospace; background:
       radial-gradient(circle at top, rgba(88,166,255,0.08), transparent 28%),
       linear-gradient(180deg, #0b1016 0%, var(--bg) 35%);
       color: var(--text); font-size: 13px; padding: 20px; }
.shell { max-width: 1760px; margin: 0 auto; }
h1 { font-size: 28px; margin-bottom: 6px; color: var(--blue); letter-spacing: 1px; }
h2 { font-size: 14px; margin-bottom: 10px; color: var(--muted); text-transform: uppercase;
     letter-spacing: 1px; }
.hero { margin-bottom: 16px; padding: 16px 18px; border-radius: 14px;
        border: 1px solid rgba(88,166,255,0.16); background:
        linear-gradient(135deg, rgba(88,166,255,0.12), rgba(22,27,34,0.94) 55%);
        box-shadow: var(--shadow); }
.subtitle { color: var(--muted); font-size: 12px; margin-top: 2px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
        padding: 14px; box-shadow: 0 10px 28px rgba(0,0,0,0.18); backdrop-filter: blur(8px); }
.card:hover { border-color: rgba(88,166,255,0.28); }
.card-wide { grid-column: 1 / -1; }
.status-bar { display: flex; gap: 16px; margin-bottom: 14px; align-items: center; flex-wrap: wrap; justify-content: space-between; }
.status-meta { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.indicator { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px;
             border-radius: 999px; background: rgba(13,17,23,0.55); border: 1px solid var(--border); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.green { background: var(--green); }
.dot.red { background: var(--red); animation: pulse 1s infinite; }
.dot.yellow { background: var(--yellow); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.stat-card { padding: 12px 14px; border-radius: 12px; background: rgba(13,17,23,0.52);
             border: 1px solid rgba(88,166,255,0.12); box-shadow: inset 0 1px 0 rgba(255,255,255,0.03); }
.stat-label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
.stat-value { margin-top: 8px; font-size: 22px; font-weight: 700; color: var(--text); }
.stat-meta { margin-top: 6px; font-size: 11px; color: var(--muted); line-height: 1.4; min-height: 16px; }
.card-header { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
table { width: 100%; border-collapse: collapse; }
td { padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
td:first-child { color: var(--muted); white-space: nowrap; width: 40%; }
.progress-bar { height: 8px; background: rgba(48,54,61,0.9); border-radius: 999px;
                overflow: hidden; margin-top: 6px; }
.progress-fill { height: 100%; background: linear-gradient(90deg, var(--green), #5ee37b); border-radius: 999px;
                 transition: width 0.5s; }
canvas { width: 100%; height: 120px; margin-top: 8px; }
.camera-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; grid-column: 1 / -1; }
.camera-card { min-height: 250px; }
.cam-shell { position: relative; min-height: 210px; margin-top: 4px; border-radius: 10px;
             overflow: hidden; border: 1px solid var(--border); background: linear-gradient(180deg, rgba(88,166,255,0.06), rgba(13,17,23,0.88)); }
img.cam { display: block; width: 100%; min-height: 210px; object-fit: cover; }
.cam-empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
             color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 1px; padding: 16px; text-align: center; }
.cam-status { position: absolute; top: 10px; right: 10px; font-size: 10px; font-weight: 700;
              text-transform: uppercase; letter-spacing: 1px; padding: 4px 8px; border-radius: 999px;
              border: 1px solid var(--border); background: rgba(13,17,23,0.72); color: var(--muted); }
.cam-status.live { color: var(--green); border-color: rgba(63,185,80,0.4); }
.cam-status.stale { color: var(--yellow); border-color: rgba(210,153,34,0.4); }
.cam-status.offline { color: var(--red); border-color: rgba(248,81,73,0.4); }
#conn { font-size: 11px; }
@media (max-width: 900px) { body { padding: 14px; } h1 { font-size: 24px; } .hero { padding: 14px; } }
/* --- Feed panels (intent + agent activity) --- */
.feed { max-height: 340px; overflow-y: auto; scrollbar-width: thin;
        scrollbar-color: var(--border) transparent; }
.feed-entry { padding: 8px 10px; border-left: 3px solid transparent;
              cursor: default; transition: background 0.15s; position: relative; }
.feed-entry:not(:last-child) { border-bottom: 1px solid var(--border); }
.feed-entry:hover { background: rgba(88,166,255,0.04); }
.feed-entry.expanded { background: rgba(88,166,255,0.06); }
.feed-head { display: flex; align-items: center; gap: 8px; }
.feed-ts { color: var(--muted); font-size: 11px; min-width: 58px; font-variant-numeric: tabular-nums; }
.feed-badge { font-size: 10px; padding: 1px 6px; border-radius: 3px; font-weight: 600;
              text-transform: uppercase; letter-spacing: 0.5px; }
.feed-badge.wait { background: #21262d; color: var(--muted); }
.feed-badge.action { background: rgba(88,166,255,0.15); color: var(--blue); }
.feed-badge.call-human { background: rgba(248,81,73,0.15); color: var(--red); }
.feed-text { font-size: 12px; line-height: 1.5; margin-top: 4px; color: var(--text); }
.feed-text.truncated { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
                       overflow: hidden; cursor: pointer; }
.feed-text.truncated:hover { color: var(--blue); }
.feed-entry.active { border-left-color: var(--blue); }
.feed-entry.stale { opacity: 0.6; }
.feed-empty { color: var(--muted); padding: 12px 0; font-style: italic; font-size: 12px; }
.intent-current { padding: 10px 12px; background: linear-gradient(135deg, rgba(88,166,255,0.08), rgba(88,166,255,0.02));
                  border: 1px solid rgba(88,166,255,0.2); border-radius: 6px; }
.intent-current .feed-text { font-size: 14px; color: var(--text); }
.intent-history { margin-top: 8px; }
.intent-history .feed-entry { padding: 4px 10px; opacity: 0.5; }
.urgent-flag { display: inline-block; background: var(--red); color: #fff; font-size: 10px;
               font-weight: 700; padding: 2px 8px; border-radius: 3px; margin-left: 8px;
               animation: pulse 1s infinite; letter-spacing: 1px; }
</style>
</head>
<body>
<div class="shell">
  <div class="hero">
    <div class="status-bar">
      <div>
        <h1>WALLEE</h1>
        <div class="subtitle">Autonomous printer oversight dashboard — live whiteboard, cameras, and agent reasoning context.</div>
      </div>
      <div class="status-meta">
        <span id="conn" class="indicator"></span>
        <span id="safety" class="indicator"></span>
      </div>
    </div>

    <div class="summary-grid">
      <div class="stat-card">
        <div class="stat-label">Phase</div>
        <div id="summary-state" class="stat-value">--</div>
        <div id="summary-state-meta" class="stat-meta"></div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Job Progress</div>
        <div id="summary-progress" class="stat-value">--</div>
        <div id="summary-progress-meta" class="stat-meta"></div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Agent</div>
        <div id="summary-agent" class="stat-value">--</div>
        <div id="summary-agent-meta" class="stat-meta"></div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Operator Intent</div>
        <div id="summary-intent" class="stat-value">--</div>
        <div id="summary-intent-meta" class="stat-meta"></div>
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card"><div class="card-header"><h2>Print Status</h2></div><div id="print-status"></div></div>
    <div class="card"><div class="card-header"><h2>Temperatures</h2></div><table id="temps"></table><canvas id="temp-chart"></canvas></div>
    <div class="card"><div class="card-header"><h2>Electrical</h2></div><table id="electrical"></table></div>
    <div class="card"><div class="card-header"><h2>Fans</h2></div><table id="fans"></table></div>
    <div class="card"><div class="card-header"><h2>Vision Analysis</h2></div><div id="vision-panel"></div></div>
    <div class="card"><div class="card-header"><h2>Position</h2></div><table id="position"></table></div>
    <div class="card"><div class="card-header"><h2>Firmware Health</h2></div><table id="health"></table></div>
    <div class="card"><div class="card-header"><h2>Human Intent</h2></div><div id="intent"></div></div>
    <div class="card card-wide"><div class="card-header"><h2>Agent Activity</h2></div><div id="agent-log" class="feed"></div></div>
    <div class="camera-grid">
      <div class="card camera-card"><div class="card-header"><h2>Nozzle Camera</h2></div><div class="cam-shell"><img id="cam-nozzle" class="cam" alt="No frame"><div id="cam-nozzle-empty" class="cam-empty">Awaiting nozzle frame</div><span id="cam-nozzle-status" class="cam-status offline">offline</span></div></div>
      <div class="card camera-card"><div class="card-header"><h2>Buddy Camera 1</h2></div><div class="cam-shell"><img id="cam-buddy1" class="cam" alt="No frame"><div id="cam-buddy1-empty" class="cam-empty">Awaiting buddy camera 1</div><span id="cam-buddy1-status" class="cam-status offline">offline</span></div></div>
      <div class="card camera-card"><div class="card-header"><h2>Buddy Camera 2</h2></div><div class="cam-shell"><img id="cam-buddy2" class="cam" alt="No frame"><div id="cam-buddy2-empty" class="cam-empty">Awaiting buddy camera 2</div><span id="cam-buddy2-status" class="cam-status offline">offline</span></div></div>
    </div>
    <div class="card card-wide"><div class="card-header"><h2>All Whiteboard Keys</h2></div><table id="all-keys"></table></div>
  </div>
</div>

<script>
"use strict";
let ws;
const tempHistory = {nozzle: [], bed: [], chamber: [], heatbreak: []};
const MAX_HISTORY = 60;

/* --- Safe DOM helpers (no innerHTML, XSS-safe) --- */
function clearEl(el) { while (el.firstChild) el.removeChild(el.firstChild); }

function makeRow(label, value, cls) {
  const tr = document.createElement('tr');
  const td1 = document.createElement('td');
  td1.textContent = label;
  const td2 = document.createElement('td');
  td2.textContent = String(value);
  if (cls) td2.className = cls;
  tr.appendChild(td1); tr.appendChild(td2);
  return tr;
}

function setIndicator(el, dotClass, text) {
  clearEl(el);
  const dot = document.createElement('span');
  dot.className = 'dot ' + dotClass;
  el.appendChild(dot);
  el.appendChild(document.createTextNode(' ' + text));
}

function setStat(id, value, meta) {
  var valueEl = document.getElementById(id);
  var metaEl = document.getElementById(id + '-meta');
  if (valueEl) valueEl.textContent = value;
  if (metaEl) metaEl.textContent = meta || '';
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.hostname + ':WS_PORT');
  ws.onopen = function() { setIndicator(document.getElementById('conn'), 'green', 'live'); };
  ws.onclose = function() {
    setIndicator(document.getElementById('conn'), 'red', 'disconnected');
    setTimeout(connect, 3000);
  };
  ws.onmessage = function(e) { try { update(JSON.parse(e.data)); } catch(err) { console.error(err); } };
}

function update(s) {
  updatePrintStatus(s); updateTemps(s); updateElectrical(s);
  updateFans(s); updatePosition(s); updateHealth(s);
  updateVision(s); updateIntent(s); updateAgentLog(s);
  updateCameras(s); updateSafety(s); updateSummary(s); updateAllKeys(s);
}

function updateSummary(s) {
  var phase = s['job.phase'] || s['printer.state'] || 'UNKNOWN';
  var detail = s['job.phase_detail'] || s['printer.job_state'] || '';
  setStat('summary-state', phase, detail);

  var progress = s['printer.job_progress'];
  var remaining = s['printer.job_time_remaining_s'];
  var progressValue = progress != null ? Math.round(progress) + '%' : '--';
  var progressMeta = remaining != null ? (Math.round(remaining / 60) + ' min remaining') : (s['printer.print_filename'] || 'No file loaded');
  setStat('summary-progress', progressValue, progressMeta);

  var decision = s['agent.last_decision'] || 'No recent decision';
  var decisionLabel = decision.split(':')[0] || 'Agent';
  if (decisionLabel.length > 18) decisionLabel = 'Decision';
  var decisionMeta = decision;
  if (decisionMeta.indexOf(': ') > 0) decisionMeta = decisionMeta.substring(decisionMeta.indexOf(': ') + 2);
  setStat('summary-agent', decisionLabel.toUpperCase(), decisionMeta);

  var intent = s['human.intent'];
  var urgent = s['human.urgent'];
  if (intent) setStat('summary-intent', urgent ? 'URGENT' : 'ACTIVE', intent);
  else setStat('summary-intent', 'CLEAR', 'No active operator intent');
}

function updatePrintStatus(s) {
  var el = document.getElementById('print-status');
  clearEl(el);
  var tbl = document.createElement('table');
  tbl.appendChild(makeRow('Printer', s['printer.state'] || 'UNKNOWN'));
  tbl.appendChild(makeRow('Job', s['printer.job_state'] || '-'));
  if (s['printer.print_filename']) tbl.appendChild(makeRow('File', s['printer.print_filename']));
  if (s['printer.job_progress'] != null) {
    var pct = Math.round(s['printer.job_progress']);
    tbl.appendChild(makeRow('Progress', pct + '%'));
    var pRow = document.createElement('tr');
    var pTd = document.createElement('td'); pTd.colSpan = 2;
    var bar = document.createElement('div'); bar.className = 'progress-bar';
    var fill = document.createElement('div'); fill.className = 'progress-fill';
    fill.style.width = pct + '%';
    bar.appendChild(fill); pTd.appendChild(bar); pRow.appendChild(pTd);
    tbl.appendChild(pRow);
  }
  if (s['printer.job_time_remaining_s'] != null)
    tbl.appendChild(makeRow('Remaining', Math.round(s['printer.job_time_remaining_s'] / 60) + ' min'));
  el.appendChild(tbl);
}

function updateTemps(s) {
  var el = document.getElementById('temps');
  clearEl(el);
  var pairs = [
    ['Nozzle', s['printer.temp_nozzle'], s['printer.target_nozzle']],
    ['Bed', s['printer.temp_bed'], s['printer.target_bed']],
    ['Chamber', s['printer.temp_chamber'], null],
    ['Heatbreak', s['printer.temp_heatbreak'], null],
    ['Board', s['printer.temp_board'], null],
    ['MCU', s['printer.temp_mcu'], null],
  ];
  for (var i = 0; i < pairs.length; i++) {
    var name = pairs[i][0], actual = pairs[i][1], target = pairs[i][2];
    if (actual == null) continue;
    var t = target != null ? ' / ' + target + '\u00B0C' : '';
    el.appendChild(makeRow(name, actual + '\u00B0C' + t));
  }
  if (s['printer.temp_nozzle'] != null) pushHistory('nozzle', s['printer.temp_nozzle']);
  if (s['printer.temp_bed'] != null) pushHistory('bed', s['printer.temp_bed']);
  if (s['printer.temp_chamber'] != null) pushHistory('chamber', s['printer.temp_chamber']);
  if (s['printer.temp_heatbreak'] != null) pushHistory('heatbreak', s['printer.temp_heatbreak']);
  drawChart();
}

function pushHistory(key, val) {
  tempHistory[key].push(val);
  if (tempHistory[key].length > MAX_HISTORY) tempHistory[key].shift();
}

function drawChart() {
  var canvas = document.getElementById('temp-chart');
  var ctx = canvas.getContext('2d');
  var W = canvas.offsetWidth * 2, H = 260;
  canvas.width = W; canvas.height = H;
  ctx.clearRect(0, 0, W, H);
  var colors = {nozzle: '#f85149', bed: '#d29922', chamber: '#58a6ff', heatbreak: '#8b949e'};
  var labels = {nozzle: 'Nozzle', bed: 'Bed', chamber: 'Chamber', heatbreak: 'Heatbreak'};
  var PAD_L = 50, PAD_R = 10, PAD_T = 10, PAD_B = 40;
  var plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;
  var maxVal = 50;
  for (var k in tempHistory) for (var i = 0; i < tempHistory[k].length; i++) maxVal = Math.max(maxVal, tempHistory[k][i]);
  maxVal = Math.ceil(maxVal / 10) * 10 + 10;

  // Y-axis gridlines and labels
  ctx.strokeStyle = '#30363d'; ctx.lineWidth = 1; ctx.fillStyle = '#8b949e'; ctx.font = '18px monospace';
  ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  for (var t = 0; t <= maxVal; t += Math.max(10, Math.round(maxVal / 5 / 10) * 10)) {
    var y = PAD_T + plotH - (t / maxVal) * plotH;
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(W - PAD_R, y); ctx.stroke();
    ctx.fillText(t + '\u00B0C', PAD_L - 6, y);
  }

  // X-axis label
  ctx.fillStyle = '#8b949e'; ctx.font = '16px monospace'; ctx.textAlign = 'center';
  ctx.fillText('Time \u2192', W / 2, H - 4);

  // Plot lines
  for (var key in tempHistory) {
    var data = tempHistory[key]; if (data.length < 2) continue;
    ctx.beginPath(); ctx.strokeStyle = colors[key]; ctx.lineWidth = 3;
    for (var j = 0; j < data.length; j++) {
      var x = PAD_L + (j / (MAX_HISTORY - 1)) * plotW;
      var y = PAD_T + plotH - (data[j] / maxVal) * plotH;
      if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  // Legend
  var lx = PAD_L + 8, ly = PAD_T + 6;
  ctx.font = '16px monospace'; ctx.textAlign = 'left';
  for (var key in labels) {
    if (tempHistory[key].length < 1) continue;
    ctx.fillStyle = colors[key];
    ctx.fillRect(lx, ly, 16, 10);
    ctx.fillStyle = '#e6edf3';
    ctx.fillText(labels[key], lx + 22, ly + 9);
    lx += ctx.measureText(labels[key]).width + 40;
  }
}

function updateElectrical(s) {
  var el = document.getElementById('electrical'); clearEl(el);
  var items = [['Bed V', s['printer.volt_bed'], 'V'], ['Nozzle V', s['printer.volt_nozzle'], 'V'],
               ['Nozzle A', s['printer.curr_nozzle'], 'A'], ['OC Nozzle', s['printer.oc_nozzle'], ''],
               ['OC Input', s['printer.oc_input'], '']];
  for (var i = 0; i < items.length; i++) {
    if (items[i][1] == null) continue;
    el.appendChild(makeRow(items[i][0], items[i][1] + (items[i][2] ? ' ' + items[i][2] : '')));
  }
}

function updateFans(s) {
  var el = document.getElementById('fans'); clearEl(el);
  if (s['printer.fan_heatbreak_rpm'] != null) el.appendChild(makeRow('Heatbreak', s['printer.fan_heatbreak_rpm'] + ' RPM'));
  if (s['printer.fan_print_rpm'] != null) el.appendChild(makeRow('Print', s['printer.fan_print_rpm'] + ' RPM'));
}

function updateVision(s) {
  var el = document.getElementById('vision-panel');
  clearEl(el);
  var tbl = document.createElement('table');

  /* Check if vision sensor is alive */
  var lastTs = s['vision.last_analysis_ts'];
  var age = lastTs ? Math.round((Date.now() / 1000) - parseFloat(lastTs)) : null;

  if (!lastTs || age > 30) {
    tbl.appendChild(makeRow('Status', 'OFFLINE — no vision data'));
    el.appendChild(tbl);
    return;
  }

  /* Combined status */
  var status = s['vision.status'] || s['vision.nozzle.status'] || 'NO_DATA';
  var statusCls = status.startsWith('DEFECT') ? 'color:var(--red)' :
                  status.startsWith('POSSIBLE') ? 'color:var(--yellow)' : '';
  var statusRow = makeRow('Status', status);
  if (statusCls) statusRow.lastChild.style.cssText = statusCls + ';font-weight:700';
  tbl.appendChild(statusRow);

  /* Confidence */
  var conf = s['vision.confidence'] || s['vision.nozzle.confidence'];
  if (conf != null) tbl.appendChild(makeRow('Confidence', conf));

  /* Description */
  var desc = s['vision.description'] || s['vision.nozzle.description'];
  if (desc) tbl.appendChild(makeRow('Description', desc));

  /* Defect scores — nozzle */
  var defects = ['stringing', 'spaghetti', 'blob', 'warping', 'layer_shift',
                 'underextrusion', 'overextrusion', 'burn_marks', 'bed_adhesion_ok', 'normal'];
  for (var i = 0; i < defects.length; i++) {
    var key = defects[i];
    var val = s['vision.nozzle.' + key] || s['vision.' + key];
    if (val == null) continue;
    var row = makeRow(key.replace('_', ' '), val);
    if (val > 0.7 && key !== 'normal' && key !== 'bed_adhesion_ok')
      row.lastChild.style.cssText = 'color:var(--red);font-weight:700';
    else if (val > 0.4 && key !== 'normal' && key !== 'bed_adhesion_ok')
      row.lastChild.style.cssText = 'color:var(--yellow)';
    tbl.appendChild(row);
  }

  /* Buddy camera status if available */
  var buddyStatus = s['vision.buddy.status'];
  if (buddyStatus) {
    var bRow = makeRow('Buddy cam', buddyStatus);
    if (buddyStatus.startsWith('DEFECT')) bRow.lastChild.style.cssText = 'color:var(--red);font-weight:700';
    tbl.appendChild(bRow);
  }

  tbl.appendChild(makeRow('Last update', age + 's ago'));
  el.appendChild(tbl);
}

function updatePosition(s) {
  var el = document.getElementById('position'); clearEl(el);
  for (var a of ['x','y','z']) { var v = s['printer.pos_'+a]; if (v != null) el.appendChild(makeRow(a.toUpperCase(), v + ' mm')); }
}

function updateHealth(s) {
  var el = document.getElementById('health'); clearEl(el);
  if (s['printer.cpu_usage'] != null) el.appendChild(makeRow('CPU', s['printer.cpu_usage'] + '%'));
  if (s['printer.heap_free'] != null) el.appendChild(makeRow('Heap', s['printer.heap_free'] + ' / ' + (s['printer.heap_total']||'?') + ' free'));
  if (s['printer.stepper_stall'] != null) el.appendChild(makeRow('Stall', s['printer.stepper_stall']));
}

function updateCamCard(imgId, statusKey, frameKey, s) {
  var img = document.getElementById(imgId);
  var empty = document.getElementById(imgId + '-empty');
  var badge = document.getElementById(imgId + '-status');
  var status = s[statusKey] || 'offline';
  if (badge) {
    badge.className = 'cam-status ' + status;
    badge.textContent = status;
  }
  if (status === 'live' && s[frameKey]) {
    img.src = 'data:image/jpeg;base64,' + s[frameKey];
    img.alt = '';
    if (empty) empty.style.display = 'none';
  } else {
    img.removeAttribute('src');
    img.alt = status === 'stale' ? 'Camera stale (frozen frame)' : 'Camera offline';
    if (empty) {
      empty.style.display = 'flex';
      empty.textContent = status === 'stale' ? 'Camera stale — last frame frozen' : 'Camera offline';
    }
  }
}

function updateCameras(s) {
  updateCamCard('cam-nozzle', 'camera.nozzle_status', 'camera.nozzle_frame', s);
  updateCamCard('cam-buddy1', 'camera.buddy1_status', 'camera.buddy1_frame', s);
  updateCamCard('cam-buddy2', 'camera.buddy2_status', 'camera.buddy2_frame', s);
}

function updateSafety(s) {
  var el = document.getElementById('safety');
  var ocn = s['printer.oc_nozzle'], oci = s['printer.oc_input'];
  if (s['safety.estop']) setIndicator(el, 'red', 'ESTOP ACTIVE');
  else if (ocn != null && ocn !== 0) setIndicator(el, 'red', 'OC NOZZLE');
  else if (oci != null && oci !== 0) setIndicator(el, 'red', 'OC INPUT');
  else setIndicator(el, 'green', 'safe');
}

function formatValue(key, v) {
  if (v === null || v === undefined) return '-';
  // Camera frames — just show size
  if (typeof v === 'string' && v.length > 200) return '[' + v.length + ' chars]';
  // Arrays — summarize
  if (Array.isArray(v)) {
    if (key === 'printer.files') return v.length + ' files';
    return JSON.stringify(v).substring(0, 80);
  }
  // Objects — show top-level keys with stringified values
  if (typeof v === 'object') {
    var parts = [];
    for (var k in v) {
      var sub = v[k];
      if (typeof sub === 'object' && sub !== null) sub = JSON.stringify(sub);
      parts.push(k + '=' + sub);
      if (parts.length > 4) { parts.push('...'); break; }
    }
    return parts.join(', ');
  }
  // Booleans
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  return String(v);
}

/* --- Intent panel with history --- */
function updateIntent(s) {
  var el = document.getElementById('intent');
  clearEl(el);
  var intent = s['human.intent'];
  var urgent = s['human.urgent'];
  var intentLog = s['human.intent_log'];

  /* Parse intent log from Redis list */
  var entries = [];
  if (Array.isArray(intentLog)) {
    for (var i = 0; i < intentLog.length; i++) {
      try {
        var parsed = typeof intentLog[i] === 'string' ? JSON.parse(intentLog[i]) : intentLog[i];
        entries.push(parsed);
      } catch(e) {}
    }
  }

  /* Current active intent */
  if (intent) {
    var box = document.createElement('div');
    box.className = 'intent-current';
    var head = document.createElement('div');
    head.className = 'feed-head';
    if (entries.length > 0) {
      var ts = document.createElement('span');
      ts.className = 'feed-ts';
      ts.textContent = entries[0].ts || '';
      head.appendChild(ts);
    }
    var label = document.createElement('span');
    label.textContent = 'ACTIVE';
    label.style.cssText = 'font-size:10px;color:var(--green);font-weight:700;letter-spacing:1px';
    head.appendChild(label);
    if (urgent) {
      var uf = document.createElement('span');
      uf.className = 'urgent-flag';
      uf.textContent = 'URGENT';
      head.appendChild(uf);
    }
    box.appendChild(head);
    var txt = document.createElement('div');
    txt.className = 'feed-text';
    txt.textContent = intent;
    box.appendChild(txt);
    el.appendChild(box);
  } else if (entries.length > 0) {
    /* No active intent but we have history — show last as expired */
    var box = document.createElement('div');
    box.className = 'feed-entry stale';
    var head = document.createElement('div');
    head.className = 'feed-head';
    var ts = document.createElement('span');
    ts.className = 'feed-ts';
    ts.textContent = entries[0].ts || '';
    head.appendChild(ts);
    var label = document.createElement('span');
    label.textContent = 'EXPIRED';
    label.style.cssText = 'font-size:10px;color:var(--muted);font-weight:700;letter-spacing:1px';
    head.appendChild(label);
    box.appendChild(head);
    var txt = document.createElement('div');
    txt.className = 'feed-text';
    txt.style.fontSize = '13px';
    txt.textContent = entries[0].text || '';
    box.appendChild(txt);
    el.appendChild(box);
  } else {
    var empty = document.createElement('div');
    empty.className = 'feed-empty';
    empty.textContent = 'No intent history';
    el.appendChild(empty);
  }

  /* Show older intents (always visible from Redis log) */
  var startIdx = intent ? 1 : 1;  /* skip first entry (shown above) */
  if (entries.length > startIdx) {
    var hist = document.createElement('div');
    hist.className = 'intent-history';
    for (var i = startIdx; i < entries.length; i++) {
      var entry = document.createElement('div');
      entry.className = 'feed-entry stale';
      var h = document.createElement('div');
      h.className = 'feed-head';
      var ts2 = document.createElement('span');
      ts2.className = 'feed-ts';
      ts2.textContent = entries[i].ts || '';
      h.appendChild(ts2);
      entry.appendChild(h);
      var t2 = document.createElement('div');
      t2.className = 'feed-text';
      t2.style.fontSize = '11px';
      t2.textContent = entries[i].text || '';
      entry.appendChild(t2);
      hist.appendChild(entry);
    }
    el.appendChild(hist);
  }
}

/* --- Agent activity feed --- */
var lastActivityLog = null;

function updateAgentLog(s) {
  var el = document.getElementById('agent-log');

  /* Read activity log from whiteboard (JSON array stored in Redis list) */
  var logData = s['agent.activity_log'];
  var logKey = JSON.stringify(logData);
  if (logKey === lastActivityLog) return; /* no change, skip redraw */
  lastActivityLog = logKey;

  clearEl(el);

  /* Parse entries — could be array or need extraction from whiteboard */
  var entries = [];
  if (Array.isArray(logData)) {
    for (var i = 0; i < logData.length; i++) {
      try {
        if (typeof logData[i] === 'string') entries.push(JSON.parse(logData[i]));
        else entries.push(logData[i]);
      } catch(e) { /* skip bad entries */ }
    }
  }

  /* Fallback: use agent.last_decision if no log array */
  if (entries.length === 0) {
    var last = s['agent.last_decision'];
    if (last) {
      entries.push({ts: new Date().toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit'}), type: 'WAIT', text: last});
    }
  }

  if (entries.length === 0) {
    var empty = document.createElement('div');
    empty.className = 'feed-empty';
    empty.textContent = 'Waiting for agent activity...';
    el.appendChild(empty);
    return;
  }

  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    var row = document.createElement('div');
    row.className = 'feed-entry' + (i === 0 ? ' active' : ' stale');

    /* Header: timestamp + badge */
    var head = document.createElement('div');
    head.className = 'feed-head';

    var ts = document.createElement('span');
    ts.className = 'feed-ts';
    ts.textContent = e.ts || '--:--:--';
    head.appendChild(ts);

    var badge = document.createElement('span');
    badge.className = 'feed-badge';
    var btype = (e.type || 'WAIT').toUpperCase();
    if (btype === 'ACTION') badge.className += ' action';
    else if (btype === 'CALL_HUMAN') badge.className += ' call-human';
    else badge.className += ' wait';
    badge.textContent = btype.replace('_', ' ');
    head.appendChild(badge);

    row.appendChild(head);

    /* Text body — truncated, click to expand */
    var text = document.createElement('div');
    text.className = 'feed-text truncated';
    var fullText = e.text || '';
    /* Strip the type prefix if present */
    if (fullText.indexOf(': ') > 0 && fullText.indexOf(': ') < 30) {
      fullText = fullText.substring(fullText.indexOf(': ') + 2);
    }
    text.textContent = fullText;
    text.addEventListener('click', (function(t, r) {
      return function() {
        if (t.classList.contains('truncated')) {
          t.classList.remove('truncated');
          r.classList.add('expanded');
        } else {
          t.classList.add('truncated');
          r.classList.remove('expanded');
        }
      };
    })(text, row));
    row.appendChild(text);

    el.appendChild(row);
  }
}

function updateAllKeys(s) {
  var el = document.getElementById('all-keys'); clearEl(el);
  var keys = Object.keys(s).sort();
  for (var i = 0; i < keys.length; i++) {
    el.appendChild(makeRow(keys[i], formatValue(keys[i], s[keys[i]])));
  }
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
