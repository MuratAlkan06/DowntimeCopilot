// Phase 2: policy questions + paper-form capture (with an offline queue).
import { api, isOffline, policyReplayKey } from './api.js';
import { h, mount, stateBox, chip, usd } from './dom.js';
import { store, on } from './state.js';
import { renderRecord } from './records.js';
import { idbStore, drain, isDead, retryDead, migrateLegacy, newId, MAX_ATTEMPTS, SYNC_TAG } from './queue.js';
import { createRecorder, typedOverridesAudio } from './recorder.js';

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
  const r = await api('/policy/ask', { method: 'POST', json: { question, role: store.role }, replay: policyReplayKey(question) });
  if (!r.ok) {
    mount(out, stateBox(r.offline ? 'offline' : 'error', r.offline
      ? 'Offline — AI policy answers resume when connected. Use the paper downtime binder meanwhile.'
      : `Couldn't answer (${r.error}).`));
    return;
  }
  const a = r.data || {};
  mount(out, h('div', { class: 'policy-answer' },
    r.offline ? h('p', { class: 'cached-note' }, 'Offline — the answer recorded earlier on this device for this question.') : null,
    h('div', { class: 'chips' },
      a.grounded ? chip('Grounded in policy', 'ok') : chip('Not grounded — check the binder', 'warn'),
      a.guardrail?.status ? chip(`Guardrail: ${a.guardrail.status}`, 'neutral', (a.guardrail.reasons || []).join('; ')) : null),
    h('p', { class: 'answer-text' }, a.answer || 'No answer returned.'),
    (a.citations || []).length && !a.grounded ? h('p', { class: 'sub-title' }, 'Nearest sections in the binder') : null,
    (a.citations || []).length
      ? h('ol', { class: 'citations' }, a.citations.map((c) => h('li', {},
        h('span', { class: 'section-badge' }, `§ ${c.section_id}`), ' ', h('strong', {}, c.title),
        c.quote ? h('blockquote', {}, c.quote) : null)))
      : h('p', { class: 'muted' }, 'No section citations returned.'),
    h('p', { class: 'muted small' }, h('span', { class: 'mono' }, a.model_id || ''), a.cost ? ` · ${usd(a.cost.usd)}` : ''),
  ));
}

// ---------------------------------------------------------------- capture

// Offline queue: IndexedDB via queue.js (S7). Drained on Background Sync (sw.js wakes the page), the
// online event (app.js), a slow interval and startup; queue.js lets only one drain run at a time.
const queue = idbStore();
const DRAIN_EVERY_MS = 60000;

function buildForm(meta, file) {
  const fd = new FormData();
  if (file) fd.append('file', file, file.name || 'capture.jpg');
  fd.append('form_type', meta.form_type);
  fd.append('captured_by_role', meta.role);
  fd.append('role', meta.role);
  if (meta.patient_id) fd.append('patient_id', meta.patient_id);
  if (meta.readback_text) fd.append('readback_text', meta.readback_text);
  if (meta.id) fd.append('client_id', meta.id); // server idempotency: a re-sent id returns its record
  return fd;
}

// ---------------------------------------------------------------- verbal-order recorder (S9)

let recorder = null;
const REC_TEXT = {
  idle: 'Or choose an audio file, or type the read-back.',
  recording: 'Recording… press Stop when the read-back is done.',
  denied: 'Microphone blocked. Choose an audio file or type the read-back instead.',
  unsupported: 'Recording is not available in this browser. Choose an audio file or type the read-back.',
};

// Typed-over-audio precedence (server rule): said before submit, never silently applied.
const PRECEDENCE = 'Typed read-back will be used. The audio is saved with the order but not transcribed.';
let precedenceShown = false;

function attachedAudio() {
  if ($('capture-type').value !== 'verbal_order') return null;
  return recorder?.file || $('capture-file').files[0] || null;
}

function renderPrecedence() {
  const show = typedOverridesAudio(attachedAudio(), $('capture-readback').value);
  if (show === precedenceShown) return; // mount only on change, so the live region speaks once
  precedenceShown = show;
  const note = show ? stateBox('warn', PRECEDENCE) : null;
  note?.removeAttribute('role'); // #readback-notice is the live region; a nested role=status would announce twice
  mount($('readback-notice'), note);
}

function renderRecorder() {
  const st = recorder.state;
  const toggle = $('rec-toggle');
  const hadFocus = document.activeElement === toggle;
  toggle.hidden = st === 'unsupported';
  // The Record control just disappeared (no microphone): land focus on the status line, not on <body>.
  if (toggle.hidden && hadFocus) $('rec-status').focus();
  toggle.textContent = st === 'recording' ? 'Stop recording' : st === 'attached' ? 'Record again' : 'Record read-back';
  toggle.setAttribute('aria-pressed', String(st === 'recording'));
  toggle.className = `btn btn-sm ${st === 'recording' ? 'btn-danger' : 'btn-secondary'}`;
  $('rec-discard').hidden = st !== 'attached';
  $('rec-status').textContent = st === 'attached'
    ? `Recording attached (${recorder.seconds} s) — used only if the typed box is empty.`
    : REC_TEXT[st];
  if (st === 'attached') { $('capture-file').value = ''; $('capture-preview').hidden = true; } // one audio source
  renderPrecedence();
}

// Transcribe outcome (X-Transcribe) when the audio could not become a transcript: say so, typed read-back next.
const TRANSCRIBE_FALLBACK = {
  denied: 'Transcription is not allowed on this account',
  failed: 'Transcription failed',
  timeout: 'Transcription took too long',
  empty: 'No speech was heard in the recording',
  error: 'Transcription could not run',
  unsupported: 'This file type cannot be transcribed',
  unconfigured: 'Transcription is not set up on this server',
};

const heard = (r) => r.headers?.get?.('x-transcribe') || '';

function transcribeNote(r) {
  const why = TRANSCRIBE_FALLBACK[heard(r)];
  return why ? stateBox('warn', `${why}. The audio is saved with this order and no field was filled in. `
    + 'Fill the fields in review from the audio, or type the read-back instead.') : null;
}

// The queued item's own role is the actor (X-Role), not whoever is signed in when it drains.
const sendQueued = (item) => api('/capture', {
  method: 'POST', headers: { 'X-Role': item.role },
  form: buildForm(item, item.blob ? new File([item.blob], item.name || 'capture.jpg', { type: item.blob.type }) : null),
});

// Live and dead items, the live ids, and whether the server has refused a live item: a first submit queued on a
// 5xx/408/429 (server_status) or an answered failure on a drain (attempts). `ids` is null when the store is
// unreadable, so nothing is concluded about an item from a failed read.
async function queueState() {
  try {
    const items = await queue.list();
    const live = items.filter((it) => !isDead(it));
    return { active: live.length, dead: items.length - live.length, ids: new Set(live.map((it) => it.id)),
      refused: live.some((it) => Number(it.attempts) > 0 || it.server_status) };
  } catch { return { active: 0, dead: 0, ids: null, refused: false }; }
}

const captures = (n) => `${n} capture${n === 1 ? '' : 's'}`;

// Each rendered queue action by name ('upload', 'retry'). renderPending redraws #capture-pending, which replaces
// the button the user was on: focus goes back to the same action, else the one still shown, else #capture-result,
// where an uploaded capture's record lands; never <body> (WCAG 2.4.3; the #38 pattern in recovery.js).
let actions = new Map();

function queueBox(kind, message, name, label, onClick) {
  const box = stateBox(kind, message);
  const btn = h('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: onClick }, label);
  actions.set(name, btn);
  box.append(btn);
  return box;
}

// Why live items are still waiting (#59): no connection, the server refusing them, or no drain has reached them
// yet. "Will upload when connected" is said only when the connection is the problem.
function waitingBox(n, refused) {
  const [kind, why] = isOffline() ? ['offline', 'will upload when connected.']
    : refused ? ['warn', 'the server is not accepting uploads right now; retrying automatically.']
      : ['queued', 'waiting to upload.'];
  return queueBox(kind, `${captures(n)} queued on this device — ${why}`, 'upload', 'Upload now', flushQueue);
}

// The submit notice of the capture last queued here ({ id, el } in #capture-result). It promises a retry, so it is
// withdrawn once its item leaves the live queue without a record on this page: dead-lettered (the failed box
// says so instead) or uploaded by another tab. When this tab uploads it, onSent replaces it with the record.
let queuedNote = null;

// The header chips share one live region: rewrite a chip's text only when it changes, or every drain re-announces.
const setText = (el, text) => { if (el.textContent !== text) el.textContent = text; };

async function renderPending() {
  const note = queuedNote; // read before the store: an item queued after this read is not in the list below
  const { active: n, dead, ids, refused } = await queueState();
  if (note && note === queuedNote && ids && !ids.has(note.id)) { note.el.hidden = true; queuedNote = null; }
  const badge = $('queue-count'); // header, next to "Simulate offline"; counts down as the queue drains
  badge.hidden = !n;
  setText(badge, `${n} queued`);
  badge.title = `${captures(n)} waiting to upload from this device`;
  // Dead-lettered (#55): kept on this device, retried only on request. The chip is a button (#59): app.js opens
  // During and showFailedUploads() lands on the retry action. The name starts with the visible text (WCAG 2.5.3).
  const failed = $('queue-failed');
  failed.hidden = !dead;
  setText(failed, `${dead} failed`);
  failed.setAttribute('aria-label', `${dead} failed upload${dead === 1 ? '' : 's'} — open to retry`);
  failed.title = `${captures(dead)} stopped retrying after ${MAX_ATTEMPTS} failed uploads — kept on this device`;
  const had = [...actions].find(([, btn]) => btn === document.activeElement)?.[0]; // read just before the redraw
  actions = new Map();
  const waiting = n ? waitingBox(n, refused) : null;
  const deadBox = dead ? queueBox('error', `${captures(dead)} could not upload after ${MAX_ATTEMPTS} tries and stopped `
    + 'retrying. Kept on this device; keep the paper original.', 'retry', 'Retry failed uploads', retryFailed) : null;
  deadBox?.removeAttribute('role'); // #capture-pending is the live region; role=alert here would re-announce on every redraw
  mount($('capture-pending'), waiting, deadBox);
  if (had) (actions.get(had) || [...actions.values()][0] || $('capture-result')).focus();
}

async function retryFailed() {
  try { await retryDead(queue); } catch { /* store unavailable: the next render shows the same count */ }
  await flushQueue();
}

/** The header "N failed" chip (#59): app.js opens During, then this puts focus on the failed box's retry action,
 *  the one place a dead-lettered capture is sent again. Centered, so the sticky phase bar never covers it. */
export function showFailedUploads() {
  const retry = actions.get('retry');
  if (!retry) return;
  retry.scrollIntoView({ block: 'center' });
  retry.focus({ preventScroll: true });
}

function requestBackgroundSync() {
  // Chromium only; elsewhere the online event and the interval drain the queue.
  navigator.serviceWorker?.ready.then((reg) => reg.sync?.register(SYNC_TAG)).catch(() => {});
}

export async function flushQueue() {
  if (isOffline()) { await renderPending(); return; }
  const result = await drain(queue, sendQueued, {
    onSent: async (_item, r) => { mount($('capture-result'), transcribeNote(r), renderRecord(r.data, { transcribe: heard(r) })); await renderPending(); },
  });
  await renderPending();
  return result;
}

// Queue a first submit under the same id on these: 5xx, or a transient 408 (timeout) / 429 (throttle). Retrying
// the same client_id is safe, the server dedupes it. Keep tools/durability/queue_cycles.py submit() in step.
const RETRYABLE = (s) => s >= 500 || s === 408 || s === 429;
// What the notice calls each (#59): only a 5xx is the server's error; a 408 timed out, a 429 is throttling.
const REFUSAL = { 408: 'Upload timed out', 429: 'Rate-limited' };

async function submitCapture(e) {
  e.preventDefault();
  const verbalAudio = $('capture-type').value === 'verbal_order' ? recorder.file : null;
  const file = verbalAudio || $('capture-file').files[0] || null; // a recorded clip takes the file's place
  const meta = {
    id: newId(), // sent now and on every retry, so the server keeps one record per submission
    form_type: $('capture-type').value,
    patient_id: $('capture-patient').value || null,
    role: store.role,
    readback_text: $('capture-readback').value.trim() || null,
  };
  const out = $('capture-result');
  const prev = [...out.childNodes].filter((n) => !n.classList?.contains('state')); // kept on failure
  if (meta.form_type === 'verbal_order' && !meta.patient_id) {
    mount(out, stateBox('error', 'Choose the patient for this verbal order. The read-back has no bed box to read, and an order for no one cannot be co-signed or backloaded.'), prev);
    return;
  }
  if (!file && !(meta.form_type === 'verbal_order' && meta.readback_text)) {
    mount(out, stateBox('error', 'Choose a photo of the form (or type the read-back for a verbal order).'), prev);
    return;
  }
  const btn = e.submitter || $('capture-form').querySelector('button[type=submit]');
  btn.disabled = true;
  mount(out, stateBox('loading', 'Two readers are reading the form…'));
  const r = await api('/capture', { method: 'POST', form: buildForm(meta, file) });
  btn.disabled = false;
  if (r.ok && r.data?.record_id) {
    mount(out, transcribeNote(r), renderRecord(r.data, { transcribe: heard(r) }));
    recorder.discard();
    $('capture-form').reset();
    $('capture-preview').hidden = true;
    syncFormType();
    return;
  }
  // A real 5xx may come after the server committed the record (#55): queue it under the SAME id, so the retry
  // returns that record instead of a resubmit minting a new id and a duplicate. Any other 4xx is the request's
  // own fault and is not queued: fixing the form and submitting again under a new id is right there.
  const serverError = !r.offline && RETRYABLE(r.status);
  if (r.offline || serverError) {
    let queued = false;
    try {
      // server_status only tells renderPending the server refused this item; it never counts toward MAX_ATTEMPTS.
      await queue.put({ ...meta, blob: file, name: file?.name, queued_at: new Date().toISOString(),
        ...(serverError ? { server_status: r.status } : {}) });
      queued = true;
      requestBackgroundSync();
      // Stored safely: clear the form so the same paper is not queued twice under a new id.
      recorder.discard();
      $('capture-form').reset();
      $('capture-preview').hidden = true;
      syncFormType();
    } catch { queued = false; }
    const why = serverError ? `${REFUSAL[r.status] || 'Server error'} (${r.status})` : 'Offline';
    const note = stateBox(serverError ? 'warn' : 'offline', queued
      ? (serverError
        ? `${why} — form saved on this device and will be retried automatically. Keep the paper original.`
        : `${why} — form saved on this device and queued for reading when connected. Keep the paper original.`)
      : `${why} and this device could not store the form. Keep the paper original for backload.`);
    queuedNote = queued ? { id: meta.id, el: note } : null;
    mount(out, note, prev);
    renderPending();
    return;
  }
  mount(out, stateBox('error', r.ok ? 'Unexpected response from /capture.' : `Couldn't read the form (${r.error}).`), prev);
}

function syncFormType() {
  const verbal = $('capture-type').value === 'verbal_order';
  $('readback-wrap').hidden = !verbal;
  $('recorder').hidden = !verbal;
  if (!verbal && recorder) recorder.discard(); // leaving the verbal form releases the microphone
  const input = $('capture-file');
  input.accept = verbal ? 'audio/*,image/*' : 'image/*';
  $('capture-file-label').textContent = verbal ? 'Choose read-back audio (optional)' : 'Photograph or choose the form';
  renderPrecedence();
}

function fillPatients() {
  const sel = $('capture-patient');
  const current = sel.value;
  const opts = [...store.patients.values()]
    .sort((a, b) => String(a.bed).localeCompare(String(b.bed), undefined, { numeric: true }))
    .map((p) => h('option', { value: p.patient_id }, `${p.bed} · ${p.name}`));
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

  recorder = createRecorder({ onChange: renderRecorder });
  $('rec-toggle').addEventListener('click', () => (recorder.state === 'recording' ? recorder.stop() : recorder.start()));
  $('rec-discard').addEventListener('click', () => recorder.discard());
  renderRecorder();
  $('capture-type').addEventListener('change', syncFormType);
  $('capture-file').addEventListener('change', (e) => {
    const f = e.target.files[0];
    if (f && recorder.state === 'attached') recorder.discard(); // a chosen file replaces the recording
    const img = $('capture-preview');
    if (f && f.type.startsWith('image/')) { img.src = URL.createObjectURL(f); img.hidden = false; } else img.hidden = true;
    renderPrecedence();
  });
  $('capture-readback').addEventListener('input', renderPrecedence);
  $('capture-form').addEventListener('submit', submitCapture);
  mount($('capture-result'), h('p', { class: 'muted' }, 'Read fields appear here. Green = both readers agree; amber = flagged for human review.'));
  syncFormType();
  fillPatients();
  on('patients', fillPatients);
  navigator.serviceWorker?.addEventListener('message', (e) => { if (e.data?.type === SYNC_TAG) flushQueue(); });
  setInterval(flushQueue, DRAIN_EVERY_MS);
  // One-time move of a pre-S7 localStorage queue, then a startup drain.
  migrateLegacy(queue).catch(() => 0).then(flushQueue);
}
