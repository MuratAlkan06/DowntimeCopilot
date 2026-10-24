// CapturedRecord rendering shared by capture (During) and the backload queue (Recovery).
import { api } from './api.js';
import { h, chip, fmtTime, labelize, fieldLabel, stateBox, mount } from './dom.js';
import { store, emit, patientLabel, roleLabel, GROUPS } from './state.js';

const FORM_LABELS = {
  vitals_flowsheet: 'Vitals flowsheet',
  downtime_mar: 'Downtime MAR',
  admission_note: 'Admission note',
  verbal_order: 'Verbal order',
};

const STATUS_KIND = { needs_review: 'warn', verified: 'info', entered: 'ok', rejected: 'crit' };
const groupLabel = (id) => GROUPS.find((g) => g.id === id)?.label || labelize(id);

// A reviewer edit (PATCH .../fields/{name}) recomputes the field's flag and leaves the readers' raw values
// in `reads`; `agreement` keeps the readers' original verdict. So a field is "corrected" when its value
// matches neither reader, or when the readers disagreed and the disagreement flag is gone (the reviewer
// picked one reader's value). State follows the current flag, never the original agreement alone.
const corrected = (f, tx, br) => (f.agreement === 'disagree' && f.flag !== 'disagreement')
  || ([tx, br].some((r) => r && f.name in (r.fields || {})) && ![tx, br].some((r) => r && r.fields?.[f.name] === f.value));

function fieldState(f, tx, br) {
  if (f.flag || f.value == null) return 'flagged';
  if (corrected(f, tx, br)) return 'corrected';
  if (f.agreement === 'disagree') return 'flagged';
  return f.agreement === 'agree' ? 'agree' : 'single';
}

// After a save or verify the card is re-rendered: put focus back on the control the user was on (or the
// next one that still applies), never on <body>.
function restoreFocus(card, selector) {
  const target = [...card.querySelectorAll(selector)].find((b) => !b.disabled)
    || [...card.querySelectorAll('.record-actions button')].find((b) => !b.disabled)
    || card;
  target.focus();
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
      class: 'field-crop', loading: 'lazy', alt: `Photo of the ${fieldLabel(f.name)} box`, src,
      onerror: () => link.replaceWith(h('span', { class: 'muted small' }, 'No crop')),
    }));
  return h('td', { class: 'crop-cell' }, link);
}

// One reader's raw read of a field: "—" when the reader did not run or did not report the field,
// "unreadable" when it ran and returned null (agreement.merge counts that as a disagreement).
const readerValue = (r, name) => (r && name in (r.fields || {}) ? r.fields[name] ?? 'unreadable' : '—');

function valueCell(rec, f, getCard, msg, onChange, tx, bv) {
  const td = h('td', { class: 'field-value' });
  const show = () => mount(td,
    f.value ?? h('em', {}, 'unreadable'),
    f.agreement === 'disagree' ? h('div', { class: 'reader-values' }, `Textract: ${readerValue(tx, f.name)} · Bedrock: ${bv}`) : null,
    rec.status === 'needs_review'
      ? h('button', { class: 'btn btn-quiet btn-sm field-edit', type: 'button', 'aria-label': `Edit ${fieldLabel(f.name)}`, onclick: edit }, 'Edit')
      : null);
  function edit() {
    const input = h('input', { class: 'input input-sm', value: f.value ?? '', 'aria-label': `New value for ${fieldLabel(f.name)}` });
    const save = async (e) => {
      e.preventDefault();
      mount(msg, stateBox('loading', 'Saving correction…'));
      const r = await api(`/backload/${encodeURIComponent(rec.record_id)}/fields/${encodeURIComponent(f.name)}`,
        { method: 'PATCH', json: { value: input.value }, headers: asRole() });
      if (!r.ok || !r.data?.record_id) {
        mount(msg, stateBox(r.offline ? 'offline' : 'error', r.offline ? 'Offline — correction not saved.' : `Correction not saved (${r.error}).`));
        return;
      }
      const next = renderRecord(r.data, { onChange });
      getCard().replaceWith(next);
      restoreFocus(next, `tr[data-field="${CSS.escape(f.name)}"] .field-edit`);
      onChange?.(r.data);
      emit('records-changed', r.data);
    };
    mount(td, h('form', { class: 'field-edit-form', onsubmit: save }, input,
      h('span', { class: 'field-edit-actions' },
        h('button', { class: 'btn btn-primary btn-sm', type: 'submit' }, 'Save'),
        h('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: () => { show(); td.querySelector('.field-edit')?.focus(); } }, 'Cancel'))));
    input.focus();
  }
  show();
  return td;
}

// "n of 2 verifications" for high-alert medication records (enforced server-side). The same response carries
// the record's text source (onSource); with no box, only that is read.
async function loadVerification(rec, box, verifyBtn, onSource) {
  const r = await api(`/backload/${encodeURIComponent(rec.record_id)}/verification`, { headers: asRole() });
  if (r.ok && r.data?.text_source) onSource?.(r.data.text_source);
  if (!box || !r.ok || !r.data || r.data.required < 2) return;
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

function agreementChip(f) {
  if (f.agreement === 'agree') return chip('Readers agree', 'ok');
  if (f.agreement === 'disagree') return chip('Readers disagree', 'crit');
  return chip('Single read', 'neutral');
}

// Where a verbal order's text came from. The server stores it at capture (text_source, read from
// GET /backload/{id}/verification), so the line holds after a reload or on another device. Nothing is
// claimed until it is known: a typed read-back over attached audio is not a transcript of that audio.
const SOURCE_TEXT = {
  typed: 'Read from the typed read-back (single read)',
  transcribed: 'Transcribed from audio (single read)',
  audio_untranscribed: 'Audio saved, not transcribed',
};
const SOURCE_UNKNOWN = 'Read-back source unknown';
const isVerbal = (rec) => Boolean(rec.audio_ref && !rec.image_ref);

// Text source by record id: the server's value once read, or this device's X-Transcribe outcome from the
// capture (during.js) as a same-session fast path, so a re-render after verify or an edit keeps it.
const sourceBy = new Map();
const TRANSCRIBED = new Set(['mock', 'transcribed']);
export const sourceFromOutcome = (o) => (o === 'typed' ? 'typed' : TRANSCRIBED.has(o) ? 'transcribed' : 'audio_untranscribed');

// `source`: text_source (typed | transcribed | audio_untranscribed | photo), or null while not known.
// Photo records keep their reader wording.
export function sourceLine(rec, readers, source) {
  if (isVerbal(rec)) return SOURCE_TEXT[source] || SOURCE_UNKNOWN;
  return `${readers.length ? `Read by ${readers.join(' + ')}` : 'No reader output'}${readers.length === 1 ? ' (single read)' : ''}`;
}

export function renderRecord(rec, { onChange, transcribe } = {}) {
  if (transcribe) sourceBy.set(rec.record_id, sourceFromOutcome(transcribe));
  const fields = rec.fields || [];
  const tx = rec.reads?.textract;
  const br = rec.reads?.bedrock;
  const states = fields.map((f) => fieldState(f, tx, br));
  const count = (st) => states.filter((x) => x === st).length;
  const flagged = count('flagged');
  const readers = [tx ? 'Textract' : null, br ? 'Bedrock vision' : null].filter(Boolean);
  const msg = h('div', { class: 'record-msg', 'aria-live': 'polite' });
  const verifyBox = h('div', { class: 'verify-box' });
  const sourceEl = h('span', { class: 'record-source' }, sourceLine(rec, readers, sourceBy.get(rec.record_id)));
  const onSource = (s) => { sourceBy.set(rec.record_id, s); sourceEl.textContent = sourceLine(rec, readers, s); };
  let card;
  const verifyBtn = rec.status === 'needs_review'
    ? h('button', { class: 'btn btn-primary', type: 'button', 'data-action': 'verify', onclick: () => act(rec, 'verify', card, msg, onChange) },
      flagged ? `Verify (${flagged} flagged checked)` : 'Verify')
    : null;

  card = h('article', { class: `record status-${rec.status || 'needs_review'}`, 'data-id': rec.record_id, tabindex: '-1' },
    h('header', { class: 'record-head' },
      h('div', {},
        h('h3', { class: 'record-title' }, FORM_LABELS[rec.form_type] || labelize(rec.form_type), ' · ', patientLabel(rec.patient_id)),
        h('p', { class: 'record-meta' },
          `Captured ${fmtTime(rec.captured_at)} by ${roleLabel(rec.captured_by_role)} · `,
          sourceEl,
          ' · ', h('span', { class: 'mono' }, rec.record_id)),
      ),
      h('span', { class: 'chips record-chips' },
        // Where this record lands at backload (Recovery groups by it, so its section title shows it there).
        rec.backload_group ? h('span', { class: 'chip chip-neutral record-group' }, `Backload: ${groupLabel(rec.backload_group)}`) : null,
        chip(labelize(rec.status || 'needs_review'), STATUS_KIND[rec.status] || 'neutral')),
    ),
    rec.form_type === 'verbal_order'
      ? h('div', { class: 'alert alert-warn' }, h('span', { class: 'rule-badge' }, 'R-VO-01'), h('span', {}, 'Requires provider co-sign before entry.'))
      : null,
    fields.length
      ? h('div', { class: 'table-wrap' }, h('table', { class: 'fields-table' },
        h('thead', {}, h('tr', {}, ['Field', 'Photo', 'Value', 'Confidence', 'Readers', 'Review'].map((t) => h('th', { scope: 'col' }, t)))),
        h('tbody', {}, fields.map((f, i) => {
          const st = states[i];
          const bv = readerValue(br, f.name);
          return h('tr', { class: `field field-${st}`, 'data-field': f.name },
            h('th', { scope: 'row' }, fieldLabel(f.name)),
            cropCell(rec, f),
            valueCell(rec, f, () => card, msg, onChange, tx, bv),
            h('td', { class: 'num' }, typeof f.confidence === 'number' ? `${Math.round(f.confidence * 100)}%` : '—'),
            h('td', {}, st === 'corrected' ? chip('Corrected by reviewer', 'info') : agreementChip(f)),
            h('td', {}, st === 'flagged' ? chip(`Review: ${labelize(f.flag || 'disagreement')}`, 'warn')
              : st === 'corrected' ? chip('Corrected', 'info') : chip('OK', 'ok')));
        }))))
      : h('p', { class: 'muted' }, 'No fields were read from this form.'),
    verifyBox,
    h('footer', { class: 'record-foot' },
      h('span', { class: 'record-summary' }, `${count('agree')} agree · `,
        count('corrected') ? `${count('corrected')} corrected · ` : '', `${flagged} flagged for review`,
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
  // The verify-state call also carries the text source; a verified or entered verbal order not seen on this
  // device yet reads just the source.
  if (rec.status === 'needs_review') loadVerification(rec, verifyBox, verifyBtn, onSource);
  else if (isVerbal(rec) && !sourceBy.has(rec.record_id)) loadVerification(rec, null, null, onSource);
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

function redraw(card, rec, onChange, action) {
  const next = renderRecord(rec, { onChange });
  card.replaceWith(next);
  if (action) restoreFocus(next, `.record-actions [data-action="${action}"]`);
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
      mount(redraw(card, cur, onChange, action).querySelector('.record-msg'), note);
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
  redraw(card, r.data?.record_id ? r.data : (await serverRecord(rec.record_id)) || rec, onChange, action);
}
