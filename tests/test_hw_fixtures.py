"""Handwriting fixture generator (tools/hw_fixtures.py), driven by the stub SD19-format glyph pack.

Offline by construction: every test here runs with sockets and urlopen refused, the glyphs are drawn at test
time (tests/test_hw_glyphs.py), and outputs go to tmp dirs. No dataset bytes, nothing committed.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter

import pytest
from PIL import Image

from app.ai.forms import MOCK_READS
from app.config import vital_ranges
from contracts import FormType, ReaderOutput
from tests.test_hw_glyphs import SD19_SIZE, StubGlyphs, no_network, stub_pack  # noqa: F401 - fixture
from tools import form_fixtures as ff
from tools import hw_fixtures as hf
from tools import hw_glyphs

SEED = 20261027
TRUTH_KEYS = ["schema", "form_type", "fields", "image", "seed", "writer", "distortion", "tags", "source",
              "image_sha256"]
PEN = hf.PEN_COLORS[0]


@pytest.fixture(autouse=True)
def _offline(no_network):  # noqa: F811 - the imported fixture, applied to every test in this module
    yield


def source(**kw) -> StubGlyphs:
    return StubGlyphs(stub_pack(n_writers=40, samples=2, **kw))


def _writer(i: int) -> str:
    return f"f{i:04d}_{i % 100:02d}"


def _all_specs(count: int = 12) -> list[hf.Spec]:
    return [s for ft in hf.FORM_TYPES for s in hf.plan(ft, count)]


# ------------------------------------------------------------------ plan and value policy

def test_field_kinds_match_the_printed_layout_and_reader_schema():
    for ft in hf.FORM_TYPES:
        assert list(hf.FIELD_KINDS[ft]) == list(ff.layout(ft))
        assert set(hf.FIELD_KINDS[ft]) == set(MOCK_READS[FormType(ft)])


def test_default_plan_spread():
    for ft in hf.FORM_TYPES:
        specs = hf.plan(ft)
        assert [s.stem for s in specs] == [f"hw-{ft}-{i:03d}" for i in range(1, 13)]
        kinds = Counter(s.kind for s in specs)
        if ft == "vitals_flowsheet":
            assert kinds == {"nominal": 6, "ambiguous": 3, "out_of_range": 3}
        else:  # no vitals to put out of range: those three become ambiguous-heavy with an extra blank
            assert kinds == {"nominal": 6, "ambiguous": 6}
        assert Counter(s.distortion for s in specs) == {"L0": 4, "L1": 4, "L2": 4}
        assert sum(1 for s in specs if s.blanks) >= 4
    assert len(_all_specs()) == 36
    assert sum(len(ff.layout(s.form_type)) for s in _all_specs()) == 264  # scored fields
    assert len(hf.plan("downtime_mar", 5)) == 5 and len(hf.plan("downtime_mar", 13)) == 13
    with pytest.raises(hf.Refused):
        hf.plan("downtime_mar", 0)


def test_value_policy():
    ranges = vital_ranges()
    for seed in range(8):
        for s in _all_specs():
            vals = hf.choose_values(s, seed)
            assert list(vals) == list(ff.layout(s.form_type))
            assert sum(v is None for v in vals.values()) == s.blanks and vals["bed"] is not None
            outside = 0
            for f, v in vals.items():
                if v is None:
                    continue
                assert hf.ALLOWED_VALUE.fullmatch(v) and not set(v) & set("()[]{}/-")
                kind = hf.FIELD_KINDS[s.form_type][f]
                if kind == "vital":
                    lo, hi = ranges[f]
                    ext_lo, ext_hi = hf.VITAL_EXTREMES[f]
                    assert ext_lo <= float(v) <= ext_hi
                    outside += not (lo <= float(v) <= hi)
                if f in hf.DECIMAL_FIELDS:
                    assert re.fullmatch(r"\d+\.\d", v)
                elif kind in ("vital", "bed", "pain", "height"):
                    assert re.fullmatch(r"\d+", v)
                if kind == "time":
                    assert re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v)
                if kind in ("initials", "route"):
                    assert re.fullmatch(r"[A-Z]{2,3}", v)
                if kind == "pain":
                    assert 0 <= int(v) <= 10
                if s.kind == "ambiguous" and kind == "initials":
                    assert set(v) <= set(hf.CONFUSION_LETTERS)
                if s.kind == "ambiguous" and kind in hf.WORDS:
                    assert v in hf.WORDS[kind][1]
            assert (1 <= outside <= 2) if s.kind == "out_of_range" else outside == 0, (s.stem, vals)
            tags = hf.truth_record(s, vals, seed, "w")["tags"]
            assert tags[0] == s.kind and ("blank" in tags) == bool(s.blanks)


def test_ambiguous_numbers_lean_on_the_confusion_digits():
    rng = random.Random(0)
    for _ in range(50):
        assert set(hf.field_value("vitals_flowsheet", "bed", "ambiguous", rng)) <= hf.CONFUSION_DIGITS
        assert set(hf.field_value("vitals_flowsheet", "time", "ambiguous", rng).replace(":", "")) <= hf.CONFUSION_DIGITS


def test_out_of_range_is_derived_from_vital_ranges_at_generation_time(monkeypatch):
    monkeypatch.setenv("VITAL_RANGE_SPO2", "95,100")
    monkeypatch.setenv("VITAL_RANGE_HEART_RATE", "40,190")
    for i in range(100):
        assert float(hf.out_of_range_value("spo2", random.Random(i))) < 95
        assert float(hf.out_of_range_value("heart_rate", random.Random(i))) < 40  # 190+ is beyond plausible
    monkeypatch.setenv("VITAL_RANGE_SPO2", "60,100")
    with pytest.raises(hf.Refused, match="no plausible out-of-range spo2"):
        hf.out_of_range_value("spo2", random.Random(0))


# ------------------------------------------------------------------ writers

def test_one_writer_per_fixture_covering_its_characters():
    src, specs = source(), _all_specs()
    values = {s.stem: hf.choose_values(s, SEED) for s in specs}
    writers = hf.assign_writers(specs, values, src, SEED)
    assert len(set(writers.values())) == len(specs) == 36
    for stem, w in writers.items():
        assert hf.needed_chars(values[stem]) <= src.coverage(w)
    assert writers == hf.assign_writers(specs, values, source(), SEED)


def test_a_value_needing_an_uncoverable_character_refuses():
    spec = hf.Spec("hw-downtime_mar-001", "downtime_mar", "nominal", "L0", 0)
    with pytest.raises(hf.Refused, match="no writer has 'q'"):
        hf.assign_writers([spec], {spec.stem: {"medication": "quinine"}}, source(drop={"*": "q"}), SEED)
    # punctuation counts: with no writer's i/j to harvest a dot from, "37.5" cannot be written
    with pytest.raises(hf.Refused, match="no writer has '.'"):
        hf.assign_writers([spec], {spec.stem: {"temp_c": "37.5"}}, source(drop={"*": "ij"}), SEED)
    half = {**{_writer(i): "q" for i in range(20)}, **{_writer(i): "z" for i in range(20, 40)}}
    with pytest.raises(hf.Refused, match="no single writer"):
        hf.assign_writers([spec], {spec.stem: {"medication": "quiz"}}, source(drop=half), SEED)


def test_too_few_writers_refuses_before_writing_anything(tmp_path):
    with pytest.raises(hf.Refused, match="36 fixtures need 36 distinct writers"):
        hf.generate(tmp_path, SEED, StubGlyphs(stub_pack(n_writers=4)))
    only_three_can_dot = source(drop={_writer(i): "ij" for i in range(3, 40)})
    with pytest.raises(hf.Refused, match="already used"):
        hf.generate(tmp_path, SEED, only_three_can_dot)
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ truth before render, determinism

def test_truth_is_on_disk_before_any_glyph_is_picked(tmp_path):
    stems = {"hw-vitals_flowsheet-012", "hw-admission_note-003"}
    expected = {s.stem: hf.choose_values(s, SEED) for s in _all_specs() if s.stem in stems}
    src, calls = source(), []
    real_get = src.get

    def get(writer, char, rng):
        if not calls:  # the first glyph pick of the run
            for stem in stems:
                truth = json.loads((tmp_path / f"{stem}.json").read_text())
                assert truth["fields"] == expected[stem] and "image_sha256" not in truth
                assert not (tmp_path / f"{stem}.jpg").exists()
        calls.append(char)
        return real_get(writer, char, rng)

    src.get = get
    hf.generate(tmp_path, SEED, src, only=stems)
    assert calls
    for stem in stems:
        truth = json.loads((tmp_path / f"{stem}.json").read_text())
        assert truth["fields"] == expected[stem]
        assert truth["image_sha256"] == hashlib.sha256((tmp_path / f"{stem}.jpg").read_bytes()).hexdigest()


def test_same_seed_same_bytes_and_truth_schema(tmp_path):
    stems = {"hw-vitals_flowsheet-001", "hw-downtime_mar-002", "hw-admission_note-012"}  # L0, L1, L2
    a, b = tmp_path / "a", tmp_path / "b"
    hf.generate(a, SEED, source(), only=stems)
    hf.generate(b, SEED, source(), only=stems)
    names = sorted(p.name for p in a.iterdir())
    assert names == sorted(f"{s}.{ext}" for s in stems for ext in ("jpg", "json"))
    for name in names:
        assert (a / name).read_bytes() == (b / name).read_bytes(), f"{name} not deterministic"

    specs = _all_specs()
    values = {s.stem: hf.choose_values(s, SEED) for s in specs}
    full_set_writers = hf.assign_writers(specs, values, source(), SEED)
    levels = set()
    for stem in stems:
        truth = json.loads((a / f"{stem}.json").read_text())
        image = (a / f"{stem}.jpg").read_bytes()
        assert list(truth) == TRUTH_KEYS
        assert truth["schema"] == 1 and truth["seed"] == SEED and truth["source"] == hf.SOURCE
        assert truth["image"] == f"{stem}.jpg" and truth["image_sha256"] == hashlib.sha256(image).hexdigest()
        assert truth["writer"] == full_set_writers[stem]  # --only renders with the full set's writer
        assert set(truth["fields"]) == set(MOCK_READS[FormType(truth["form_type"])])
        ReaderOutput(fields=truth["fields"])
        assert image[:2] == b"\xff\xd8" and len(image) < 300_000
        assert Image.open(a / f"{stem}.jpg").size == (850, 1100)
        levels.add(truth["distortion"])
    assert levels == {"L0", "L1", "L2"}

    c = tmp_path / "c"
    hf.generate(c, SEED + 1, source(), only={"hw-vitals_flowsheet-001"})
    assert (c / "hw-vitals_flowsheet-001.jpg").read_bytes() != (a / "hw-vitals_flowsheet-001.jpg").read_bytes()
    with pytest.raises(hf.Refused, match="unknown stem"):
        hf.generate(tmp_path / "d", SEED, source(), only={"hw-vitals_flowsheet-999"})


# ------------------------------------------------------------------ compositing hard checks

def _gaps(value: str, placed: list[hf.Placed]) -> tuple[list[int], list[int]]:
    breaks: list[bool] = []
    for word in value.split(" "):
        breaks += [False] * (len(word) - 1) + [True]
    letter, word = [], []
    for i, is_word in enumerate(breaks[:-1]):
        (word if is_word else letter).append(placed[i + 1].box[0] - placed[i].box[2])
    return letter, word


@pytest.mark.parametrize("weight", hf.INK_WEIGHTS)
def test_compositing_checks_hold(weight):
    page, boxes = ff.render_blank("admission_note", hf.COMPOSE_DPI)
    assert page.size == (1700, 2200)
    src = source()
    for field, value in (("history", "type 2 diabetes, hypertension"), ("weight_kg", "92.4"), ("bed", "12")):
        placed = hf.write_value(page, boxes[field], value, _writer(5), src, random.Random(1), random.Random(2),
                                PEN, weight)
        assert [p.char for p in placed] == [c for c in value if c != " "]
        left, top, right, bottom = boxes[field]
        for p in placed:
            x0, y0, x1, y1 = p.box
            assert left < x0 < x1 < right and top < y0 < y1 < bottom
            assert p.ink >= (hf.MIN_PUNCT_INK_PX if p.char in ".:," else hf.MIN_INK_PX)
        letter, word = _gaps(value, placed)
        assert all(g > 0 for g in letter)
        if word:
            assert min(word) >= hf.MIN_WORD_GAP_RATIO * sum(letter) / len(letter)


def test_a_long_value_is_shrunk_whole_never_truncated():
    page, boxes = ff.render_blank("vitals_flowsheet", hf.COMPOSE_DPI)
    src = source()
    narrow = hf.write_value(page, boxes["bed"], "acetaminophen", _writer(1), src, random.Random(1),
                            random.Random(2), PEN, "regular")
    wide = hf.write_value(page, boxes["temp_c"], "a", _writer(1), src, random.Random(1), random.Random(2), PEN,
                          "regular")
    assert len(narrow) == len("acetaminophen")
    assert narrow[-1].box[2] < boxes["bed"][2]
    assert narrow[0].box[3] - narrow[0].box[1] < wide[0].box[3] - wide[0].box[1]  # the same 'a', shrunk
    with pytest.raises(hf.GenerationError):  # so long it would be illegible: refuse rather than ship it
        hf.write_value(page, boxes["bed"], " ".join(["acetaminophen"] * 8), _writer(1), src, random.Random(1),
                       random.Random(2), PEN, "regular")


def test_hard_checks_fail_loudly(monkeypatch):
    page, boxes = ff.render_blank("vitals_flowsheet", hf.COMPOSE_DPI)
    src = source()

    def write(value, field="temp_c", s=src):
        return hf.write_value(page, boxes[field], value, _writer(2), s, random.Random(1), random.Random(2), PEN,
                              "regular")

    write("37.5")
    with monkeypatch.context() as m:
        m.setattr(hf, "MIN_PUNCT_INK_PX", 10_000)
        with pytest.raises(hf.GenerationError, match=r"'\.' has \d+ ink pixels"):
            write("37.5")
    with monkeypatch.context() as m:
        m.setattr(hf, "MIN_WORD_GAP_RATIO", 1000.0)
        with pytest.raises(hf.GenerationError, match="word gap"):
            write("5000 units", "initials")
    blank = StubGlyphs({_writer(2): {"5": [Image.new("L", SD19_SIZE, 255)]}})
    with pytest.raises(hf.GenerationError, match="no ink"):
        write("5", s=blank)
    with pytest.raises(hf.GenerationError, match="value policy"):
        write("penicillin (hives)", "initials")
    placed = write("37")
    with pytest.raises(hf.GenerationError, match="glyphs pasted"):
        hf.check_placement("375", boxes["temp_c"], placed)
    moved = [hf.Placed(p.char, (p.box[0] + 2000, p.box[1], p.box[2] + 2000, p.box[3]), p.ink) for p in placed]
    with pytest.raises(hf.GenerationError, match="outside its box"):
        hf.check_placement("37", boxes["temp_c"], moved)


# ------------------------------------------------------------------ distortion ladder

def test_distortion_levels_are_the_frozen_bounds():
    assert hf.LEVELS == {
        "L0": hf.Level(perspective=0.0, lighting=0.0, max_angle=1.6, blur=(0.6, 0.6), quality=70),
        "L1": hf.Level(perspective=0.015, lighting=0.18, max_angle=1.6, blur=(0.6, 0.6), quality=60),
        "L2": hf.Level(perspective=0.025, lighting=0.30, max_angle=3.0, blur=(0.6, 1.0), quality=50),
    }


def test_l0_is_exactly_the_synthetic_photo_effect():
    page, _boxes = ff.render_blank("downtime_mar", ff.DPI)
    assert hf.distort(page.copy(), "L0", random.Random(5)) == ff.photo_effect(page.copy(), random.Random(5))


def test_perspective_coefficients_and_drawn_parameters_stay_in_bounds():
    W, H = 850, 1100
    page_corners = [(0, 0), (W, 0), (W, H), (0, H)]
    assert hf.perspective_coefficients(page_corners, page_corners) == pytest.approx((1, 0, 0, 0, 1, 0, 0, 0), abs=1e-12)
    for name in ("L1", "L2"):
        lv = hf.LEVELS[name]
        for i in range(200):
            p = hf.draw_distortion(name, (W, H), random.Random(i))
            assert lv.lighting / 2 <= p["lighting"] <= lv.lighting and lv.blur[0] <= p["blur"] <= lv.blur[1]
            for (X, Y), (x, y) in zip(p["corners"], page_corners):
                assert abs(X - x) <= lv.perspective * W + 1e-9 and abs(Y - y) <= lv.perspective * H + 1e-9
            a, b, c, d, e, f, g, h = hf.perspective_coefficients(p["corners"], page_corners)
            for (X, Y), (x, y) in zip(p["corners"], page_corners):
                den = g * X + h * Y + 1
                assert abs((a * X + b * Y + c) / den - x) < 1e-6 and abs((d * X + e * Y + f) / den - y) < 1e-6
            assert abs(g) * W + abs(h) * H <= 8 * lv.perspective  # the projective terms stay small


def test_lighting_gradient_amplitude():
    for amplitude in (0.0, 0.18, 0.30):
        for angle in (0.0, 45.0, 200.0):
            lo, hi = hf.lighting_gradient((850, 1100), amplitude, angle).convert("L").getextrema()
            assert hi <= 255 and lo >= math.floor(255 * (1 - amplitude)) - 1
            assert hi - lo >= 0.8 * 255 * amplitude


# ------------------------------------------------------------------ CLI

def test_cli_refuses_without_verified_sources(tmp_path, monkeypatch, capsys):
    lock = hw_glyphs.load_lock()
    for entry in lock["archives"].values():
        entry.update(sha256=None, size=None)
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(lock))
    monkeypatch.setattr(hw_glyphs, "LOCKFILE", path)
    out = tmp_path / "out"
    assert hf.main(["--seed", str(SEED), "--out", str(out), "--glyph-root", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "unverified" in err
    assert not out.exists()
