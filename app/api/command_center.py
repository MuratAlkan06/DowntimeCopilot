"""Supervisor command center (S14, docs/decisions.md). Outside the frozen contracts.

GET /command-center: {units: [{unit_id, name, as_of, overdue_high_alert, open_discrepancies, backload_counts,
critical_labs_awaiting}], generated_at}. One unit today (config.UNIT_NAME, from the live store); the roll-up
itself is app/command_center.py (pure, unit clock injected here). `generated_at` and `as_of` are unit time.
Read-only: the only write is the audit row for the view.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app import command_center, config, sim_client
from app.api import actor_role
from app.store import get_store

router = APIRouter(tags=["command-center"])


@router.get("/command-center")
def get_command_center(role: str = Depends(actor_role)) -> dict:
    store = get_store()
    now = sim_client.now()  # unit clock, read once: every panel is judged at the same instant
    unit = command_center.UnitInputs(unit_id=config.UNIT_ID, name=config.UNIT_NAME,
                                     snapshot=store.latest_snapshot("pre"), records=store.list_records(),
                                     discrepancies=store.list_discrepancies())
    view = command_center.build([unit], clock=lambda: now)
    store.audit(role, "command_center.view", "unit", unit.unit_id,
                {name: panel["count"] for u in view["units"] for name, panel in u.items() if isinstance(panel, dict)})
    return view
