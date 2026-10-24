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

  const card = h('article', { class: `record status-${rec.status || 'needs_review'}`, 'data-id': rec.record_id },
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
        h('thead', {}, h('tr', {}, ['Field', 'Value', 'Confidence', 'Readers', 'Review'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, fields.map((f) => {
          const st = fieldState(f);
          const tv = tx?.fields?.[f.name];
          const bv = br?.fields?.[f.name];
          return h('tr', { class: `field field-${st}` },
            h('th', { scope: 'row' }, labelize(f.name)),
            h('td', { class: 'field-value' },
              f.value ?? h('em', {}, 'unreadable'),
              f.agreement === 'disagree' ? h('div', { class: 'reader-values' }, `Textract: ${tv ?? '—'} · Bedrock: ${bv ?? '—'}`) : null),
            h('td', { class: 'num' }, typeof f.confidence === 'number' ? `${Math.round(f.confidence * 100)}%` : '—'),
            h('td', {}, agreementChip(f)),
            h('td', {}, st === 'flagged' ? chip(`Review: ${labelize(f.flag || 'disagreement')}`, 'warn') : chip('OK', 'ok')));
        }))))
      : h('p', { class: 'muted' }, 'No fields were read from this form.'),
    h('footer', { class: 'record-foot' },
      h('span', { class: 'record-summary' }, `${agree} agree · ${flagged} flagged for review`,
        rec.verified_by ? ` · Verified by ${roleLabel(rec.verified_by)} ${fmtTime(rec.verified_at)}` : '',
        rec.entered_at ? ` · Entered ${fmtTime(rec.entered_at)}` : ''),
      h('span', { class: 'record-actions' },
        rec.status === 'needs_review'
          ? h('button', { class: 'btn btn-primary', type: 'button', onclick: () => act(rec, 'verify', card, msg, onChange) },
            flagged ? `Verify (${flagged} flagged checked)` : 'Verify')
          : null,
        rec.status === 'verified' || rec.status === 'needs_review'
          ? h('button', {
            class: 'btn btn-success', type: 'button', disabled: rec.status !== 'verified',
            title: rec.status !== 'verified' ? 'A human must verify before marking entered' : null,
            onclick: () => act(rec, 'entered', card, msg, onChange),
          }, 'Mark entered in EHR')
          : null),
    ),
    msg,
  );
  return card;
}

async function act(rec, action, card, msg, onChange) {
  card.querySelectorAll('.record-actions button').forEach((b) => { b.disabled = true; });
  mount(msg, stateBox('loading', action === 'verify' ? 'Saving verification…' : 'Marking entered…'));
  const body = { role: store.role, actor_role: store.role, verified_by: store.role };
  const r = await api(`/backload/${encodeURIComponent(rec.record_id)}/${action}`, { method: 'POST', json: body });
  if (!r.ok) {
    mount(msg, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — not saved. Try again when connected.' : `Not saved (${r.error}).`));
    card.querySelectorAll('.record-actions button').forEach((b) => { b.disabled = false; });
    return;
  }
  const updated = r.data && r.data.record_id ? r.data
    : { ...rec, status: action === 'verify' ? 'verified' : 'entered', verified_by: rec.verified_by || store.role,
      verified_at: rec.verified_at || new Date().toISOString(), entered_at: action === 'entered' ? new Date().toISOString() : rec.entered_at };
  const next = renderRecord(updated, { onChange });
  card.replaceWith(next);
  onChange?.(updated);
  emit('records-changed', updated);
}
