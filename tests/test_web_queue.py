"""web/js/queue.js: the offline capture queue drains each item once, deletes it only after a 2xx, keeps it
on failure, never runs two drains at once, and moves a pre-S7 localStorage queue over once. Runs the real
module in node against its in-memory store (no IndexedDB, no new deps; skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parent.parent / "web" / "js" / "queue.js"


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
