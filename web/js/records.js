// CapturedRecord rendering shared by capture (During) and the backload queue (Recovery).
import { api } from './api.js';
import { h, chip, fmtTime, labelize, stateBox, mount } from './dom.js';
import { store, emit, patientLabel, roleLabel } from './state.js';

const FORM_LABELS = {
  vitals_flowsheet: 'Vitals flowsheet',
  downtime_mar: 'Downtime MAR',
  admission_note: 'Admission note',
  verbal_order: 'Verbal order',
};

const STATUS_KIND = { needs_review: 'warn', verified: 'info', entered: 'ok', rejected: 'crit' };

function fieldState(f) {
  if (f.flag || f.agreement === 'disagree' || f.value == null) return 'flagged';
  if (f.agreement === 'agree') return 'agree';
  return 'single';
}

// Per-field crop of the photo: blob `crop:<record_id>:<field>` (server stores one per field box).
const cropRef = (rec, name) => `crop:${rec.record_id}:${name}`;
const asRole = () => ({ 'X-Role': store.role });
const whoLabel = (v) => String(v || '').split(' + ').map(roleLabel).join(' + ');

function cropCell(rec, f) {
  if (!rec.image_ref || !f.crop) return h('td', { class: 'crop-cell' }, h('span', { class: 'muted small' }, '—'));
  const src = `/blobs/${encodeURIComponent(cropRef(rec, f.name))}`;
  // Click (or Enter) opens the crop at full size in a new tab.
  const link = h('a', { class: 'crop-link', href: src, target: '_blank', rel: 'noopener', title: 'Open full size in a new tab' },
    h('img', {
      class: 'field-crop', loading: 'lazy', alt: `Photo of the ${labelize(f.name)} box`, src,
      onerror: () => link.replaceWith(h('span', { class: 'muted small' }, 'No crop')),
    }));
  return h('td', { class: 'crop-cell' }, link);
}

function valueCell(rec, f, getCard, msg, onChange, tx, bv) {
  const td = h('td', { class: 'field-value' });
  const show = () => mount(td,
    f.value ?? h('em', {}, 'unreadable'),
    f.agreement === 'disagree' ? h('div', { class: 'reader-values' }, `Textract: ${tx?.fields?.[f.name] ?? '—'} · Bedrock: ${bv ?? '—'}`) : null,
    rec.status === 'needs_review'
      ? h('button', { class: 'btn btn-quiet btn-sm field-edit', type: 'button', 'aria-label': `Edit ${labelize(f.name)}`, onclick: edit }, 'Edit')
      : null);
  function edit() {
    const input = h('input', { class: 'input input-sm', value: f.value ?? '', 'aria-label': `New value for ${labelize(f.name)}` });
    const save = async (e) => {
      e.preventDefault();
      mount(msg, stateBox('loading', 'Saving correction…'));
      const r = await api(`/backload/${encodeURIComponent(rec.record_id)}/fields/${encodeURIComponent(f.name)}`,
        { method: 'PATCH', json: { value: input.value }, headers: asRole() });
      if (!r.ok || !r.data?.record_id) {
        mount(msg, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — correction not saved.' : `Correction not saved (${r.error}).`));
        return;
      }
      getCard().replaceWith(renderRecord(r.data, { onChange }));
      onChange?.(r.data);
      emit('records-changed', r.data);
    };
    mount(td, h('form', { class: 'field-edit-form', onsubmit: save }, input,
      h('span', { class: 'field-edit-actions' },
        h('button', { class: 'btn btn-primary btn-sm', type: 'submit' }, 'Save'),
        h('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: show }, 'Cancel'))));
    input.focus();
  }
  show();
  return td;
}

// "n of 2 verifications" for high-alert medication records (enforced server-side).
async function loadVerification(rec, box, verifyBtn) {
  const r = await api(`/backload/${encodeURIComponent(rec.record_id)}/verification`, { headers: asRole() });
  if (!r.ok || !r.data || r.data.required < 2) return;
  const done = r.data.verifications || [];
  const first = done[0];
  if (verifyBtn) verifyBtn.textContent = done.length ? 'Second verification (2 of 2)' : 'Verify (1 of 2)';
  mount(box, h('div', { class: 'alert alert-warn verify-state', role: 'status' },
    h('span', { class: 'rule-badge' }, `${done.length} of 2 verifications`),
    h('span', {},
      `High-alert medication (${r.data.high_alert_medication}): two people must verify independently.`,
      first ? ` First check by ${whoLabel(first.verified_by || first.role)}. A different person must do the second — ` : ' ',
      first ? h('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: () => document.getElementById('role-select')?.focus() }, 'Switch role') : null)));
}

// A reviewer edit (PATCH .../fields/{name}) leaves the readers' raw values in `reads`.
const corrected = (f, tx, br) => [tx, br].some((r) => r && f.name in (r.fields || {}))
  && ![tx, br].some((r) => r && r.fields?.[f.name] === f.value);

function agreementChip(f) {
  if (f.agreement === 'agree') return chip('Readers agree', 'ok');
  if (f.agreement === 'disagree') return chip('Readers disagree', 'crit');
  return chip('Single read', 'neutral');
}

export function renderRecord(rec, { onChange } = {}) {
  const fields = rec.fields || [];
  const flagged = fields.filter((f) => fieldState(f) === 'flagged').length;
  const agree = fields.filter((f) => f.agreement === 'agree' && !f.flag).length;
  const tx = rec.reads?.textract;
  const br = rec.reads?.bedrock;
  const readers = [tx ? 'Textract' : null, br ? 'Bedrock vision' : null].filter(Boolean);
  const msg = h('div', { class: 'record-msg', 'aria-live': 'polite' });
  const verifyBox = h('div', { class: 'verify-box' });
  let card;
  const verifyBtn = rec.status === 'needs_review'
    ? h('button', { class: 'btn btn-primary', type: 'button', 'data-action': 'verify', onclick: () => act(rec, 'verify', card, msg, onChange) },
      flagged ? `Verify (${flagged} flagged checked)` : 'Verify')
    : null;

  card = h('article', { class: `record status-${rec.status || 'needs_review'}`, 'data-id': rec.record_id },
    h('header', { class: 'record-head' },
      h('div', {},
        h('h3', { class: 'record-title' }, FORM_LABELS[rec.form_type] || labelize(rec.form_type), ' · ', patientLabel(rec.patient_id)),
        h('p', { class: 'record-meta' },
          `Captured ${fmtTime(rec.captured_at)} by ${roleLabel(rec.captured_by_role)} · `,
          readers.length ? `Read by ${readers.join(' + ')}` : 'No reader output',
          readers.length === 1 ? ' (single read)' : '',
          ' · ', h('span', { class: 'mono' }, rec.record_id)),
      ),
      chip(labelize(rec.status || 'needs_review'), STATUS_KIND[rec.status] || 'neutral'),
    ),
    rec.form_type === 'verbal_order'
      ? h('div', { class: 'alert alert-warn' }, h('span', { class: 'rule-badge' }, 'R-VO-01'), h('span', {}, 'Requires provider co-sign before entry.'))
      : null,
    fields.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 'fields-table' },
        h('thead', {}, h('tr', {}, ['Field', 'Photo', 'Value', 'Confidence', 'Readers', 'Review'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, fields.map((f) => {
          const st = fieldState(f);
          const bv = br?.fields?.[f.name];
          return h('tr', { class: `field field-${st}` },
            h('th', { scope: 'row' }, labelize(f.name)),
            cropCell(rec, f),
            valueCell(rec, f, () => card, msg, onChange, tx, bv),
            h('td', { class: 'num' }, typeof f.confidence === 'number' ? `${Math.round(f.confidence * 100)}%` : '—'),
            h('td', {}, corrected(f, tx, br) ? chip('Corrected by reviewer', 'info') : agreementChip(f)),
            h('td', {}, st === 'flagged' ? chip(`Review: ${labelize(f.flag || 'disagreement')}`, 'warn') : chip('OK', 'ok')));
        }))))
      : h('p', { class: 'muted' }, 'No fields were read from this form.'),
    verifyBox,
    h('footer', { class: 'record-foot' },
      h('span', { class: 'record-summary' }, `${agree} agree · ${flagged} flagged for review`,
        rec.verified_by ? ` · Verified by ${whoLabel(rec.verified_by)} ${fmtTime(rec.verified_at)}` : '',
        rec.entered_at ? ` · Entered ${fmtTime(rec.entered_at)}` : ''),
      h('span', { class: 'record-actions' },
        verifyBtn,
        rec.status === 'verified' || rec.status === 'needs_review'
          ? h('button', {
            class: 'btn btn-success', type: 'button', 'data-action': 'entered', disabled: rec.status !== 'verified',
            title: rec.status !== 'verified' ? 'A human must verify before marking entered' : null,
            onclick: () => act(rec, 'entered', card, msg, onChange),
          }, 'Mark entered in EHR')
          : null),
    ),
    msg,
  );
  if (rec.status === 'needs_review') loadVerification(rec, verifyBox, verifyBtn);
  return card;
}

// The record as the server holds it now (null when offline or not found). There is no single-record GET.
async function serverRecord(id) {
  const r = await api('/backload', { headers: asRole() });
  if (!r.ok || r.offline || !r.data) return null;
  return Object.values(r.data).flat().find((x) => x?.record_id === id) || null;
}

// Buttons follow the record's status, never the outcome of the last click: "Mark entered" only when verified.
function syncActions(card, rec) {
  card.querySelectorAll('.record-actions button[data-action]').forEach((b) => {
    b.disabled = b.dataset.action === 'verify' ? rec.status !== 'needs_review' : rec.status !== 'verified';
  });
}

function redraw(card, rec, onChange) {
  const next = renderRecord(rec, { onChange });
  card.replaceWith(next);
  onChange?.(rec);
  emit('records-changed', rec);
  return next;
}

async function act(rec, action, card, msg, onChange) {
  card.querySelectorAll('.record-actions button').forEach((b) => { b.disabled = true; });
  mount(msg, stateBox('loading', action === 'verify' ? 'Saving verification…' : 'Marking entered…'));
  // Identity is the role switcher's role (X-Role); the server rejects the same role verifying twice.
  const r = await api(`/backload/${encodeURIComponent(rec.record_id)}/${action}`, { method: 'POST', json: {}, headers: asRole() });
  if (!r.ok) {
    const note = stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — not saved. Try again when connected.' : `Not saved (${r.error}).`);
    // Offline: the last state the server gave us is `rec`. Online: re-read it (the error body carries no record).
    const cur = (r.offline ? null : await serverRecord(rec.record_id)) || rec;
    if (cur.status !== rec.status) {
      mount(redraw(card, cur, onChange).querySelector('.record-msg'), note);
      return;
    }
    mount(msg, note);
    syncActions(card, cur);
    if (!r.offline && cur.status === 'needs_review') {
      loadVerification(cur, card.querySelector('.verify-box'), card.querySelector('.record-actions [data-action="verify"]'));
    }
    return;
  }
  // Success: the server returns the record; if it ever does not, re-read rather than assume a status.
  redraw(card, r.data?.record_id ? r.data : (await serverRecord(rec.record_id)) || rec, onChange);
}
