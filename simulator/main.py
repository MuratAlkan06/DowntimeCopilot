"""Simulated BCA feed (simulator.main:app). Run: uvicorn simulator.main:app --port $SIM_PORT."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from contracts import PatientSnapshot
from simulator.state import SimState

app = FastAPI(title="Downtime Copilot BCA feed simulator")
state = SimState()


class Advance(BaseModel):
    minutes: int = 15


class Reset(BaseModel):
    seed: int | None = None


def _status() -> dict:
    return {
        "now": state.now.isoformat(),
        "outage": state.outage,
        "restored": state.restored,
        "seed": state.seed,
        "patients": len(state.patients),
    }


@app.get("/bca/snapshot", response_model=list[PatientSnapshot])
def bca_snapshot() -> list[PatientSnapshot]:
    if state.outage:
        raise HTTPException(status_code=503, detail="EHR unreachable (simulated outage)")
    return state.snapshot()


@app.post("/outage/start")
def outage_start() -> dict:
    state.start_outage()
    return _status()


@app.post("/outage/restore")
def outage_restore() -> dict:
    changes = state.restore()
    return {**_status(), "changes": changes}


@app.get("/clock")
def clock() -> dict:
    return _status()


@app.post("/clock/advance")
def clock_advance(body: Advance) -> dict:
    state.advance(body.minutes)
    return _status()


@app.post("/reset")
def reset(body: Reset | None = None) -> dict:
    state.reset(body.seed if body else None)
    return _status()
