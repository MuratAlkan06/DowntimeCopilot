"""Runtime settings, read from the environment at call time so tests can override them."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no")


def dry_run() -> bool:
    """DRY_RUN=1: canned Bedrock / Textract / Transcribe responses, zero network."""
    return _flag("DRY_RUN")


def sim_url() -> str:
    return os.environ.get("SIM_URL", f"http://localhost:{os.environ.get('SIM_PORT', '8001')}")


def store_path() -> str:
    return os.environ.get("STORE_PATH", "./downtime.sqlite")


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", ROOT / "data"))


def aws_region() -> str:
    return os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def snapshot_interval_seconds() -> int:
    """Real-seconds tick of the in-app scheduler (docs/decisions.md: Lambda + EventBridge UNKNOWN).
    Each tick reads the simulator clock; the 15-minute snapshot cadence is in simulated minutes
    (app.api.snapshot.should_pull). 0 disables the scheduler."""
    return int(os.environ.get("SNAPSHOT_INTERVAL_SECONDS", "5"))


def packet_concurrency() -> int:
    """Max packet generations in flight during /downtime/start (bounded so Bedrock is not flooded)."""
    return max(1, int(os.environ.get("PACKET_CONCURRENCY", "4")))


def guardrail_published() -> tuple[str, str] | None:
    """(GUARDRAIL_ID, GUARDRAIL_VERSION) of the published guardrail, or None unless both are set."""
    gid, ver = os.environ.get("GUARDRAIL_ID", "").strip(), os.environ.get("GUARDRAIL_VERSION", "").strip()
    return (gid, ver) if gid and ver else None


def guardrail_mode() -> str:
    """'bedrock' (ApplyGuardrail on the published version) or 'checker' (second model call).

    docs/decisions.md (S3): 'bedrock' whenever GUARDRAIL_ID and GUARDRAIL_VERSION are both set;
    otherwise the checker fallback runs behind the same interface. GUARDRAIL_MODE=checker forces it.
    """
    if os.environ.get("GUARDRAIL_MODE", "").strip().lower() == "checker":
        return "checker"
    return "bedrock" if guardrail_published() else "checker"


def audio_bucket() -> str | None:
    """S3 bucket for verbal-order audio sent to Transcribe (docs/decisions.md S9). `tools/audio_bucket.py setup`
    generates `downtime-copilot-audio-<random suffix>` once and appends AUDIO_BUCKET to the local .env.
    Unset: audio is kept locally only and the read-back falls back to typed."""
    return os.environ.get("AUDIO_BUCKET", "").strip() or None


def textract_enabled() -> bool:
    """docs/decisions.md: Textract ALLOWED. Set TEXTRACT_ENABLED=0 for the 'single read' path."""
    return _flag("TEXTRACT_ENABLED", "1")


# Unit parameters for the Vitals Flowsheet (policy 3.4). Inclusive normal ranges: a value exactly at a
# bound is in range; anything beyond is flagged out_of_range on captured records and by R-REC-02.
VITAL_RANGE_DEFAULTS: dict[str, tuple[float, float]] = {
    "temp_c": (36.0, 38.0),
    "heart_rate": (50, 110),
    "resp_rate": (10, 24),
    "bp_systolic": (90, 160),
    "bp_diastolic": (50, 100),
    "spo2": (92, 100),
}


# Display names for captured-record field keys; same labels as the web client (web/js/dom.js FIELD_LABELS).
# Single source for server-side prose: rule descriptions (R-REC-02) are written with these labels at
# emission, so the discrepancy panel, the digest and the incident report all show "SpO2 89", never "spo2=89".
FIELD_LABELS: dict[str, str] = {
    "bed": "Bed", "time": "Time", "temp_c": "Temp (°C)", "heart_rate": "Heart rate", "resp_rate": "Resp rate",
    "bp_systolic": "BP systolic", "bp_diastolic": "BP diastolic", "spo2": "SpO2", "pain_score": "Pain score",
    "initials": "Initials", "medication": "Medication", "dose": "Dose", "route": "Route", "time_given": "Time given",
    "second_verifier": "Second verifier", "allergies": "Allergies", "height_cm": "Height (cm)",
    "weight_kg": "Weight (kg)", "history": "History", "frequency": "Frequency",
    "ordering_provider": "Ordering provider", "read_back": "Read-back", "provider_cosign": "Provider co-sign",
    "response": "Response",
}


def vital_ranges() -> dict[str, tuple[float, float]]:
    """Defaults, each overridable as VITAL_RANGE_<FIELD>="low,high" (e.g. VITAL_RANGE_SPO2="90,100")."""
    out = dict(VITAL_RANGE_DEFAULTS)
    for name in out:
        raw = os.environ.get(f"VITAL_RANGE_{name.upper()}", "").strip()
        if raw:
            lo, hi = (float(v) for v in raw.split(",", 1))
            out[name] = (lo, hi)
    return out
