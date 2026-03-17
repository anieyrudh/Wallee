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

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wallee Dashboard</title>
<style>
:root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
        --muted: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922;
        --blue: #58a6ff; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono', 'Fira Code', monospace; background: var(--bg);
       color: var(--text); font-size: 13px; padding: 16px; }
h1 { font-size: 18px; margin-bottom: 12px; color: var(--blue); }
h2 { font-size: 14px; margin-bottom: 8px; color: var(--muted); text-transform: uppercase;
     letter-spacing: 1px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
        gap: 12px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 12px; }
.status-bar { display: flex; gap: 16px; margin-bottom: 12px; align-items: center; }
.indicator { display: inline-flex; align-items: center; gap: 4px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.green { background: var(--green); }
.dot.red { background: var(--red); animation: pulse 1s infinite; }
.dot.yellow { background: var(--yellow); }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
table { width: 100%; border-collapse: collapse; }
td { padding: 3px 8px; border-bottom: 1px solid var(--border); }
td:first-child { color: var(--muted); white-space: nowrap; width: 40%; }
.progress-bar { height: 6px; background: var(--border); border-radius: 3px;
                overflow: hidden; margin-top: 4px; }
.progress-fill { height: 100%; background: var(--green); border-radius: 3px;
                 transition: width 0.5s; }
canvas { width: 100%; height: 120px; }
img.cam { max-width: 100%; border-radius: 4px; margin-top: 4px; }
#conn { font-size: 11px; }
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
<div class="status-bar">
  <h1>WALLEE</h1>
  <span id="conn" class="indicator"></span>
  <span id="safety" class="indicator"></span>
</div>

<div class="grid">
  <div class="card"><h2>Print Status</h2><div id="print-status"></div></div>
  <div class="card"><h2>Temperatures</h2><table id="temps"></table><canvas id="temp-chart"></canvas></div>
  <div class="card"><h2>Electrical</h2><table id="electrical"></table></div>
  <div class="card"><h2>Fans</h2><table id="fans"></table></div>
  <div class="card"><h2>Position</h2><table id="position"></table></div>
  <div class="card"><h2>Firmware Health</h2><table id="health"></table></div>
  <div class="card"><h2>Human Intent</h2><div id="intent"></div></div>
  <div class="card" style="grid-column: 1 / -1"><h2>Agent Activity</h2><div id="agent-log" class="feed"></div></div>
  <div class="card"><h2>Nozzle Camera</h2><img id="cam-nozzle" class="cam" alt="No frame"></div>
  <div class="card"><h2>Buddy Camera 1</h2><img id="cam-buddy1" class="cam" alt="No frame"></div>
  <div class="card"><h2>Buddy Camera 2</h2><img id="cam-buddy2" class="cam" alt="No frame"></div>
  <div class="card" style="grid-column: 1 / -1"><h2>All Whiteboard Keys</h2><table id="all-keys"></table></div>
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
  updateIntent(s); updateAgentLog(s);
  updateCameras(s); updateSafety(s); updateAllKeys(s);
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
  var status = s[statusKey];
  if (status === 'live' && s[frameKey]) {
    img.src = 'data:image/jpeg;base64,' + s[frameKey];
    img.alt = '';
  } else {
    img.removeAttribute('src');
    img.alt = status === 'stale' ? 'Camera stale (frozen frame)' : 'Camera offline';
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
  if (ocn != null && ocn !== 0) setIndicator(el, 'red', 'OC NOZZLE');
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
var intentHistory = [];
var MAX_INTENT_HISTORY = 5;
var lastIntentText = null;

function updateIntent(s) {
  var el = document.getElementById('intent');
  clearEl(el);
  var intent = s['human.intent'];
  var urgent = s['human.urgent'];

  /* Track intent history */
  if (intent && intent !== lastIntentText) {
    intentHistory.unshift({text: intent, ts: new Date().toLocaleTimeString('en-GB', {hour:'2-digit',minute:'2-digit',second:'2-digit'})});
    if (intentHistory.length > MAX_INTENT_HISTORY) intentHistory.pop();
    lastIntentText = intent;
  }

  if (intent) {
    var box = document.createElement('div');
    box.className = 'intent-current';
    var head = document.createElement('div');
    head.className = 'feed-head';
    if (intentHistory.length > 0) {
      var ts = document.createElement('span');
      ts.className = 'feed-ts';
      ts.textContent = intentHistory[0].ts;
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
  } else {
    var empty = document.createElement('div');
    empty.className = 'feed-empty';
    empty.textContent = 'No active intent';
    el.appendChild(empty);
  }

  /* Show history (older intents, dimmed) */
  if (intentHistory.length > 1) {
    var hist = document.createElement('div');
    hist.className = 'intent-history';
    for (var i = 1; i < intentHistory.length; i++) {
      var entry = document.createElement('div');
      entry.className = 'feed-entry stale';
      var h = document.createElement('div');
      h.className = 'feed-head';
      var ts2 = document.createElement('span');
      ts2.className = 'feed-ts';
      ts2.textContent = intentHistory[i].ts;
      h.appendChild(ts2);
      entry.appendChild(h);
      var t2 = document.createElement('div');
      t2.className = 'feed-text';
      t2.style.fontSize = '11px';
      t2.textContent = intentHistory[i].text;
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
        asyncio.set_event_loop(loop)

        try:
            import websockets
            import websockets.server
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
            ws_server = await websockets.server.serve(ws_handler, self.host, self._ws_port)

            http_thread = threading.Thread(target=self._run_http, daemon=True)
            http_thread.start()

            while self._running:
                try:
                    state = self.wb.read_all()
                    # Include agent activity log (Redis list, not a string key)
                    try:
                        log_raw = self.wb.r.lrange("agent.activity_log", 0, 19)
                        if log_raw:
                            state["agent.activity_log"] = [v for v in log_raw]
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

        loop.run_until_complete(main())

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

        server = HTTPServer((self.host, self.port), Handler)
        logger.info(f"Dashboard HTTP on port {self.port}, WebSocket on port {self._ws_port}")
        while outer._running:
            server.handle_request()

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

        server = HTTPServer((self.host, self.port), Handler)
        logger.info(f"Dashboard HTTP-only on port {self.port}")
        while outer._running:
            server.handle_request()

    def stop(self):
        self._running = False
