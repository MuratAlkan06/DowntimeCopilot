// Offline capture queue (S7). Each item is one full POST /capture submission (image Blob + form fields)
// under a client-generated id; the server returns the existing record when an id is re-sent, so a
// retry after an ambiguous failure can never create a second record. An item leaves the queue only
// after a 2xx. No DOM access here: during.js renders, node tests drive this module with memoryStore().

export const DB_NAME = 'dc-queue';
const OBJECTS = 'captures';
export const LEGACY_KEY = 'dc.pendingCaptures'; // pre-S7 localStorage queue (data URLs)
export const SYNC_TAG = 'dc-capture-drain'; // Background Sync tag; sw.js wakes the page with it
const LOCK = 'dc-capture-drain'; // Web Locks name: one drain per origin across tabs (and the SW)

export const newId = () => (globalThis.crypto?.randomUUID
  ? crypto.randomUUID()
  : `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`);

/** Store interface: put(item) (upsert by id), list() (oldest first), delete(id). */
export function memoryStore() {
  const items = new Map();
  return {
    put: async (item) => { items.set(item.id, item); },
    list: async () => [...items.values()].sort(byQueuedAt),
    delete: async (id) => { items.delete(id); },
  };
}

const byQueuedAt = (a, b) => String(a.queued_at ?? '').localeCompare(String(b.queued_at ?? ''));

/** IndexedDB-backed store (Blobs stored as-is). Falls back to memory if IndexedDB is unavailable. */
export function idbStore(idb = globalThis.indexedDB) {
  if (!idb) return memoryStore();
  let opened = null;
  const db = () => (opened ||= new Promise((resolve, reject) => {
    const req = idb.open(DB_NAME, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(OBJECTS, { keyPath: 'id' });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => { opened = null; reject(req.error); };
  }));
  const run = async (mode, fn) => {
    const d = await db();
    return new Promise((resolve, reject) => {
      const tx = d.transaction(OBJECTS, mode);
      const req = fn(tx.objectStore(OBJECTS));
      tx.oncomplete = () => resolve(req.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  };
  return {
    put: (item) => run('readwrite', (s) => s.put(item)).then(() => undefined),
    list: () => run('readonly', (s) => s.getAll()).then((xs) => (xs || []).sort(byQueuedAt)),
    delete: (id) => run('readwrite', (s) => s.delete(id)).then(() => undefined),
  };
}

let inFlight = false; // same-context guard; also the whole guard where Web Locks is missing

/** Run `fn` unless a drain is already running here or (Web Locks) in another tab. */
export async function withDrainLock(fn, locks = globalThis.navigator?.locks) {
  if (inFlight) return { skipped: true };
  inFlight = true;
  try {
    if (locks?.request) return await locks.request(LOCK, { ifAvailable: true }, (lock) => (lock ? fn() : { skipped: true }));
    return await fn();
  } finally {
    inFlight = false;
  }
}

// Dead letter (#55): an item the server keeps refusing (a deterministic 4xx, say) would otherwise be retried
// on every drain forever. After MAX_ATTEMPTS answered failures it is marked status 'dead': kept in the store
// (never deleted silently), skipped by drain, counted apart, and sent again only by retryDead.
export const MAX_ATTEMPTS = 8;
export const DEAD = 'dead';
export const isDead = (item) => item?.status === DEAD;

/** Send every live queued item, oldest first. `send(item)` resolves to { ok, offline } (api.js shape).
 *  2xx -> deleted; an answered failure -> attempts + 1 and kept (dead at MAX_ATTEMPTS); offline or a thrown
 *  send -> stop without counting (the rest would fail too). `left` counts live items only. */
export function drain(store, send, { onSent, locks = globalThis.navigator?.locks } = {}) {
  return withDrainLock(async () => {
    let sent = 0;
    for (const item of await store.list()) {
      if (isDead(item)) continue;
      let r = null;
      try { r = await send(item); } catch { r = null; }
      if (r?.ok) {
        await store.delete(item.id);
        sent += 1;
        await onSent?.(item, r);
      } else if (!r || r.offline) break;
      else {
        const attempts = (Number(item.attempts) || 0) + 1;
        await store.put({ ...item, attempts, ...(attempts >= MAX_ATTEMPTS ? { status: DEAD } : {}) });
      }
    }
    return { sent, left: (await counts(store)).active };
  }, locks);
}

/** { active, dead }: live items drain will send, and dead-lettered items waiting for retryDead. */
export async function counts(store) {
  const items = await store.list();
  const dead = items.filter(isDead).length;
  return { active: items.length - dead, dead };
}

/** Put dead items back in the queue with a fresh attempt budget: one id, or every dead item. Returns how many. */
export async function retryDead(store, id) {
  let n = 0;
  for (const item of await store.list()) {
    if (!isDead(item) || (id != null && item.id !== id)) continue;
    const { status: _s, attempts: _a, ...rest } = item;
    await store.put(rest);
    n += 1;
  }
  return n;
}

export function dataUrlToBlob(url) {
  const [head, b64] = String(url).split(',');
  const mime = /data:([^;]+)/.exec(head)?.[1] || 'application/octet-stream';
  const bin = atob(b64 || '');
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

/** One-time move of the pre-S7 localStorage queue into `store`, then clear the key. Ids are derived
 *  from position + queue time, so a migration interrupted before the key is cleared re-runs without
 *  duplicating an item (put is an upsert) and the server sees the same id. Returns items moved. */
export async function migrateLegacy(store, ls = globalThis.localStorage) {
  let raw = null;
  try { raw = ls?.getItem(LEGACY_KEY); } catch { return 0; }
  if (raw == null) return 0;
  let items = [];
  try { items = JSON.parse(raw) || []; } catch { items = []; }
  if (!Array.isArray(items)) items = [];
  for (const [i, it] of items.entries()) {
    const { dataUrl, ...meta } = it || {};
    await store.put({ ...meta, id: meta.id || `legacy-${meta.queued_at || 'unknown'}-${i}`, blob: dataUrl ? dataUrlToBlob(dataUrl) : null });
  }
  try { ls.removeItem(LEGACY_KEY); } catch { /* private mode */ }
  return items.length;
}
