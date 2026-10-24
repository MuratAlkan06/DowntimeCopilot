"""Restore state reaches every device: web/js/packets.js loadStatus mirrors GET /status restored_at into
store.restoredAt (the EHR banner's source), so a restore made elsewhere shows as restored and a new outage or a
reseed clears it; web/js/recovery.js then shows the Recovery view as restored from GET /discrepancies, without
a second POST /recovery/restore, and the charge nurse's review (acknowledge / resolve) edits that one list
in place. Runs the real modules in node with a stubbed fetch and a minimal fake DOM (skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB_JS = Path(__file__).resolve().parent.parent / "web" / "js"

# Just enough DOM for dom.js h()/mount() and recovery.js getElementById lookups; the fetch stub answers
# /status from `nextStatus` (a live response, or a service-worker cached copy when `cached` is set).
PRELUDE = """
class El {
  constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.className = ''; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, fn) { (this.on ??= {})[t] = fn; }
  focus() { globalThis.focused = this; }
  append(...ks) { this.kids.push(...ks); }
  replaceChildren(...ks) { this.kids = ks; }
  get textContent() { return this.kids.map((k) => k.textContent).join(''); }
  set textContent(v) { this.kids = [new Text(v)]; }
}
class Text { constructor(t) { this.t = t; } get textContent() { return this.t; } }
const els = new Map();
globalThis.Node = Object;
globalThis.document = {
  hidden: false,
  createElement: (t) => new El(t), createTextNode: (t) => new Text(t),
  getElementById: (id) => { if (!els.has(id)) els.set(id, new El('div')); return els.get(id); },
  querySelectorAll: () => [],
};
globalThis.window = { addEventListener() {} };
globalThis.navigator ??= { onLine: true };
const mem = new Map();
globalThis.localStorage = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
globalThis.setInterval = () => 0;
const calls = [];
let nextStatus = null; let cached = false; let reviewDown = false;
const DISC = [{ discrepancy_id: 'd1', rule_id: 'R-REC-01', patient_id: 'pt-02', severity: 'critical',
  description: 'heparin on paper, no order', status: 'open' }];
const PATIENTS = [{ patient_id: 'pt-02', bed: 'Bed 2', name: 'Test Person' }];
globalThis.fetch = async (path, init) => {
  calls.push(`${init?.method || 'GET'} ${path}`);
  const review = init?.method === 'POST' && path.startsWith('/discrepancies/');
  if (review && reviewDown) throw new TypeError('Failed to fetch');
  const body = path === '/status' ? nextStatus : path === '/discrepancies' ? DISC : path === '/patients' ? PATIENTS
    : review ? { ...DISC.find((x) => path.includes(`/${x.discrepancy_id}/`)), status: JSON.parse(init.body).status } : [];
  const headers = { 'content-type': 'application/json', ...(path === '/status' && cached ? { 'x-dc-fallback': '1' } : {}) };
  return new Response(JSON.stringify(body), { status: 200, headers });
};
const tick = () => new Promise((r) => setTimeout(r, 0));
const RECOVERY = { phase: 'recovery', now: '2026-10-24T03:45:10Z', snapshot_time: '2026-10-24T02:45:00Z',
  outage_started_at: '2026-10-24T03:00:00Z', outage_id: 'a', restored_at: '2026-10-24T03:45:00Z' };
const NEW_OUTAGE = { ...RECOVERY, phase: 'downtime', outage_started_at: '2026-10-24T04:00:00Z', outage_id: 'b', restored_at: null };
const FRESH = { phase: 'before', now: '2026-10-24T02:45:00Z', snapshot_time: '2026-10-24T02:45:00Z',
  outage_started_at: null, outage_id: null, restored_at: null };
"""


def _run(tmp_path: Path, body: str) -> dict:
    for p in WEB_JS.glob("*.js"):
        (tmp_path / p.name).write_text(p.read_text())
    (tmp_path / "package.json").write_text('{"type": "module"}')
    imports = "".join(
        f"const {name} = await import({json.dumps((tmp_path / f'{mod}.js').as_uri())});\n"
        for name, mod in (("S", "state"), ("P", "packets"), ("R", "recovery")))
    script = PRELUDE + imports + body + "\nprocess.exit(0);"
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_status_hydrates_restore_state_and_clears_it(tmp_path):
    seen = _run(tmp_path, """
const marks = () => [S.store.downtimeStartedAt, S.store.restoredAt];
const seen = {};
for (const [name, s, viaCache] of [['restored', RECOVERY, false], ['cached_copy', FRESH, true],
    ['new_outage', NEW_OUTAGE, false], ['restored_again', RECOVERY, false], ['fresh_seed', FRESH, false]]) {
  nextStatus = s; cached = viaCache;
  await P.loadStatus();
  seen[name] = marks();
}
console.log(JSON.stringify(seen));
""")
    restored = ["2026-10-24T03:00:00Z", "2026-10-24T03:45:00Z"]
    assert seen == {
        "restored": restored,  # a device that never pressed "Systems restored" (or cleared storage)
        "cached_copy": restored,  # an offline cached /status never overwrites the markers
        "new_outage": ["2026-10-24T04:00:00Z", None],  # a later outage: back to "EHR unreachable"
        "restored_again": ["2026-10-24T03:00:00Z", "2026-10-24T03:45:00Z"],
        "fresh_seed": [None, None],  # seed --fresh: no outage, no stale restore
    }


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_recovery_view_follows_restore_state_without_a_second_restore(tmp_path):
    seen = _run(tmp_path, """
const $ = (id) => document.getElementById(id);
$('recovery-status').textContent = 'When the EHR returns, diff it against the snapshot and captured records.';
R.initRecovery();
S.set('phase', 'recovery');
const view = () => ({ status: $('recovery-status').textContent, disc: $('discrepancies').textContent });
const seen = { idle: view() };
nextStatus = RECOVERY; await P.loadStatus(); await tick(); await tick();
seen.restored = view();
nextStatus = FRESH; await P.loadStatus(); await tick();
seen.cleared = view();
console.log(JSON.stringify({ ...seen, calls }));
""")
    idle = seen["idle"]
    assert "Tap \"Systems restored\"" in idle["disc"]
    assert seen["restored"]["status"].startswith("Systems restored ")
    assert "1 discrepancies flagged." in seen["restored"]["status"]
    assert "R-REC-01" in seen["restored"]["disc"] and "heparin on paper" in seen["restored"]["disc"]
    assert seen["cleared"] == idle
    assert seen["calls"].count("GET /discrepancies") == 1
    assert not any(c.startswith("POST") for c in seen["calls"])  # hydration never re-runs the restore


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_discrepancy_review_edits_the_one_hydrated_list(tmp_path):
    """PR #37 reconciled: the Acknowledge / Resolve buttons ride syncRestored's single GET /discrepancies. Opening
    Recovery again does not fetch the list a second time, each review is one POST that updates the item in place,
    and a reload straight into Recovery still fetches the patient list once for the bed labels."""
    seen = _run(tmp_path, """
const $ = (id) => document.getElementById(id);
R.initRecovery();
S.set('phase', 'recovery');
nextStatus = RECOVERY; await P.loadStatus(); await tick(); await tick();
R.enterRecovery(); await tick(); await tick();  // the view opens again (tab switch, reconnect)
const walk = (el) => [el, ...(el.kids || []).flatMap(walk)];
const buttons = () => walk($('discrepancies')).filter((e) => e.tag === 'button');
const texts = (cls) => walk($('discrepancies')).filter((e) => e.className?.split(' ').includes(cls)).map((e) => e.textContent);
const step = () => ({ buttons: buttons().map((b) => b.textContent), disc: $('discrepancies').textContent,
  chips: texts('chip'), meta: texts('disc-meta'), status: $('recovery-status').textContent });
const seen = { hydrated: step() };
for (const label of ['Acknowledge', 'Resolve']) {
  const btn = buttons().find((b) => b.textContent === label);
  btn.on.click({ target: btn }); await tick(); await tick();
  seen[label] = step();
}
console.log(JSON.stringify({ ...seen, calls }));
""")
    assert seen["hydrated"]["buttons"] == ["Acknowledge", "Resolve"]
    assert "Bed 2 · Test Person" in seen["hydrated"]["disc"] and "critical · open" in seen["hydrated"]["disc"]
    assert seen["hydrated"]["chips"] == [] and seen["hydrated"]["meta"] == ["critical · open"]
    # A reviewed row carries its state in a chip; the meta line keeps the severity.
    assert seen["Acknowledge"]["buttons"] == ["Resolve"] and seen["Acknowledge"]["chips"] == ["Acknowledged"]
    assert seen["Resolve"]["buttons"] == [] and seen["Resolve"]["chips"] == ["✓Resolved"]  # the check is aria-hidden
    assert seen["Resolve"]["meta"] == ["critical"]
    assert seen["Resolve"]["status"].startswith("Systems restored ")
    calls = seen["calls"]
    assert calls.count("GET /discrepancies") == 1 and calls.count("GET /patients") == 1
    assert [c for c in calls if c.startswith("POST")] == ["POST /discrepancies/d1/status"] * 2


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_discrepancy_review_keeps_focus_announces_and_counts(tmp_path):
    """Design review on the review UI: the header count line follows every review in place, focus returns to the
    acted row (its remaining button, else the row) after the list redraws, #disc-live announces the result with
    the open count, and a failed save shows at that row with its button usable again; #recovery-status keeps the
    restore state."""
    seen = _run(tmp_path, """
const $ = (id) => document.getElementById(id);
DISC.push({ discrepancy_id: 'd2', rule_id: 'R-REC-02', patient_id: 'pt-02', severity: 'warning',
  description: 'vitals out of range, no response', status: 'open' });
R.initRecovery();
S.set('phase', 'recovery');
nextStatus = RECOVERY; await P.loadStatus(); await tick(); await tick();
R.enterRecovery(); await tick(); await tick();  // bed labels for the announcement
const walk = (el) => [el, ...(el.kids || []).flatMap(walk)];
const rowOf = (id) => walk($('discrepancies')).find((e) => e.tag === 'li' && e.textContent.includes(id));
const button = (id, label) => walk(rowOf(id)).find((e) => e.tag === 'button' && e.textContent === label);
const errors = (id) => walk(rowOf(id)).filter((e) => e.attrs?.role === 'alert').map((e) => e.textContent);
const click = async (id, label) => { const b = button(id, label); b.on.click({ target: b }); await tick(); await tick(); return b; };
const view = (id) => ({ summary: $('disc-summary').textContent, live: $('disc-live').textContent,
  status: $('recovery-status').textContent, errors: errors(id), focusedTag: globalThis.focused?.tag,
  focused: globalThis.focused?.textContent, focusedInRow: walk(rowOf(id)).includes(globalThis.focused),
  rowClass: rowOf(id).className });
const seen = { hydrated: view('R-REC-01') };
reviewDown = true;
const failed = await click('R-REC-01', 'Acknowledge');
seen.offline = { ...view('R-REC-01'), disabled: failed.disabled, sameButton: globalThis.focused === failed };
reviewDown = false;
await click('R-REC-01', 'Acknowledge');
seen.acknowledged = view('R-REC-01');
await click('R-REC-01', 'Resolve');
seen.resolved = view('R-REC-01');
console.log(JSON.stringify({ ...seen, calls }));
""")
    assert seen["hydrated"]["summary"] == "2 discrepancies · 2 open · 0 acknowledged · 0 resolved"
    assert seen["hydrated"]["live"] == ""

    offline = seen["offline"]  # the network drops mid-review: told at the row, the button works again
    assert offline["errors"] == ["Offline — not saved. Try again when connected."]
    assert offline["disabled"] is False and offline["sameButton"]
    assert offline["status"].startswith("Systems restored ") and "not saved" not in offline["status"].lower()
    assert offline["summary"] == seen["hydrated"]["summary"] and offline["live"] == ""

    ack = seen["acknowledged"]
    assert ack["summary"] == "2 discrepancies · 1 open · 1 acknowledged · 0 resolved"
    assert ack["live"] == "Bed 2 (R-REC-01) acknowledged — 1 open remaining"
    assert ack["focused"] == "Resolve" and ack["focusedInRow"]  # the row's remaining button
    assert ack["errors"] == [] and "is-acknowledged" in ack["rowClass"]

    res = seen["resolved"]
    assert res["summary"] == "2 discrepancies · 1 open · 0 acknowledged · 1 resolved"
    assert res["live"] == "Bed 2 (R-REC-01) resolved — 1 open remaining"
    assert res["focusedTag"] == "li" and res["focusedInRow"]  # no button left: the row itself
    assert "is-resolved" in res["rowClass"]
    assert [c for c in seen["calls"] if c.startswith("POST")] == ["POST /discrepancies/d1/status"] * 3
