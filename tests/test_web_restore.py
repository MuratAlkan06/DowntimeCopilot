"""Restore state reaches every device: web/js/packets.js loadStatus mirrors GET /status restored_at into
store.restoredAt (the EHR banner's source), so a restore made elsewhere shows as restored and a new outage or a
reseed clears it; web/js/recovery.js then shows the Recovery view as restored from GET /discrepancies, without
a second POST /recovery/restore. Runs the real modules in node with a stubbed fetch and a minimal fake DOM
(skipped if node is absent)."""
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
  addEventListener() {}
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
let nextStatus = null; let cached = false;
const DISC = [{ discrepancy_id: 'd1', rule_id: 'R-REC-01', patient_id: 'pt-02', severity: 'critical',
  description: 'heparin on paper, no order', status: 'open' }];
globalThis.fetch = async (path, init) => {
  calls.push(`${init?.method || 'GET'} ${path}`);
  const body = path === '/status' ? nextStatus : path === '/discrepancies' ? DISC : [];
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
