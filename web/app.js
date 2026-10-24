// Downtime Copilot — static PWA shell. No build step; ES modules only.
import { isOffline, net, onNetChange, setSimulatedOffline } from './js/api.js';
import { mount, h, fmtTime, fmtAgo } from './js/dom.js';
import { store, set, on, ROLES, STALE_MINUTES } from './js/state.js';
import { loadPatients, goToDowntime, loadStatus, printAllPackets, refreshRelativeTimes, refreshOnReconnect } from './js/packets.js';
import { initDuring, flushQueue } from './js/during.js';
import { initChecklist, loadChecklist } from './js/checklists.js';
import { initRecovery, enterRecovery } from './js/recovery.js';

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- routing

function getCurrentRoute() {
  // Determine which view should be visible based on app state
  if (store.downtimeStartedAt && !store.restoredAt) {
    return 'downtime';
  }
  return 'normal';
}

function getDowntimeRoute() {
  // Get the current tab in downtime mode (snapshots or capture)
  return store.downtimeRoute || 'snapshots';
}

function setRoute(route) {
  // Navigate to the specified route (normal or downtime)
  if (route === 'downtime') {
    set('route', 'downtime');
  } else {
    set('route', 'normal');
  }
  render();
}

function setDowntimeRoute(route) {
  // Navigate to a tab within downtime mode
  if (['snapshots', 'capture'].includes(route)) {
    set('downtimeRoute', route);
    render();
  }
}

function autoRoute() {
  // Auto-navigate based on app state
  const currentRoute = getCurrentRoute();
  const stored = store.route;

  // If in downtime and systems restored, auto-exit to normal
  if (stored === 'downtime' && currentRoute === 'normal' && store.restoredAt) {
    setRoute('normal');
    return;
  }

  // If not in downtime but downtime starts, auto-enter
  if (stored !== 'downtime' && currentRoute === 'downtime') {
    setRoute('downtime');
    return;
  }
}

function render() {
  const route = store.route || 'normal';
  const auto = getCurrentRoute();

  // Show the appropriate main view
  document.querySelectorAll('.view[data-route]').forEach((v) => {
    v.hidden = !(v.dataset.route === route);
  });

  // One patient list + packet panel, moved into whichever view is showing.
  const split = $('patient-split');
  const slot = route === 'downtime' ? $('snapshot-slot') : $('recovery-section');
  if (route === 'downtime') slot.appendChild(split);
  else slot.parentNode.insertBefore(split, slot);
  loadPatients();

  // If in downtime, show the downtime navigation and tabs
  const downtimeNav = $('downtime-nav');
  if (downtimeNav) {
    downtimeNav.hidden = route !== 'downtime';
  }

  // Show/hide downtime tabs
  if (route === 'downtime') {
    const downtimeRoute = getDowntimeRoute();
    document.querySelectorAll('.route-view').forEach((v) => {
      const routeVal = v.id.replace('route-', '');
      v.hidden = routeVal !== downtimeRoute;
    });

    document.querySelectorAll('.downtime-tab').forEach((t) => {
      const tabRoute = t.dataset.route;
      const active = tabRoute === downtimeRoute;
      t.setAttribute('aria-current', active ? 'page' : 'false');
    });
  }

  // Show/hide recovery section (only in normal view after restoration)
  const recoverySection = $('recovery-section');
  if (recoverySection) {
    recoverySection.hidden = route !== 'normal' || !store.restoredAt;
  }

  // Update normal view status text
  const normalStatus = $('normal-status');
  if (normalStatus && route === 'normal') {
    if (store.restoredAt) {
      normalStatus.textContent = 'Systems restored. Review backlog and discrepancies.';
    } else {
      normalStatus.textContent = 'Normal patient list and system status.';
    }
  }
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

  // Downtime navigation tabs
  document.querySelectorAll('.downtime-tab').forEach((t) => {
    t.addEventListener('click', () => setDowntimeRoute(t.dataset.route));
  });

  $('btn-go-downtime').addEventListener('click', goToDowntime);
  $('btn-print-all').addEventListener('click', printAllPackets);
  $('btn-refresh-packets').addEventListener('click', loadPatients);
  $('btn-refresh-patients').addEventListener('click', loadPatients);
  $('sim-offline').addEventListener('click', () => setSimulatedOffline(!net.simulated));

  onNetChange((offline, info) => {
    renderOffline();
    if (!info.wentOnline) return;
    flushQueue();
    refreshOnReconnect();
    loadChecklist();
  });
  on('snapshotTime', renderSnapshot);
  on('snapshotTime', () => { if (store.route === 'downtime') loadPatients(); });
  on('unitNow', renderSnapshot);
  on('unitNow', refreshRelativeTimes);
  on('downtimeStartedAt', () => {
    renderEhrBanner();
    autoRoute();
    render();
  });
  on('restoredAt', () => {
    renderEhrBanner();
    autoRoute();
    render();
    enterRecovery();
  });

  renderOffline();
  renderEhrBanner();
  renderSnapshot();
  loadStatus();
  setInterval(loadStatus, 30000);
  autoRoute();
  render();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js')
      .then((reg) => console.info('[dc] service worker registered, scope:', reg.scope))
      .catch((err) => console.warn('[dc] service worker registration failed:', err));
  }
}

init();
