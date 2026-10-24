"""web/js/records.js sourceLine: a verbal order's source line follows the server's text_source (stored at
capture), so a reload or another device never shows a typed read-back as "transcribed from audio". Runs the
real modules in node (skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB_JS = Path(__file__).resolve().parent.parent / "web" / "js"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_source_line_from_the_server_text_source(tmp_path):
    for name in ("api.js", "dom.js", "state.js", "records.js"):
        (tmp_path / name).write_text((WEB_JS / name).read_text())
    (tmp_path / "package.json").write_text('{"type": "module"}')
    script = (
        "globalThis.window = { addEventListener() {} };\nglobalThis.navigator ??= { onLine: true };\n"
        f"const R = await import({json.dumps((tmp_path / 'records.js').as_uri())});\n"
        "const verbal = { record_id: 'r1', audio_ref: 'blob:a', image_ref: null };\n"
        "const photo = { record_id: 'r2', audio_ref: null, image_ref: 'blob:p' };\n"
        "const bedrock = ['Bedrock vision'];\n"
        "console.log(JSON.stringify({\n"
        "  typed: R.sourceLine(verbal, bedrock, 'typed'),\n"
        "  transcribed: R.sourceLine(verbal, bedrock, 'transcribed'),\n"
        "  audio_untranscribed: R.sourceLine(verbal, bedrock, 'audio_untranscribed'),\n"
        "  photo: R.sourceLine(photo, ['Textract', 'Bedrock vision'], 'photo'),\n"
        "  photo_single: R.sourceLine(photo, bedrock, 'photo'),\n"
        "  unknown: [R.sourceLine(verbal, bedrock, null), R.sourceLine(verbal, bedrock, undefined)],\n"
        "  outcomes: ['typed', 'mock', 'transcribed', 'denied', 'timeout', 'unconfigured'].map(R.sourceFromOutcome),\n"
        "}));"
    )
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {
        "typed": "Read from the typed read-back (single read)",
        "transcribed": "Transcribed from audio (single read)",
        "audio_untranscribed": "Audio saved, not transcribed",
        "photo": "Read by Textract + Bedrock vision",
        "photo_single": "Read by Bedrock vision (single read)",
        # Not known yet (loading, offline, or a record stored before the column): no provenance claimed.
        "unknown": ["Read-back source unknown", "Read-back source unknown"],
        "outcomes": ["typed", "transcribed", "transcribed", "audio_untranscribed", "audio_untranscribed",
                     "audio_untranscribed"],
    }
