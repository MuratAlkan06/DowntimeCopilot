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
    """In-app scheduler (docs/decisions.md: Lambda + EventBridge UNKNOWN). 0 disables it."""
    return int(os.environ.get("SNAPSHOT_INTERVAL_SECONDS", "900"))


def packet_concurrency() -> int:
    """Max packet generations in flight during /downtime/start (bounded so Bedrock is not flooded)."""
    return max(1, int(os.environ.get("PACKET_CONCURRENCY", "4")))


def guardrail_mode() -> str:
    """'bedrock' (ApplyGuardrail, needs GUARDRAIL_ID) or 'checker' (second model call).

    docs/decisions.md: Guardrails ALLOWED, so 'bedrock' is preferred once Role 3 publishes the real
    guardrail and sets GUARDRAIL_ID; until then the checker fallback runs behind the same interface.
    """
    mode = os.environ.get("GUARDRAIL_MODE", "").strip().lower()
    if mode in ("bedrock", "checker"):
        return mode
    return "bedrock" if os.environ.get("GUARDRAIL_ID") else "checker"


def textract_enabled() -> bool:
    """docs/decisions.md: Textract ALLOWED. Set TEXTRACT_ENABLED=0 for the 'single read' path."""
    return _flag("TEXTRACT_ENABLED", "1")
