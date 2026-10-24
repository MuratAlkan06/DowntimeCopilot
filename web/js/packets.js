// Phase 1: Go to downtime -> patient list -> SBAR packet view (printable).
import { api } from './api.js';
import { h, mount, stateBox, chip, fmtTime, fmtDateTime, asList, cachedNote, usd } from './dom.js';
import { store, set, emit, noteSnapshot, STALE_MINUTES } from './state.js';
import { dueAt, dueRelative, ageMinutes } from './rules.js';
import { precacheChecklists } from './checklists.js';

const $ = (id) => document.getElementById(id);
let selectedId = null;

const SBAR = [
  ['situation', 'S', 'Situation'],
  ['background', 'B', 'Background'],
  ['assessment', 'A', 'Assessment'],
  ['recommendation', 'R', 'Recommendation'],
];

export function ruleSeverity(ruleId = '') {
  if (/^R-(ALG|DUE|REC-01|REC-03)/.test(ruleId)) return 'crit';
  if (/^R-(DUP|STALE|REC|VO)/.test(ruleId)) return 'warn';
  return 'info';
}

const bedSort = (a, b) => String(a.bed).localeCompare(String(b.bed), undefined, { numeric: true });

const DUE = 'R-DUE-01';
const STALE = 'R-STALE-01';

// ---------------------------------------------------------------- rule hits (GET /rules/current)
// Hits are evaluated server-side on every snapshot pull and at downtime start. Their text holds absolute
// unit times only; "in N min" / "N min old" are rendered here from the unit clock and refreshed on every
// /status poll (refreshRelativeTimes), never frozen.

const relDue = (iso) => h('span', { class: 'rel-time', 'data-due': iso }, dueRelative({ due_at: iso }, store.unitNow));
const ageText = (iso) => {
  const m = ageMinutes(iso, store.unitNow);
  return m == null ? 'age unknown' : `${m} min old`;
};

function ruleBadges(id) {
  const byRule = new Map();
  (store.ruleHits?.[id] || []).forEach((x) => byRule.set(x.rule_id, [...(byRule.get(x.rule_id) || []), x]));
  if (!byRule.size) return null;
  return h('span', { class: 'rule-badges' }, [...byRule].map(([rid, hits]) => {
    const text = hits.map((x) => x.text).join('\n');
    const due = rid === DUE ? hits.map((x) => x.due_at).filter(Boolean).sort()[0] : null;
    return h('span', { class: 'rule-hit' },
      h('span', { class: `rule-badge sev-${ruleSeverity(rid)}`, title: text, 'aria-label': `${rid}: ${text}` }, rid),
      due ? h('span', { class: 'rule-note' }, `due ${fmtTime(due)} · `, relDue(due)) : null);
  }));
}

function renderReadiness() {
  const el = $('readiness');
  if (!el) return;
  if (!store.ruleHits) { el.hidden = true; return; }
  const p = (pid) => store.patients.get(pid) || { bed: pid };
  const stale = Object.entries(store.ruleHits)
    .flatMap(([pid, hits]) => hits.filter((x) => x.rule_id === STALE).map((x) => ({ pid, at: x.snapshot_time })))
    .sort((a, b) => bedSort(p(a.pid), p(b.pid)));
  el.hidden = false;
  if (!stale.length) {
    el.className = 'banner banner-ok readiness';
    mount(el, h('strong', {}, 'Readiness:'), ` every patient snapshot is under ${STALE_MINUTES} min old.`);
    return;
  }
  el.className = 'banner banner-warn readiness';
  mount(el,
    h('span', { class: 'rule-badge' }, STALE),
    h('strong', {}, `Readiness: ${stale.length} stale snapshot${stale.length > 1 ? 's' : ''}`),
    ` — older than ${STALE_MINUTES} min. Reconcile with the patient and paper chart.`,
    h('ul', { class: 'readiness-list' }, stale.map(({ pid, at }) => h('li', {},
      h('strong', {}, p(pid).bed), ` ${p(pid).name || ''} · snapshot ${fmtTime(at, { unit: true })} · `,
      h('span', { class: 'rel-age', 'data-age-from': at }, ageText(at))))));
}

/** Re-render every relative time on screen from the current unit clock (called on each /status poll). */
export function refreshRelativeTimes() {
  document.querySelectorAll('[data-due]').forEach((el) => { el.textContent = dueRelative({ due_at: el.dataset.due }, store.unitNow); });
  document.querySelectorAll('[data-age-from]').forEach((el) => { el.textContent = ageText(el.dataset.ageFrom); });
}

let inflight = null;
/** Patient list + rule hits. Concurrent calls (refresh button, new snapshot seen) share one load. */
export function loadPatients() {
  inflight ||= fetchPatients().finally(() => { inflight = null; });
  return inflight;
}

async function fetchPatients() {
  const list = $('patient-list');
  if (!store.patients.size) mount(list, stateBox('loading', 'Loading patients…'));
  if (!selectedId && !$('packet').hasChildNodes()) {
    mount($('packet'), stateBox('empty', 'Packets appear here. Tap "Declare downtime", then choose a patient.'));
  }
  const [r, rules] = await Promise.all([api('/patients'), api('/rules/current'), loadStatus()]);
  if (rules.ok && rules.data?.patients) store.ruleHits = rules.data.patients;
  if (!r.ok && store.patients.size) {
    // Keep the list already on screen; just say it may be stale.
    mount(list, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — showing the list already on this device.' : `Refresh failed (${r.error}).`),
      h('ul', { class: 'patient-list', role: 'list' }, [...store.patients.values()].sort(bedSort).map(patientRow)));
    return;
  }
  if (!r.ok) {
    mount(list, stateBox(r.offline ? 'offline' : 'error',
      r.offline ? 'Offline and no cached patient list on this device yet.' : `Couldn't load patients (${r.error}).`,
      { label: 'Retry', onClick: loadPatients }));
    return;
  }
  const patients = asList(r.data, 'patients', 'items');
  store.patients.clear();
  patients.forEach((p) => store.patients.set(p.patient_id, p));
  emit('patients', patients);
  renderReadiness();
  if (!r.offline) noteSnapshot(patients.map((p) => p.snapshot_time));
  if (!patients.length) {
    mount(list, stateBox('empty', 'No patients in the snapshot yet. Run the simulator feed, then refresh.'));
    return;
  }
  mount(list, cachedNote(r), h('ul', { class: 'patient-list', role: 'list' }, [...patients].sort(bedSort).map(patientRow)));
  if (!selectedId) mount($('packet'), stateBox('empty', store.downtimeStartedAt
    ? 'Select a patient to open their downtime packet.'
    : 'Tap "Declare downtime" to generate packets, or select a patient to view the last packet.'));
}

function patientRow(p) {
  const highAlert = (p.medications || []).filter((m) => m.high_alert).length;
  const allergies = p.allergies || [];
  return h('li', {},
    h('button', {
      class: 'patient-row', type: 'button', 'data-id': p.patient_id,
      'aria-current': p.patient_id === selectedId ? 'true' : null,
      onclick: () => openPacket(p.patient_id),
    },
    h('span', { class: 'bed' }, p.bed ?? '—'),
    h('span', { class: 'patient-main' },
      h('span', { class: 'patient-name' }, p.name),
      h('span', { class: 'patient-sub' }, [p.age != null ? `${p.age}${p.sex ? ' ' + p.sex : ''}` : null, p.admitting_dx].filter(Boolean).join(' · ')),
      h('span', { class: 'chips' },
        allergies.length ? chip(`Allergy: ${allergies.map((a) => a.substance).join(', ')}`, 'crit') : chip('NKA', 'neutral'),
        p.code_status ? chip(p.code_status, /dnr|dni/i.test(p.code_status) ? 'warn' : 'neutral') : null,
        p.isolation ? chip(`Isolation: ${p.isolation}`, 'warn') : null,
        highAlert ? chip(`${highAlert} high-alert`, 'crit') : null,
      ),
      ruleBadges(p.patient_id),
    )),
  );
}

export async function openPacket(id) {
  selectedId = id;
  document.querySelectorAll('.patient-row').forEach((b) => b.toggleAttribute('aria-current', b.dataset.id === id));
  const pane = $('packet');
  mount(pane, stateBox('loading', 'Loading packet…'));
  if (window.matchMedia('(max-width: 760px)').matches) pane.scrollIntoView({ behavior: 'smooth', block: 'start' });
  const r = await api(`/patients/${encodeURIComponent(id)}/packet`);
  if (selectedId !== id) return; // user moved on
  if (!r.ok) {
    mount(pane, stateBox(r.offline ? 'offline' : 'error',
      r.offline ? 'This packet is not cached on this device. Use the printed packet or the BCA PC.' : `Couldn't load packet (${r.error}).`,
      { label: 'Retry', onClick: () => openPacket(id) }));
    return;
  }
  // No noteSnapshot here: one patient's snapshot can be older than the unit's (R-STALE-01).
  mount(pane, renderPacket(r.data, store.patients.get(id), r));
}

function highAlertMatcher(patient) {
  const meds = patient?.medications || [];
  const idx = new Set(meds.map((m, i) => (m.high_alert ? i : -1)).filter((i) => i >= 0));
  const names = meds.filter((m) => m.high_alert).map((m) => String(m.name).toLowerCase());
  return (item) => {
    const path = item.source_path || '';
    const m = /medications[[.](\d+)/.exec(path);
    if (m && idx.has(Number(m[1]))) return true;
    if (/high[- ]alert/i.test(item.text || '')) return true;
    return path.includes('medications') && names.some((n) => n && String(item.text).toLowerCase().includes(n));
  };
}

function renderPacket(pk, patient, res) {
  const isHigh = highAlertMatcher(patient);
  const title = patient ? `${patient.bed} · ${patient.name}` : `Patient ${pk.patient_id}`;
  const g = pk.guardrail || {};
  return h('div', { class: 'packet' },
    h('header', { class: 'packet-head' },
      h('div', {},
        h('h2', { class: 'packet-title' }, title),
        h('p', { class: 'packet-meta' },
          patient ? `MRN ${patient.mrn} · ${patient.age ?? ''} ${patient.sex ?? ''} · ${patient.code_status ?? ''}` : '',
        ),
        h('p', { class: 'packet-meta' },
          `Snapshot ${fmtDateTime(pk.snapshot_time)} · Generated ${fmtTime(pk.generated_at, { unit: true })} · `,
          h('span', { class: 'mono' }, pk.model_id || 'model ?'),
        ),
        cachedNote(res),
      ),
      h('div', { class: 'packet-actions no-print' },
        g.status ? chip(`Guardrail: ${g.status}`, /pass|ok|none|allowed/i.test(g.status) ? 'ok' : 'warn', (g.reasons || []).join('; ')) : null,
        h('button', { class: 'btn btn-secondary', type: 'button', onclick: printNow }, 'Print packet'),
      ),
    ),
    (pk.alerts || []).length
      ? h('section', { class: 'alerts', 'aria-label': 'Safety alerts' },
        pk.alerts.map((a) => {
          const due = a.rule_id === DUE ? dueAt(a, store.unitNow) : null; // "due HH:MM" in the text
          return h('div', { class: `alert alert-${ruleSeverity(a.rule_id)}` },
            h('span', { class: 'rule-badge' }, a.rule_id),
            h('span', {}, a.text, due && !isNaN(due) ? [' (', relDue(due.toISOString()), ')'] : null));
        }))
      : h('p', { class: 'muted' }, 'No rule alerts for this patient.'),
    h('div', { class: 'sbar' }, SBAR.map(([key, letter, label]) => {
      const items = pk.sbar?.[key] || [];
      return h('section', { class: 'sbar-section' },
        h('h3', { class: 'sbar-title' }, h('span', { class: 'sbar-letter', 'aria-hidden': 'true' }, letter), label),
        items.length
          ? h('ul', { class: 'sbar-list' }, items.map((it) => {
            const high = isHigh(it);
            return h('li', { class: `sbar-item${high ? ' high-alert' : ''}` },
              high ? h('span', { class: 'ha-badge' }, 'HIGH-ALERT') : null,
              h('span', { class: 'sbar-text' }, it.text),
              h('code', { class: 'src', title: 'Snapshot source field' }, it.source_path || 'no source'));
          }))
          : h('p', { class: 'muted' }, 'Nothing recorded.'));
    })),
    h('footer', { class: 'packet-foot' },
      `Every line cites its snapshot field. AI draft — verify before acting. `,
      pk.cost ? `Tokens ${pk.cost.tokens_in}/${pk.cost.tokens_out} · ${usd(pk.cost.usd)}` : '',
      h('span', { class: 'print-only print-stamp' }, printStamp()),
    ),
  );
}

// Print stamp uses the unit (simulated) clock from /status, the same clock as the packet header.
const printStamp = () => ` · Printed ${fmtDateTime(store.unitNow, { unit: true })} · Synthetic data, not for clinical use.`;

/** Unit clock, outage start and snapshot time from the server. Returns the status, or null if unreachable. */
export async function loadStatus() {
  const r = await api('/status');
  if (!r.ok || !r.data || typeof r.data !== 'object') return null;
  const s = r.data;
  if (s.now) set('unitNow', s.now);
  if (r.offline) return s; // cached copy: do not overwrite the snapshot time or outage markers from it
  noteSnapshot(s.snapshot_time);
  if ((s.outage_id || null) !== store.outageId) set('outageId', s.outage_id || null);
  if (s.outage_started_at && s.outage_started_at !== store.downtimeStartedAt) set('downtimeStartedAt', s.outage_started_at);
  if (s.phase === 'before' && !s.outage_started_at && (store.downtimeStartedAt || store.restoredAt)) {
    set('downtimeStartedAt', null); // server was reset (seed --fresh): clear this device's stale markers
    set('restoredAt', null);
  }
  return s;
}

async function printNow() {
  await loadStatus();
  document.querySelectorAll('.print-stamp').forEach((el) => { el.textContent = printStamp(); });
  window.print();
}

/** Every packet, one printed page per patient (print.css: .print-all .packet breaks after each). */
export async function printAllPackets() {
  const btn = $('btn-print-all');
  const status = $('downtime-status');
  btn.disabled = true;
  const r = await api('/packets');
  btn.disabled = false;
  const list = r.ok ? asList(r.data, 'packets') : [];
  if (!list.length) {
    mount(status, stateBox(r.ok ? 'empty' : (r.offline ? 'offline' : 'error'), r.ok
      ? 'No packets yet. Tap "Declare downtime" first.'
      : `Couldn't load packets to print (${r.error}).`));
    return;
  }
  const holder = $('print-all');
  mount(holder, list.map((pk) => renderPacket(pk, store.patients.get(pk.patient_id), r)));
  document.body.classList.add('printing-all');
  window.addEventListener('afterprint', () => {
    document.body.classList.remove('printing-all');
    holder.replaceChildren();
  }, { once: true });
  await printNow();
}

export async function goToDowntime() {
  const btn = $('btn-go-downtime');
  const status = $('downtime-status');
  btn.disabled = true;
  mount(status, stateBox('loading', 'Generating packets from the last good snapshot…'));
  const r = await api('/downtime/start', { method: 'POST', json: { role: store.role } });
  btn.disabled = false;
  if (!r.ok) {
    mount(status, stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Offline — showing cached packets. Generation will run when connectivity returns.'
      : `Couldn't start downtime (${r.error}).`));
    await loadPatients();
    return;
  }
  // POST /downtime/start returns the packet list; the outage start (unit clock) comes from /status,
  // falling back to the packets' generated_at (also unit clock). Never the browser clock.
  const list = asList(r.data, 'packets');
  const s = await loadStatus();
  set('downtimeStartedAt', s?.outage_started_at || list[0]?.generated_at || store.downtimeStartedAt);
  set('restoredAt', null);
  noteSnapshot(list.map((p) => p.snapshot_time));
  const n = list.length;
  mount(status, h('span', {}, `Downtime started ${fmtTime(store.downtimeStartedAt)}${n ? ` · ${n} packets generated` : ''}. Packets are cached for offline use.`));
  await loadPatients();
  prefetchPackets();
  precacheChecklists(); // all four roles, alongside the packets
}

/** Warm the service-worker cache so packets survive the network being cut. */
async function prefetchPackets() {
  const ids = [...store.patients.keys()];
  for (let i = 0; i < ids.length; i += 4) {
    await Promise.all(ids.slice(i, i + 4).map((id) => api(`/patients/${encodeURIComponent(id)}/packet`)));
  }
}
