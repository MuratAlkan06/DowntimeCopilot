// Phase 2: policy questions + paper-form capture (with an offline queue).
import { api, isOffline } from './api.js';
import { h, mount, stateBox, chip, usd } from './dom.js';
import { store, on } from './state.js';
import { renderRecord } from './records.js';

const $ = (id) => document.getElementById(id);
const SUGGESTIONS = [
  'How do I get a stat lab with the order system down?',
  'How do I document a medication given during downtime?',
  'Who can take a verbal order during downtime?',
];

// ---------------------------------------------------------------- policy

async function ask(question) {
  const out = $('policy-answer');
  mount(out, stateBox('loading', 'Searching the downtime policy…'));
  const r = await api('/policy/ask', { method: 'POST', json: { question, role: store.role } });
  if (!r.ok) {
    mount(out, stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Offline — AI policy answers resume when connected. Use the paper downtime binder meanwhile.'
      : `Couldn't answer (${r.error}).`));
    return;
  }
  const a = r.data || {};
  mount(out, h('div', { class: 'policy-answer' },
    h('div', { class: 'chips' },
      a.grounded ? chip('Grounded in policy', 'ok') : chip('Not grounded — check the binder', 'warn'),
      a.guardrail?.status ? chip(`Guardrail: ${a.guardrail.status}`, 'neutral', (a.guardrail.reasons || []).join('; ')) : null),
    h('p', { class: 'answer-text' }, a.answer || 'No answer returned.'),
    (a.citations || []).length
      ? h('ol', { class: 'citations' }, a.citations.map((c) => h('li', {},
        h('span', { class: 'section-badge' }, `§ ${c.section_id}`), ' ', h('strong', {}, c.title),
        c.quote ? h('blockquote', {}, c.quote) : null)))
      : h('p', { class: 'muted' }, 'No section citations returned.'),
    h('p', { class: 'muted small' }, h('span', { class: 'mono' }, a.model_id || ''), a.cost ? ` · ${usd(a.cost.usd)}` : ''),
  ));
}

// ---------------------------------------------------------------- capture

const QKEY = 'dc.pendingCaptures';
const loadQ = () => { try { return JSON.parse(localStorage.getItem(QKEY)) || []; } catch { return []; } };
const saveQ = (q) => { try { localStorage.setItem(QKEY, JSON.stringify(q)); return true; } catch { return false; } };

const readDataUrl = (file) => new Promise((res, rej) => {
  const fr = new FileReader(); fr.onload = () => res(fr.result); fr.onerror = rej; fr.readAsDataURL(file);
});

function dataUrlToBlob(url) {
  const [head, b64] = url.split(',');
  const mime = /data:([^;]+)/.exec(head)?.[1] || 'application/octet-stream';
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

function buildForm(meta, file) {
  const fd = new FormData();
  if (file) fd.append('file', file, file.name || 'capture.jpg');
  fd.append('form_type', meta.form_type);
  fd.append('captured_by_role', meta.role);
  fd.append('role', meta.role);
  if (meta.patient_id) fd.append('patient_id', meta.patient_id);
  if (meta.readback_text) fd.append('readback_text', meta.readback_text);
  return fd;
}

function renderPending() {
  const q = loadQ();
  mount($('capture-pending'), q.length
    ? stateBox('offline', `${q.length} capture${q.length > 1 ? 's' : ''} queued on this device — will upload when connected.`,
      { label: 'Upload now', onClick: flushQueue })
    : null);
}

export async function flushQueue() {
  const q = loadQ();
  if (!q.length || isOffline()) { renderPending(); return; }
  const left = [];
  for (const item of q) {
    const file = item.dataUrl ? new File([dataUrlToBlob(item.dataUrl)], item.name || 'capture.jpg') : null;
    const r = await api('/capture', { method: 'POST', form: buildForm(item, file) });
    if (r.ok && r.data?.record_id) mount($('capture-result'), renderRecord(r.data));
    else left.push(item);
  }
  saveQ(left);
  renderPending();
}

async function submitCapture(e) {
  e.preventDefault();
  const file = $('capture-file').files[0] || null;
  const meta = {
    form_type: $('capture-type').value,
    patient_id: $('capture-patient').value || null,
    role: store.role,
    readback_text: $('capture-readback').value.trim() || null,
  };
  const out = $('capture-result');
  if (!file && !(meta.form_type === 'verbal_order' && meta.readback_text)) {
    mount(out, stateBox('error', 'Choose a photo of the form (or type the read-back for a verbal order).'));
    return;
  }
  const btn = e.submitter || $('capture-form').querySelector('button[type=submit]');
  btn.disabled = true;
  mount(out, stateBox('loading', 'Two readers are reading the form…'));
  const r = await api('/capture', { method: 'POST', form: buildForm(meta, file) });
  btn.disabled = false;
  if (r.ok && r.data?.record_id) {
    mount(out, renderRecord(r.data));
    $('capture-form').reset();
    $('capture-preview').hidden = true;
    syncFormType();
    return;
  }
  if (r.offline) {
    let queued = false;
    try { queued = saveQ([...loadQ(), { ...meta, dataUrl: file ? await readDataUrl(file) : null, name: file?.name, queued_at: new Date().toISOString() }]); } catch { queued = false; }
    mount(out, stateBox('offline', queued
      ? 'Offline — form saved on this device and queued for reading when connected. Keep the paper original.'
      : 'Offline and the image is too large to queue locally. Keep the paper original for backload.'));
    renderPending();
    return;
  }
  mount(out, stateBox('error', r.ok ? 'Unexpected response from /capture.' : `Couldn't read the form (${r.error}).`));
}

function syncFormType() {
  const verbal = $('capture-type').value === 'verbal_order';
  $('readback-wrap').hidden = !verbal;
  const input = $('capture-file');
  input.accept = verbal ? 'audio/*,image/*' : 'image/*';
  $('capture-file-label').textContent = verbal ? 'Record or choose read-back audio (optional)' : 'Photograph or choose the form';
}

function fillPatients() {
  const sel = $('capture-patient');
  const current = sel.value;
  const opts = [...store.patients.values()]
    .sort((a, b) => String(a.bed).localeCompare(String(b.bed), undefined, { numeric: true }))
    .map((p) => h('option', { value: p.patient_id }, `Bed ${p.bed} · ${p.name}`));
  mount(sel, h('option', { value: '' }, 'Read from form'), opts);
  sel.value = current;
}

export function initDuring() {
  mount($('policy-suggestions'), SUGGESTIONS.map((q) => h('button', {
    class: 'chip chip-button', type: 'button', onclick: () => { $('policy-q').value = q; ask(q); },
  }, q)));
  $('policy-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const q = $('policy-q').value.trim();
    if (q) ask(q);
  });
  mount($('policy-answer'), h('p', { class: 'muted' }, 'Answers cite the numbered section of the hospital downtime policy.'));

  $('capture-type').addEventListener('change', syncFormType);
  $('capture-file').addEventListener('change', (e) => {
    const f = e.target.files[0];
    const img = $('capture-preview');
    if (f && f.type.startsWith('image/')) { img.src = URL.createObjectURL(f); img.hidden = false; } else img.hidden = true;
  });
  $('capture-form').addEventListener('submit', submitCapture);
  mount($('capture-result'), h('p', { class: 'muted' }, 'Read fields appear here. Green = both readers agree; amber = flagged for human review.'));
  syncFormType();
  fillPatients();
  on('patients', fillPatients);
  renderPending();
}
