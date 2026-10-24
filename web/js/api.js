// Fetch wrapper: same-origin relative URLs, never throws, tracks connectivity.
// Result shape: { ok, status, data, error, offline, cachedAt }.
import { store } from './state.js';

const listeners = new Set();
const SIM_KEY = 'dc.simOffline';

export const net = {
  browserOnline: navigator.onLine,
  fetchFailed: false,
  simulated: (() => { try { return localStorage.getItem(SIM_KEY) === '1'; } catch { return false; } })(),
};

export const isOffline = () => !net.browserOnline || net.fetchFailed || net.simulated;

export function onNetChange(fn) { listeners.add(fn); }

let lastOffline = isOffline();
function emit() {
  const now = isOffline();
  listeners.forEach((fn) => fn(now, { ...net, wentOnline: lastOffline && !now }));
  lastOffline = now;
}

window.addEventListener('online', () => { net.browserOnline = true; net.fetchFailed = false; emit(); });
window.addEventListener('offline', () => { net.browserOnline = false; emit(); });

export function setSimulatedOffline(v) {
  net.simulated = !!v;
  try { localStorage.setItem(SIM_KEY, v ? '1' : '0'); } catch { /* ignore */ }
  if (!v) net.fetchFailed = false;
  emit();
}

function setFetchFailed(v) {
  if (net.fetchFailed === v) return;
  net.fetchFailed = v;
  emit();
}

function errorText(data, status) {
  if (data && typeof data === 'object') {
    const d = data.detail ?? data.error ?? data.message;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join('; ');
  }
  if (typeof data === 'string' && data.length < 200 && !data.trim().startsWith('<')) return data;
  return `HTTP ${status}`;
}

async function parse(res) {
  const ct = res.headers.get('content-type') || '';
  try { return ct.includes('json') ? await res.json() : await res.text(); } catch { return null; }
}

async function fromCache(path) {
  try {
    if (!('caches' in window)) throw new Error('no cache api');
    const hit = await caches.match(new URL(path, location.href).href, { ignoreVary: true });
    if (hit) return { ok: true, status: hit.status, data: await parse(hit), cachedAt: hit.headers.get('x-dc-cached-at') || 'unknown', offline: true };
  } catch { /* fall through */ }
  return { ok: false, status: 0, data: null, offline: true, error: 'Offline and not cached on this device yet' };
}

// Cache key of the last answer to a policy question; must match replayKey in sw.js, which writes it.
const normQuestion = (q) => String(q ?? '').trim().replace(/\s+/g, ' ').toLowerCase();
export const policyReplayKey = (q) => `/policy/ask?replay=${encodeURIComponent(normQuestion(q))}`;

/** `replay`: cache key a read-only POST (policy answers) falls back to when offline. */
export async function api(path, { method = 'GET', json, form, headers, replay } = {}) {
  // Every write carries the role switcher's role (X-Role): the server audits it as the actor.
  const role = method === 'GET' ? {} : { 'X-Role': store.role };
  const init = { method, headers: { Accept: 'application/json', ...role, ...(headers || {}) } };
  if (json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(json); }
  if (form) init.body = form;

  if (net.simulated) {
    if (method === 'GET' || replay) return fromCache(replay || path);
    return { ok: false, status: 0, data: null, offline: true, error: 'Offline — action not sent' };
  }

  let res;
  try {
    res = await fetch(path, init);
  } catch {
    setFetchFailed(true);
    if (method === 'GET' || replay) return fromCache(replay || path);
    return { ok: false, status: 0, data: null, offline: true, error: 'Network unreachable — action not sent' };
  }

  // The service worker marks responses it had to answer without the network.
  const swOffline = res.headers.get('x-dc-offline') === '1';
  const swFallback = res.headers.get('x-dc-fallback') === '1';
  setFetchFailed(swOffline || swFallback);

  const data = await parse(res);
  if (!res.ok) return { ok: false, status: res.status, data, offline: swOffline, error: errorText(data, res.status) };
  return { ok: true, status: res.status, data, cachedAt: res.headers.get('x-dc-cached-at'), offline: swFallback };
}
