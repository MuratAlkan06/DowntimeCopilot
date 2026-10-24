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


def textract_enabled() -> bool:
    """docs/decisions.md: Textract ALLOWED. Set TEXTRACT_ENABLED=0 for the 'single read' path."""
    return _flag("TEXTRACT_ENABLED", "1")
