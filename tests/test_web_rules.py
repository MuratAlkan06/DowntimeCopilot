"""web/js/rules.js: the relative "in N min" is computed at render time from the unit clock, so the same
alert reads differently at two unit times. Runs the real module in node (skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parent.parent / "web" / "js" / "rules.js"

HIT = {"rule_id": "R-DUE-01", "text": "High-alert heparin 5000 units SC due 03:30; independent double-check required",
       "due_at": "2026-10-24T03:30:00+00:00"}


def run_js(tmp_path: Path, expr: str):
    # Copy to .mjs so any node version loads it as an ES module without a package.json.
    mod = tmp_path / "rules.mjs"
    mod.write_text(MODULE.read_text())
    script = f"import * as r from {json.dumps(mod.as_uri())};\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_same_alert_reads_differently_at_two_unit_times(tmp_path):
    hit = json.dumps(HIT)
    text_only = json.dumps({k: v for k, v in HIT.items() if k != "due_at"})
    got = run_js(tmp_path, f"""[
        r.dueRelative({hit}, '2026-10-24T02:47:00Z'),
        r.dueRelative({hit}, '2026-10-24T03:10:00Z'),
        r.dueRelative({hit}, '2026-10-24T03:30:20Z'),
        r.dueRelative({hit}, '2026-10-24T03:45:00Z'),
        r.dueRelative({text_only}, '2026-10-24T02:47:00Z'),
        r.dueRelative({json.dumps(dict(HIT, due_at=None, text="due 00:15"))}, '2026-10-23T23:50:00Z'),
        r.dueRelative({hit}, null),
        r.ageMinutes('2026-10-24T02:05:00Z', '2026-10-24T02:47:00Z'),
    ]""")
    assert got == ["in 43 min", "in 20 min", "due now", "overdue 15 min", "in 43 min", "in 25 min", "", 42]
