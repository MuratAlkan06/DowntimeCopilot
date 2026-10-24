"""Deterministic rules engine (README "Rules"). Never calls a model."""
from app.rules.engine import (  # noqa: F401
    RULES,
    VITAL_RANGES,
    RecoveryContext,
    out_of_range,
    recovery_discrepancies,
    snapshot_alerts,
)
