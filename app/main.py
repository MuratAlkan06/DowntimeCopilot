"""Downtime Copilot API (app.main:app). Run: uvicorn app.main:app --port $APP_PORT."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import checklists as role_checklists
from app import config, sim_client
from app.api import (audit, backload, capture, checklists, command_center, digest, downtime, incident, readiness,
                     snapshot)
from app.sync import worker as sync_worker

log = logging.getLogger(__name__)


async def _snapshot_loop(tick: int) -> None:
    """In-app snapshot puller (docs/decisions.md fallback for Lambda + EventBridge). Ticks every `tick`
    real seconds, but pulls on the SIMULATED clock: every 15 unit minutes, "before" phase only
    (snapshot.should_pull)."""
    while True:
        await asyncio.sleep(tick)
        try:
            await asyncio.to_thread(snapshot.scheduled_pull, "scheduler")
        except sim_client.SimUnavailable as exc:
            log.info("scheduled snapshot skipped: %s", exc)
        except Exception:  # noqa: BLE001 - the scheduler must never die
            log.exception("scheduled snapshot failed")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    role_checklists.load()  # parse the policy's role checklists once, at startup
    interval = config.snapshot_interval_seconds()
    task = asyncio.create_task(_snapshot_loop(interval)) if interval > 0 else None
    sync_interval = config.sync_interval_seconds()
    syncer = sync_worker.start(sync_interval) if sync_interval > 0 else None  # outbox drain thread
    yield
    if task:
        task.cancel()
    if syncer:
        syncer[1].set()
        syncer[0].join(timeout=3)


app = FastAPI(title="Downtime Copilot", lifespan=lifespan)
for module in (snapshot, downtime, checklists, capture, backload, digest, incident, audit, readiness, command_center):
    app.include_router(module.router)

# Static PWA, mounted AFTER every API route so it never shadows them.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
