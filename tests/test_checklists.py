"""Role checklists: deterministic parser over the downtime policy + GET /checklists."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import checklists
from app.store import get_store
from tests.test_api import api  # noqa: F401 - shared DRY_RUN app fixture

APP_ROLES = ["charge_nurse", "rn", "pharmacy", "unit_clerk"]


def test_parser_lists_are_non_empty_and_distinct():
    lists = checklists.load()
    texts = {r: [i.text for i in lists[r]] for r in APP_ROLES}
    assert all(len(t) >= 3 for t in texts.values())
    assert len({tuple(t) for t in texts.values()}) == len(APP_ROLES)  # no two roles share a list
    for r in APP_ROLES:  # each list leads with its own section-2 duty statement
        assert lists[r][0].source == "role" and lists[r][0].section_id.startswith("2.")
        assert len({i.id for i in lists[r]}) == len(lists[r])  # ids unique per role (localStorage keys)


@pytest.mark.parametrize("role, section_id, needle", [
    ("charge_nurse", "2.1", "Keeps the Downtime Log"),
    ("charge_nurse", "7.1", "records the restore time"),
    ("rn", "5.1", "five rights"),
    ("pharmacy", "7.3", "reconciles the medication profile"),
    ("unit_clerk", "4.2.4", "census sheet"),
])
def test_parser_picks_up_numbered_duties(role, section_id, needle):
    assert any(i.section_id == section_id and needle in i.text for i in checklists.load()[role])


def test_parser_splits_duties_and_is_deterministic():
    text = Path("data/policy/downtime_policy.md").read_text()
    assert checklists.parse(text) == checklists.parse(text)
    assert [i.text for i in checklists.parse(text)["pharmacy"] if i.source == "role"] == [
        "Verifies all new orders on paper", "Prepares and labels doses manually",
        "Re-enters orders into the EHR during recovery"]


def test_parser_has_no_model_imports():
    tree = ast.parse(Path(checklists.__file__).read_text())
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("app.ai", "boto3", "botocore")) for m in mods), mods


def checklist_audit() -> list:
    return [e for e in reversed(get_store().list_audit(100)) if e.target_type == "checklist"]


@pytest.mark.parametrize("role", APP_ROLES)
@pytest.mark.parametrize("params", [{}, {"auto": 1}], ids=["deliberate", "auto"])
def test_get_checklists(api, role, params):
    r = api.get("/checklists", params={"role": role, **params})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == role and len(body["items"]) >= 3
    assert set(body["items"][0]) == {"id", "section_id", "text", "source"}
    assert r.json() == api.get("/checklists", params={"role": role}).json()  # auto never changes the body


def test_auto_fetch_is_a_view_and_a_plain_fetch_acknowledges_the_requested_role(api):
    # GETs carry no X-Role (web/js/api.js), so the actor defaults to charge_nurse; the requested role is the subject.
    api.get("/checklists", params={"role": "rn", "auto": 1})
    api.get("/checklists", params={"role": "rn"})
    view, ack = checklist_audit()
    assert (view.action, view.actor_role, view.target_id, view.details) == (
        "checklist.view", "charge_nurse", "rn", {"found": True})
    assert (ack.action, ack.actor_role, ack.target_id, ack.details) == (
        "checklist.ack", "charge_nurse", "rn", {"found": True, "ack_role": "rn"})


def test_get_checklists_defaults_to_actor_role_and_404s(api):
    assert api.get("/checklists", headers={"X-Role": "pharmacy"}).json()["role"] == "pharmacy"
    assert api.get("/checklists", params={"role": "janitor"}).status_code == 404
    assert api.get("/checklists", params={"role": "janitor", "auto": 1}).status_code == 404
    assert [(e.action, e.details) for e in checklist_audit()] == [
        ("checklist.ack", {"found": True, "ack_role": "pharmacy"}),  # no ?role=: the X-Role's own list
        ("checklist.ack", {"found": False, "ack_role": "janitor"}),
        ("checklist.view", {"found": False})]
