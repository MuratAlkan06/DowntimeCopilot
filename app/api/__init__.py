"""HTTP routes per phase (README "Repository layout")."""
from __future__ import annotations

from fastapi import Header


def actor_role(x_role: str | None = Header(default=None)) -> str:
    """Role from the `X-Role` header (charge_nurse, rn, pharmacy, unit_clerk). TODO: real auth."""
    return (x_role or "charge_nurse").strip() or "charge_nurse"
