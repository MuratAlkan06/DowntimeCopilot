"""web/print.css: each print mode prints its own view and no other. print.css forces `main, .view` to display:block
(over the hidden attribute), so a view missing from a mode's hide list leaks into that print. This resolves the
!important display rules per mode (specificity, then source order) against every #view-* id in index.html, so a
new view or a new print mode must be handled in print.css or this test fails."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"

# Body class set during the print -> the one view that prints. The single packet print sets no class.
MODES = {
    "": "view-downtime",
    "printing-all": "view-downtime",
    "printing-incident": "view-recovery",
    "printing-notice": "view-notice",
}


def _view_ids() -> set[str]:
    ids = set(re.findall(r'\bid="(view-[\w-]+)"', (WEB / "index.html").read_text()))
    assert {"view-downtime", "view-during", "view-notice", "view-recovery", "view-command"} <= ids
    return ids


def _display_rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        m = re.search(r"display:\s*(none|block)\s*!important", body)
        if m:
            out += [(" ".join(s.split()), m.group(1)) for s in sel.split(",")]
    return out


def _specificity(sel: str) -> tuple[int, int, int]:
    return (len(re.findall(r"#[\w-]+", sel)),
            len(re.findall(r"\.[\w-]+", sel)) + len(re.findall(r":(?!not\()[\w-]+", sel)),
            len(re.findall(r"(?:^|[\s>+~])[a-z][\w-]*", sel)))


def _resolve(mode: str, ids: set[str], rules: list[tuple[str, str]]) -> set[str]:
    """The view ids that display in a print with body class `mode`."""
    best: dict[str, tuple[tuple[int, int, int], int, str]] = {}
    for order, (sel, display) in enumerate(rules):
        rest = sel
        q = re.match(r"body((?:\.[\w-]+|:not\(\.[\w-]+\))+) (.+)$", sel)
        if q:
            if any(cls != mode for cls in re.findall(r"(?<!\()\.([\w-]+)", q.group(1))):
                continue  # needs a body class this mode does not set
            if mode in re.findall(r":not\(\.([\w-]+)\)", q.group(1)):
                continue  # excluded for this mode
            rest = q.group(2)
        if m := re.fullmatch(r"#(view-[\w-]+)", rest):
            targets = {m.group(1)} & ids
        elif m := re.fullmatch(r"main > \.view:not\(#(view-[\w-]+)\)", rest):
            targets = ids - {m.group(1)}
        elif rest == ".view":
            targets = ids
        else:
            continue
        for v in targets:
            key = (_specificity(sel), order, display)
            if v not in best or key[:2] > best[v][:2]:
                best[v] = key
    return {v for v in ids if v in best and best[v][2] == "block"}


def test_print_modes_match_the_body_classes_the_app_sets():
    src = "".join(p.read_text() for p in [WEB / "app.js", *sorted((WEB / "js").glob("*.js"))])
    assert set(re.findall(r"classList\.add\('(printing-[\w-]+)'\)", src)) == set(MODES) - {""}


@pytest.mark.parametrize("mode", list(MODES))
def test_each_print_mode_prints_only_its_own_view(mode):
    ids, rules = _view_ids(), _display_rules((WEB / "print.css").read_text())
    shown = _resolve(mode, ids, rules)
    assert shown == {MODES[mode]}, (
        f"print mode {mode or 'packet'!r} shows {sorted(shown)}; hide every other #view-* in web/print.css")
