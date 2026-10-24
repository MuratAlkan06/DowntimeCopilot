"""web/js/state.js noteSnapshot: the banner follows the server's snapshot time, also when a reseed rewinds
it. Runs the real module in node (skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parent.parent / "web" / "js" / "state.js"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_snapshot_time_follows_server_backward_and_ignores_nulls(tmp_path):
    mod = tmp_path / "state.mjs"
    mod.write_text(MODULE.read_text())
    script = (
        f"import {{ store, noteSnapshot }} from {json.dumps(mod.as_uri())};\n"
        "const seen = [];\n"
        "noteSnapshot('2026-10-24T03:15:00Z', null); seen.push(store.snapshotTime);\n"
        "noteSnapshot(['2026-10-24T02:05:00Z', '2026-10-24T02:45:00Z']); seen.push(store.snapshotTime);\n"
        "noteSnapshot(null, undefined, 'not a time'); seen.push(store.snapshotTime);\n"
        "console.log(JSON.stringify(seen));"
    )
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["2026-10-24T03:15:00.000Z", "2026-10-24T02:45:00.000Z",
                                      "2026-10-24T02:45:00.000Z"]
