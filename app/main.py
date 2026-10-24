"""Downtime Copilot API (app.main:app). Run: uvicorn app.main:app --port $APP_PORT."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config, sim_client
from app.api import audit, backload, capture, digest, downtime, snapshot

log = logging.getLogger(__name__)


async def _snapshot_loop(interval: int) -> None:
    """In-app 15-minute snapshot puller (docs/decisions.md fallback for Lambda + EventBridge)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(snapshot.pull_and_store, "scheduler")
        except sim_client.SimUnavailable as exc:
            log.info("scheduled snapshot skipped: %s", exc)
        except Exception:  # noqa: BLE001 - the scheduler must never die
            log.exception("scheduled snapshot failed")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    interval = config.snapshot_interval_seconds()
    task = asyncio.create_task(_snapshot_loop(interval)) if interval > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Downtime Copilot", lifespan=lifespan)
for module in (snapshot, downtime, capture, backload, digest, audit):
    app.include_router(module.router)

# Static PWA, mounted AFTER every API route so it never shadows them.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
