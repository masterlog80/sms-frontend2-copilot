/* ═══════════════════════════════════════════════════════════════════════════
   USB Modem Dashboard – app.js
════════════════════════════════════════════════════════════════════════════ */
'use strict';

const API = '';
const REFRESH_INTERVAL = 5;   // seconds
const EMAIL_DEFAULT_PORT = 587;
let countdown = REFRESH_INTERVAL;
let _smsList  = [];
let _logList  = [];
let _logFilter = 'ALL';

// Signal history chart – declared early to avoid temporal dead-zone when
// applyTheme() calls updateChartTheme() at startup.
let _signalHistory = [];
let _signalRangeMinutes = 10;
let _signalChart = null;
let _signalHistoryLastTs = null;

/* ─── Theme ──────────────────────────────────────────────────────────────── */
const html    = document.documentElement;
const btnTheme = document.getElementById('btnTheme');
const THEME_KEY = 'modem-dash-theme';

function applyTheme(theme) {
  html.setAttribute('data-bs-theme', theme);
  btnTheme.innerHTML = theme === 'dark'
    ? '<i class="bi bi-sun-fill"></i>'
    : '<i class="bi bi-moon-stars-fill"></i>';
  localStorage.setItem(THEME_KEY, theme);
  updateChartTheme(theme);
}

btnTheme.addEventListener('click', () => {
  applyTheme(html.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark');
});

applyTheme(localStorage.getItem(THEME_KEY) || 'dark');

/* ─── Toast helper ───────────────────────────────────────────────────────── */
function showToast(message, type = 'info') {
  const area = document.getElementById('toastArea');
  const cls  = { success: 'bg-success', danger: 'bg-danger', info: 'bg-primary', warning: 'bg-warning' };
  const el   = document.createElement('div');
  el.className = `toast align-items-center text-white ${cls[type] || 'bg-primary'} border-0`;
  el.setAttribute('role', 'alert');
  el.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">${message}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>`;
  area.appendChild(el);
  const t = new bootstrap.Toast(el, { delay: 3000 });
  t.show();
  el.addEventListener('hidden.bs.toast', () => el.remove());
}

/* ─── Utility ────────────────────────────────────────────────────────────── */
function escHtml(str) {
  return String(str)
    .replace(/&/g,  '&amp;')
    .replace(/</g,  '&lt;')
    .replace(/>/g,  '&gt;')
    .replace(/"/g,  '&quot;');
}

function fmtTime(isoStr) {
  if (!isoStr) return '–';
  try { return new Date(isoStr).toLocaleTimeString(); } catch { return isoStr; }
}
function fmtDateTime(isoStr) {
  if (!isoStr) return '–';
  try { return new Date(isoStr).toLocaleString(); } catch { return isoStr; }
}

function qualityClass(quality) {
  return { Excellent: 'excellent', Good: 'good', Fair: 'fair', Poor: 'poor', 'Very Poor': 'verypoor' }[quality] || 'unknown';
}

/* ─── Signal bars ────────────────────────────────────────────────────────── */
const BAR_COLORS = ['#6b7280', '#ef4444', '#f97316', '#eab308', '#84cc16', '#22c55e'];

function pctToBars(pct) {
  if (pct <= 0)  return 0;
  if (pct <= 20) return 1;
  if (pct <= 40) return 2;
  if (pct <= 60) return 3;
  if (pct <= 80) return 4;
  return 5;
}

function updateSignalBars(pct) {
  const bars = document.querySelectorAll('#signalBars .signal-bar');
  if (!bars.length) return;
  const active = pctToBars(pct);
  const color  = BAR_COLORS[active];
  bars.forEach((bar, i) => {
    bar.style.background = i < active ? color : '';
  });
}

/* ─── Refresh ring ───────────────────────────────────────────────────────── */
const RING_CIRCUM = 94.25;

function updateRing(secondsLeft) {
  const el = document.getElementById('ringProgress');
  if (!el) return;
  const filled = (secondsLeft / REFRESH_INTERVAL) * RING_CIRCUM;
  el.setAttribute('stroke-dashoffset', RING_CIRCUM - filled);
  document.getElementById('countdown').textContent = secondsLeft;
}

/* ─── Status / Signal / Memory ──────────────────────────────────────────── */
function updateStatus(data) {
  // Connection badge
  const badge = document.getElementById('connBadge');
  if (data.modem_connected) {
    badge.className = 'status-pill status-connected';
    badge.innerHTML = '<span class="status-dot"></span><span class="status-label">Connected</span>';
  } else {
    badge.className = 'status-pill status-error';
    badge.innerHTML = '<span class="status-dot"></span><span class="status-label">Disconnected</span>';
  }

  document.getElementById('deviceLabel').textContent  = data.device || '–';
  document.getElementById('lastUpdated').textContent  = fmtTime(data.last_updated);

  // Signal
  const sig  = data.signal || {};
  const pct  = sig.percent || 0;
  const qCls = qualityClass(sig.quality || '');

  document.getElementById('sigRssi').textContent = sig.rssi !== undefined ? sig.rssi : '–';
  document.getElementById('sigDbm').textContent  = sig.dbm !== null && sig.dbm !== undefined ? `${sig.dbm} dBm` : '–';
  document.getElementById('sigBer').textContent  = sig.ber !== undefined ? sig.ber : '–';

  const qBadge = document.getElementById('sigQuality');
  qBadge.textContent = sig.quality || '–';
  qBadge.className   = `quality-badge quality-${qCls}`;

  updateSignalBars(pct);

  // Push signal reading into the history chart
  if (data.last_updated) {
    pushSignalPoint(sig, data.last_updated);
  }

  // Memory
  const mem    = data.memory || {};
  const memPct = mem.percent_used || 0;
  document.getElementById('memUsed').textContent   = mem.used  !== undefined ? mem.used  : '–';
  document.getElementById('memTotal').textContent  = mem.total !== undefined ? mem.total : '–';
  document.getElementById('memUsed2').textContent  = mem.used  !== undefined ? mem.used  : '–';
  document.getElementById('memFree').textContent   = mem.free  !== undefined ? mem.free  : '–';
  document.getElementById('memTotal2').textContent = mem.total !== undefined ? mem.total : '–';
  document.getElementById('memPctLabel').textContent = memPct + '%';

  const bar = document.getElementById('memBar');
  bar.style.width = memPct + '%';
  bar.className   = 'mem-bar-fill' + (memPct >= 90 ? ' crit' : memPct >= 70 ? ' warn' : '');

  // Modem info
  const info = data.modem_info || {};
  document.getElementById('infoManuf').textContent = info.manufacturer  || '–';
  document.getElementById('infoModel').textContent = info.model         || '–';
  document.getElementById('infoImei').textContent  = info.imei          || '–';
  document.getElementById('infoNet').textContent      = info.network_status || '–';
  document.getElementById('infoNetName').textContent  = info.network_name    || '–';
}

/* ─── Signal History Chart ───────────────────────────────────────────────── */
const CHART_COLORS = {
  line:        '#6366f1',
  fill:        'rgba(99,102,241,0.15)',
  grid_dark:   'rgba(255,255,255,0.06)',
  grid_light:  'rgba(0,0,0,0.06)',
  tick_dark:   '#64748b',
  tick_light:  '#94a3b8',
};

function _chartGridColor() {
  return html.getAttribute('data-bs-theme') === 'dark'
    ? CHART_COLORS.grid_dark : CHART_COLORS.grid_light;
}
function _chartTickColor() {
  return html.getAttribute('data-bs-theme') === 'dark'
    ? CHART_COLORS.tick_dark : CHART_COLORS.tick_light;
}

function initSignalChart() {
  const ctx = document.getElementById('signalChart');
  if (!ctx) return;
  const gc = _chartGridColor();
  const tc = _chartTickColor();
  _signalChart = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'Signal %',
          data: [],
          borderColor: CHART_COLORS.line,
          backgroundColor: CHART_COLORS.fill,
          fill: true,
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 5,
          borderWidth: 2,
          yAxisID: 'yPct',
        },
        {
          label: 'dBm',
          data: [],
          borderColor: '#22c55e',
          backgroundColor: 'transparent',
          fill: false,
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 5,
          borderWidth: 1.5,
          borderDash: [4, 3],
          yAxisID: 'yDbm',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          type: 'time',
          time: { tooltipFormat: 'HH:mm:ss', displayFormats: { second: 'HH:mm:ss', minute: 'HH:mm', hour: 'HH:mm' } },
          grid: { color: gc },
          ticks: { color: tc, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
        },
        yPct: {
          type: 'linear',
          position: 'left',
          min: 0,
          max: 100,
          grid: { color: gc },
          ticks: { color: tc, callback: v => v + '%' },
          title: { display: true, text: 'Signal %', color: tc },
        },
        yDbm: {
          type: 'linear',
          position: 'right',
          grid: { drawOnChartArea: false },
          ticks: { color: '#22c55e', callback: v => v + ' dBm' },
          title: { display: true, text: 'dBm', color: '#22c55e' },
        },
      },
      plugins: {
        legend: {
          display: true,
          labels: { color: tc, boxWidth: 12, padding: 14 },
        },
        tooltip: {
          callbacks: {
            label: ctx => {
              if (ctx.dataset.yAxisID === 'yPct') {
                const q = ctx.raw.quality || '';
                return ` Signal: ${ctx.parsed.y}%${q ? '  (' + q + ')' : ''}`;
              }
              return ctx.parsed.y !== null ? ` dBm: ${ctx.parsed.y}` : null;
            },
          },
        },
      },
    },
  });
}

function updateChartTheme(theme) {
  if (!_signalChart) return;  // chart may not be initialised yet
  const gc = theme === 'dark' ? CHART_COLORS.grid_dark : CHART_COLORS.grid_light;
  const tc = theme === 'dark' ? CHART_COLORS.tick_dark : CHART_COLORS.tick_light;
  const s = _signalChart.options.scales;
  s.x.grid.color        = gc;
  s.x.ticks.color       = tc;
  s.yPct.grid.color     = gc;
  s.yPct.ticks.color    = tc;
  s.yPct.title.color    = tc;
  _signalChart.options.plugins.legend.labels.color = tc;
  _signalChart.update('none');
}

function _buildChartData() {
  if (!_signalChart) return;
  const cutoff = Date.now() - _signalRangeMinutes * 60 * 1000;
  const visible = _signalHistory.filter(e => new Date(e.timestamp).getTime() >= cutoff);
  _signalChart.data.datasets[0].data = visible.map(e => ({ x: e.timestamp, y: e.percent, quality: e.quality }));
  _signalChart.data.datasets[1].data = visible
    .filter(e => e.dbm !== null && e.dbm !== undefined)
    .map(e => ({ x: e.timestamp, y: e.dbm }));
  _signalChart.update();
}

function pushSignalPoint(signal, timestamp) {
  // Avoid duplicates (same timestamp already in history from the initial bulk fetch)
  if (_signalHistoryLastTs && timestamp <= _signalHistoryLastTs) return;
  _signalHistory.push({
    timestamp,
    percent: signal.percent || 0,
    dbm:     signal.dbm,
    rssi:    signal.rssi,
    quality: signal.quality,
  });
  _signalHistoryLastTs = timestamp;
  // Prune to 24 h in memory
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  while (_signalHistory.length > 0 && new Date(_signalHistory[0].timestamp).getTime() < cutoff) {
    _signalHistory.shift();
  }
  _buildChartData();
}

async function fetchSignalHistory() {
  try {
    const r = await fetch(`${API}/api/signal_history`);
    if (!r.ok) throw new Error(r.statusText);
    const { history } = await r.json();
    _signalHistory = history || [];
    if (_signalHistory.length > 0) {
      _signalHistoryLastTs = _signalHistory[_signalHistory.length - 1].timestamp;
    }
    _buildChartData();
  } catch (err) { console.warn('fetchSignalHistory error:', err); }
}

// Time-range selector
document.getElementById('signalRanges').addEventListener('click', e => {
  const btn = e.target.closest('.lf-btn');
  if (!btn) return;
  document.querySelectorAll('#signalRanges .lf-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _signalRangeMinutes = parseInt(btn.dataset.minutes, 10);
  _buildChartData();
});

/* ─── SMS rendering ──────────────────────────────────────────────────────── */
function smsItemHtml(msg, idx) {
  const isUnread = (msg.status || '').toUpperCase().includes('UNREAD');
  const sender   = msg.sender || 'Unknown';
  const initial  = sender.replace(/[^a-zA-Z0-9]/g, '').charAt(0).toUpperCase() || '?';
  const ts       = fmtDateTime(msg.timestamp);
  const newBadge = isUnread ? '<span class="new-badge">NEW</span>' : '';

  return `
    <div class="sms-item ${isUnread ? 'unread' : 'read'}">
      <div class="sms-avatar">${initial}</div>
      <div class="sms-meta">
        <div class="d-flex align-items-center gap-1">
          <span class="sms-sender">${escHtml(sender)}</span>${newBadge}
        </div>
        <div class="sms-time">${escHtml(ts)}</div>
        <div class="sms-body">${escHtml(msg.message || '')}</div>
      </div>
      <div class="sms-actions">
        <button class="icon-btn btn-del-sms" data-idx="${idx}" title="Delete message" style="border-color:rgba(239,68,68,.3);color:#ef4444">
          <i class="bi bi-trash3"></i>
        </button>
      </div>
    </div>`;
}

function renderSms(smsList) {
  _smsList = smsList;
  const count = smsList.length;

  // Badge on tab
  document.getElementById('smsBadge').textContent = count;

  // ── Full SMS tab ──
  const container = document.getElementById('smsContainer');
  if (!count) {
    container.innerHTML = `<div class="empty-state py-5" id="smsEmpty">
      <i class="bi bi-inbox display-5"></i><p>No messages in inbox</p></div>`;
  } else {
    container.innerHTML = smsList.map((m, i) => smsItemHtml(m, i)).join('');
    container.querySelectorAll('.btn-del-sms').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        deleteSms(parseInt(btn.dataset.idx, 10));
      });
    });
  }
}

async function deleteSms(idx) {
  try {
    const r = await fetch(`${API}/api/sms/${idx}`, { method: 'DELETE' });
    if (r.ok) { showToast('SMS deleted', 'success'); await fetchSms(); }
    else        showToast('Failed to delete SMS', 'danger');
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

async function clearAllSms() {
  if (!confirm('Delete all SMS messages?')) return;
  try {
    const r = await fetch(`${API}/api/sms`, { method: 'DELETE' });
    if (r.ok) { showToast('All SMS cleared', 'success'); await fetchSms(); }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

/* ─── Log rendering ──────────────────────────────────────────────────────── */
function logEntryHtml(entry) {
  const ts    = fmtTime(entry.timestamp);
  const level = (entry.level || 'INFO').toUpperCase();
  return `
    <div class="log-entry log-${level}" data-level="${level}">
      <span class="log-ts">${escHtml(ts)}</span>
      <span class="log-level">[${level}]</span>
      <span class="log-msg">${escHtml(entry.message || '')}</span>
    </div>`;
}

function applyLogFilter() {
  document.querySelectorAll('#logContainer .log-entry').forEach(el => {
    const lv = el.dataset.level;
    el.classList.toggle('hidden', _logFilter !== 'ALL' && lv !== _logFilter);
  });
}

function renderLog(logs) {
  _logList = logs;

  // Warning badge on tab
  const hasError = logs.some(l => (l.level || '').toUpperCase() === 'ERROR');
  const logBadge = document.getElementById('logBadge');
  logBadge.classList.toggle('d-none', !hasError);

  // ── Full log tab ──
  const container = document.getElementById('logContainer');
  if (!logs.length) {
    container.innerHTML = `<div class="empty-state py-5" id="logEmpty">
      <i class="bi bi-terminal display-5"></i><p>No log entries</p></div>`;
  } else {
    // Newest first
    container.innerHTML = [...logs].reverse().map(logEntryHtml).join('');
    applyLogFilter();
  }
}

async function clearLog() {
  try {
    await fetch(`${API}/api/logs`, { method: 'DELETE' });
    renderLog([]);
    showToast('Log cleared', 'info');
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

/* ─── Data fetching ──────────────────────────────────────────────────────── */
async function fetchStatus() {
  try {
    const r = await fetch(`${API}/api/status`);
    if (!r.ok) throw new Error(r.statusText);
    updateStatus(await r.json());
  } catch (err) { console.warn('fetchStatus error:', err); }
}

async function fetchSms() {
  try {
    const r = await fetch(`${API}/api/sms`);
    if (!r.ok) throw new Error(r.statusText);
    const { sms } = await r.json();
    renderSms(sms || []);
  } catch (err) { console.warn('fetchSms error:', err); }
}

async function fetchLogs() {
  try {
    const r = await fetch(`${API}/api/logs`);
    if (!r.ok) throw new Error(r.statusText);
    const { logs } = await r.json();
    renderLog(logs || []);
  } catch (err) { console.warn('fetchLogs error:', err); }
}

async function refreshAll() {
  await Promise.all([fetchStatus(), fetchSms(), fetchLogs()]);
}

/* ─── Countdown & auto-refresh ───────────────────────────────────────────── */
function resetCountdown() {
  countdown = REFRESH_INTERVAL;
  updateRing(countdown);
}

function tickCountdown() {
  countdown -= 1;
  if (countdown <= 0) {
    resetCountdown();
    refreshAll();
  } else {
    updateRing(countdown);
  }
}

/* ─── Button wiring ──────────────────────────────────────────────────────── */
document.getElementById('btnRefresh').addEventListener('click', async () => {
  resetCountdown();
  try { await fetch(`${API}/api/refresh`, { method: 'POST' }); } catch (_) { /* ignore */ }
  await refreshAll();
  showToast('Refreshed', 'success');
});

document.getElementById('btnClearSms').addEventListener('click', clearAllSms);
document.getElementById('btnClearLog').addEventListener('click', clearLog);

// Log filter buttons
document.getElementById('logFilters').addEventListener('click', e => {
  const btn = e.target.closest('.lf-btn');
  if (!btn) return;
  document.querySelectorAll('.lf-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  _logFilter = btn.dataset.level;
  applyLogFilter();
});

/* ─── Network selection ──────────────────────────────────────────────────── */
let _currentNetwork = {};

function networkStatusClass(status) {
  return { current: 'net-current', available: 'net-available', forbidden: 'net-forbidden' }[status] || 'net-unknown';
}

function networkTechBadge(tech) {
  if (!tech || tech === 'Unknown') return '';
  return `<span class="net-tech-badge">${escHtml(tech)}</span>`;
}

function renderNetworkList(networks, current) {
  _currentNetwork = current || {};

  const info = document.getElementById('currentNetworkInfo');
  const nameEl = document.getElementById('currentNetworkName');
  const modeEl = document.getElementById('currentNetworkMode');

  if (current && current.operator) {
    nameEl.textContent = current.operator;
    modeEl.textContent = current.mode === 'auto' ? 'Auto' : 'Manual';
    modeEl.className = 'network-mode-badge ms-2' + (current.mode === 'auto' ? ' net-mode-auto' : ' net-mode-manual');
    info.classList.remove('d-none');
  } else {
    info.classList.add('d-none');
  }

  const list = document.getElementById('networkList');
  const empty = document.getElementById('networkScanEmpty');

  if (!networks.length) {
    list.classList.add('d-none');
    list.innerHTML = '';
    empty.innerHTML = `<i class="bi bi-broadcast display-5"></i><p>No networks found.</p>`;
    empty.classList.remove('d-none');
    return;
  }

  empty.classList.add('d-none');

  // Build list: Auto first, then networks sorted by status (current first, then available, then others)
  const order = { current: 0, available: 1, unknown: 2, forbidden: 3 };
  const sorted = [...networks].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));

  const autoSelected = _currentNetwork.mode === 'auto';

  let html = `
    <div class="network-item ${autoSelected ? 'net-selected' : ''}" data-mode="auto">
      <div class="net-name">
        <i class="bi bi-arrow-repeat me-1 opacity-75"></i>
        <strong>Auto</strong>
        <span class="net-sub text-muted ms-1">– automatic selection</span>
      </div>
      <button class="net-select-btn ${autoSelected ? 'net-select-btn--active' : ''}"
              data-mode="auto" ${autoSelected ? 'disabled' : ''}>
        ${autoSelected ? '<i class="bi bi-check2"></i> Selected' : 'Select'}
      </button>
    </div>`;

  for (const net of sorted) {
    const isCurrent = net.status === 'current';
    const isForbidden = net.status === 'forbidden';
    const statusCls = networkStatusClass(net.status);
    html += `
      <div class="network-item ${isCurrent ? 'net-selected' : ''} ${statusCls}" data-mode="manual" data-numeric="${escHtml(net.numeric)}">
        <div class="net-name">
          <strong>${escHtml(net.long_name || net.short_name || net.numeric)}</strong>
          ${net.short_name && net.short_name !== net.long_name ? `<span class="net-sub text-muted ms-1">(${escHtml(net.short_name)})</span>` : ''}
          ${networkTechBadge(net.tech)}
          <span class="net-status-badge ${statusCls}">${escHtml(net.status)}</span>
        </div>
        <button class="net-select-btn ${isCurrent ? 'net-select-btn--active' : ''}"
                data-mode="manual" data-numeric="${escHtml(net.numeric)}"
                ${isCurrent || isForbidden ? 'disabled' : ''}>
          ${isCurrent ? '<i class="bi bi-check2"></i> Selected' : isForbidden ? 'Forbidden' : 'Select'}
        </button>
      </div>`;
  }

  list.innerHTML = html;
  list.classList.remove('d-none');

  list.querySelectorAll('.net-select-btn:not([disabled])').forEach(btn => {
    btn.addEventListener('click', () => selectNetwork(btn.dataset.mode, btn.dataset.numeric));
  });
}

async function scanNetworks() {
  const btn = document.getElementById('btnScanNetworks');
  const spinner = document.getElementById('networkScanSpinner');
  const empty = document.getElementById('networkScanEmpty');
  const list = document.getElementById('networkList');

  btn.disabled = true;
  spinner.classList.remove('d-none');
  empty.classList.add('d-none');
  list.classList.add('d-none');

  try {
    const r = await fetch(`${API}/api/networks`);
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      showToast('Scan failed: ' + (body.error || r.statusText), 'danger');
      empty.innerHTML = `<i class="bi bi-broadcast display-5"></i><p>Scan failed.</p>`;
      empty.classList.remove('d-none');
      return;
    }
    const { networks, current } = await r.json();
    renderNetworkList(networks || [], current || {});
    showToast(`Found ${(networks || []).length} network(s)`, 'success');
  } catch (err) {
    showToast('Network error: ' + err.message, 'danger');
    empty.innerHTML = `<i class="bi bi-broadcast display-5"></i><p>Scan failed.</p>`;
    empty.classList.remove('d-none');
  } finally {
    spinner.classList.add('d-none');
    btn.disabled = false;
  }
}

async function selectNetwork(mode, numeric) {
  const body = mode === 'auto' ? { mode: 'auto' } : { mode: 'manual', numeric };
  try {
    const r = await fetch(`${API}/api/networks/select`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.ok) {
      showToast(mode === 'auto' ? 'Switched to automatic selection' : `Selected network ${numeric}`, 'success');
      // Re-scan to reflect the new selection
      await scanNetworks();
    } else {
      const data = await r.json().catch(() => ({}));
      showToast('Selection failed: ' + (data.error || r.statusText), 'danger');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'danger');
  }
}

document.getElementById('btnScanNetworks').addEventListener('click', scanNetworks);

/* ─── App settings ───────────────────────────────────────────────────────── */
async function fetchSettings() {
  try {
    const r = await fetch(`${API}/api/settings`);
    if (!r.ok) throw new Error(r.statusText);
    const { settings } = await r.json();
    const toggle = document.getElementById('toggleAutoDeleteSim');
    if (toggle) toggle.checked = !!settings.auto_delete_from_sim;

    // Telegram settings
    const tgToggle = document.getElementById('toggleTelegramEnabled');
    const tgToken  = document.getElementById('inputTelegramToken');
    const tgChat   = document.getElementById('inputTelegramChatId');
    if (tgToggle) tgToggle.checked = !!settings.telegram_enabled;
    if (tgToken)  tgToken.value   = settings.telegram_bot_token  || '';
    if (tgChat)   tgChat.value    = settings.telegram_chat_id    || '';

    // Email settings
    const emToggle   = document.getElementById('toggleEmailEnabled');
    const emHost     = document.getElementById('inputEmailSmtpHost');
    const emPort     = document.getElementById('inputEmailSmtpPort');
    const emTls      = document.getElementById('toggleEmailUseTls');
    const emProtocol = document.getElementById('selectEmailProtocol');
    const emUser     = document.getElementById('inputEmailUsername');
    const emPass     = document.getElementById('inputEmailPassword');
    const emFrom     = document.getElementById('inputEmailFrom');
    const emTo       = document.getElementById('inputEmailTo');
    const emSubject  = document.getElementById('inputEmailSubject');
    if (emToggle)   emToggle.checked   = !!settings.email_enabled;
    if (emHost)     emHost.value       = settings.email_smtp_host  || '';
    if (emPort)     emPort.value       = settings.email_smtp_port  ?? EMAIL_DEFAULT_PORT;
    if (emTls)      emTls.checked      = settings.email_use_tls !== false;
    if (emProtocol) emProtocol.value   = settings.email_protocol   || 'starttls';
    if (emUser)     emUser.value       = settings.email_username   || '';
    if (emPass)     emPass.value       = settings.email_password   || '';
    if (emFrom)     emFrom.value       = settings.email_from       || '';
    if (emTo)       emTo.value         = settings.email_to         || '';
    if (emSubject)  emSubject.value    = settings.email_subject    || '';

    // Raw log setting
    const rawToggle = document.getElementById('toggleRawLogEnabled');
    if (rawToggle) rawToggle.checked = !!settings.raw_log_enabled;
    if (settings.raw_log_enabled) await refreshRawLogStats();

    // GatewayAPI settings
    const gwToggle    = document.getElementById('toggleGatewayapiEnabled');
    const gwToken     = document.getElementById('inputGatewayapiToken');
    const gwSender    = document.getElementById('inputGatewayapiSender');
    const gwRecipient = document.getElementById('inputGatewayapiRecipient');
    if (gwToggle)    gwToggle.checked  = !!settings.gatewayapi_enabled;
    if (gwToken)     gwToken.value     = settings.gatewayapi_token     || '';
    if (gwSender)    gwSender.value    = settings.gatewayapi_sender    || '';
    if (gwRecipient) gwRecipient.value = settings.gatewayapi_recipient || '';
  } catch (err) { console.warn('fetchSettings error:', err); }
}

async function saveAutoDeleteSetting(enabled) {
  try {
    const r = await fetch(`${API}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_delete_from_sim: enabled }),
    });
    if (r.ok) {
      showToast(
        enabled ? 'Auto-delete from SIM enabled' : 'Auto-delete from SIM disabled',
        'success'
      );
    } else {
      showToast('Failed to save setting', 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

document.getElementById('toggleAutoDeleteSim').addEventListener('change', function () {
  saveAutoDeleteSetting(this.checked);
});

/* ─── Telegram forwarding settings ──────────────────────────────────────── */
async function saveTelegramSettings() {
  const enabled = document.getElementById('toggleTelegramEnabled').checked;
  const token   = (document.getElementById('inputTelegramToken').value  || '').trim();
  const chatId  = (document.getElementById('inputTelegramChatId').value || '').trim();
  try {
    const r = await fetch(`${API}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        telegram_enabled:   enabled,
        telegram_bot_token: token,
        telegram_chat_id:   chatId,
      }),
    });
    if (r.ok) {
      showToast('Telegram settings saved', 'success');
    } else {
      showToast('Failed to save Telegram settings', 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

async function testTelegramSettings() {
  const token  = (document.getElementById('inputTelegramToken').value  || '').trim();
  const chatId = (document.getElementById('inputTelegramChatId').value || '').trim();
  const btn    = document.getElementById('btnTestTelegram');
  btn.disabled = true;
  try {
    const r = await fetch(`${API}/api/settings/test_telegram`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ telegram_bot_token: token, telegram_chat_id: chatId }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok && data.success) {
      showToast('Test message sent! Check your Telegram chat.', 'success');
    } else {
      showToast('Test failed: ' + (data.error || r.statusText), 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
  finally { btn.disabled = false; }
}

document.getElementById('btnSaveTelegram').addEventListener('click', saveTelegramSettings);
document.getElementById('btnTestTelegram').addEventListener('click', testTelegramSettings);
document.getElementById('toggleTelegramEnabled').addEventListener('change', saveTelegramSettings);

/* ─── Email forwarding settings ──────────────────────────────────────────── */
async function saveEmailSettings() {
  const enabled  = document.getElementById('toggleEmailEnabled').checked;
  const host     = (document.getElementById('inputEmailSmtpHost').value  || '').trim();
  const port     = parseInt(document.getElementById('inputEmailSmtpPort').value || EMAIL_DEFAULT_PORT, 10);
  const useTls   = document.getElementById('toggleEmailUseTls').checked;
  const protocol = document.getElementById('selectEmailProtocol').value || 'starttls';
  const username = (document.getElementById('inputEmailUsername').value  || '').trim();
  const password = (document.getElementById('inputEmailPassword').value  || '');
  const from     = (document.getElementById('inputEmailFrom').value      || '').trim();
  const to       = (document.getElementById('inputEmailTo').value        || '').trim();
  const subject  = (document.getElementById('inputEmailSubject').value   || '').trim();
  try {
    const r = await fetch(`${API}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email_enabled:   enabled,
        email_smtp_host: host,
        email_smtp_port: port,
        email_use_tls:   useTls,
        email_protocol:  protocol,
        email_username:  username,
        email_password:  password,
        email_from:      from,
        email_to:        to,
        email_subject:   subject,
      }),
    });
    if (r.ok) {
      showToast('Email settings saved', 'success');
    } else {
      showToast('Failed to save email settings', 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

async function testEmailSettings() {
  const host     = (document.getElementById('inputEmailSmtpHost').value  || '').trim();
  const port     = parseInt(document.getElementById('inputEmailSmtpPort').value || EMAIL_DEFAULT_PORT, 10);
  const useTls   = document.getElementById('toggleEmailUseTls').checked;
  const protocol = document.getElementById('selectEmailProtocol').value || 'starttls';
  const username = (document.getElementById('inputEmailUsername').value  || '').trim();
  const password = (document.getElementById('inputEmailPassword').value  || '');
  const from     = (document.getElementById('inputEmailFrom').value      || '').trim();
  const to       = (document.getElementById('inputEmailTo').value        || '').trim();
  const subject  = (document.getElementById('inputEmailSubject').value   || '').trim();
  const btn = document.getElementById('btnTestEmail');
  btn.disabled = true;
  try {
    const r = await fetch(`${API}/api/settings/test_email`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        email_smtp_host: host,
        email_smtp_port: port,
        email_use_tls:   useTls,
        email_protocol:  protocol,
        email_username:  username,
        email_password:  password,
        email_from:      from,
        email_to:        to,
        email_subject:   subject,
      }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok && data.success) {
      showToast('Test email sent! Check your inbox.', 'success');
    } else {
      showToast('Test failed: ' + (data.error || r.statusText), 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
  finally { btn.disabled = false; }
}

document.getElementById('btnSaveEmail').addEventListener('click', saveEmailSettings);
document.getElementById('btnTestEmail').addEventListener('click', testEmailSettings);
document.getElementById('toggleEmailEnabled').addEventListener('change', saveEmailSettings);

/* ─── GatewayAPI forwarding settings ────────────────────────────────────── */
async function saveGatewayapiSettings() {
  const enabled   = document.getElementById('toggleGatewayapiEnabled').checked;
  const token     = (document.getElementById('inputGatewayapiToken').value     || '').trim();
  const sender    = (document.getElementById('inputGatewayapiSender').value    || '').trim();
  const recipient = (document.getElementById('inputGatewayapiRecipient').value || '').trim();
  try {
    const r = await fetch(`${API}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gatewayapi_enabled:   enabled,
        gatewayapi_token:     token,
        gatewayapi_sender:    sender,
        gatewayapi_recipient: recipient,
      }),
    });
    if (r.ok) {
      showToast('GatewayAPI settings saved', 'success');
    } else {
      showToast('Failed to save GatewayAPI settings', 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

async function testGatewayapiSettings() {
  const token     = (document.getElementById('inputGatewayapiToken').value     || '').trim();
  const sender    = (document.getElementById('inputGatewayapiSender').value    || '').trim();
  const recipient = (document.getElementById('inputGatewayapiRecipient').value || '').trim();
  const btn       = document.getElementById('btnTestGatewayapi');
  btn.disabled = true;
  try {
    const r = await fetch(`${API}/api/settings/test_gatewayapi`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ gatewayapi_token: token, gatewayapi_sender: sender, gatewayapi_recipient: recipient }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok && data.success) {
      showToast('Test SMS sent! Check the destination phone.', 'success');
    } else {
      showToast('Test failed: ' + (data.error || r.statusText), 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
  finally { btn.disabled = false; }
}

document.getElementById('btnSaveGatewayapi').addEventListener('click', saveGatewayapiSettings);
document.getElementById('btnTestGatewayapi').addEventListener('click', testGatewayapiSettings);
document.getElementById('toggleGatewayapiEnabled').addEventListener('change', saveGatewayapiSettings);

/* ─── Raw Modem Log ──────────────────────────────────────────────────────── */
async function saveRawLogSetting(enabled) {
  try {
    const r = await fetch(`${API}/api/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ raw_log_enabled: enabled }),
    });
    if (r.ok) {
      showToast(
        enabled ? 'Raw modem logging enabled' : 'Raw modem logging disabled',
        'success'
      );
      if (enabled) await refreshRawLogStats();
    } else {
      showToast('Failed to save setting', 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

async function refreshRawLogStats() {
  try {
    const r = await fetch(`${API}/api/raw_log`);
    if (!r.ok) return;
    const { count } = await r.json();
    const stats = document.getElementById('rawLogStats');
    const countEl = document.getElementById('rawLogCount');
    if (stats) stats.classList.toggle('d-none', count === 0);
    if (countEl) countEl.textContent = count;
  } catch (err) { console.warn('refreshRawLogStats error:', err); }
}

async function exportRawLog() {
  try {
    const r = await fetch(`${API}/api/raw_log/export`);
    if (!r.ok) { showToast('Export failed', 'danger'); return; }
    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    const match = cd.match(/filename=([^\s;]+)/);
    const filename = match ? match[1] : 'raw_modem_log.json';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast('Log exported', 'success');
  } catch (err) { showToast('Export error: ' + err.message, 'danger'); }
}

async function clearRawLog() {
  if (!confirm('Clear the entire raw modem log? This cannot be undone.')) return;
  try {
    const r = await fetch(`${API}/api/raw_log`, { method: 'DELETE' });
    if (r.ok) {
      showToast('Raw modem log cleared', 'success');
      const stats = document.getElementById('rawLogStats');
      if (stats) stats.classList.add('d-none');
    } else {
      showToast('Failed to clear log', 'danger');
    }
  } catch (err) { showToast('Network error: ' + err.message, 'danger'); }
}

document.getElementById('toggleRawLogEnabled').addEventListener('change', function () {
  saveRawLogSetting(this.checked);
});
document.getElementById('btnExportRawLog').addEventListener('click', exportRawLog);
document.getElementById('btnClearRawLog').addEventListener('click', clearRawLog);

/* ─── Bootstrap ──────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initSignalChart();
  updateRing(REFRESH_INTERVAL);
  fetchSignalHistory().then(() => refreshAll());
  fetchSettings();
  setInterval(tickCountdown, 1000);
});
