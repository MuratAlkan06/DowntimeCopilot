"""Readiness drill and score (S13, docs/decisions.md). Outside the frozen contracts.

GET /readiness: the last drill's result, {score, components: {name: {score, weight, detail}}, last_drill_at,
summary, drill: {steps, summary_source, summary_model_id, summary_usd, summary_check}}; every value null (and
components {}) before the first drill of this process.
POST /readiness/drill: run one drill (app/drill.py) and return the same shape.

Neither route writes to the live store, so neither is audited: the drill's own audit rows stay in its
ephemeral store, and an audit row here would itself be a live write with an outbox item to mirror.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import drill

router = APIRouter(tags=["readiness"])


@router.get("/readiness")
def get_readiness() -> dict:
    return drill.last()


@router.post("/readiness/drill")
def run_drill() -> dict:
    try:
        return drill.run()
    except drill.DrillError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
