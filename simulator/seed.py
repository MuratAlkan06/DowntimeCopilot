"""`make demo`: reset the simulator to its seed, pull the 12 patients into the app, print the URL.

Needs `make dev` running. Ports come from SIM_PORT / APP_PORT (defaults 8001 / 8000).
`--fresh` also empties the app store first (packets, records, audit, phase -> "before"). It opens the
same STORE_PATH the app uses, so run it with the app's STORE_PATH (default ./downtime.sqlite).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import httpx


def main(argv: list[str] | None = None) -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--fresh", action="store_true", help="empty the app store before seeding")
    opts = args.parse_args(argv)
    sim = os.environ.get("SIM_URL", f"http://localhost:{os.environ.get('SIM_PORT', '8001')}")
    api = os.environ.get("API_URL", f"http://localhost:{os.environ.get('APP_PORT', '8000')}")
    if opts.fresh:
        from app.store import get_store
        store = get_store()
        store.clear()
        print(f"app store cleared: {store.path}")
    try:
        clock = httpx.post(f"{sim}/reset", timeout=5).json()
        if opts.fresh:  # stamp the reset on the unit clock the simulator just restarted, not this process's wall clock
            from app import clock as unit_clock
            unit_clock.sync(datetime.fromisoformat(clock["now"]))
            store.audit("system", "store.reset", "store", None, {"by": "simulator.seed --fresh"})
        pulled = httpx.post(f"{api}/snapshot/pull", timeout=10)
        pulled.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"demo seed FAILED ({exc}). Start the services first: `make dev` (or DRY_RUN=1 make dev).",
              file=sys.stderr)
        return 1
    print(f"seeded {len(pulled.json())} patients; simulated clock {clock['now']} (EHR healthy)")
    print(f"open {api}/?phase=before")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
