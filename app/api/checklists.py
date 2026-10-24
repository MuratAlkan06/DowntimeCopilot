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
def get_checklist(role: str | None = None, actor: str = Depends(actor_role)) -> Checklist:
    """`?role=` (charge_nurse, rn, pharmacy, unit_clerk, provider); defaults to the caller's X-Role."""
    wanted = (role or actor).strip()
    all_lists = checklists.load()
    get_store().audit(actor, "checklist.view", "checklist", wanted, {"found": wanted in all_lists})
    if wanted not in all_lists:
        raise HTTPException(404, f"no checklist for role {wanted!r}; one of {sorted(all_lists)}")
    return Checklist(role=wanted, items=[ChecklistItem(**vars(i)) for i in all_lists[wanted]])
