// Supervisor command center (S14). One screen from GET /command-center (server, deterministic,
// app/command_center.py): per unit, four panels — overdue high-alert doses, open discrepancies, backload queue,
// critical labs awaiting. Nothing is computed here: counts, items and times are the server's, stamped in unit
// time. The payload is a list of units, so several units render as several blocks with the same four panels.
// Refreshes every 30 s while the view is open and the tab is visible.
import { api } from './api.js';
import { h, mount, stateBox, fmtTime, cachedNote } from './dom.js';
import { GROUPS } from './state.js';

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 30000;
const SEVERITIES = [['critical', 'crit', 'Critical'], ['warning', 'warn', 'Warning'], ['info', 'info', 'Info']];
// Roles that see the command center tab (supervisor-facing; the charge nurse is the supervisor's contact).
export const COMMAND_ROLES = ['charge_nurse'];

let timer = null;
let loaded = false;

const empty = (text) => h('p', { class: 'muted cc-empty' }, text);

function panel(unit, key, title, tone, body) {
  const id = `cc-${unit.unit_id}-${key}`;
  const n = unit[key]?.count ?? 0;
  return h('section', { class: 'cc-panel', 'aria-labelledby': id },
    h('div', { class: 'cc-panel-head' },
      h('h3', { id, class: 'cc-panel-title' }, title),
      h('span', { class: `chip chip-${n ? tone : 'neutral'}`, 'aria-label': `${title}: ${n}` }, String(n))),
    body,
    h('p', { class: 'muted cc-asof' }, `As of ${fmtTime(unit.as_of, { unit: true })}`));
}

function overdue(unit) {
  const items = unit.overdue_high_alert?.items || [];
  return items.length ? h('ul', { class: 'cc-list', role: 'list' }, items.map((i) => h('li', { class: 'cc-item' },
    h('span', { class: 'cc-item-head' }, h('strong', {}, i.bed || i.patient_id), h('span', { class: 'ha-badge' }, 'HIGH-ALERT')),
    h('span', {}, `${i.medication} ${i.dose} ${i.route}`),
    h('span', { class: 'cc-meta' }, `Due ${fmtTime(i.next_due)} · ${i.overdue_minutes} min overdue · no administration recorded`))))
    : empty('No high-alert dose is past due without an administration record.');
}

function discrepancies(unit) {
  const d = unit.open_discrepancies || {};
  const items = d.items || [];
  if (!items.length) return empty('No open discrepancies. They appear when the EHR is restored and diffed.');
  return h('div', { class: 'cc-stack' },
    h('div', { class: 'chips' }, SEVERITIES.map(([k, tone, label]) =>
      h('span', { class: `chip chip-${d.by_severity?.[k] ? tone : 'neutral'}` }, `${label} ${d.by_severity?.[k] ?? 0}`))),
    h('ul', { class: 'cc-list', role: 'list' }, items.map((i) => {
      const tone = SEVERITIES.find(([k]) => k === i.severity)?.[1] || 'info';
      return h('li', { class: 'cc-item' },
        h('span', { class: 'cc-item-head' }, h('span', { class: `rule-badge sev-${tone}` }, i.rule_id), h('strong', {}, i.bed || i.patient_id)),
        h('span', {}, i.description));
    })));
}

function backload(unit) {
  const b = unit.backload_counts || {};
  if (!b.count) return empty('No paper records captured yet.');
  return h('div', { class: 'cc-stack' },
    h('p', {}, h('strong', {}, `${b.count} record${b.count === 1 ? '' : 's'}`), ` · ${b.needs_review ?? 0} awaiting review`),
    h('ul', { class: 'cc-list cc-groups', role: 'list' }, GROUPS.map((g) => {
      const n = b.by_group?.[g.id] ?? 0;
      return h('li', { class: `cc-group${n ? '' : ' muted'}` }, h('span', {}, g.label), h('strong', {}, String(n)));
    })));
}

function labs(unit) {
  const items = unit.critical_labs_awaiting?.items || [];
  return items.length ? h('ul', { class: 'cc-list', role: 'list' }, items.map((i) => h('li', { class: 'cc-item' },
    h('span', { class: 'cc-item-head' }, h('strong', {}, i.bed || i.patient_id), h('span', { class: 'chip chip-crit' }, 'Critical')),
    h('span', {}, i.lab))))
    : empty('No critical labs awaiting results.');
}

export function render(data, res) {
  const units = data?.units || [];
  mount($('command-status'), units.length
    ? `Updated ${fmtTime(data.generated_at, { unit: true })} · refreshes every 30 seconds.`
    : 'No unit reported.');
  mount($('command-units'), cachedNote(res), units.map((u) => h('section', { class: 'cc-unit', 'aria-labelledby': `cc-${u.unit_id}` },
    h('h2', { id: `cc-${u.unit_id}`, class: 'cc-unit-title' }, u.name || u.unit_id),
    h('div', { class: 'cc-panels' },
      panel(u, 'overdue_high_alert', 'Overdue high-alert doses', 'crit', overdue(u)),
      panel(u, 'open_discrepancies', 'Open discrepancies', 'warn', discrepancies(u)),
      panel(u, 'backload_counts', 'Backload queue', 'info', backload(u)),
      panel(u, 'critical_labs_awaiting', 'Critical labs awaiting', 'crit', labs(u))))));
}

export async function loadCommand() {
  if (!loaded) mount($('command-units'), stateBox('loading', 'Loading the command center…'));
  const r = await api('/command-center');
  if (r.ok) {
    loaded = true;
    render(r.data, r);
  } else if (!loaded) {
    mount($('command-units'), stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Offline — the command center needs the server.' : `Couldn't load the command center (${r.error}).`,
    { label: 'Retry', onClick: loadCommand }));
  }
}

export function enterCommand() {
  loadCommand();
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) loadCommand(); }, REFRESH_MS);
}

export function leaveCommand() {
  clearInterval(timer);
  timer = null;
}

export function initCommand() {
  $('btn-refresh-command').addEventListener('click', loadCommand);
}
