"""web/js/notice.js (S15): the bilingual patient notice is static shell text. English and Spanish render with the
native-review label on the Spanish page only; rendering makes no network call (so no model call); no unit time is
shown; and "Print notice" prints the notice alone, one page per language. The render test runs the real modules
in node with a minimal fake DOM (skipped if node is absent)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parent.parent / "web"
LABEL = "Traducción pendiente de revisión por hablante nativo. / Translation pending native-speaker review."

# Just enough DOM for dom.js h()/mount(): elements keep children, attributes and text.
FAKE_DOM = """
class El {
  constructor(tag) { this.tag = tag; this.kids = []; this.attrs = {}; this.className = ''; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  append(...ks) { this.kids.push(...ks); }
  replaceChildren(...ks) { this.kids = ks; }
  get textContent() { return this.kids.map((k) => k.textContent).join(''); }
  set textContent(v) { this.kids = [new Text(v)]; }
}
class Text { constructor(t) { this.t = t; } get textContent() { return this.t; } }
globalThis.Node = Object;
globalThis.document = { createElement: (t) => new El(t), createTextNode: (t) => new Text(t) };
globalThis.window = { addEventListener() {} };
globalThis.navigator ??= { onLine: true };
globalThis.localStorage = { getItem: () => null, setItem() {} };
const calls = [];
globalThis.fetch = async (...a) => { calls.push(String(a[0])); throw new Error('no network at render'); };
"""


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    if NODE is None:
        pytest.skip("node not installed")
    tmp = tmp_path_factory.mktemp("notice")
    for name in ("api.js", "dom.js", "state.js", "notice.js"):
        (tmp / name).write_text((WEB / "js" / name).read_text())
    (tmp / "package.json").write_text('{"type": "module"}')
    script = FAKE_DOM + (
        f"const N = await import({json.dumps((tmp / 'notice.js').as_uri())});\n"
        "const root = document.createElement('div');\n"
        "N.renderNotice(root);\n"
        "await new Promise((r) => setTimeout(r, 0));\n"
        "const sections = root.kids.map((s) => ({ tag: s.tag, lang: s.attrs.lang, cls: s.className,\n"
        "  paras: s.kids.map((k) => ({ tag: k.tag, cls: k.className, text: k.textContent })) }));\n"
        "console.log(JSON.stringify({ sections, calls, texts: N.NOTICE.map(N.noticeText), label: N.REVIEW_LABEL }));"
    )
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _text(section: dict) -> str:
    return "\n".join(p["text"] for p in section["paras"])


def test_english_and_spanish_render_in_order_with_key_phrases(rendered):
    en, es = rendered["sections"]
    assert (en["lang"], es["lang"]) == ("en", "es")
    assert all(s["tag"] == "section" and "notice-lang" in s["cls"].split() for s in (en, es))
    for s in (en, es):
        assert s["paras"][0] == {"tag": "p", "cls": "notice-hospital", "text": "Bayview Community Hospital"}
        assert s["paras"][1]["tag"] == "h2"
    t = _text(en)
    for phrase in ("Our computer systems are down. Your care continues.", "temporarily unavailable",
                   "established paper procedures", "medications and treatments continue",
                   "ask your nurse or the charge nurse"):
        assert phrase in t, phrase
    t = _text(es)
    for phrase in ("Nuestros sistemas informáticos no están funcionando. Su atención continúa.",
                   "no están disponibles por el momento", "procedimientos establecidos en papel",
                   "Sus medicamentos y tratamientos continúan", "a cargo del turno"):
        assert phrase in t, phrase


def test_native_review_label_on_the_spanish_page_only(rendered):
    en, es = rendered["sections"]
    assert rendered["label"] == LABEL
    assert es["paras"][-1] == {"tag": "p", "cls": "notice-review", "text": LABEL}
    assert LABEL not in _text(en) and not any(p["cls"] == "notice-review" for p in en["paras"])
    # The guardrail evidence check reads exactly what each page shows, the label included.
    assert rendered["texts"] == [_text(en), _text(es)]


def test_render_makes_no_network_call_and_shows_no_clock(rendered):
    assert rendered["calls"] == []
    src = (WEB / "js" / "notice.js").read_text()
    assert re.findall(r"^import .*$", src, re.M) == ["import { h, mount } from './dom.js';"]
    assert not re.search(r"\b(fetch|api|fmtTime|fmtDateTime|Date)\b\s*[.(]", src)
    for s in rendered["sections"]:
        t = _text(s)
        assert not re.search(r"\d{1,2}:\d{2}", t) and "unit time" not in t.lower()


def test_shell_serves_the_notice_without_any_model_call(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.ai import client as ai
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")

    def boom(*a, **k):
        raise AssertionError("model call while serving the notice")

    monkeypatch.setattr(ai, "call", boom)
    monkeypatch.setattr(ai, "_runtime", boom)
    from app.main import app
    c = TestClient(app)  # no lifespan: static files only
    assert c.get("/index.html").status_code == 200 and c.get("/js/notice.js").status_code == 200
    assert not [r for r in app.routes if "notice" in getattr(r, "path", "")]  # no server route for it


def test_notice_view_ships_in_the_shell_and_opens_from_during():
    sw = (WEB / "sw.js").read_text()
    assert "const VERSION = 'v26';" in sw
    assert "'js/notice.js'" in re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)
    html = (WEB / "index.html").read_text()
    during = re.search(r'<section id="view-during".*?\n    </section>', html, re.S).group(0)
    assert '<button id="btn-notice" class="btn btn-secondary" type="button">Patient notice</button>' in during
    notice = re.search(r'<section id="view-notice"[^>]*>.*?\n    </section>', html, re.S).group(0)
    assert re.match(r'<section id="view-notice" class="view" data-view="notice"[^>]* hidden>', notice)
    for el in ('id="btn-notice-back"', 'id="btn-print-notice"', '<div id="notice" class="notice"></div>'):
        assert el in notice
    app_js = (WEB / "app.js").read_text()
    assert "'notice'" in re.search(r"const PHASES = \[(.*?)\];", app_js).group(1)


def _rules(css: str) -> dict[str, str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: dict[str, str] = {}
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        for s in sel.split(","):
            out[" ".join(s.split())] = out.get(" ".join(s.split()), "") + body
    return out


def test_print_isolates_the_notice_one_page_per_language():
    rules = _rules((WEB / "print.css").read_text())
    hide = "display: none !important"
    # Only the notice prints: every other view, the batch packet holder and the notice's own controls are hidden;
    # the header, nav, banners and buttons are already hidden for every print.
    assert hide in rules["body.printing-notice main > .view:not(#view-notice)"]
    assert hide in rules["body.printing-notice .print-all"]
    assert hide in rules["body.printing-notice #view-notice > :not(#notice)"]
    assert "display: block !important" in rules["body.printing-notice #view-notice"]
    for el in (".app-header", ".phase-nav", ".banners", ".btn"):
        assert hide in rules[el]
    # Every other print (packets, incident report) leaves the notice out.
    assert hide in rules["body:not(.printing-notice) #view-notice"]
    # A page break after each language except the last: English page, then Spanish page.
    assert "break-after: page" in rules[".notice-lang"] and "page-break-after: always" in rules[".notice-lang"]
    assert "break-after: auto" in rules[".notice-lang:last-child"]
    # The notice print never toggles the stamp other prints add (no internal clock on a patient notice).
    src = (WEB / "js" / "notice.js").read_text()
    assert "print-stamp" not in src and "classList.add('printing-notice')" in src
    assert "classList.remove('printing-notice')" in src
