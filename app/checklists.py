"""Role checklists parsed deterministically from data/policy/downtime_policy.md (no model calls).

Each role gets its section-2 duty statement split into items ("2.1 Charge nurse: leads ..., assigns ...")
plus every numbered paragraph elsewhere in the policy that names the role, reduced to the sentences that
name it ("1.3 On activation the charge nurse retrieves ..."). Parsed once at app startup (app.main
lifespan -> load()); items keep their section number so the UI can cite the binder.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import config

ROLES: dict[str, tuple[str, re.Pattern[str]]] = {  # role id -> (section-2 label, how the policy names it)
    "charge_nurse": ("Charge nurse", re.compile(r"\bcharge nurses?\b", re.I)),
    "rn": ("Registered nurse (RN)", re.compile(r"\bRNs?\b")),
    "pharmacy": ("Pharmacy", re.compile(r"\bpharmacy\b", re.I)),
    "unit_clerk": ("Unit clerk", re.compile(r"\bunit clerks?\b", re.I)),
    "provider": ("Provider", re.compile(r"\bproviders?\b", re.I)),
}
ROLES_SECTION = "2"
_HEADER = re.compile(r"^#{2,3}\s+(\d+(?:\.\d+)*)\.?\s")
_PARA = re.compile(r"^(\d+(?:\.\d+)+)\s+(.+)$", re.S)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


@dataclass(frozen=True)
class Item:
    id: str          # stable within a policy revision: "<section>#<n>"
    section_id: str  # numbered paragraph the item comes from
    text: str
    source: str      # "role" (section 2 duty statement) or "duty" (named elsewhere in the policy)


def paragraphs(text: str) -> list[tuple[str, str, str]]:
    """(top-level section, paragraph number, paragraph text) for every numbered paragraph."""
    out, top = [], ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if m := _HEADER.match(block):
            top = m.group(1).split(".")[0]
        elif top and (m := _PARA.match(block)):
            out.append((top, m.group(1), " ".join(m.group(2).split())))
    return out


def _cap(s: str) -> str:
    s = s.strip().rstrip(".").strip()
    return s[:1].upper() + s[1:]


def _duties(statement: str) -> list[str]:
    """'leads x, assigns y, and keeps z.' -> [x, y, z]; ';' separates items when the duties contain lists."""
    parts = statement.split(";") if ";" in statement else re.split(r",\s+(?:and\s+)?", statement)
    return [_cap(re.sub(r"^and\s+", "", p.strip())) for p in parts if p.strip()]


def parse(text: str) -> dict[str, list[Item]]:
    out: dict[str, list[Item]] = {role: [] for role in ROLES}
    paras = paragraphs(text)
    for top, num, body in paras:
        if top != ROLES_SECTION:
            continue
        label, _, statement = body.partition(":")
        role = next((r for r, (lbl, _) in ROLES.items() if lbl.lower() == label.strip().lower()), None)
        if role and statement:
            out[role] += [Item(f"{num}#{i}", num, d, "role") for i, d in enumerate(_duties(statement), 1)]
    for top, num, body in paras:
        if top == ROLES_SECTION:
            continue
        for role, (_, pattern) in ROLES.items():
            named = [s for s in _SENTENCE.split(body) if pattern.search(s)]
            if named:
                out[role].append(Item(f"{num}#1", num, " ".join(named), "duty"))
    return out


_CACHE: dict[str, list[Item]] | None = None


def load() -> dict[str, list[Item]]:
    """Parse the policy once (called at startup); later calls return the cached result."""
    global _CACHE
    if _CACHE is None:
        _CACHE = parse((config.data_dir() / "policy" / "downtime_policy.md").read_text())
    return _CACHE
