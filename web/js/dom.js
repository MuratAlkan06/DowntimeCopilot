// Tiny DOM helpers. All text goes through textContent, never innerHTML.

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, String(v));
  }
  append(el, kids);
  return el;
}

function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

export function mount(el, ...kids) {
  if (!el) return;
  el.replaceChildren();
  append(el, kids);
}

/** Loading / empty / error / offline state box with an optional action button. */
export function stateBox(kind, message, action) {
  return h('div', { class: `state state-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    kind === 'loading' ? h('span', { class: 'spinner', 'aria-hidden': 'true' }) : null,
    h('span', {}, message),
    action ? h('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: action.onClick }, action.label) : null,
  );
}

export function chip(text, kind = 'neutral', title) {
  return h('span', { class: `chip chip-${kind}`, title }, text);
}

// The unit runs on the simulated clock (UTC); show that time, never the browser's local zone.
// Pass { unit: true } where a time stands alone (banners, packet header, digest window).
const UNIT = ' unit time';

export function fmtTime(iso, { unit = false } = {}) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? String(iso)
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + (unit ? UNIT : '');
}

export function fmtDateTime(iso, { unit = false } = {}) {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? String(iso)
    : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })
      + (unit ? UNIT : '');
}

export function fmtAgo(ms) {
  const m = Math.floor(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  return `${Math.floor(m / 60)} h ${m % 60} min ago`;
}

export const labelize = (s) => String(s ?? '').replace(/_/g, ' ');

export const usd = (n) => (typeof n === 'number' ? `$${n.toFixed(n < 1 ? 4 : 2)}` : '—');

/** Accept either a bare array or an envelope object holding the array under one of `keys`. */
export function asList(data, ...keys) {
  if (Array.isArray(data)) return data;
  if (data && typeof data === 'object') {
    for (const k of keys) if (Array.isArray(data[k])) return data[k];
  }
  return [];
}

/** "Cached copy" note shown when a response came from the service worker cache. */
export function cachedNote(res) {
  if (!res?.cachedAt) return null;
  return h('p', { class: 'cached-note' }, `Cached copy from ${fmtDateTime(res.cachedAt)}`);
}
