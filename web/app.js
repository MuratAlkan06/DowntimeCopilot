// Downtime Copilot — static PWA shell. No build step; ES modules only.
import { isOffline, net, onNetChange, setSimulatedOffline } from './js/api.js';
import { mount, h, fmtTime, fmtAgo } from './js/dom.js';
import { store, set, on, ROLES, STALE_MINUTES } from './js/state.js';
import { loadPatients, goToDowntime, loadStatus, printAllPackets, refreshRelativeTimes, refreshOnReconnect } from './js/packets.js';
import { initDuring, flushQueue } from './js/during.js';
import { initChecklist, loadChecklist } from './js/checklists.js';
import { initRecovery, enterRecovery } from './js/recovery.js';
import { initReadiness, loadReadiness } from './js/readiness.js';

const $ = (id) => document.getElementById(id);
const PHASES = ['downtime', 'during', 'recovery'];

// ---------------------------------------------------------------- phase navigation

function setPhase(phase) {
  if (!PHASES.includes(phase)) phase = 'downtime';
  set('phase', phase);
  document.body.dataset.phase = phase;
  document.querySelectorAll('.phase-tab').forEach((t) => {
    const active = t.dataset.phase === phase;
    t.setAttribute('aria-current', active ? 'step' : 'false');
  });
  document.querySelectorAll('.view').forEach((v) => { v.hidden = v.dataset.view !== phase; });
  if (phase === 'downtime') loadPatients();
  if (phase === 'recovery') enterRecovery();
}

// ---------------------------------------------------------------- banners

function renderOffline() {
  const off = isOffline();
  $('offline-banner').hidden = !off;
  $('offline-detail').textContent = net.simulated ? '(simulated) Cached packets, capture queue and navigation keep working.'
    : !net.browserOnline ? 'This device has no network. Cached packets and capture queue keep working.'
      : 'The server is unreachable. Cached packets and capture queue keep working.';
  const btn = $('sim-offline');
  btn.setAttribute('aria-pressed', String(net.simulated));
  btn.textContent = net.simulated ? 'Go back online' : 'Simulate offline';
  document.body.classList.toggle('is-offline', off);
}

function renderEhrBanner() {
  const el = $('ehr-banner');
  if (store.downtimeStartedAt && !store.restoredAt) {
    el.hidden = false;
    el.className = 'banner banner-crit';
    mount(el, h('strong', {}, 'EHR unreachable.'), ` Downtime procedures in effect since ${fmtTime(store.downtimeStartedAt, { unit: true })}. Document on paper.`);
  } else if (store.restoredAt) {
    el.hidden = false;
    el.className = 'banner banner-ok';
    mount(el, h('strong', {}, 'Systems restored.'), ` ${fmtTime(store.restoredAt, { unit: true })} — backload paper records and review discrepancies.`);
  } else {
    el.hidden = true;
  }
}

function renderSnapshot() {
  const el = $('snapshot-banner');
  if (!store.snapshotTime) {
    el.className = 'banner banner-info';
    mount(el, 'No snapshot received yet on this device.');
    return;
  }
  // Age on the unit (simulated) clock from /status, the same clock R-STALE-01 uses; never Date.now().
  const t = new Date(store.snapshotTime);
  const now = store.unitNow ? new Date(store.unitNow) : null;
  const ageMs = now && !isNaN(now) ? Math.max(0, now.getTime() - t.getTime()) : null;
  const stale = ageMs != null && ageMs > STALE_MINUTES * 60000;
  el.className = `banner ${stale ? 'banner-warn' : 'banner-info'}`;
  mount(el,
    stale ? h('span', { class: 'rule-badge' }, 'R-STALE-01') : null,
    h('strong', {}, `Last snapshot ${fmtTime(store.snapshotTime, { unit: true })}`),
    ageMs != null ? ` · ${fmtAgo(ageMs)}` : '',
    stale ? ` — older than ${STALE_MINUTES} min. Confirm with the patient and paper record before acting.` : '');
}

// ---------------------------------------------------------------- role switcher

function initRoles() {
  const sel = $('role-select');
  mount(sel, ROLES.map((r) => h('option', { value: r.id }, r.label)));
  sel.value = store.role;
  sel.addEventListener('change', () => set('role', sel.value));
}

// ---------------------------------------------------------------- boot

function init() {
  initRoles();
  initDuring();
  initChecklist();
  initRecovery();
  initReadiness();

  document.querySelectorAll('.phase-tab').forEach((t) => t.addEventListener('click', () => setPhase(t.dataset.phase)));
  $('btn-go-downtime').addEventListener('click', goToDowntime);
  $('btn-print-all').addEventListener('click', printAllPackets);
  $('btn-refresh-patients').addEventListener('click', loadPatients);
  $('sim-offline').addEventListener('click', () => setSimulatedOffline(!net.simulated));

  onNetChange((offline, info) => {
    renderOffline();
    if (!info.wentOnline) return;
    flushQueue();
    // Redraw what was drawn from the cache while offline (cached-copy notes, offline messages).
    refreshOnReconnect();
    loadChecklist();
    loadReadiness();
    if (store.phase === 'recovery') enterRecovery();
  });
  on('snapshotTime', renderSnapshot);
  // A newer snapshot (scheduler pull) re-evaluated the rules server-side: refresh list badges + readiness.
  on('snapshotTime', () => { if (store.phase === 'downtime') loadPatients(); });
  on('unitNow', renderSnapshot);
  on('unitNow', refreshRelativeTimes);
  on('downtimeStartedAt', renderEhrBanner);
  on('restoredAt', renderEhrBanner);

  renderOffline();
  renderEhrBanner();
  renderSnapshot();
  loadStatus();
  setInterval(loadStatus, 30000); // refreshes the unit clock; the banner re-renders on 'unitNow'
  setPhase(store.phase);

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js')
      .then((reg) => console.info('[dc] service worker registered, scope:', reg.scope))
      .catch((err) => console.warn('[dc] service worker registration failed:', err));
  }
}

init();
