"""web/js/recorder.js: the verbal-order recorder is feature-detected, goes idle -> recording -> attached,
lands in denied / unsupported when the microphone is refused or missing, picks a MIME type the browser can
record, and stops every microphone track. Runs the real module in node against a fake MediaRecorder and
getUserMedia (no new deps; skipped if node is absent)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parent.parent / "web" / "js" / "recorder.js"

FAKES = """
const tracks = [];
const mkStream = () => { const t = [{ stopped: false, stop() { this.stopped = true; } }, { stopped: false, stop() { this.stopped = true; } }]; tracks.push(...t); return { getTracks: () => t }; };
class FakeMR {
  static types = ['audio/webm;codecs=opus', 'audio/webm'];
  static isTypeSupported(t) { return FakeMR.types.includes(t); }
  constructor(stream, opts) { this.mimeType = opts?.mimeType || ''; this.state = 'inactive'; }
  start() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; this.ondataavailable?.({ data: new Blob(['abc'], { type: this.mimeType }) }); this.onstop?.(); }
}
const envWith = (gum, MR = FakeMR) => ({ MediaRecorder: MR, Blob, File, navigator: { mediaDevices: { getUserMedia: gum } } });
const allStopped = () => tracks.length > 0 && tracks.every((t) => t.stopped);
const seen = [];
let t = 0;
"""


def run(tmp_path, body: str):
    mod = tmp_path / "recorder.mjs"
    mod.write_text(MODULE.read_text())
    script = f"import * as R from {json.dumps(mod.as_uri())};\n" + FAKES + body
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def test_records_attaches_a_file_and_stops_every_track(tmp_path):
    got = run(tmp_path, (
        "const r = R.createRecorder({ env: envWith(async () => mkStream()), onChange: (s) => seen.push(s.state), now: () => t });\n"
        "const first = r.state;\n"
        "await r.start(); const during = allStopped(); t = 7400; r.stop();\n"
        "console.log(JSON.stringify({ first, seen, during, after: allStopped(), name: r.file.name, type: r.file.type,"
        " size: r.file.size, seconds: r.seconds }));"
    ))
    assert got == {"first": "idle", "seen": ["recording", "attached"], "during": False, "after": True,
                   "name": "readback.webm", "type": "audio/webm", "size": 3, "seconds": 7}


def test_microphone_refused_is_denied_and_missing_is_unsupported(tmp_path):
    got = run(tmp_path, (
        "const deny = R.createRecorder({ env: envWith(async () => { throw Object.assign(new Error('x'), { name: 'NotAllowedError' }); }) });\n"
        "await deny.start();\n"
        "const none = R.createRecorder({ env: envWith(async () => { throw Object.assign(new Error('x'), { name: 'NotFoundError' }); }) });\n"
        "await none.start();\n"
        "const old = R.createRecorder({ env: { Blob, File, navigator: {} } });\n"
        "await old.start();\n"
        "console.log(JSON.stringify({ deny: [deny.state, deny.error, deny.file], none: none.state, old: old.state,"
        " detect: [R.supported({ navigator: {} }), R.supported(envWith(async () => 0))] }));"
    ))
    assert got == {"deny": ["denied", "NotAllowedError", None], "none": "unsupported", "old": "unsupported",
                   "detect": [False, True]}


def test_discard_while_recording_releases_the_microphone(tmp_path):
    got = run(tmp_path, (
        "const r = R.createRecorder({ env: envWith(async () => mkStream()) });\n"
        "await r.start(); r.discard();\n"
        "console.log(JSON.stringify({ state: r.state, file: r.file, stopped: allStopped() }));"
    ))
    assert got == {"state": "idle", "file": None, "stopped": True}


def test_safari_style_mp4_recording(tmp_path):
    got = run(tmp_path, (
        "FakeMR.types = ['audio/mp4'];\n"
        "const r = R.createRecorder({ env: envWith(async () => mkStream()) });\n"
        "await r.start(); r.stop();\n"
        "console.log(JSON.stringify({ pick: R.pickMime(FakeMR), name: r.file.name, type: r.file.type }));"
    ))
    assert got == {"pick": "audio/mp4", "name": "readback.m4a", "type": "audio/mp4"}


def test_typed_read_back_over_audio_is_announced_only_when_both_are_present(tmp_path):
    got = run(tmp_path, (
        "const clip = new File(['abc'], 'readback.webm', { type: 'audio/webm' });\n"
        "console.log(JSON.stringify([R.typedOverridesAudio(clip, 'insulin 4 units'), R.typedOverridesAudio(clip, ''),"
        " R.typedOverridesAudio(clip, '   '), R.typedOverridesAudio(null, 'insulin 4 units'), R.typedOverridesAudio(null, '')]));"
    ))
    assert got == [True, False, False, False, False]
