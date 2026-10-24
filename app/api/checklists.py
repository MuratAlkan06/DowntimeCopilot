"""Role checklists from the downtime policy (app.checklists). Outside the frozen contracts: UI-only shape."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import checklists
from app.api import actor_role
from app.store import get_store

router = APIRouter(tags=["during"])


class ChecklistItem(BaseModel):
    id: str
    section_id: str
    text: str
    source: str


class Checklist(BaseModel):
    role: str
    items: list[ChecklistItem]


@router.get("/checklists", response_model=Checklist)
def get_checklist(role: str | None = None, auto: bool = False, actor: str = Depends(actor_role)) -> Checklist:
    """`?role=` (charge_nurse, rn, pharmacy, unit_clerk, provider); defaults to the caller's X-Role.

    `?auto=1` marks a background fetch (page load, role prefetch, the declare-time pre-cache): audited as
    `checklist.view`. Without it the fetch is a deliberate open of that checklist: audited as `checklist.ack`
    with the requested role in `details.ack_role` (the subject; the actor stays the caller's X-Role). The
    readiness drill counts only these (app/drill.py acknowledged_roles)."""
    wanted = (role or actor).strip()
    all_lists = checklists.load()
    found = wanted in all_lists
    if auto:
        get_store().audit(actor, "checklist.view", "checklist", wanted, {"found": found})
    else:
        get_store().audit(actor, "checklist.ack", "checklist", wanted, {"found": found, "ack_role": wanted})
    if not found:
        raise HTTPException(404, f"no checklist for role {wanted!r}; one of {sorted(all_lists)}")
    return Checklist(role=wanted, items=[ChecklistItem(**vars(i)) for i in all_lists[wanted]])
