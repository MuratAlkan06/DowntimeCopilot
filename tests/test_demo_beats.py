"""tools/demo_video/beats.json, the demo video's single source of truth, pinned to DEMO.md (the script) and to the
caption allowlist (ui-strings.json). Pure Python: no node, ffmpeg, browser or network.

The video is DEMO.md's 90-second cut: one beat per run-of-show row that is not marked *(cut)*, in order. Captions
describe actions; a “quoted” fragment must be an allowlisted UI string, the allowlist may hold only strings the app
renders (web/), and the beat that shows a quote must assert it on screen. The budgets are the frozen table."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VIDEO = ROOT / "tools" / "demo_video"
BEATS = json.loads((VIDEO / "beats.json").read_text(encoding="utf-8"))
ALLOW = json.loads((VIDEO / "ui-strings.json").read_text(encoding="utf-8"))
WEB = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "web").rglob("*")) if p.suffix in (".html", ".js"))
MODES = ("live", "dry_run")

OPENING = {"live": "All data is synthetic. Recorded live against real AWS services.",
           "dry_run": "DRY RUN — development footage, not a deliverable."}
BUDGET = {"declare": 15, "policy": 10, "vitals": 17, "verbal": 11, "offline": 16, "restore": 12, "digest": 7}
OPS = {  # action -> required keys
    "pause": {"s"}, "hold": set(), "mark": {"id"}, "click": {"sel"}, "select": {"sel", "value"},
    "upload": {"sel", "file"}, "type": {"sel", "value"}, "scroll": {"sel", "block"}, "idle": set(),
    "waitFor": {"sel"}, "trimWait": {"id", "timeout_s"}, "assertText": {"sel", "text"}, "check": {"name"},
    "offline": {"value"}, "reload": set(), "clock": {"minutes"},
}
CHECKS = {"policy", "offlineShell", "cachedList", "drained", "planted", "syncChip"}  # record.mjs CHECKS
# Allowlisted strings the app builds from a template: the full string must match, and the template's fixed text
# must be in web/. Every other allowlisted string must appear in web/ verbatim.
TEMPLATES = [
    (r"\d+ agree · (\d+ corrected · )?\d+ flagged for review", "flagged for review"),
    (r"Guardrail: passed", "Guardrail: "),
    (r"\d+ queued", " queued"),
    (r"Offline — form saved on this device and queued for reading when connected\. Keep the paper original\.",
     " — form saved on this device and queued for reading when connected. Keep the paper original."),
    (r"\d+ captures? queued on this device — will upload when connected\.", "will upload when connected."),
    (r"Cached for offline: all \d+ roles\.", "Cached for offline: all "),
    (r"R-REC-0\d", "REC-01"),
    (r"Synced · (DynamoDB|dry run)", "Synced · "),
]


def quotes(text: str) -> list[str]:
    return re.findall(r"“([^”]+)”", text)


def texts(beat: dict, mode: str) -> list[str]:
    out = [(mode == "dry_run" and c.get("text_dry_run")) or c["text"] for c in beat["captions"]]
    if beat.get("notice"):
        out.append((mode == "dry_run" and beat["notice"].get("text_dry_run")) or beat["notice"]["text"])
    return out


def run_of_show() -> list[dict]:
    """DEMO.md's "Run of show" table: one dict per row (time, beat cell, cut flag)."""
    md = (ROOT / "DEMO.md").read_text(encoding="utf-8")
    section = md.split("## Run of show", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"\d:\d\d", cells[0]):
            rows.append({"time": cells[0], "beat": cells[1], "cut": "*(cut)*" in cells[1]})
    return rows


def test_the_manifest_parses_and_every_beat_is_well_formed():
    assert BEATS["schema"] == 1 and BEATS["script"] == "DEMO.md" and BEATS["still_s"] >= 1.5
    ids = [b["id"] for b in BEATS["beats"]]
    assert len(ids) == len(set(ids))
    for b in BEATS["beats"]:
        assert set(b) >= {"id", "demo", "max_s", "speedup", "speedup_label", "captions", "actions"}, b["id"]
        assert isinstance(b["speedup"], bool) and b["max_s"] > 0
        assert re.search(r"sped up", b["speedup_label"], re.I) and "{s}" in b["speedup_label"], b["id"]
        assert b["captions"] and all(c["text"] for c in b["captions"]) and "at" not in b["captions"][0], b["id"]
        marks = [a["id"] for a in b["actions"] if a["do"] == "mark"]
        assert len(marks) == len(set(marks)), b["id"]
        for c in [*b["captions"][1:], *([b["notice"]] if b.get("notice") and b["notice"].get("at") else [])]:
            assert c["at"] in marks, f"{b['id']}: caption marker {c['at']} has no mark action"
        waits = [a["id"] for a in b["actions"] if a["do"] == "trimWait"]
        assert len(waits) == len(set(waits)), b["id"]
        for a in b["actions"]:
            assert a["do"] in OPS and OPS[a["do"]] <= set(a), f"{b['id']}: {a}"
            if a["do"] == "trimWait":
                assert ("sel" in a) != ("check" in a), f"{b['id']}: a trimWait waits on a selector or a check"
            if a["do"] == "check" or (a["do"] == "trimWait" and "check" in a):
                assert (a.get("name") or a.get("check")) in CHECKS
            if a["do"] == "upload":
                assert (ROOT / a["file"]).is_file(), a["file"]
            if a["do"] == "scroll":
                assert a["block"] in ("start", "center", "top")
        assert b["actions"][-1]["do"] == "hold", f"{b['id']}: the last action pads the beat to its budget"
    gif = BEATS["gif"]
    beat = next(b for b in BEATS["beats"] if b["id"] == gif["beat"])
    assert gif["beat"] == "offline" and 0 < gif["seconds"] <= 12  # the default GIF window: the offline-reload beat
    assert gif["from"] in [a["id"] for a in beat["actions"] if a["do"] == "mark"]


def test_every_beat_is_a_demo_run_of_show_row_and_every_kept_row_is_a_beat():
    rows = run_of_show()
    assert len(rows) == 10 and [r["time"] for r in rows if r["cut"]] == ["0:00", "0:35", "1:35"]
    by_time = {r["time"]: r for r in rows}
    for b in BEATS["beats"]:
        row = by_time.get(b["demo"]["time"])
        assert row, f"{b['id']}: DEMO.md has no run-of-show row at {b['demo']['time']}"
        assert b["demo"]["beat"] in row["beat"], f"{b['id']}: {b['demo']['beat']!r} not in {row['beat']!r}"
        assert not row["cut"], f"{b['id']}: maps to a row marked *(cut)* for the 90-second version"
    kept = [r["time"] for r in rows if not r["cut"]]
    assert [b["demo"]["time"] for b in BEATS["beats"]] == kept  # all of them, in the script's order


def test_budgets_are_the_frozen_table_and_fit_the_duration_gate():
    assert {b["id"]: b["max_s"] for b in BEATS["beats"]} == BUDGET
    assert 85 <= sum(b["max_s"] for b in BEATS["beats"]) <= 95


@pytest.mark.parametrize("mode", MODES)
def test_quoted_caption_fragments_are_allowlisted_and_asserted_on_screen(mode):
    allow = set(ALLOW["strings"])
    for b in BEATS["beats"]:
        asserted = [(mode == "dry_run" and a.get("text_dry_run")) or a["text"]
                    for a in b["actions"] if a["do"] == "assertText"]
        for text in texts(b, mode):
            assert '"' not in text, f"{b['id']}: quote UI strings with “ ” so the allowlist check sees them"
            for q in quotes(text):
                assert q in allow, f"{b['id']} ({mode}): “{q}” is not in ui-strings.json"
                assert any(q in s for s in asserted), f"{b['id']} ({mode}): “{q}” is never asserted on screen"


def test_the_allowlist_holds_only_strings_the_app_renders():
    for s in ALLOW["strings"]:
        if s in WEB:
            continue
        match = [frag for pattern, frag in TEMPLATES if re.fullmatch(pattern, s)]
        assert match, f"“{s}” is neither in web/ verbatim nor built by a known template"
        assert match[0] in WEB, f"the template text {match[0]!r} for “{s}” is no longer in web/"


def test_both_opening_captions_are_present():
    assert BEATS["opening"] == OPENING
    assert BEATS["dry_run_mark"] == "DRY RUN"


@pytest.mark.parametrize("mode", MODES)
def test_each_beats_captions_can_be_read_within_its_budget(mode):
    # lib/post.mjs readS: 20 characters a second, at least 2 s per caption
    for b in BEATS["beats"]:
        caps = [(mode == "dry_run" and c.get("text_dry_run")) or c["text"] for c in b["captions"]]
        assert sum(max(2.0, len(t) * 0.05) for t in caps) <= b["max_s"], b["id"]


def test_the_planted_set_guard_matches_demo_md():
    (check,) = [a for b in BEATS["beats"] for a in b["actions"] if a["do"] == "check" and a["name"] == "planted"]
    row = next(r for r in run_of_show() if r["time"] == "1:20")
    md = (ROOT / "DEMO.md").read_text(encoding="utf-8")
    assert check["items"] == 6 and "Exactly **6** discrepancies" in md
    assert check["groups"] == 3 and "3 items in three groups" in md
    assert sorted(check["rules"]) == sorted(["R-REC-01", "R-REC-02", "R-VO-01", "DIFF-ALLERGY", "DIFF-ORDER", "DIFF-ADT"])
    assert "Systems restored" in row["beat"]
