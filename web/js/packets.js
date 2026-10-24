// Phase 1: Go to downtime -> patient list -> SBAR packet view (printable).
import { api } from './api.js';
import { h, mount, stateBox, chip, fmtTime, fmtDateTime, asList, cachedNote, usd } from './dom.js';
import { store, set, emit, noteSnapshot } from './state.js';

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

export async function loadPatients() {
  const list = $('patient-list');
  if (!store.patients.size) mount(list, stateBox('loading', 'Loading patients…'));
  if (!selectedId && !$('packet').hasChildNodes()) {
    mount($('packet'), stateBox('empty', 'Packets appear here. Tap "Go to downtime", then choose a patient.'));
  }
  const r = await api('/patients');
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
  noteSnapshot(patients.map((p) => p.snapshot_time));
  if (!patients.length) {
    mount(list, stateBox('empty', 'No patients in the snapshot yet. Run the simulator feed, then refresh.'));
    return;
  }
  mount(list, cachedNote(r), h('ul', { class: 'patient-list', role: 'list' }, [...patients].sort(bedSort).map(patientRow)));
  if (!selectedId) mount($('packet'), stateBox('empty', store.downtimeStartedAt
    ? 'Select a patient to open their downtime packet.'
    : 'Tap "Go to downtime" to generate packets, or select a patient to view the last packet.'));
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
  noteSnapshot(r.data?.snapshot_time);
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
  const title = patient ? `Bed ${patient.bed} · ${patient.name}` : `Patient ${pk.patient_id}`;
  const g = pk.guardrail || {};
  return h('div', { class: 'packet' },
    h('header', { class: 'packet-head' },
      h('div', {},
        h('h2', { class: 'packet-title' }, title),
        h('p', { class: 'packet-meta' },
          patient ? `MRN ${patient.mrn} · ${patient.age ?? ''} ${patient.sex ?? ''} · ${patient.code_status ?? ''}` : '',
        ),
        h('p', { class: 'packet-meta' },
          `Snapshot ${fmtDateTime(pk.snapshot_time)} · Generated ${fmtTime(pk.generated_at)} · `,
          h('span', { class: 'mono' }, pk.model_id || 'model ?'),
        ),
        cachedNote(res),
      ),
      h('div', { class: 'packet-actions no-print' },
        g.status ? chip(`Guardrail: ${g.status}`, /pass|ok|none|allowed/i.test(g.status) ? 'ok' : 'warn', (g.reasons || []).join('; ')) : null,
        h('button', { class: 'btn btn-secondary', type: 'button', onclick: () => window.print() }, 'Print packet'),
      ),
    ),
    (pk.alerts || []).length
      ? h('section', { class: 'alerts', 'aria-label': 'Safety alerts' },
        pk.alerts.map((a) => h('div', { class: `alert alert-${ruleSeverity(a.rule_id)}` },
          h('span', { class: 'rule-badge' }, a.rule_id), h('span', {}, a.text))))
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
      h('span', { class: 'print-only' }, ` · Printed ${new Date().toLocaleString()} · Synthetic data, not for clinical use.`),
    ),
  );
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
  const d = r.data && typeof r.data === 'object' ? r.data : {};
  set('downtimeStartedAt', d.started_at || d.downtime_started_at || new Date().toISOString());
  set('restoredAt', null);
  noteSnapshot(d.snapshot_time);
  const n = asList(d, 'packets').length || d.packet_count || d.packets_generated;
  mount(status, h('span', {}, `Downtime started ${fmtTime(store.downtimeStartedAt)}${n ? ` · ${n} packets generated` : ''}. Packets are cached for offline use.`));
  await loadPatients();
  prefetchPackets();
}

/** Warm the service-worker cache so packets survive the network being cut. */
async function prefetchPackets() {
  const ids = [...store.patients.keys()];
  for (let i = 0; i < ids.length; i += 4) {
    await Promise.all(ids.slice(i, i + 4).map((id) => api(`/patients/${encodeURIComponent(id)}/packet`)));
  }
}
