/* ═══════════════════════════════════════════════════════════════════════════
   USB Modem Dashboard – app.js
════════════════════════════════════════════════════════════════════════════ */
'use strict';

const API = '';
const REFRESH_INTERVAL = 5;   // seconds
let countdown = REFRESH_INTERVAL;
let _smsList  = [];
let _logList  = [];
let _logFilter = 'ALL';

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

/* ─── SVG gauge ──────────────────────────────────────────────────────────── */
// The gauge arc path "M10,65 A50,50 0 0,1 110,65" has circumference ≈ 157.
const GAUGE_LEN = 157;
const GAUGE_COLORS = {
  excellent: '#22c55e',
  good:      '#84cc16',
  fair:      '#eab308',
  poor:      '#f97316',
  verypoor:  '#ef4444',
  unknown:   '#6b7280',
};

function updateGauge(pct, quality) {
  const el = document.getElementById('gaugeSig');
  if (!el) return;
  const filled = (pct / 100) * GAUGE_LEN;
  el.setAttribute('stroke-dashoffset', GAUGE_LEN - filled);
  el.style.stroke = GAUGE_COLORS[qualityClass(quality)] || GAUGE_COLORS.unknown;
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

  document.getElementById('sigPercent').textContent = pct;
  document.getElementById('sigRssi').textContent = sig.rssi !== undefined ? sig.rssi : '–';
  document.getElementById('sigDbm').textContent  = sig.dbm !== null && sig.dbm !== undefined ? `${sig.dbm} dBm` : '–';
  document.getElementById('sigBer').textContent  = sig.ber !== undefined ? sig.ber : '–';

  const qBadge = document.getElementById('sigQuality');
  qBadge.textContent = sig.quality || '–';
  qBadge.className   = `quality-badge quality-${qCls}`;

  updateGauge(pct, sig.quality || '');

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
  document.getElementById('infoNet').textContent   = info.network_status || '–';
}

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

/* ─── Signal history chart ───────────────────────────────────────────────── */
const QUALITY_COLORS = {
  excellent: '#22c55e',
  good:      '#84cc16',
  fair:      '#eab308',
  poor:      '#f97316',
  verypoor:  '#ef4444',
  unknown:   '#6b7280',
};

function renderSignalChart(history) {
  const canvas = document.getElementById('signalChart');
  if (!canvas) return;

  // Size the canvas to its CSS layout size
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const W = rect.width  || canvas.offsetWidth  || 600;
  const H = rect.height || canvas.offsetHeight || 260;
  canvas.width  = W * dpr;
  canvas.height = H * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);

  // Detect theme
  const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
  const colorText    = isDark ? '#e2e8f0'  : '#0f172a';
  const colorMuted   = isDark ? '#64748b'  : '#94a3b8';
  const colorGrid    = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.07)';
  const colorAreaTop = isDark ? 'rgba(99,102,241,0.25)'  : 'rgba(99,102,241,0.15)';
  const colorAreaBot = isDark ? 'rgba(99,102,241,0)'     : 'rgba(99,102,241,0)';
  const colorLine    = '#6366f1';

  // Layout
  const PAD_L = 46, PAD_R = 16, PAD_T = 16, PAD_B = 36;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  ctx.clearRect(0, 0, W, H);

  // ── No data state ──
  if (!history || history.length === 0) {
    ctx.fillStyle = colorMuted;
    ctx.font = '0.85rem sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No signal history yet – data appears after the first poll.', W / 2, H / 2);
    const meta = document.getElementById('sigHistoryMeta');
    if (meta) meta.textContent = '';
    return;
  }

  // Time window: last 30 min relative to newest point
  const now = new Date(history[history.length - 1].timestamp).getTime();
  const windowMs = 30 * 60 * 1000;
  const tMin = now - windowMs;

  // Helpers to map data -> canvas coords
  const toX = t  => PAD_L + ((t - tMin) / windowMs) * plotW;
  const toY = pct => PAD_T + plotH - (Math.max(0, Math.min(100, pct)) / 100) * plotH;

  // ── Grid & Y-axis labels ──
  ctx.save();
  ctx.strokeStyle = colorGrid;
  ctx.lineWidth   = 1;
  ctx.fillStyle   = colorMuted;
  ctx.font        = `11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
  ctx.textAlign   = 'right';
  ctx.textBaseline = 'middle';
  [0, 25, 50, 75, 100].forEach(pct => {
    const y = toY(pct);
    ctx.beginPath();
    ctx.moveTo(PAD_L, y);
    ctx.lineTo(PAD_L + plotW, y);
    ctx.stroke();
    ctx.fillText(pct + '%', PAD_L - 6, y);
  });
  ctx.restore();

  // ── X-axis time labels (every 5 min) ──
  ctx.save();
  ctx.fillStyle    = colorMuted;
  ctx.font         = `11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
  ctx.textAlign    = 'center';
  ctx.textBaseline = 'top';
  for (let minAgo = 30; minAgo >= 0; minAgo -= 5) {
    const t = now - minAgo * 60 * 1000;
    const x = toX(t);
    if (x < PAD_L - 2 || x > PAD_L + plotW + 2) continue;
    const label = minAgo === 0 ? 'now' : `-${minAgo}m`;
    ctx.fillText(label, x, PAD_T + plotH + 6);
  }
  ctx.restore();

  // ── Area fill (gradient) ──
  const grad = ctx.createLinearGradient(0, PAD_T, 0, PAD_T + plotH);
  grad.addColorStop(0, colorAreaTop);
  grad.addColorStop(1, colorAreaBot);

  ctx.save();
  ctx.beginPath();
  history.forEach((pt, i) => {
    const x = toX(new Date(pt.timestamp).getTime());
    const y = toY(pt.percent);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  // Close area down to baseline
  ctx.lineTo(toX(new Date(history[history.length - 1].timestamp).getTime()), PAD_T + plotH);
  ctx.lineTo(toX(new Date(history[0].timestamp).getTime()), PAD_T + plotH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.restore();

  // ── Line (coloured segments by quality) ──
  ctx.save();
  ctx.lineWidth   = 2.5;
  ctx.lineJoin    = 'round';
  ctx.lineCap     = 'round';
  for (let i = 1; i < history.length; i++) {
    const prev = history[i - 1];
    const curr = history[i];
    const qc   = qualityClass(curr.quality || '');
    ctx.strokeStyle = QUALITY_COLORS[qc] || colorLine;
    ctx.beginPath();
    ctx.moveTo(toX(new Date(prev.timestamp).getTime()), toY(prev.percent));
    ctx.lineTo(toX(new Date(curr.timestamp).getTime()), toY(curr.percent));
    ctx.stroke();
  }
  ctx.restore();

  // ── Dots at each sample ──
  ctx.save();
  history.forEach(pt => {
    const qc = qualityClass(pt.quality || '');
    ctx.fillStyle = QUALITY_COLORS[qc] || colorLine;
    ctx.beginPath();
    ctx.arc(
      toX(new Date(pt.timestamp).getTime()),
      toY(pt.percent),
      history.length > 60 ? 1.5 : 3,
      0, Math.PI * 2
    );
    ctx.fill();
  });
  ctx.restore();

  // ── Latest value label ──
  const latest = history[history.length - 1];
  const lx = toX(new Date(latest.timestamp).getTime());
  const ly = toY(latest.percent);
  const qc = qualityClass(latest.quality || '');
  ctx.save();
  ctx.fillStyle = QUALITY_COLORS[qc] || colorLine;
  ctx.font      = `bold 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
  ctx.textAlign = lx > PAD_L + plotW - 50 ? 'right' : 'left';
  ctx.textBaseline = ly < PAD_T + 20 ? 'top' : 'bottom';
  const labelOffset = 8;
  ctx.fillText(
    `${latest.percent}%`,
    lx + (ctx.textAlign === 'right' ? -labelOffset : labelOffset),
    ly + (ctx.textBaseline === 'top' ? labelOffset : -labelOffset)
  );
  ctx.restore();

  // ── Meta line ──
  const meta = document.getElementById('sigHistoryMeta');
  if (meta) {
    meta.textContent = `${history.length} sample${history.length !== 1 ? 's' : ''}`;
  }
}

async function fetchSignalHistory() {
  try {
    const r = await fetch(`${API}/api/signal/history`);
    if (!r.ok) throw new Error(r.statusText);
    const { history } = await r.json();
    renderSignalChart(history || []);
  } catch (err) { console.warn('fetchSignalHistory error:', err); }
}

// Re-draw chart on theme change
btnTheme.addEventListener('click', () => {
  fetchSignalHistory();
});

// Re-draw chart whenever the Signal Strength tab becomes visible
document.addEventListener('DOMContentLoaded', () => {
  const signalTabBtn = document.getElementById('tab-signal');
  if (signalTabBtn) {
    signalTabBtn.addEventListener('shown.bs.tab', fetchSignalHistory);
  }
});

// Redraw on window resize (canvas needs explicit resizing)
window.addEventListener('resize', () => {
  const pane = document.getElementById('pane-signal');
  if (pane && pane.classList.contains('show')) {
    fetchSignalHistory();
  }
});

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
  await Promise.all([fetchStatus(), fetchSms(), fetchLogs(), fetchSignalHistory()]);
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

/* ─── Bootstrap ──────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  updateRing(REFRESH_INTERVAL);
  refreshAll();
  setInterval(tickCountdown, 1000);
});
