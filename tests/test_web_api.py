"""web/js/api.js: every write carries the role switcher's role as X-Role (the server audits it as the actor);
GETs are unchanged. Runs the real modules in node with a stubbed fetch (skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB_JS = Path(__file__).resolve().parent.parent / "web" / "js"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_writes_send_x_role_from_current_role(tmp_path):
    for name in ("api.js", "state.js"):
        (tmp_path / name).write_text((WEB_JS / name).read_text())
    (tmp_path / "package.json").write_text('{"type": "module"}')
    script = (
        "globalThis.window = { addEventListener() {} };\nglobalThis.navigator ??= { onLine: true };\n"
        "const sent = [];\n"
        "globalThis.fetch = async (path, init) => { sent.push([init.method, init.headers['X-Role'] ?? null]);\n"
        "  return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }); };\n"
        f"const {{ api }} = await import({json.dumps((tmp_path / 'api.js').as_uri())});\n"
        f"const {{ set }} = await import({json.dumps((tmp_path / 'state.js').as_uri())});\n"
        "set('role', 'pharmacy');\n"
        "await api('/capture', { method: 'POST', form: new FormData() });\n"
        "await api('/status');\n"
        "set('role', 'rn');\n"
        "await api('/backload/r1/fields/dose', { method: 'PATCH', json: {} });\n"
        "console.log(JSON.stringify(sent));"
    )
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [["POST", "pharmacy"], ["GET", None], ["PATCH", "rn"]]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_cached_packet_hit_does_not_flip_connectivity(tmp_path):
    """A stale-while-revalidate hit (x-dc-cached-at, no fallback mark) says nothing about the network, so
    it must not end "offline": that flip would fire a reconnect (drain + redraw) on every cached packet."""
    for name in ("api.js", "state.js"):
        (tmp_path / name).write_text((WEB_JS / name).read_text())
    (tmp_path / "package.json").write_text('{"type": "module"}')
    script = (
        "globalThis.window = { addEventListener() {} };\n"
        "Object.defineProperty(globalThis, 'navigator', { value: { onLine: true }, configurable: true });\n"
        "const mk = (h) => new Response('{}', { status: 200, headers: { 'content-type': 'application/json', ...h } });\n"
        "let next;\nglobalThis.fetch = async () => next;\n"
        f"const {{ api, isOffline, onNetChange }} = await import({json.dumps((tmp_path / 'api.js').as_uri())});\n"
        "const reconnects = []; onNetChange((off, info) => { if (info.wentOnline) reconnects.push(1); });\n"
        "const seen = [];\n"
        "next = mk({ 'x-dc-fallback': '1', 'x-dc-cached-at': 't' }); await api('/patients'); seen.push(isOffline());\n"
        "next = mk({ 'x-dc-cached-at': 't' }); await api('/patients/pt-12/packet'); seen.push(isOffline());\n"
        "next = mk({}); await api('/status'); seen.push(isOffline());\n"
        "console.log(JSON.stringify({ seen, reconnects: reconnects.length }));"
    )
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"seen": [True, True, False], "reconnects": 1}
