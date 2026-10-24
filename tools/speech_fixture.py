"""Speech fixture for the verbal-order audio path (docs/decisions.md S9). macOS only; CI never runs it.

    python tools/speech_fixture.py

Writes data/forms/fixtures/verbal_order-01.wav (16 kHz mono 16-bit PCM, well under 500 KB) and the
ground truth verbal_order-01.json. It uses the built-in macOS speech tools: `say -o` renders the script
to AIFF, and `afconvert` converts it to WAV. Fictional content only, no PHI: Bed 7 is a seeded demo bed and
"Doctor Quill" is a fictional provider.

Ground truth:
- `script` is what is spoken. The dose is said in words.
- `transcript` is the expected Transcribe rendering (numerals). DRY_RUN returns it for this exact
  recording (app.capture.transcribe.canned_transcript).
- `fields` are the structured order fields a correct read gives.

Regenerating changes the audio bytes, and the DRY_RUN lookup keys on them, so commit the .wav and the
.json together.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "forms" / "fixtures"
NAME = "verbal_order-01"
VOICE = "Samantha"  # en-US system voice; falls back to the default voice when it is not installed
RATE_WPM = 165
MAX_BYTES = 500_000

SCRIPT = ("Verbal order for bed 7: heparin five thousand units subcutaneous now. Ordering provider Doctor Quill. "
          "Read-back confirmed.")
TRUTH = {
    "form_type": "verbal_order",
    "audio": f"{NAME}.wav",
    "synthetic": True,
    "bed": "7",
    "patient_id": "pt-07",
    "script": SCRIPT,
    "transcript": ("Verbal order for bed 7: heparin 5000 units subcutaneous now. Ordering provider Doctor Quill. "
                   "Read-back confirmed."),
    "fields": {"medication": "heparin", "dose": "5000 units", "route": "SC", "frequency": "now",
               "ordering_provider": "Dr. Quill", "read_back": "yes", "provider_cosign": "no"},
}


def main() -> int:
    if sys.platform != "darwin" or not (shutil.which("say") and shutil.which("afconvert")):
        print("speech_fixture: needs macOS `say` and `afconvert`; the committed fixture is used elsewhere")
        return 1
    voices = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    voice = ["-v", VOICE] if any(line.split()[:1] == [VOICE] for line in voices.splitlines()) else []
    wav = OUT / f"{NAME}.wav"
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / f"{NAME}.aiff"
        subprocess.run(["say", *voice, "-r", str(RATE_WPM), "-o", str(aiff), SCRIPT], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
    size = wav.stat().st_size
    if size >= MAX_BYTES:
        print(f"speech_fixture: {wav.name} is {size} bytes (limit {MAX_BYTES})")
        return 1
    truth = {**TRUTH, "voice": voice[1] if voice else "default", "rate_wpm": RATE_WPM}
    (OUT / f"{NAME}.json").write_text(json.dumps(truth, indent=2) + "\n")
    print(f"wrote {wav.relative_to(ROOT)} ({size} bytes) and {NAME}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
