"""Write JSON Schema for each top-level contract to contracts/schema/<Name>.json.

Usage: python -m contracts.export_schemas   (or `make schemas`)
"""
from __future__ import annotations

import json
from pathlib import Path

from contracts.models import TOP_LEVEL_MODELS

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


def render(model: type) -> str:
    """Deterministic JSON text for one model's validation schema."""
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for model in TOP_LEVEL_MODELS:
        path = SCHEMA_DIR / f"{model.__name__}.json"
        path.write_text(render(model), encoding="utf-8")
        print(f"wrote {path.relative_to(SCHEMA_DIR.parent.parent)}")


if __name__ == "__main__":
    main()
