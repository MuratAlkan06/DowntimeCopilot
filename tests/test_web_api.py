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
