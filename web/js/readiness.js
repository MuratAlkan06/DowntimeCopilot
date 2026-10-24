// Readiness drill (S13). The score comes from GET /readiness (server, deterministic, app/readiness.py);
// "Run drill" posts /readiness/drill, which runs on a throwaway server-side store and never touches the live
// unit or the simulator's EHR state. Nothing is computed here: this view only renders the server's numbers.
import { api } from './api.js';
import { h, mount, stateBox, fmtTime } from './dom.js';

const $ = (id) => document.getElementById(id);

const LABELS = {
  snapshot_freshness: 'Snapshot freshness',
  packet_coverage: 'Packet coverage',
  checklist_ack: 'Checklist acknowledgment',
  forms_templates: 'Forms templates',
};

const tone = (score) => (score >= 90 ? 'ok' : score >= 60 ? 'warn' : 'crit');

function render(data) {
  const el = $('drill-result');
  if (!data || data.score == null) {
    mount(el, h('p', { class: 'muted' }, 'No drill run yet. A drill rehearses an outage on a throwaway copy: the live unit and the EHR are not touched.'));
    return;
  }
  const comps = Object.entries(data.components || {});
  const steps = data.drill?.steps || [];
  mount(el,
    h('div', { class: 'drill-score' },
      h('span', { class: `drill-total tone-${tone(data.score)}`, 'aria-label': `Readiness ${data.score} of 100` }, String(data.score)),
      h('span', { class: 'muted' }, ' / 100'),
      data.last_drill_at ? h('span', { class: 'muted drill-when' }, `Drill at ${fmtTime(data.last_drill_at, { unit: true })}`) : null),
    data.summary ? h('p', { class: 'drill-summary' }, data.summary) : null,
    h('ul', { class: 'drill-components', role: 'list' }, comps.map(([name, c]) => h('li', { class: 'drill-component' },
      h('span', { class: 'drill-component-head' },
        h('strong', {}, LABELS[name] || name),
        h('span', { class: `chip chip-${tone(c.score)}` }, `${c.score}`),
        h('span', { class: 'muted drill-weight' }, `weight ${c.weight}`)),
      h('span', { class: 'muted drill-detail' }, c.detail)))),
    steps.length ? h('p', { class: 'muted drill-steps' },
      steps.map((s) => `${s.ok ? '✓' : '✗'} ${s.name}: ${s.detail}`).join(' · ')) : null);
}

export async function loadReadiness() {
  const r = await api('/readiness');
  if (r.ok) render(r.data);
  else if (!$('drill-result').hasChildNodes()) {
    mount($('drill-result'), stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Offline — the readiness score needs the server.' : `Couldn't load readiness (${r.error}).`));
  }
}

async function runDrill() {
  const btn = $('btn-run-drill');
  const status = $('drill-status');
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  btn.textContent = 'Running drill…';
  mount(status, stateBox('loading', 'Rehearsing an outage on a throwaway copy…'));
  const r = await api('/readiness/drill', { method: 'POST' });
  btn.disabled = false;
  btn.removeAttribute('aria-busy');
  btn.textContent = 'Run drill';
  if (!r.ok) {
    mount(status, stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Offline — a drill needs the server and the BCA feed.' : `Drill failed (${r.error}). Nothing changed.`));
    return;
  }
  mount(status);
  render(r.data);
}

export function initReadiness() {
  $('btn-run-drill').addEventListener('click', runDrill);
  loadReadiness();
}
