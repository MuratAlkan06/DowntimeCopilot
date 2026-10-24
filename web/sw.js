// Service worker: network-first navigations (cached shell offline), cache-first shell assets,
// stale-while-revalidate packets, network-first other API GETs.
// Bump VERSION whenever shell files change.
const VERSION = 'v19';
const SHELL_CACHE = `dc-shell-${VERSION}`;
const DATA_CACHE = `dc-data-${VERSION}`;

const SHELL = [
  './', 'index.html', 'app.js', 'styles.css', 'print.css', 'manifest.webmanifest', 'icon.svg',
  'js/api.js', 'js/dom.js', 'js/state.js', 'js/rules.js', 'js/packets.js', 'js/records.js', 'js/during.js', 'js/checklists.js', 'js/recovery.js',
  'js/queue.js', 'js/recorder.js',
];

const SHELL_EXT = /\.(?:html|js|css|webmanifest|svg|png|ico)$/;
const PACKET = /\/patients\/[^/]+\/packet$/;

// Offline replay of AI output (S5). GET /digest needs nothing extra: networkFirst keeps the last digest
// under its URL. POST /policy/ask cannot be a cache key (the Cache API stores GETs only), so the last
// answer to each question is stored under a synthetic GET key, /policy/ask?replay=<normalized question>,
// keeping the newest REPLAY_MAX. Network down: the stored answer comes back marked x-dc-fallback; a
// question never asked on this device still gets the offline 503. With "Simulate offline" the page never
// fetches, so api.js reads the same key itself (policyReplayKey there must stay in step with this one).
const POLICY = '/policy/ask';
const REPLAY_MAX = 20;
const normQuestion = (q) => String(q ?? '').trim().replace(/\s+/g, ' ').toLowerCase();
const replayKey = (origin, q) => `${origin}${POLICY}?replay=${encodeURIComponent(normQuestion(q))}`;

self.addEventListener('install', (event) => {
  // cache: 'reload' bypasses the HTTP cache, so a VERSION bump never installs a heuristically cached old file.
  const fresh = SHELL.map((u) => new Request(u, { cache: 'reload' }));
  event.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(fresh)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('dc-') && k !== SHELL_CACHE && k !== DATA_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

async function withHeaders(res, extra) {
  const headers = new Headers(res.headers);
  for (const [k, v] of Object.entries(extra)) headers.set(k, v);
  return new Response(await res.clone().blob(), { status: res.status, statusText: res.statusText, headers });
}

const offlineResponse = () => new Response(JSON.stringify({ detail: 'Offline and not cached on this device yet' }), {
  status: 503, headers: { 'Content-Type': 'application/json', 'x-dc-offline': '1' },
});

// x-dc-cached-at marks cache provenance for the page; it is never rendered (UI shows unit time only).
async function putStamped(cacheName, req, res) {
  if (!res || !res.ok) return;
  const cache = await caches.open(cacheName);
  await cache.put(req, await withHeaders(res, { 'x-dc-cached-at': new Date().toISOString() }));
}

// Cache-first for the shell; refresh the cached copy in the background so teammates' edits land on next reload.
async function cacheFirst(event) {
  const req = event.request;
  const hit = await caches.match(req, { ignoreSearch: true });
  if (hit) {
    event.waitUntil(fetch(req).then(async (res) => { if (res.ok) await (await caches.open(SHELL_CACHE)).put(req, res); }).catch(() => {}));
    return hit;
  }
  try {
    const res = await fetch(req);
    if (res.ok) (await caches.open(SHELL_CACHE)).put(req, res.clone());
    return res;
  } catch {
    return offlineResponse();
  }
}

// Navigations: network first, so an online reload always gets the current shell. Offline (or a server
// error) falls back to the cached shell with the query ignored, so /?phase=during reloads with no network.
async function navigate(event) {
  const req = event.request;
  const scope = self.registration.scope;
  const url = new URL(req.url);
  const isShell = url.pathname === new URL(scope).pathname || url.pathname === new URL('index.html', scope).pathname;
  let res = null;
  try {
    res = await fetch(req);
    if (res.ok) {
      if (isShell) event.waitUntil(caches.open(SHELL_CACHE).then((c) => c.put(url.origin + url.pathname, res.clone())).catch(() => {}));
      return res;
    }
  } catch { /* offline: cached shell below */ }
  const cached = (await caches.match(req, { ignoreSearch: true })) || (await caches.match(new URL('index.html', scope).href));
  return cached || res || offlineResponse();
}

async function staleWhileRevalidate(event) {
  const req = event.request;
  const cached = await caches.match(req, { ignoreVary: true });
  const network = fetch(req).then(async (res) => { await putStamped(DATA_CACHE, req, res.clone()); return res; });
  if (cached) {
    event.waitUntil(network.catch(() => {}));
    return cached;
  }
  try { return await network; } catch { return offlineResponse(); }
}

async function networkFirst(req) {
  try {
    const res = await fetch(req);
    await putStamped(DATA_CACHE, req, res.clone());
    return res;
  } catch {
    const cached = await caches.match(req, { ignoreVary: true });
    return cached ? withHeaders(cached, { 'x-dc-fallback': '1' }) : offlineResponse();
  }
}

async function policyReplay(req) {
  let key = null;
  try { key = replayKey(new URL(req.url).origin, (await req.clone().json()).question); } catch { /* no replay */ }
  try {
    const res = await fetch(req);
    if (key && res.ok) {
      await putStamped(DATA_CACHE, key, res.clone());
      const cache = await caches.open(DATA_CACHE);
      const olds = (await cache.keys()).filter((k) => new URL(k.url).pathname === POLICY);
      await Promise.all(olds.slice(0, Math.max(0, olds.length - REPLAY_MAX)).map((k) => cache.delete(k)));
    }
    return res;
  } catch {
    const hit = key && await caches.match(key);
    return hit ? withHeaders(hit, { 'x-dc-fallback': '1' }) : offlineResponse();
  }
}

// Background Sync (Chromium): the page registers SYNC_TAG when it queues a capture offline. The queue,
// its lock and the upload live in the page (web/js/queue.js), so the worker only wakes open pages; with
// none open the queue drains on the next start. Other browsers: the page's online event and interval.
const SYNC_TAG = 'dc-capture-drain';
self.addEventListener('sync', (event) => {
  if (event.tag !== SYNC_TAG) return;
  event.waitUntil(self.clients.matchAll({ type: 'window' }).then((cs) => cs.forEach((c) => c.postMessage({ type: SYNC_TAG }))));
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.method === 'POST' && url.pathname === POLICY) { event.respondWith(policyReplay(req)); return; }
  if (req.method !== 'GET') return; // Other POSTs go straight to the network; the page queues on failure.
  const scopePath = new URL(self.registration.scope).pathname;

  if (req.mode === 'navigate') {
    event.respondWith(navigate(event));
  } else if (url.pathname === scopePath || SHELL_EXT.test(url.pathname)) {
    event.respondWith(cacheFirst(event));
  } else if (PACKET.test(url.pathname)) {
    event.respondWith(staleWhileRevalidate(event));
  } else {
    event.respondWith(networkFirst(req));
  }
});
