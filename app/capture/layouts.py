"""Printed-form layout maps (data/forms/<form_type>.layout.json, written by tools/form_fixtures.py).

Each map gives field name -> normalized box {x, y, w, h} on the printed page. The capture pipeline
falls back to these boxes for per-field crops when the vision model returns no usable box.
"""
from __future__ import annotations

import json
from functools import lru_cache

from app import config
from contracts import Crop, FormType


@lru_cache(maxsize=None)
def _load(path: str) -> dict[str, Crop]:
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except FileNotFoundError:
        return {}
    return {name: Crop(left=b["x"], top=b["y"], width=b["w"], height=b["h"]) for name, b in raw["fields"].items()}


def layout(form_type: FormType) -> dict[str, Crop]:
    """Field -> printed box; {} for form types without a printed template (verbal orders)."""
    return _load(str(config.data_dir() / "forms" / f"{form_type.value}.layout.json"))
