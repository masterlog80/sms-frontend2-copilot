/* ─── USB Modem Dashboard – app.js ─────────────────────────────────────── */
'use strict';

const API = '';          // same origin
const REFRESH_INTERVAL = 5; // seconds

let countdown = REFRESH_INTERVAL;
let refreshTimer = null;

/* ─── Theme ──────────────────────────────────────────────────────────────── */
const html = document.documentElement;
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

// Restore saved preference (default: dark)
applyTheme(localStorage.getItem(THEME_KEY) || 'dark');

/* ─── Toast helper ───────────────────────────────────────────────────────── */
function showToast(message, type = 'info') {
  const area = document.getElementById('toastArea');
  const colours = { success: 'bg-success', danger: 'bg-danger', info: 'bg-primary', warning: 'bg-warning' };
  const el = document.createElement('div');
  el.className = `toast align-items-center text-white ${colours[type] || 'bg-primary'} border-0`;
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

/* ─── Status / Signal / Memory ──────────────────────────────────────────── */
function qualityClass(quality) {
  const map = {
    'Excellent': 'excellent',
    'Good':      'good',
    'Fair':      'fair',
    'Poor':      'poor',
    'Very Poor': 'verypoor',
  };
  return map[quality] || 'unknown';
}

function updateStatus(data) {
  // Connection badge
  const badge = document.getElementById('connBadge');
  if (data.modem_connected) {
    badge.className = 'badge rounded-pill bg-success';
    badge.innerHTML = '<i class="bi bi-circle-fill me-1"></i>Connected';
  } else {
    badge.className = 'badge rounded-pill bg-danger';
    badge.innerHTML = '<i class="bi bi-circle-fill me-1"></i>Disconnected';
  }

  // Meta
  document.getElementById('deviceLabel').textContent = data.device || '–';
  document.getElementById('lastUpdated').textContent = data.last_updated
    ? new Date(data.last_updated).toLocaleTimeString()
    : '–';

  // Signal
  const sig = data.signal || {};
  const pct = sig.percent || 0;
  const qClass = qualityClass(sig.quality || '');

  document.getElementById('sigPercent').textContent = pct;
  document.getElementById('sigRssi').textContent = sig.rssi !== undefined ? sig.rssi : '–';
  document.getElementById('sigDbm').textContent = sig.dbm !== null && sig.dbm !== undefined ? `${sig.dbm} dBm` : '–';
  document.getElementById('sigBer').textContent = sig.ber !== undefined ? sig.ber : '–';

  const qBadge = document.getElementById('sigQuality');
  qBadge.textContent = sig.quality || '–';
  qBadge.className = `badge fs-6 mb-1 badge-${qClass}`;

  const sigBar = document.getElementById('sigBar');
  sigBar.style.width = pct + '%';
  sigBar.className = `progress-bar sig-${qClass}`;

  // Memory
  const mem = data.memory || {};
  document.getElementById('memUsed').textContent = mem.used !== undefined ? mem.used : '–';
  document.getElementById('memTotal').textContent = mem.total !== undefined ? mem.total : '–';
  document.getElementById('memUsed2').textContent = mem.used !== undefined ? mem.used : '–';
  document.getElementById('memFree').textContent = mem.free !== undefined ? mem.free : '–';
  document.getElementById('memTotal2').textContent = mem.total !== undefined ? mem.total : '–';

  const memPct = mem.percent_used || 0;
  const memBar = document.getElementById('memBar');
  memBar.style.width = memPct + '%';
  memBar.className = 'progress-bar ' + (memPct >= 90 ? 'mem-crit' : memPct >= 70 ? 'mem-warn' : 'mem-ok');

  // Modem info
  const info = data.modem_info || {};
  document.getElementById('infoManuf').textContent = info.manufacturer || '–';
  document.getElementById('infoModel').textContent = info.model || '–';
  document.getElementById('infoImei').textContent = info.imei || '–';
  document.getElementById('infoNet').textContent = info.network_status || '–';
}

/* ─── SMS ────────────────────────────────────────────────────────────────── */
function renderSms(smsList) {
  const container = document.getElementById('smsContainer');
  const empty = document.getElementById('smsEmpty');
  document.getElementById('smsBadge').textContent = smsList.length;

  if (!smsList.length) {
    container.innerHTML = '';
    container.appendChild(empty);
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  // Build list
  const frag = document.createDocumentFragment();
  smsList.forEach((msg, idx) => {
    const isUnread = (msg.status || '').toUpperCase().includes('UNREAD');
    const sender = msg.sender || 'Unknown';
    const initial = sender.replace(/[^a-zA-Z0-9]/g, '').charAt(0).toUpperCase() || '?';
    const ts = msg.timestamp
      ? (() => { try { return new Date(msg.timestamp).toLocaleString(); } catch { return msg.timestamp; } })()
      : '';

    const item = document.createElement('div');
    item.className = `sms-item ${isUnread ? 'unread' : 'read'}`;
    item.dataset.idx = idx;
    item.innerHTML = `
      <div class="sms-avatar">${initial}</div>
      <div class="sms-meta">
        <div class="d-flex align-items-center gap-2">
          <span class="sms-sender">${escHtml(sender)}</span>
          ${isUnread ? '<span class="badge bg-primary" style="font-size:.65rem">NEW</span>' : ''}
        </div>
        <div class="sms-time">${escHtml(ts)}</div>
        <div class="sms-body">${escHtml(msg.message || '')}</div>
      </div>
      <div class="sms-actions">
        <button class="btn btn-sm btn-outline-danger btn-del-sms" data-idx="${idx}" title="Delete">
          <i class="bi bi-trash3"></i>
        </button>
      </div>`;
    frag.appendChild(item);
  });

  container.innerHTML = '';
  container.appendChild(frag);

  // Bind delete buttons
  container.querySelectorAll('.btn-del-sms').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const i = parseInt(btn.dataset.idx, 10);
      await deleteSms(i);
    });
  });
}

async function deleteSms(idx) {
  try {
    const r = await fetch(`${API}/api/sms/${idx}`, { method: 'DELETE' });
    if (r.ok) {
      showToast('SMS deleted', 'success');
      await fetchSms();
    } else {
      showToast('Failed to delete SMS', 'danger');
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'danger');
  }
}

async function clearAllSms() {
  if (!confirm('Delete all SMS messages?')) return;
  try {
    const r = await fetch(`${API}/api/sms`, { method: 'DELETE' });
    if (r.ok) {
      showToast('All SMS cleared', 'success');
      await fetchSms();
    }
  } catch (err) {
    showToast('Network error: ' + err.message, 'danger');
  }
}

/* ─── Event Log ──────────────────────────────────────────────────────────── */
function renderLog(logs) {
  const container = document.getElementById('logContainer');
  const empty = document.getElementById('logEmpty');

  if (!logs.length) {
    container.innerHTML = '';
    container.appendChild(empty);
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';

  const frag = document.createDocumentFragment();
  // Show newest first
  [...logs].reverse().forEach(entry => {
    const ts = entry.timestamp
      ? (() => { try { return new Date(entry.timestamp).toLocaleTimeString(); } catch { return entry.timestamp; } })()
      : '';
    const level = (entry.level || 'INFO').toUpperCase();
    const div = document.createElement('div');
    div.className = `log-entry log-${level}`;
    div.innerHTML = `
      <span class="log-ts">${escHtml(ts)}</span>
      <span class="log-level">[${level}]</span>
      <span class="log-msg">${escHtml(entry.message || '')}</span>`;
    frag.appendChild(div);
  });

  container.innerHTML = '';
  container.appendChild(frag);
}

async function clearLog() {
  try {
    await fetch(`${API}/api/logs`, { method: 'DELETE' });
    renderLog([]);
    showToast('Log cleared', 'info');
  } catch (err) {
    showToast('Network error: ' + err.message, 'danger');
  }
}

/* ─── Data fetching ──────────────────────────────────────────────────────── */
async function fetchStatus() {
  try {
    const r = await fetch(`${API}/api/status`);
    if (!r.ok) throw new Error(r.statusText);
    updateStatus(await r.json());
  } catch (err) {
    console.warn('fetchStatus error:', err);
  }
}

async function fetchSms() {
  try {
    const r = await fetch(`${API}/api/sms`);
    if (!r.ok) throw new Error(r.statusText);
    const { sms } = await r.json();
    renderSms(sms || []);
  } catch (err) {
    console.warn('fetchSms error:', err);
  }
}

async function fetchLogs() {
  try {
    const r = await fetch(`${API}/api/logs`);
    if (!r.ok) throw new Error(r.statusText);
    const { logs } = await r.json();
    renderLog(logs || []);
  } catch (err) {
    console.warn('fetchLogs error:', err);
  }
}

async function refreshAll() {
  await Promise.all([fetchStatus(), fetchSms(), fetchLogs()]);
}

/* ─── Manual refresh button ─────────────────────────────────────────────── */
document.getElementById('btnRefresh').addEventListener('click', async () => {
  resetCountdown();
  try {
    await fetch(`${API}/api/refresh`, { method: 'POST' });
  } catch (_) { /* ignore */ }
  await refreshAll();
  showToast('Refreshed', 'success');
});

/* ─── Clear buttons ──────────────────────────────────────────────────────── */
document.getElementById('btnClearSms').addEventListener('click', clearAllSms);
document.getElementById('btnClearLog').addEventListener('click', clearLog);

/* ─── Auto-refresh countdown ─────────────────────────────────────────────── */
function resetCountdown() {
  countdown = REFRESH_INTERVAL;
  document.getElementById('countdown').textContent = countdown;
}

function tickCountdown() {
  countdown -= 1;
  if (countdown <= 0) {
    resetCountdown();
    refreshAll();
  } else {
    document.getElementById('countdown').textContent = countdown;
  }
}

/* ─── Utility ────────────────────────────────────────────────────────────── */
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ─── Bootstrap ──────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  refreshAll();
  refreshTimer = setInterval(tickCountdown, 1000);
});
