// Verbal-order read-back recorder (S9). MediaRecorder, feature-detected; no dependencies.
// States: idle -> recording -> attached (a clip is ready), or denied (microphone refused) / unsupported
// (no MediaRecorder, no getUserMedia, no microphone). The clip becomes a File that goes through the same
// upload path and offline queue as a chosen audio file. Every microphone track is stopped when recording
// stops, fails or is discarded. The file picker and the typed read-back stay usable in every state.

export const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
const EXT = { webm: 'webm', mp4: 'm4a', ogg: 'ogg' };

export function supported(env = globalThis) {
  return typeof env.MediaRecorder === 'function' && typeof env.navigator?.mediaDevices?.getUserMedia === 'function';
}

// Server rule (app/api/capture.py): a typed read-back wins; attached audio is saved but not transcribed.
// True when both are present, so the form can say so before submit.
export function typedOverridesAudio(audio, text) {
  return Boolean(audio) && String(text ?? '').trim() !== '';
}

export function pickMime(MR) {
  return MIME_CANDIDATES.find((t) => { try { return MR.isTypeSupported?.(t); } catch { return false; } }) || '';
}

export function createRecorder({ env = globalThis, onChange = () => {}, now = () => Date.now() } = {}) {
  let state = supported(env) ? 'idle' : 'unsupported';
  let rec = null;
  let stream = null;
  let chunks = [];
  let file = null;
  let error = '';
  let startedAt = 0;
  let seconds = 0;
  const self = {
    get state() { return state; },
    get file() { return file; },
    get error() { return error; },
    get seconds() { return seconds; },
  };
  const set = (s) => { state = s; onChange(self); };
  const stopTracks = () => { stream?.getTracks().forEach((t) => t.stop()); stream = null; };

  self.start = async () => {
    if (state === 'unsupported' || state === 'recording') return;
    file = null;
    try {
      stream = await env.navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      error = e?.name || 'Error';
      // NotFoundError: no microphone at all, so recording is unsupported here; anything else is a refusal.
      set(error === 'NotFoundError' ? 'unsupported' : 'denied');
      return;
    }
    const mimeType = pickMime(env.MediaRecorder);
    try {
      rec = new env.MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    } catch (e) {
      stopTracks();
      error = e?.name || 'Error';
      set('unsupported');
      return;
    }
    chunks = [];
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      stopTracks();
      seconds = Math.max(0, Math.round((now() - startedAt) / 1000));
      const type = String(rec.mimeType || mimeType || 'audio/webm').split(';')[0].trim();
      const sub = type.split('/')[1] || 'webm';
      const blob = new env.Blob(chunks, { type });
      chunks = [];
      file = blob.size ? new env.File([blob], `readback.${EXT[sub] || sub}`, { type }) : null;
      set(file ? 'attached' : 'idle');
    };
    startedAt = now();
    rec.start();
    error = '';
    set('recording');
  };

  self.stop = () => {
    if (state === 'recording' && rec && rec.state !== 'inactive') rec.stop(); // onstop stops the tracks
    else stopTracks();
  };

  self.discard = () => {
    if (state === 'recording' && rec) { rec.onstop = null; try { rec.stop(); } catch { /* already stopped */ } }
    stopTracks();
    file = null;
    chunks = [];
    if (state === 'recording' || state === 'attached') set('idle');
  };

  return self;
}
