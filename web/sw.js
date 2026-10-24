// Service worker: cache-first app shell, stale-while-revalidate packets, network-first other API GETs.
// Bump VERSION whenever shell files change.
const VERSION = 'v1';
const SHELL_CACHE = `dc-shell-${VERSION}`;
const DATA_CACHE = `dc-data-${VERSION}`;

const SHELL = [
  './', 'index.html', 'app.js', 'styles.css', 'print.css', 'manifest.webmanifest', 'icon.svg',
  'js/api.js', 'js/dom.js', 'js/state.js', 'js/packets.js', 'js/records.js', 'js/during.js', 'js/recovery.js',
];

const SHELL_EXT = /\.(?:html|js|css|webmanifest|svg|png|ico)$/;
const PACKET = /\/patients\/[^/]+\/packet$/;

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
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
    if (req.mode === 'navigate') return (await caches.match('index.html')) || offlineResponse();
    return offlineResponse();
  }
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

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // POSTs go straight to the network; the page queues on failure.
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const scopePath = new URL(self.registration.scope).pathname;

  if (req.mode === 'navigate' || url.pathname === scopePath || SHELL_EXT.test(url.pathname)) {
    event.respondWith(cacheFirst(event));
  } else if (PACKET.test(url.pathname)) {
    event.respondWith(staleWhileRevalidate(event));
  } else {
    event.respondWith(networkFirst(req));
  }
});
