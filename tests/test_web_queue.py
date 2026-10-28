"""web/js/queue.js: the offline capture queue drains each item once, deletes it only after a 2xx, keeps it
on failure, never runs two drains at once, and moves a pre-S7 localStorage queue over once. An item the server
keeps refusing is dead-lettered after MAX_ATTEMPTS answered failures: kept, skipped, counted, retryable (#55).
web/js/during.js queues a first submit that got a real 5xx under the same client_id, and never a 4xx (#55).
Runs the real modules in node against the in-memory store (no IndexedDB, no new deps; skipped if node is absent)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB_JS = Path(__file__).resolve().parent.parent / "web" / "js"
MODULE = WEB_JS / "queue.js"


def run(tmp_path, body: str):
    mod = tmp_path / "queue.mjs"
    mod.write_text(MODULE.read_text())
    script = f"import * as Q from {json.dumps(mod.as_uri())};\n" + body
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


ITEMS = (
    "const s = Q.memoryStore();\n"
    "await s.put({ id: 'b', form_type: 'vitals_flowsheet', role: 'rn', queued_at: '2026-10-24T03:02:00Z', blob: new Blob(['b']) });\n"
    "await s.put({ id: 'a', form_type: 'downtime_mar', role: 'pharmacy', queued_at: '2026-10-24T03:01:00Z', blob: new Blob(['a']) });\n"
)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_drain_sends_oldest_first_and_deletes_only_on_success(tmp_path):
    got = run(tmp_path, ITEMS + (
        "const sent = [];\n"
        "const r = await Q.drain(s, async (it) => { sent.push([it.id, it.role]); return { ok: true, status: 201, data: {} }; }, { locks: null });\n"
        "console.log(JSON.stringify({ r, sent, left: (await s.list()).length }));"
    ))
    assert got == {"r": {"sent": 2, "left": 0}, "sent": [["a", "pharmacy"], ["b", "rn"]], "left": 0}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_failed_item_stays_for_retry_and_offline_stops_the_drain(tmp_path):
    got = run(tmp_path, ITEMS + (
        "const calls = [];\n"
        "let first = true;\n"
        "const flaky = async (it) => { calls.push(it.id);\n"
        "  if (it.id === 'a' && first) { first = false; return { ok: false, status: 500 }; }\n"
        "  return { ok: true, status: 201 }; };\n"
        "const r1 = await Q.drain(s, flaky, { locks: null });\n"
        "const after1 = (await s.list()).map((x) => x.id);\n"
        "const r2 = await Q.drain(s, flaky, { locks: null });\n"
        "await s.put({ id: 'c', queued_at: '2026-10-24T03:03:00Z' }); await s.put({ id: 'd', queued_at: '2026-10-24T03:04:00Z' });\n"
        "const offCalls = [];\n"
        "const r3 = await Q.drain(s, async (it) => { offCalls.push(it.id); return { ok: false, offline: true }; }, { locks: null });\n"
        "const r4 = await Q.drain(s, async () => { throw new Error('boom'); }, { locks: null });\n"
        "console.log(JSON.stringify({ r1, after1, r2, calls, r3, offCalls, r4 }));"
    ))
    assert got["r1"] == {"sent": 1, "left": 1} and got["after1"] == ["a"]
    assert got["r2"] == {"sent": 1, "left": 0} and got["calls"] == ["a", "b", "a"]
    assert got["r3"] == {"sent": 0, "left": 2} and got["offCalls"] == ["c"]  # stops at the first offline
    assert got["r4"] == {"sent": 0, "left": 2}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_concurrent_drains_send_each_item_once(tmp_path):
    got = run(tmp_path, ITEMS + (
        "const sent = [];\n"
        "let release; const gate = new Promise((r) => { release = r; });\n"
        "const slow = async (it) => { sent.push(it.id); await gate; return { ok: true, status: 201 }; };\n"
        # In-flight flag (no Web Locks): a second drain while the first is running is skipped.
        "const p1 = Q.drain(s, slow, { locks: null });\n"
        "const p2 = await Q.drain(s, slow, { locks: null });\n"
        "release();\n"
        "const r1 = await p1;\n"
        # Web Locks held by another tab: request(..., {ifAvailable}) hands the callback null.
        "await s.put({ id: 'c', queued_at: '2026-10-24T03:05:00Z' });\n"
        "const opts = [];\n"
        "const held = { request: async (name, o, cb) => { opts.push([name, o]); return cb(null); } };\n"
        "const r3 = await Q.drain(s, slow, { locks: held });\n"
        "const free = { request: async (name, o, cb) => cb({ name }) };\n"
        "const r4 = await Q.drain(s, async (it) => { sent.push(it.id); return { ok: true }; }, { locks: free });\n"
        "console.log(JSON.stringify({ p2, r1, sent, r3, opts, r4 }));"
    ))
    assert got["p2"] == {"skipped": True} and got["r1"] == {"sent": 2, "left": 0}
    assert got["r3"] == {"skipped": True} and got["opts"] == [["dc-capture-drain", {"ifAvailable": True}]]
    assert got["r4"] == {"sent": 1, "left": 0}
    assert got["sent"] == ["a", "b", "c"]  # every item sent exactly once


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_legacy_localstorage_queue_moves_once(tmp_path):
    got = run(tmp_path, (
        "const mem = new Map();\n"
        "let failRemove = true;\n"
        "const ls = { getItem: (k) => mem.has(k) ? mem.get(k) : null, setItem: (k, v) => mem.set(k, String(v)),\n"
        "  removeItem: (k) => { if (failRemove) { failRemove = false; throw new Error('quota'); } mem.delete(k); } };\n"
        "const png = 'data:image/png;base64,' + Buffer.from('fake-png').toString('base64');\n"
        "ls.setItem(Q.LEGACY_KEY, JSON.stringify([\n"
        "  { form_type: 'vitals_flowsheet', role: 'rn', patient_id: 'pt-12', dataUrl: png, name: 'v.png', queued_at: '2026-10-24T03:00:00Z' },\n"
        "  { form_type: 'verbal_order', role: 'rn', readback_text: 'heparin', dataUrl: null, queued_at: '2026-10-24T03:01:00Z' }]));\n"
        "const s = Q.memoryStore();\n"
        "const n1 = await Q.migrateLegacy(s, ls);\n"  # removeItem fails once: the key survives
        "const n2 = await Q.migrateLegacy(s, ls);\n"  # re-run: same ids, so no duplicates
        "const n3 = await Q.migrateLegacy(s, ls);\n"  # key gone: nothing to do
        "const items = await s.list();\n"
        "console.log(JSON.stringify({ n1, n2, n3, key: ls.getItem(Q.LEGACY_KEY), ids: items.map((x) => x.id),\n"
        "  blob: items[0].blob instanceof Blob ? [items[0].blob.type, await items[0].blob.text()] : null,\n"
        "  noDataUrl: items.every((x) => !('dataUrl' in x)), second: [items[1].blob, items[1].readback_text] }));"
    ))
    assert (got["n1"], got["n2"], got["n3"]) == (2, 2, 0) and got["key"] is None
    assert got["ids"] == ["legacy-2026-10-24T03:00:00Z-0", "legacy-2026-10-24T03:01:00Z-1"]
    assert got["blob"] == ["image/png", "fake-png"] and got["noDataUrl"]
    assert got["second"] == [None, "heparin"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_answered_failures_dead_letter_an_item_at_the_cap(tmp_path):
    got = run(tmp_path, ITEMS + (
        "const calls = [];\n"
        "const poison = async (it) => { calls.push(it.id); return it.id === 'a' ? { ok: false, status: 422 } : { ok: false, offline: true }; };\n"
        "const seen = [];\n"
        "for (let i = 0; i < Q.MAX_ATTEMPTS + 2; i++) {\n"
        "  const r = await Q.drain(s, async (it) => (it.id === 'a' ? { ok: false, status: 422 } : { ok: true, status: 201 }), { locks: null });\n"
        "  const a = (await s.list()).find((x) => x.id === 'a');\n"
        "  seen.push([r.left, a?.attempts ?? null, a?.status ?? null]);\n"
        "}\n"
        # offline and a thrown send never count toward the cap
        "await s.put({ id: 'o', queued_at: '2026-10-24T03:09:00Z' });\n"
        "for (let i = 0; i < 10; i++) await Q.drain(s, async () => ({ ok: false, offline: true }), { locks: null });\n"
        "for (let i = 0; i < 10; i++) await Q.drain(s, async () => { throw new Error('boom'); }, { locks: null });\n"
        "const o = (await s.list()).find((x) => x.id === 'o');\n"
        "console.log(JSON.stringify({ max: Q.MAX_ATTEMPTS, seen, o: [o.attempts ?? 0, o.status ?? null], counts: await Q.counts(s) }));"
    ))
    assert got["max"] == 8
    # drain 1 sends both: b is accepted, a gets its first counted failure; a dies on the 8th
    assert got["seen"][0] == [1, 1, None]
    assert got["seen"][6] == [1, 7, None]
    assert got["seen"][7] == [0, 8, "dead"]  # zero active: the queue is no longer stalled
    assert got["seen"][8:] == [[0, 8, "dead"], [0, 8, "dead"]]  # dead: never sent again, attempts frozen
    assert got["o"] == [0, None]  # offline results and thrown sends do not count
    assert got["counts"] == {"active": 1, "dead": 1}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dead_item_is_kept_skipped_and_retryable(tmp_path):
    got = run(tmp_path, ITEMS + (
        "await s.put({ ...(await s.list())[0], attempts: Q.MAX_ATTEMPTS, status: 'dead' });\n"  # 'a' is dead
        "const sent = [];\n"
        "const r1 = await Q.drain(s, async (it) => { sent.push(it.id); return { ok: true, status: 201 }; }, { locks: null });\n"
        "const kept = (await s.list()).map((x) => [x.id, x.status]);\n"
        "const c1 = await Q.counts(s);\n"
        "const none = await Q.retryDead(s, 'nope');\n"
        "const n = await Q.retryDead(s, 'a');\n"
        "const back = (await s.list()).map((x) => [x.id, x.status ?? null, x.attempts ?? null]);\n"
        "const r2 = await Q.drain(s, async (it) => { sent.push(it.id); return { ok: true, status: 201 }; }, { locks: null });\n"
        # retry-all form: two dead items come back together
        "await s.put({ id: 'x', status: 'dead', attempts: 8, queued_at: '1' }); await s.put({ id: 'y', status: 'dead', attempts: 8, queued_at: '2' });\n"
        "const all = await Q.retryDead(s);\n"
        "console.log(JSON.stringify({ r1, kept, c1, none, n, back, r2, sent, all, c2: await Q.counts(s) }));"
    ))
    assert got["r1"] == {"sent": 1, "left": 0} and got["kept"] == [["a", "dead"]]  # skipped, not deleted
    assert got["c1"] == {"active": 0, "dead": 1}
    assert got["none"] == 0 and got["n"] == 1 and got["back"] == [["a", None, None]]  # fresh budget
    assert got["r2"] == {"sent": 1, "left": 0} and got["sent"] == ["b", "a"]
    assert got["all"] == 2 and got["c2"] == {"active": 2, "dead": 0}


# Just enough DOM for during.js initDuring/submitCapture/renderPending and dom.js h()/mount()/stateBox().
DURING = r"""
class El {
  constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.className = ''; this.value = ''; this.files = []; this.hidden = false; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, fn) { (this.on ??= {})[t] = fn; }
  focus() {}
  reset() {}
  querySelector() { return new El('button'); }
  get classList() { return { contains: (c) => this.className.split(' ').includes(c) }; }
  get childNodes() { return this.kids; }
  append(...ks) { this.kids.push(...ks); }
  replaceChildren(...ks) { this.kids = ks; }
  get textContent() { return this.kids.map((k) => k.textContent).join(''); }
  set textContent(v) { this.kids = [new Text(v)]; }
}
class Text { constructor(t) { this.t = t; } get textContent() { return this.t; } }
const els = new Map();
globalThis.Node = Object;
globalThis.document = { activeElement: null, createElement: (t) => new El(t), createTextNode: (t) => new Text(t),
  getElementById: (id) => { if (!els.has(id)) els.set(id, new El('div')); return els.get(id); } };
globalThis.window = { addEventListener() {} };
Object.defineProperty(globalThis, 'navigator', { value: { onLine: true }, configurable: true, writable: true });
const mem = new Map();
globalThis.localStorage = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
globalThis.location = { href: 'http://test/' };
globalThis.setInterval = () => 0;
const posts = []; // [client_id, answered status]
let answer = 201;
globalThis.fetch = async (path, init = {}) => {
  if (path !== '/capture') return new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } });
  if (answer === 'throw') { posts.push([init.body.get('client_id'), 0]); throw new TypeError('Failed to fetch'); }
  posts.push([init.body.get('client_id'), answer]);
  const body = answer < 300 ? { record_id: `rec-${posts.length}`, form_type: 'verbal_order', status: 'needs_review', fields: [] }
    : { detail: `harness ${answer}` };
  return new Response(JSON.stringify(body), { status: answer, headers: { 'content-type': 'application/json' } });
};
const $ = (id) => document.getElementById(id);
const D = await import(DURING_URI);
const A = await import(API_URI);
D.initDuring();
await new Promise((r) => setTimeout(r, 0));
const submit = async (status) => {
  answer = status;
  $('capture-type').value = 'verbal_order'; $('capture-patient').value = 'pt-07'; $('capture-readback').value = 'heparin 5000 units';
  await $('capture-form').on.submit({ preventDefault() {}, submitter: new El('button') });
  await new Promise((r) => setTimeout(r, 0)); // submitCapture does not await its renderPending
};
const find = (el, tag) => (el.tag === tag ? el : (el.kids || []).map((k) => find(k, tag)).find(Boolean));
const view = () => ({ queued: $('queue-count').hidden ? null : $('queue-count').textContent,
  failed: $('queue-failed').hidden ? null : $('queue-failed').textContent,
  pending: $('capture-pending').textContent, result: $('capture-result').textContent });
"""


def run_during(tmp_path, body: str):
    for p in WEB_JS.glob("*.js"):
        (tmp_path / p.name).write_text(p.read_text())
    (tmp_path / "package.json").write_text('{"type": "module"}')
    script = (DURING.replace("DURING_URI", json.dumps((tmp_path / "during.js").as_uri()))
              .replace("API_URI", json.dumps((tmp_path / "api.js").as_uri())) + body + "\nprocess.exit(0);")
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("status", [500, 503, 408, 429])
def test_first_submit_retryable_queues_the_same_client_id(tmp_path, status):
    """#55: the server may have committed before the 5xx, so the retry must carry the SAME id (dedupe returns
    that record). A resubmit would mint a new id: the duplicate this closes. 408 and 429 are 4xx but transient
    (timeout, throttle), so they take the same path."""
    got = run_during(tmp_path, f"""
await submit({status});
const queued = view();
answer = 201;
await D.flushQueue();
const drained = view();
console.log(JSON.stringify({{ queued, drained, posts }}));
""")
    assert got["queued"]["queued"] == "1 queued" and got["queued"]["failed"] is None
    label = {408: "Upload timed out", 429: "Rate-limited"}.get(status, "Server error")  # #59: 408/429 are not server errors
    assert f"{label} ({status})" in got["queued"]["result"]
    assert "will be retried automatically" in got["queued"]["result"]
    (first, s1), (retry, s2) = got["posts"]
    assert (s1, s2) == (status, 201) and first == retry and first  # one id, sent twice
    assert got["drained"]["queued"] is None and got["drained"]["pending"] == ""


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("status", [400, 422])
def test_first_submit_4xx_is_not_queued(tmp_path, status):
    """Negative control: any other 4xx is the request's own fault; nothing is queued, the error shows, no retry."""
    got = run_during(tmp_path, f"""
await submit({status});
const after = view();
await D.flushQueue();
console.log(JSON.stringify({{ after, posts }}));
""")
    assert got["after"]["queued"] is None and f"Couldn't read the form (harness {status})" in got["after"]["result"]
    assert [s for _, s in got["posts"]] == [status]  # the drain had nothing to send


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dead_lettered_capture_shows_failed_and_retries_on_request(tmp_path):
    got = run_during(tmp_path, """
await submit(500);
answer = 400;
for (let i = 0; i < 8; i++) await D.flushQueue();
const dead = view();
const btn = find($('capture-pending'), 'button');
const label = btn.textContent;
await D.flushQueue();  // a dead item is not sent again by the normal drain
const sentWhileDead = posts.length;
answer = 201;
await btn.on.click();
console.log(JSON.stringify({ dead, label, sentWhileDead, after: view(), posts }));
""")
    assert got["dead"]["queued"] is None and got["dead"]["failed"] == "1 failed"
    assert "could not upload after 8 tries" in got["dead"]["pending"] and got["label"] == "Retry failed uploads"
    assert got["sentWhileDead"] == 9  # 1 submit + 8 drains
    assert got["after"]["failed"] is None and got["after"]["queued"] is None
    ids = {c for c, _ in got["posts"]}
    assert len(ids) == 1 and got["posts"][-1][1] == 201  # the same id all the way through


# What a sighted user sees: skips nodes the page hid (the fake DOM keeps them in the tree, as the real one does).
VISIBLE = "const visible = (el) => (el.hidden ? '' : el.t ?? (el.kids || []).map(visible).join(''));\n"


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("first", [500, "'throw'"])
def test_dead_letter_withdraws_the_submit_notice(tmp_path, first):
    """#59 M1: the submit notice promises a retry ("will be retried automatically" after a server error, "queued for
    reading when connected" offline). It stays while its item is live and is withdrawn once the item dead-letters;
    the failed box says what happened instead."""
    got = run_during(tmp_path, VISIBLE + f"""
await submit({first});
A.setSimulatedOffline(false);  // back online (a thrown fetch left the page offline)
const queued = visible($('capture-result'));
answer = 422;
for (let i = 0; i < 3; i++) await D.flushQueue();
const live = visible($('capture-result'));
for (let i = 3; i < 8; i++) await D.flushQueue();
console.log(JSON.stringify({{ queued, live, dead: {{ ...view(), result: visible($('capture-result')),
  pending: visible($('capture-pending')) }} }}));
""")
    promise = "will be retried automatically" if first == 500 else "queued for reading when connected"
    assert promise in got["queued"] and promise in got["live"]  # still true while the item is live
    dead = got["dead"]
    assert dead["failed"] == "1 failed" and dead["queued"] is None
    assert promise not in dead["result"] and "form saved on this device" not in dead["result"]
    assert "Read fields appear here" in dead["result"]  # what was there before the submit stays
    assert "could not upload after 8 tries" in dead["pending"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_queued_notice_names_the_cause(tmp_path):
    """#59 M1: the queued box says "will upload when connected" only when the connection is the problem. A server
    that refuses the upload (a 5xx/408/429 first submit, or an answered failure on a drain) gets its own copy."""
    got = run_during(tmp_path, VISIBLE + """
const pending = () => visible($('capture-pending'));
const kind = () => $('capture-pending').kids[0]?.className;
const seen = {};
await submit('throw');  // network down: queued offline
seen.offline = [pending(), kind()];
A.setSimulatedOffline(false);
answer = 503;
await D.flushQueue();  // online again, and the server answers 503
seen.refusedOnDrain = [pending(), kind()];
A.setSimulatedOffline(true);
await D.flushQueue();  // the connection drops: that is the problem again
seen.offlineAgain = [pending(), kind()];
A.setSimulatedOffline(false);
answer = 201;
await D.flushQueue();
seen.drained = pending();
await submit(429);  // a refused first submit, before any drain
seen.refusedOnSubmit = [pending(), kind()];
console.log(JSON.stringify(seen));
""")
    offline = "1 capture queued on this device — will upload when connected."
    refused = "1 capture queued on this device — the server is not accepting uploads right now; retrying automatically."
    assert got["offline"][0].startswith(offline) and "state-offline" in got["offline"][1]
    assert got["refusedOnDrain"][0].startswith(refused) and "state-warn" in got["refusedOnDrain"][1]
    assert got["offlineAgain"][0].startswith(offline)
    assert got["drained"] == ""
    assert got["refusedOnSubmit"][0].startswith(refused) and "when connected" not in got["refusedOnSubmit"][0]


# Focus tracking for the fake DOM, patched here so the shared DURING prelude stays as it was.
FOCUS = (
    "El.prototype.focus = function () { document.activeElement = this; };\n"
    "const walk = (el) => [el, ...(el.kids || []).flatMap(walk)];\n"
    "const action = (label) => walk($('capture-pending')).find((e) => e.tag === 'button' && e.textContent === label);\n"
    "const focused = () => (document.activeElement === $('capture-result') ? '#capture-result'\n"
    "  : document.activeElement?.textContent ?? null);\n"
    "const press = async (label) => { const b = action(label); b.focus(); await b.on.click();\n"
    "  return { replaced: document.activeElement !== b, inPending: walk($('capture-pending')).includes(document.activeElement) }; };\n"
)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_queue_actions_keep_focus_through_the_redraw(tmp_path):
    """#59 M2 (WCAG 2.4.3): the redraw after "Upload now" or the retry action replaces the pressed button. Focus goes
    to the same action's new button, else the action still shown, else #capture-result where the upload's record
    lands, as recovery.js does for the discrepancy review (#38). A background drain never moves focus."""
    got = run_during(tmp_path, FOCUS + """
const seen = {};
await submit(500);
answer = 503;
seen.upload = { ...(await press('Upload now')), focused: focused() };  // still refused: same action
for (let i = 0; i < 6; i++) await press('Upload now');
seen.toRetry = { ...(await press('Upload now')), focused: focused() };  // 8th refusal: dead, only the retry is left
seen.retryRefused = { ...(await press('Retry failed uploads')), focused: focused() };  // back in the queue, refused again
answer = 201;
seen.uploaded = { ...(await press('Upload now')), focused: focused(), pending: $('capture-pending').textContent };
await submit(500);
const other = new El('button'); other.focus();  // the user is elsewhere when the interval drain redraws
answer = 503;
await D.flushQueue();
seen.background = document.activeElement === other;
console.log(JSON.stringify(seen));
""")
    assert got["upload"] == {"replaced": True, "inPending": True, "focused": "Upload now"}
    assert got["toRetry"] == {"replaced": True, "inPending": True, "focused": "Retry failed uploads"}
    assert got["retryRefused"] == {"replaced": True, "inPending": True, "focused": "Upload now"}
    assert got["uploaded"]["focused"] == "#capture-result" and got["uploaded"]["pending"] == ""
    assert got["background"] is True


def test_failed_chip_is_a_button_in_one_live_region():
    """#59 M3: the header "N failed" chip is a real button (keyboard and touch, no tooltip-only path) that app.js
    wires to open During at the retry action. A button cannot be the status, so both chips share one role=status
    group, with no nested live region left on either chip. Its touch target is 44px, and its rule follows .chip so
    the chip's own padding and transparent border do not win."""
    html = (WEB_JS.parent / "index.html").read_text()
    group = re.search(r'<span class="queue-chips" role="status">\s*(<span id="queue-count"[^>]*>)</span>\s*'
                      r'(<button id="queue-failed"[^>]*>)</button>\s*</span>', html)
    assert group, "both queue chips sit in one role=status group"
    count, failed = group.groups()
    assert "role=" not in count and "role=" not in failed
    assert 'type="button"' in failed and "chip-action" in failed and " hidden" in failed
    assert html.count('id="queue-failed"') == 1
    app_js = (WEB_JS.parent / "app.js").read_text()
    assert "$('queue-failed').addEventListener('click', () => { setPhase('during'); showFailedUploads(); });" in app_js
    css = (WEB_JS.parent / "styles.css").read_text()
    assert "--tap: 44px;" in css
    action = re.search(r"^\.chip-action \{([^}]*)\}", css, re.M)
    assert action and "min-height: var(--tap)" in action.group(1)
    assert css.index("\n.chip {") < action.start()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_failed_chip_names_its_action_and_opens_the_retry(tmp_path):
    """#59 M3: the chip's accessible name starts with its visible text (WCAG 2.5.3) and says what it does; the title
    keeps the explanation. showFailedUploads() (the chip's click, after app.js opens During) centers and focuses the
    failed box's retry action. A redraw with the same counts does not rewrite the chip text (no re-announcement)."""
    got = run_during(tmp_path, FOCUS + """
El.prototype.scrollIntoView = function (opts) { this.scrolled = opts; };
const chip = $('queue-failed');
const deadOut = async () => { answer = 422; for (let i = 0; i < 8; i++) await D.flushQueue(); };
await submit(500); await deadOut();
const one = { text: chip.textContent, label: chip.attrs['aria-label'], title: chip.title, hidden: chip.hidden };
const node = chip.kids[0];
await D.flushQueue();
one.rewritten = chip.kids[0] !== node;
await submit(503); await deadOut();
const two = { text: chip.textContent, label: chip.attrs['aria-label'] };
D.showFailedUploads();
const deadBox = $('capture-pending').kids.at(-1);
const shown = { focused: focused(), inDeadBox: walk(deadBox).includes(document.activeElement),
  box: deadBox.className, scrolled: document.activeElement.scrolled };
answer = 201;
await action('Retry failed uploads').on.click();
const before = document.activeElement;
D.showFailedUploads();  // nothing dead: nothing to land on, focus stays
console.log(JSON.stringify({ one, two, shown, cleared: { hidden: chip.hidden, stayed: document.activeElement === before } }));
""")
    assert got["one"] == {"text": "1 failed", "label": "1 failed upload — open to retry", "hidden": False,
                          "title": "1 capture stopped retrying after 8 failed uploads — kept on this device",
                          "rewritten": False}
    assert got["two"] == {"text": "2 failed", "label": "2 failed uploads — open to retry"}
    assert got["shown"] == {"focused": "Retry failed uploads", "inDeadBox": True, "box": "state state-error",
                            "scrolled": {"block": "center"}}
    assert got["cleared"] == {"hidden": True, "stayed": True}


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("status,label", [(500, "Server error (500)"), (503, "Server error (503)"),
                                          (408, "Upload timed out (408)"), (429, "Rate-limited (429)")])
def test_refusal_notice_names_each_status_honestly(tmp_path, status, label):
    """#59 nit: a 408 (timeout) and a 429 (throttling) are queued like a 5xx, but they are not server errors."""
    got = run_during(tmp_path, f"await submit({status});\nconsole.log(JSON.stringify(view()));")
    assert got["result"].startswith(f"{label} — form saved on this device and will be retried automatically.")
    assert ("Server error" in got["result"]) is (status >= 500)


def test_queue_failure_styles():
    """#59 nits: the two header chips wrap as one group at 375px; the failed box is filled (as the offline box is),
    never quieter than the queued one, with a focus ring that shows on red; queue actions are 44px touch targets."""
    css = re.sub(r"/\*.*?\*/", "", (WEB_JS.parent / "styles.css").read_text(), flags=re.S)
    rules = {" ".join(sel.split()): body for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)}
    assert "display: inline-flex" in rules[".queue-chips"] and "wrap" not in rules[".queue-chips"]
    assert "background: #1d2939" in rules[".state-offline"]  # the queued (offline) box is filled ...
    assert "background: var(--c-crit)" in rules["#capture-pending > .state-error"]  # ... and so is the failed box
    assert "color: #fff" in rules["#capture-pending > .state-error"]
    assert "outline-color: #fff" in rules["#capture-pending > .state-error .btn:focus-visible"]
    assert "min-height: var(--tap)" in rules["#capture-pending .btn"]
