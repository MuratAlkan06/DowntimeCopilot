"""`make demo`: reset the simulator to its seed, pull the 12 patients into the app, print the URL.

Needs `make dev` running. Ports come from SIM_PORT / APP_PORT (defaults 8001 / 8000).
"""
from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    sim = os.environ.get("SIM_URL", f"http://localhost:{os.environ.get('SIM_PORT', '8001')}")
    api = os.environ.get("API_URL", f"http://localhost:{os.environ.get('APP_PORT', '8000')}")
    try:
        clock = httpx.post(f"{sim}/reset", timeout=5).json()
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
