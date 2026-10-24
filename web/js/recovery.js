// Phase 3: Systems restored -> discrepancies, backload queue by group, digest, audit, costs.
import { api } from './api.js';
import { h, mount, stateBox, chip, fmtTime, fmtDateTime, asList, cachedNote, labelize, usd } from './dom.js';
import { store, set, on, GROUPS, ROLE_GROUPS, patientLabel, roleLabel } from './state.js';
import { renderRecord } from './records.js';
import { ruleSeverity, loadStatus } from './packets.js';

const $ = (id) => document.getElementById(id);
let records = [];
let discrepancies = null; // null = not loaded yet
const SEV_KIND = { critical: 'crit', warning: 'warn', info: 'info' };

// ---------------------------------------------------------------- discrepancies

function renderDiscrepancies() {
  const el = $('discrepancies');
  if (discrepancies == null) {
    mount(el, stateBox('empty', 'Tap "Systems restored" to diff the restored EHR against the snapshot and captured records.'));
    return;
  }
  if (!discrepancies.length) { mount(el, stateBox('empty', 'No discrepancies found.')); return; }
  const order = { critical: 0, warning: 1, info: 2 };
  const sorted = [...discrepancies].sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3));
  mount(el, h('ul', { class: 'disc-list', role: 'list' }, sorted.map((d) => {
    const kind = SEV_KIND[d.severity] || ruleSeverity(d.rule_id);
    return h('li', { class: `alert alert-${kind}` },
      h('span', { class: 'rule-badge' }, d.rule_id),
      h('span', { class: 'disc-body' },
        h('strong', {}, patientLabel(d.patient_id)), ' — ', d.description,
        h('span', { class: 'disc-meta' }, `${d.severity || ''} · ${labelize(d.status || 'open')}${d.record_id ? ` · record ${d.record_id}` : ''}`)),
      h('span', { class: 'disc-actions' },
        d.status === 'open' ? h('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: (e) => setDiscStatus(d, 'acknowledged', e.target) }, 'Acknowledge') : null,
        d.status !== 'resolved' ? h('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: (e) => setDiscStatus(d, 'resolved', e.target) }, 'Resolve') : null));
  })));
}

// Charge nurse review before end of shift (policy 7.5): open -> acknowledged -> resolved. Updates the list
// syncRestored or restore() put on screen in place; never re-fetches it (the review state is in each item).
async function setDiscStatus(d, status, btn) {
  btn.disabled = true;
  const r = await api(`/discrepancies/${encodeURIComponent(d.discrepancy_id)}/status`, { method: 'POST', json: { status } });
  if (discrepancies == null) return; // a reseed or a new outage cleared the view while this was saving
  if (!r.ok) {
    btn.disabled = false;
    mount($('recovery-status'), stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — not saved. Try again when connected.' : `Not saved (${r.error}).`));
    return;
  }
  discrepancies = discrepancies.map((x) => (x.discrepancy_id === d.discrepancy_id ? r.data : x));
  renderDiscrepancies();
  mount($('recovery-status'), restoredLine()); // clears an earlier "Not saved"
  loadAudit();
}

async function restore() {
  const btn = $('btn-restore');
  btn.disabled = true;
  mount($('recovery-status'), stateBox('loading', 'Pulling the restored EHR and diffing…'));
  const r = await api('/recovery/restore', { method: 'POST', json: { role: store.role } });
  btn.disabled = false;
  if (!r.ok) {
    mount($('recovery-status'), stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Still offline — reconciliation runs when connectivity returns.' : `Restore failed (${r.error}).`));
    return;
  }
  // Set before loadStatus: its restoredAt change must not make syncRestored fetch the list again.
  discrepancies = asList(r.data, 'discrepancies', 'items');
  // Restore time is the server's unit clock (GET /status restored_at), never the browser clock.
  const s = await loadStatus();
  set('restoredAt', s?.restored_at || store.unitNow);
  renderDiscrepancies();
  mount($('recovery-status'), restoredLine());
  await loadBackload();
  loadAudit();
  loadDigest();
  loadCosts();
}

const restoredLine = () => h('span', {}, `Systems restored ${fmtTime(store.restoredAt)} · ${discrepancies.length} discrepancies flagged.`);
let idleStatus = ''; // #recovery-status as shipped, shown again when the restore state clears

/** The Recovery view follows store.restoredAt, which loadStatus hydrates from GET /status: a restore made on
 *  another device shows here as restored (its discrepancies from GET /discrepancies, review status included), and
 *  a reseed or a new outage puts the view back to "not restored". Fetches only while Recovery is open
 *  (enterRecovery re-runs it). The one discrepancy fetch path besides restore(); setDiscStatus edits in place. */
async function syncRestored() {
  if (!store.restoredAt) {
    if (discrepancies == null) return;
    discrepancies = null;
    renderDiscrepancies();
    mount($('recovery-status'), idleStatus);
    return;
  }
  if (discrepancies != null || store.phase !== 'recovery') return;
  const r = await api('/discrepancies');
  if (!r.ok || !store.restoredAt || discrepancies != null) return;
  discrepancies = asList(r.data, 'discrepancies', 'items');
  renderDiscrepancies();
  mount($('recovery-status'), restoredLine());
}

// ---------------------------------------------------------------- backload queue

function normalizeBackload(data) {
  let list = asList(data, 'records', 'items', 'queue');
  if (!list.length && data?.groups && typeof data.groups === 'object') {
    list = Object.values(data.groups).flatMap((v) => (Array.isArray(v) ? v : asList(v, 'records', 'items')));
  }
  if (!list.length && data && typeof data === 'object' && !Array.isArray(data)) {
    list = GROUPS.flatMap((g) => (Array.isArray(data[g.id]) ? data[g.id] : [])); // GET /backload: {group_id: [...]}
  }
  return list;
}

export async function loadBackload() {
  const el = $('backload');
  if (!records.length) mount(el, stateBox('loading', 'Loading backload queue…'));
  const r = await api('/backload');
  if (!r.ok) {
    mount(el, stateBox(r.offline ? 'offline' : 'error',
      r.offline ? 'Offline and the queue is not cached yet.' : `Couldn't load the backload queue (${r.error}).`,
      { label: 'Retry', onClick: loadBackload }));
    $('backload-summary').textContent = '';
    return;
  }
  records = normalizeBackload(r.data);
  const disc = asList(r.data?.discrepancies);
  if (disc.length && discrepancies == null) { discrepancies = disc; renderDiscrepancies(); }
  renderBackload(r);
}

function renderBackload(res) {
  const el = $('backload');
  const mine = new Set(ROLE_GROUPS[store.role] || []);
  const open = records.filter((x) => x.status !== 'entered' && x.status !== 'rejected').length;
  $('backload-summary').textContent = records.length ? `${records.length} records · ${open} open` : '';
  if (!records.length) {
    mount(el, stateBox('empty', 'The queue is empty. Captured paper forms appear here, grouped for backload.'));
    return;
  }
  const groups = [...GROUPS].sort((a, b) => Number(mine.has(b.id)) - Number(mine.has(a.id)));
  mount(el, cachedNote(res), groups.map((g) => {
    const items = records.filter((x) => x.backload_group === g.id);
    const isMine = mine.has(g.id);
    return h('section', { class: `bl-group${isMine ? ' mine' : ''}${items.length ? '' : ' empty'}`, 'aria-label': g.label },
      h('h3', { class: 'bl-title' }, g.label, ' ', chip(String(items.length), items.length ? 'info' : 'neutral'),
        isMine ? chip(`${roleLabel(store.role)} worklist`, 'accent') : null),
      items.length
        ? h('div', { class: 'bl-items' }, items.map((rec) => renderRecord(rec, { onChange: replaceRecord })))
        : h('p', { class: 'muted small' }, 'No records.'));
  }));
}

function replaceRecord(updated) {
  records = records.map((x) => (x.record_id === updated.record_id ? updated : x));
  const open = records.filter((x) => x.status !== 'entered' && x.status !== 'rejected').length;
  $('backload-summary').textContent = `${records.length} records · ${open} open`;
}

// ---------------------------------------------------------------- digest

export async function loadDigest() {
  const el = $('digest');
  mount(el, stateBox('loading', 'Writing the shift digest…'));
  const r = await api('/digest');
  if (!r.ok) {
    mount(el, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — digest is written when connected.' : `Couldn't load digest (${r.error}).`,
      { label: 'Retry', onClick: loadDigest }));
    return;
  }
  const d = r.data || {};
  mount(el, h('div', { class: 'digest' },
    cachedNote(r),
    h('p', { class: 'muted small' },
      d.shift_window ? `Shift ${fmtDateTime(d.shift_window.start)} – ${fmtTime(d.shift_window.end, { unit: true })} · ` : '',
      `Written ${fmtTime(d.generated_at)} · `, h('span', { class: 'mono' }, d.model_id || '')),
    h('p', { class: 'answer-text' }, d.unit_summary || 'No unit summary.'),
    (d.open_items || []).length
      ? h('div', {}, h('h3', { class: 'sub-title' }, 'Open items'), h('ul', { class: 'open-items' }, d.open_items.map((x) => h('li', {}, x))))
      : null,
    (d.per_patient || []).length
      ? h('details', { class: 'digest-patients' }, h('summary', {}, `Per-patient SBAR (${d.per_patient.length})`),
        d.per_patient.map((p) => h('div', { class: 'digest-patient' },
          h('h4', {}, patientLabel(p.patient_id)),
          ['situation', 'background', 'assessment', 'recommendation'].map((k) => (p.sbar?.[k] || []).length
            ? h('p', { class: 'small' }, h('strong', {}, `${k[0].toUpperCase()}: `), p.sbar[k].map((i) => i.text).join(' '))
            : null))))
      : null,
    d.cost ? h('p', { class: 'muted small' }, `Cost ${usd(d.cost.usd)}`) : null,
    h('p', { class: 'muted small' }, 'AI draft for handoff — the oncoming nurse confirms at bedside.'),
  ));
}

// ---------------------------------------------------------------- audit

export async function loadAudit() {
  const el = $('audit');
  const r = await api('/audit');
  if (!r.ok) {
    mount(el, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — audit is kept locally on the server and syncs later.' : `Couldn't load audit log (${r.error}).`,
      { label: 'Retry', onClick: loadAudit }));
    return;
  }
  // GET /audit is newest-first by store sequence (ORDER BY seq DESC). Keep that order: equal or
  // clamped unit-clock stamps can tie, so sorting by ts would misplace the newest rows.
  const events = asList(r.data, 'events', 'items');
  if (!events.length) { mount(el, stateBox('empty', 'No audit events yet.')); return; }
  const synced = events.filter((e) => e.synced).length;
  mount(el,
    h('p', { class: 'muted small' }, `${events.length} events · ${synced} synced to AWS · ${events.length - synced} pending sync`),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' },
      h('thead', {}, h('tr', {}, ['Time', 'Role', 'Action', 'Target', 'Sync'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, events.slice(0, 50).map((e) => h('tr', {},
        h('td', { class: 'mono' }, fmtTime(e.ts)),
        h('td', {}, roleLabel(e.actor_role)),
        h('td', {}, labelize(e.action)),
        h('td', {}, `${labelize(e.target_type)}${e.target_id ? ` · ${e.target_id}` : ''}`),
        h('td', {}, e.synced ? chip('Synced', 'ok') : chip('Pending', 'neutral'))))))));
}

// ---------------------------------------------------------------- incident report

export async function loadIncident() {
  const el = $('incident');
  const printBtn = $('btn-print-incident');
  mount(el, stateBox('loading', 'Building the incident report…'));
  const r = await api('/incident');
  if (!r.ok) {
    printBtn.disabled = true;
    mount(el, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline and no report is cached yet.' : `Couldn't build the incident report (${r.error}).`,
      { label: 'Retry', onClick: loadIncident }));
    return;
  }
  const d = r.data || {};
  const rows = asList(d.timeline);
  mount(el, h('div', { class: 'incident' },
    cachedNote(r),
    h('p', { class: 'print-only incident-title' }, 'EHR downtime incident report'),
    h('p', { class: 'muted small' },
      `Outage ${fmtTime(d.outage_started_at)} – ${d.restored_at ? fmtTime(d.restored_at, { unit: true }) : 'not restored yet'} · `,
      `Written ${fmtTime(d.generated_at)} · `, h('span', { class: 'mono' }, d.model_id || '')),
    asList(d.summary).map((p) => h('p', { class: 'answer-text' }, p)),
    h('p', { class: 'muted small' }, `Summary is an AI draft (safety check: ${labelize(d.guardrail?.status || 'unknown')}); the timeline is the audit log.`),
    rows.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 'data-table incident-table' },
        h('thead', {}, h('tr', {}, ['Time', 'Event', 'Patient', 'Detail', 'Role'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, rows.map((x) => h('tr', {},
          h('td', { class: 'mono' }, fmtTime(x.ts)),
          h('td', {}, x.event),
          h('td', {}, x.patient || '—'),
          h('td', {}, x.detail || ''),
          h('td', {}, x.role))))))
      : stateBox('empty', 'No lifecycle events yet. Declare downtime to start the timeline.'),
  ));
  printBtn.disabled = !rows.length;
}

/** One-page print of the report only (print.css: body.printing-incident). */
function printIncident() {
  document.body.classList.add('printing-incident');
  window.addEventListener('afterprint', () => document.body.classList.remove('printing-incident'), { once: true });
  window.print();
}

// ---------------------------------------------------------------- costs

function costRows(d) {
  let rows = asList(d, 'rows', 'models', 'per_model', 'by_model', 'items');
  if (!rows.length && d && typeof d === 'object') {
    const src = [d.per_model, d.models, d.by_model].find((x) => x && typeof x === 'object' && !Array.isArray(x));
    if (src) rows = Object.entries(src).map(([model_id, v]) => ({ model_id, ...(typeof v === 'object' ? v : { usd: v }) }));
  }
  return rows;
}

export async function loadCosts() {
  const el = $('costs');
  const r = await api('/costs');
  if (!r.ok) {
    mount(el, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — cost data unavailable.' : `Couldn't load costs (${r.error}).`,
      { label: 'Retry', onClick: loadCosts }));
    return;
  }
  const d = r.data || {};
  const rows = costRows(d);
  if (!rows.length) { mount(el, stateBox('empty', 'No AI calls logged yet.')); return; }
  const total = d.total_usd ?? rows.reduce((s, x) => s + (Number(x.usd ?? x.cost_usd) || 0), 0);
  const perHour = d.usd_per_outage_hour ?? d.cost_per_outage_hour ?? d.per_outage_hour;
  mount(el,
    h('div', { class: 'cost-total' },
      h('span', { class: 'cost-big' }, usd(total)), h('span', { class: 'muted' }, ' total'),
      perHour != null ? h('span', { class: 'muted' }, ` · ${usd(Number(perHour))} per outage-hour`) : null),
    h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' },
      h('thead', {}, h('tr', {}, ['Model', 'Calls', 'Tokens in', 'Tokens out', 'USD'].map((t) => h('th', { scope: 'col' }, t)))),
      h('tbody', {}, rows.map((x) => h('tr', {},
        h('td', { class: 'mono' }, x.model_id || x.model || '—'),
        h('td', { class: 'num' }, x.calls ?? x.count ?? '—'),
        h('td', { class: 'num' }, x.tokens_in ?? '—'),
        h('td', { class: 'num' }, x.tokens_out ?? '—'),
        h('td', { class: 'num' }, usd(Number(x.usd ?? x.cost_usd))))))))
  );
}

// ---------------------------------------------------------------- sync status chip (GET /sync/status, S10)

const SYNC_MODE = { dynamodb: 'DynamoDB', local: 'local export', dry_run: 'dry run' };
let syncTimer;

/** Amber with the count while the outbox has items (re-checked every 2 s until it settles); quiet otherwise.
 *  Hidden when the status is unreachable. */
export async function loadSyncStatus() {
  clearTimeout(syncTimer);
  const r = await api('/sync/status');
  const s = r.ok && !r.offline ? r.data : null;
  const mode = s && (SYNC_MODE[s.mode] || s.mode);
  mount($('sync-chip'), s ? chip(s.pending > 0 ? `Sync: ${s.pending} pending · ${mode}` : `Synced · ${mode}`,
    s.pending > 0 ? 'warn' : 'neutral', s.last_error ? `Last sync error: ${s.last_error}` : undefined) : null);
  if (s?.pending > 0 && store.phase === 'recovery') syncTimer = setTimeout(loadSyncStatus, 2000);
}

/** A reload straight into Recovery loads no patient list (setPhase only loads it for Downtime): fetch it once
 *  so discrepancies show bed labels, not patient ids. The discrepancies themselves come only from syncRestored
 *  (GET /discrepancies, review state included) or restore(). */
async function ensurePatients() {
  if (store.patients.size) return;
  const p = await api('/patients');
  if (!p.ok || store.patients.size) return;
  asList(p.data, 'patients', 'items').forEach((x) => store.patients.set(x.patient_id, x));
  if (discrepancies?.length) renderDiscrepancies();
}

export function enterRecovery() {
  ensurePatients();
  syncRestored();
  loadBackload();
  loadAudit();
  loadCosts();
  loadSyncStatus();
}

export function initRecovery() {
  $('btn-restore').addEventListener('click', restore);
  $('btn-refresh-backload').addEventListener('click', () => { loadBackload(); loadAudit(); });
  $('btn-digest').addEventListener('click', loadDigest);
  $('btn-audit').addEventListener('click', loadAudit);
  $('btn-costs').addEventListener('click', () => { loadCosts(); loadSyncStatus(); });
  setInterval(() => { if (store.phase === 'recovery' && !document.hidden) loadSyncStatus(); }, 10000);
  $('btn-incident').addEventListener('click', loadIncident);
  $('btn-print-incident').addEventListener('click', printIncident);
  idleStatus = $('recovery-status').textContent;
  renderDiscrepancies();
  on('restoredAt', syncRestored);
  mount($('incident'), h('p', { class: 'muted' }, 'Timeline of the outage from the audit log, with a two-paragraph summary. Printable on one page.'));
  mount($('digest'), h('p', { class: 'muted' }, 'The SBAR handoff for the oncoming shift, written from the audit log and captured records.'));
  on('role', () => { if (records.length) renderBackload(); });
  on('records-changed', () => { if (store.phase === 'recovery') { loadAudit(); loadCosts(); loadSyncStatus(); } });
}
