// Role checklist (During phase). Items are parsed from the downtime policy server-side (GET /checklists);
// check marks live in localStorage only (per role, per outage), never on the backend.
// Keyed by the server's random outage id, not the outage start time: every demo reseed restarts the
// unit clock at 02:45, so start times repeat across rehearsals. With no outage yet, marks stay in memory.
import { api } from './api.js';
import { h, mount, stateBox, fmtTime } from './dom.js';
import { store, on, roleLabel } from './state.js';

const $ = (id) => document.getElementById(id);
const key = () => `dc.checklist.${store.role}.${store.outageId}`;
const ephemeral = new Map(); // role -> Set of checked ids while there is no outage id (never persisted)
const loadChecked = () => {
  if (!store.outageId) return new Set(ephemeral.get(store.role));
  try { return new Set(JSON.parse(localStorage.getItem(key())) || []); } catch { return new Set(); }
};
const saveChecked = (s) => {
  if (!store.outageId) { ephemeral.set(store.role, new Set(s)); return; }
  try { localStorage.setItem(key(), JSON.stringify([...s])); } catch { /* private mode */ }
};

let items = [];

function renderHeader() {
  const done = items.filter((i) => loadChecked().has(i.id)).length;
  mount($('checklist-meta'),
    h('strong', {}, roleLabel(store.role)),
    ` · ${store.unitNow ? fmtTime(store.unitNow, { unit: true }) : 'unit time not received yet'}`,
    items.length ? ` · ${done} of ${items.length} done` : '');
}

function toggle(id, on) {
  const s = loadChecked();
  if (on) s.add(id); else s.delete(id);
  saveChecked(s);
  renderHeader();
}

function render() {
  const checked = loadChecked();
  mount($('checklist-items'), items.length
    ? h('ul', { class: 'checklist' }, items.map((it) => {
      const id = `chk-${it.id.replace(/[^a-z0-9]/gi, '-')}`;
      return h('li', { class: 'checklist-item' },
        h('input', { type: 'checkbox', id, checked: checked.has(it.id), onchange: (e) => toggle(it.id, e.target.checked) }),
        h('label', { for: id }, h('span', { class: 'section-badge' }, `§ ${it.section_id}`), ' ', it.text));
    }))
    : h('p', { class: 'muted' }, 'No checklist items for this role in the downtime policy.'));
  renderHeader();
}

export async function loadChecklist() {
  const role = store.role;
  mount($('checklist-items'), stateBox('loading', 'Loading your checklist…'));
  const r = await api(`/checklists?role=${encodeURIComponent(role)}`);
  if (role !== store.role) return; // the role changed while this was loading
  if (!r.ok) {
    items = [];
    mount($('checklist-items'), stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Offline and this checklist is not cached on this device yet. Use the paper downtime binder, section 2.'
      : `Couldn't load the checklist (${r.error}).`));
    renderHeader();
    return;
  }
  items = r.data?.items || [];
  render();
}

export function initChecklist() {
  on('role', loadChecklist);
  on('unitNow', renderHeader);
  on('outageId', () => { ephemeral.clear(); render(); }); // a new outage starts a fresh set of check marks
  loadChecklist();
}
