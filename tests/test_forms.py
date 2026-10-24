"""Printable form templates, layout maps and seeded fixtures (tools/form_fixtures.py)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.ai.forms import MOCK_READS
from app.capture.layouts import layout
from contracts import FormType, ReaderOutput

ROOT = Path(__file__).resolve().parent.parent
FORMS_DIR = ROOT / "data" / "forms"
PRINTED = [FormType.vitals_flowsheet, FormType.downtime_mar, FormType.admission_note]


def _generator():
    spec = importlib.util.spec_from_file_location("form_fixtures", ROOT / "tools" / "form_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("ft", PRINTED)
def test_layout_map_covers_every_schema_field(ft):
    boxes = layout(ft)
    assert set(boxes) == set(MOCK_READS[ft])  # exactly the CapturedRecord field schema for this form type
    for name, b in boxes.items():
        assert b.width > 0.05 and b.height > 0.05, name
        assert b.left + b.width <= 1 and b.top + b.height <= 1, name
    ordered = sorted(boxes.values(), key=lambda b: (b.top, b.left))
    for a, b in zip(ordered, ordered[1:]):  # no two printed boxes overlap
        overlap_x = min(a.left + a.width, b.left + b.width) - max(a.left, b.left)
        overlap_y = min(a.top + a.height, b.top + b.height) - max(a.top, b.top)
        assert overlap_x <= 0 or overlap_y <= 0


def test_verbal_order_has_no_printed_layout():
    assert layout(FormType.verbal_order) == {}


@pytest.mark.parametrize("ft", PRINTED)
def test_template_html_matches_layout_map(ft):
    page = (FORMS_DIR / f"{ft.value}.html").read_text()
    assert "@page { size: letter" in page and "@media print" in page
    for name, b in layout(ft).items():
        assert (f'data-field="{name}" style="left:{b.left * 100:.2f}%;top:{b.top * 100:.2f}%;'
                f'width:{b.width * 100:.2f}%;height:{b.height * 100:.2f}%"') in page


@pytest.mark.parametrize("ft", PRINTED)
def test_fixture_ground_truth_matches_schema(ft):
    truth = json.loads((FORMS_DIR / "fixtures" / f"{ft.value}-01.json").read_text())
    assert truth["form_type"] == ft.value and truth["synthetic"] is True
    assert set(truth["fields"]) == set(MOCK_READS[ft])
    ReaderOutput(fields=truth["fields"])  # contract-valid values (str or null)
    image = FORMS_DIR / "fixtures" / truth["image"]
    assert image.read_bytes()[:2] == b"\xff\xd8" and image.stat().st_size < 300_000


def test_generator_is_seeded_and_matches_committed(tmp_path):
    gen = _generator()
    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(a)
    gen.generate(b, seed=gen.SEED)
    for path in sorted(a.rglob("*")):
        if path.is_file():
            rel = path.relative_to(a)
            assert path.read_bytes() == (b / rel).read_bytes(), f"{rel} not deterministic"
            if path.suffix != ".jpg":  # images depend on the local font; text outputs must match git
                assert path.read_bytes() == (FORMS_DIR / rel).read_bytes(), f"{rel} differs from committed"
    assert len(list((a / "fixtures").glob("*.jpg"))) == 3
