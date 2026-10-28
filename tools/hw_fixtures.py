"""Handwriting eval fixtures: the three printed forms with values composited from public handwriting samples.

  python -m tools.hw_fixtures --seed N --out evals/fixtures --glyph-root .cache/handwriting \
      [--only <stem>] [--count-per-form N]

Per fixture: <stem>.json (ground truth, committed) and <stem>.jpg (image, git-ignored), stem
hw-<form_type>-<NNN>. Every mark is a real person's ink from NIST Special Database 19 (tools/hw_glyphs.py),
one writer per fixture; nothing is drawn with a font. Call them "forms built from public handwriting
samples", never "handwritten forms". Licensing and privacy: docs/handwriting-datasets.md.

Determinism: master seed -> random.Random(f"{seed}:{stem}:{purpose}") child RNGs (the form_fixtures idiom),
in strict order:
  1. values      every field's value, chosen by the value policy below before anything else exists;
  2. writer      one writer per fixture whose coverage holds every character the values need, punctuation
                 included; picked without replacement across the whole planned set (most constrained
                 fixture first), the same set with or without --only; too few qualify -> refuse loudly.
                 The truth JSONs (everything but image_sha256) are written now, before any glyph is picked;
  3. glyphs      per-character sample picks ("glyphs") and the writing jitter ("layout");
  4. distortion  the photo-ladder parameters ("distortion"); JPEG; image_sha256 appended to the truth.
Same seed + same glyph cache -> byte-identical images and truth JSONs.

Value policy: no parentheses and no characters outside digits, letters, ". : ," and space. Vitals come from
app.config.vital_ranges() at generation time (out-of-range variants lie strictly outside them, inside
plausible bedside extremes), decimals carry one place, times are HH:MM, initials and routes 2-3 capitals,
pain 0-10, words from the fixed lists below. Ambiguous-heavy fixtures draw digits and capitals from the
confusion pairs and i/l/s-rich words. A blank is a box left empty on paper: truth null, tag "blank".

Compositing at 200 DPI on tools/form_fixtures.render_blank: glyph autocrop -> ink weight -> scale to the
box-derived x-height (+-6% per character) -> baseline jitter +-2 px -> positive letter gaps, word gaps >= 3x
the mean letter gap -> one pen color per fixture through the glyph's gray mask. A value too long for its box
is shrunk whole, never truncated. Hard checks (GenerationError, nothing shipped): one paste per non-space
character, every paste inside its box, minimum ink per glyph at the final 850x1100 resolution, the gap ratio.
Then reduce to 850x1100 and apply the distortion level (LEVELS).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import string
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import vital_ranges
from tools import form_fixtures as ff
from tools import hw_glyphs

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evals" / "fixtures"
SCHEMA = 1
SOURCE = "composited from public handwriting samples (NIST Special Database 19)"
FORM_TYPES = tuple(ff.FORMS)
ALLOWED_VALUE = re.compile(r"[0-9A-Za-z.:,]+(?: [0-9A-Za-z.:,]+)*")  # fullmatch; single spaces, never ( )

# field -> value kind, per form type (the printed layout's fields, app/ai/forms.py MOCK_READS keys)
FIELD_KINDS: dict[str, dict[str, str]] = {
    "vitals_flowsheet": {"bed": "bed", "time": "time", "initials": "initials", "temp_c": "vital",
                         "heart_rate": "vital", "resp_rate": "vital", "bp_systolic": "vital",
                         "bp_diastolic": "vital", "spo2": "vital", "pain_score": "pain"},
    "downtime_mar": {"bed": "bed", "time_given": "time", "medication": "medication", "dose": "dose",
                     "route": "route", "initials": "initials", "second_verifier": "initials"},
    "admission_note": {"bed": "bed", "height_cm": "height", "weight_kg": "weight", "allergies": "allergies",
                       "history": "history"},
}
NEVER_BLANK = frozenset({"bed"})  # the bed identifies the patient on every form

BED_RANGE = (1, 30)
HEIGHT_RANGE = (150, 195)        # cm
WEIGHT_RANGE = (45.0, 120.0)     # kg, one decimal
PAIN_RANGE = (0, 10)
DECIMAL_FIELDS = frozenset({"temp_c", "weight_kg"})
# Out-of-range vitals: strictly outside vital_ranges() (inclusive normal bounds, app/config.py), inside these.
VITAL_EXTREMES: dict[str, tuple[float, float]] = {
    "temp_c": (34.0, 41.0), "heart_rate": (30, 180), "resp_rate": (5, 40), "bp_systolic": (60, 220),
    "bp_diastolic": (30, 130), "spo2": (70, 100)}

CONFUSION_PAIRS = (("0", "O"), ("1", "7"), ("5", "S"), ("4", "9"), ("2", "Z"), ("8", "B"))
CONFUSION_DIGITS = frozenset(c for pair in CONFUSION_PAIRS for c in pair if c.isdigit())
CONFUSION_LETTERS = "".join(sorted(c for pair in CONFUSION_PAIRS for c in pair if c.isalpha()))
# kind -> (nominal list, ambiguous-heavy list)
WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "medication": (("heparin", "insulin", "warfarin", "morphine", "metoprolol", "furosemide", "vancomycin",
                    "ondansetron", "enoxaparin", "acetaminophen"),
                   ("lisinopril", "simvastatin", "allopurinol", "losartan", "isosorbide", "insulin")),
    "dose": (("5000 units", "4 mg", "10 units", "25 mg", "1 g", "40 mg", "0.5 mg", "2 mg", "650 mg", "100 mg"),
             ("10 mg", "1.5 mg", "50 mg", "20 units", "0.1 mg", "15 units")),
    "route": (("PO", "IV", "SC", "IM", "SL", "PR"), ("SL", "SC", "IO")),
    "allergies": (("penicillin hives", "sulfa rash", "latex", "codeine nausea", "no known allergies", "shellfish",
                   "morphine itching"),
                  ("penicillin hives", "sulfa rash", "lisinopril cough", "aspirin hives", "iodine rash")),
    "history": (("type 2 diabetes, hypertension", "asthma", "COPD, atrial fibrillation", "hypertension",
                 "heart failure", "chronic kidney disease", "hip fracture, osteoporosis"),
                ("lupus, sinusitis", "cellulitis, sepsis", "ileus, colitis", "sickle cell disease",
                 "lymphoma, sepsis")),
}

# Default plan per form type: (kind, distortion, blank fields). out_of_range needs vitals, so the forms without
# them write those three as ambiguous-heavy with one extra blank. 6/3/3, 4 fixtures with blanks, 4xL0/L1/L2.
DEFAULT_PLAN: tuple[tuple[str, str, int], ...] = (
    ("nominal", "L0", 0), ("nominal", "L1", 1), ("nominal", "L2", 0), ("nominal", "L0", 0),
    ("nominal", "L1", 0), ("nominal", "L2", 1), ("ambiguous", "L0", 1), ("ambiguous", "L1", 0),
    ("ambiguous", "L2", 0), ("out_of_range", "L0", 0), ("out_of_range", "L1", 0), ("out_of_range", "L2", 1),
)

# ---- compositing (pixel values at COMPOSE_DPI) ---------------------------------------------------------
COMPOSE_DPI = 200
REDUCE = COMPOSE_DPI // ff.DPI   # composed at 1700x2200, reduced (2x2 box average) to 850x1100
X_HEIGHT_FRAC = 0.17             # x-height = this fraction of the box height ...
X_HEIGHT_MAX_IN = 0.17           # ... capped: a taller box does not make the writing larger
SIZE_JITTER = 0.06               # per-character height jitter, +-6%
BASELINE_JITTER_PX = 2.0         # per-character baseline jitter
LETTER_GAP = (0.08, 0.20)        # gap between letters of a word, in x-heights ...
MIN_LETTER_GAP_PX = 1.6          # ... never below this after fitting, so integer gaps stay > 0
WORD_GAP = (0.80, 1.05)          # gap between words, in x-heights
MIN_WORD_GAP_RATIO = 3.0         # checked: every word gap >= 3x the value's mean letter gap
LABEL_ZONE_IN = 0.27             # writing starts below the printed label
PAD_IN = 0.08                    # left/right padding inside the box
BOTTOM_PAD_IN = 0.06
START_JITTER_IN = 0.12           # how far right of the padding a value may start
VERTICAL_SLACK_IN = 0.30         # how far down a tall box the line may sit
MIN_INK_PX = 12                  # ink pixels per letter or digit at the final 850x1100 resolution
MIN_PUNCT_INK_PX = 4             # ink pixels per . : , at the final resolution
INK_FINAL_LEVEL = 128            # a final pixel is ink at >= 50% coverage
PEN_COLORS = ((28, 48, 125), (22, 36, 98), (40, 62, 150), (34, 34, 40), (20, 24, 44))  # blue / black pens
INK_WEIGHTS = ("light", "regular", "heavy")  # MaxFilter(3) / none / MinFilter(3) on the white-ground raster
LIGHT_KEEP = 0.6                 # "light" is skipped for a glyph that would keep < 60% of its ink
X_LOWER = frozenset("acemnorsuvwxz")
ASCENDERS = frozenset("bdfhkl")
DESCENDERS = frozenset("gpqy")


@dataclass(frozen=True)
class Level:
    """One rung of the distortion ladder, applied to the 850x1100 page in this order: perspective (corner
    displacement <= `perspective` of the page dims) -> lighting gradient (darkest = 1 - amplitude, amplitude
    <= `lighting`, via ImageChops.multiply) -> rotation within +-`max_angle` deg -> sensor noise -> Gaussian
    blur in `blur` -> JPEG at `quality` (rotation onward is tools/form_fixtures.photo_effect)."""
    perspective: float
    lighting: float
    max_angle: float
    blur: tuple[float, float]
    quality: int


LEVELS: dict[str, Level] = {
    "L0": Level(0.0, 0.0, 1.6, (0.6, 0.6), 70),       # exactly the synthetic set's photo effect (calibration)
    "L1": Level(0.015, 0.18, 1.6, (0.6, 0.6), 60),
    "L2": Level(0.025, 0.30, 3.0, (0.6, 1.0), 50),
}
PAGE_EDGE = (196, 192, 184)  # what shows past the paper's edge (photo_effect's rotate fill)


class Refused(RuntimeError):
    """The inputs cannot produce an honest fixture set; nothing is rendered."""


class GenerationError(RuntimeError):
    """A generation-time hard check failed; the fixture's image is not written."""


@dataclass(frozen=True)
class Spec:
    stem: str
    form_type: str
    kind: str          # nominal | ambiguous | out_of_range
    distortion: str    # L0 | L1 | L2
    blanks: int


@dataclass(frozen=True)
class Placed:
    char: str
    box: tuple[int, int, int, int]  # paste box at COMPOSE_DPI
    ink: int                        # ink pixels at the final resolution


# ------------------------------------------------------------------ plan and values

def plan(form_type: str, count: int = len(DEFAULT_PLAN)) -> list[Spec]:
    """The fixtures for one form type: DEFAULT_PLAN for the default 12, an evenly spread subset for fewer,
    the plan repeated for more."""
    if count < 1:
        raise Refused("--count-per-form must be at least 1")
    n, has_vitals = len(DEFAULT_PLAN), "vital" in FIELD_KINDS[form_type].values()
    out = []
    for k in range(count):
        kind, level, blanks = DEFAULT_PLAN[int((k + 0.5) * n / count) if count <= n else k % n]
        if kind == "out_of_range" and not has_vitals:
            kind, blanks = "ambiguous", blanks + 1
        out.append(Spec(f"hw-{form_type}-{k + 1:03d}", form_type, kind, level, blanks))
    return out


def _confusable(values: list[str]) -> list[str]:
    """The values with the fewest digits outside the confusion pairs."""
    def outside(v: str) -> int:
        return sum(1 for c in v if c.isdigit() and c not in CONFUSION_DIGITS)
    best = min(map(outside, values))
    return [v for v in values if outside(v) == best]


def _pick(rng: random.Random, values: list[str], mode: str) -> str:
    return rng.choice(_confusable(values) if mode == "ambiguous" else values)


def _scale(field: str) -> int:
    return 10 if field in DECIMAL_FIELDS else 1


def _fmt(field: str, units: int) -> str:
    return f"{units / 10:.1f}" if field in DECIMAL_FIELDS else str(units)


def _span(field: str, lo: float, hi: float) -> list[str]:
    """Every value of `field` in [lo, hi] at its precision (tenths for decimals)."""
    k = _scale(field)
    return [_fmt(field, u) for u in range(math.ceil(lo * k - 1e-9), math.floor(hi * k + 1e-9) + 1)]


def out_of_range_value(field: str, rng: random.Random) -> str:
    """A plausible value strictly outside vital_ranges()[field], read now (a VITAL_RANGE_* override counts)."""
    lo, hi = vital_ranges()[field]
    ext_lo, ext_hi = VITAL_EXTREMES[field]
    k = _scale(field)
    below = [_fmt(field, u) for u in range(math.ceil(ext_lo * k - 1e-9), math.ceil(lo * k - 1e-9))]
    above = [_fmt(field, u) for u in range(math.floor(hi * k + 1e-9) + 1, math.floor(ext_hi * k + 1e-9) + 1)]
    sides = [s for s in (below, above) if s]
    if not sides:
        raise Refused(f"no plausible out-of-range {field} outside {lo}-{hi} within {ext_lo}-{ext_hi}")
    return rng.choice(rng.choice(sides))


def field_value(form_type: str, field: str, mode: str, rng: random.Random) -> str:
    kind = FIELD_KINDS[form_type][field]
    if kind == "vital":
        return _pick(rng, _span(field, *vital_ranges()[field]), mode)
    if kind == "bed":
        return _pick(rng, _span(field, *BED_RANGE), mode)
    if kind == "pain":
        return _pick(rng, _span(field, *PAIN_RANGE), mode)
    if kind == "height":
        return _pick(rng, _span(field, *HEIGHT_RANGE), mode)
    if kind == "weight":
        return _pick(rng, _span(field, *WEIGHT_RANGE), mode)
    if kind == "time":
        return _pick(rng, [f"{m // 60:02d}:{m % 60:02d}" for m in range(24 * 60)], mode)
    if kind == "initials":
        letters = CONFUSION_LETTERS if mode == "ambiguous" else string.ascii_uppercase
        return "".join(rng.choice(letters) for _ in range(rng.randint(2, 3)))
    nominal, ambiguous = WORDS[kind]
    return rng.choice(ambiguous if mode == "ambiguous" else nominal)


def choose_values(spec: Spec, seed: int) -> dict[str, str | None]:
    """Step 1: every field's value for this fixture (None = left blank on paper), in print order."""
    rng = random.Random(f"{seed}:{spec.stem}:values")
    fields = list(ff.layout(spec.form_type))
    if set(fields) != set(FIELD_KINDS[spec.form_type]):
        raise Refused(f"{spec.form_type}: FIELD_KINDS does not match the printed layout")
    oor: list[str] = []
    if spec.kind == "out_of_range":
        vitals = [f for f in fields if FIELD_KINDS[spec.form_type][f] == "vital"]
        oor = sorted(rng.sample(vitals, rng.randint(1, 2)))
    blankable = [f for f in fields if f not in NEVER_BLANK and f not in oor]
    blanks = set(rng.sample(blankable, spec.blanks))
    mode = "ambiguous" if spec.kind == "ambiguous" else "nominal"
    values: dict[str, str | None] = {}
    for f in fields:
        if f in blanks:
            values[f] = None
        elif f in oor:
            values[f] = out_of_range_value(f, rng)
        else:
            values[f] = field_value(spec.form_type, f, mode, rng)
        if values[f] is not None and not ALLOWED_VALUE.fullmatch(values[f]):
            raise Refused(f"{spec.stem}.{f}: {values[f]!r} breaks the value policy")
    return values


def needed_chars(values: dict[str, str | None]) -> set[str]:
    return {c for v in values.values() if v for c in v if c != " "}


def assign_writers(specs: list[Spec], values: dict[str, dict], source, seed: int) -> dict[str, str]:
    """Step 2: one writer per fixture, no writer twice in the set. Refuses loudly when a fixture's characters
    are not all covered by any writer, or when the qualifying writers run out."""
    pool = source.writers()
    if len(pool) < len(specs):
        raise Refused(f"{len(specs)} fixtures need {len(specs)} distinct writers; the glyph source has {len(pool)}")
    coverage = {w: source.coverage(w) for w in pool}
    qualifying: dict[str, list[str]] = {}
    for s in specs:
        need = needed_chars(values[s.stem])
        qualifying[s.stem] = [w for w in pool if need <= coverage[w]]
        if not qualifying[s.stem]:
            missing = need - set().union(*coverage.values())
            why = f"no writer has {''.join(sorted(missing))!r}" if missing else "no single writer covers all of them"
            raise Refused(f"{s.stem}: no writer can write {''.join(sorted(need))!r} ({why})")
    used: set[str] = set()
    out: dict[str, str] = {}
    for s in sorted(specs, key=lambda s: (len(qualifying[s.stem]), s.stem)):
        free = [w for w in qualifying[s.stem] if w not in used]
        if not free:
            raise Refused(f"{s.stem}: all {len(qualifying[s.stem])} writers covering its characters are already "
                          "used by other fixtures (one writer per fixture, never reused)")
        out[s.stem] = random.Random(f"{seed}:{s.stem}:writer").choice(free)
        used.add(out[s.stem])
    return out


def truth_record(spec: Spec, values: dict[str, str | None], seed: int, writer: str) -> dict:
    """Truth schema v1, without image_sha256 (appended once the image exists)."""
    tags = [spec.kind] + (["blank"] if any(v is None for v in values.values()) else [])
    return {"schema": SCHEMA, "form_type": spec.form_type, "fields": dict(values), "image": f"{spec.stem}.jpg",
            "seed": seed, "writer": writer, "distortion": spec.distortion, "tags": tags, "source": SOURCE}


# ------------------------------------------------------------------ compositing

def char_metrics(c: str) -> tuple[float, float]:
    """(bottom, top) of a character relative to the baseline, in x-heights."""
    if c == ".":
        return 0.0, 0.28
    if c == ",":
        return -0.35, 0.30
    if c == ":":
        return 0.0, 0.95
    if c == "i":
        return 0.0, 1.30
    if c == "j":
        return -0.45, 1.30
    if c == "t":
        return 0.0, 1.25
    if c in X_LOWER:
        return 0.0, 1.0
    if c in DESCENDERS:
        return -0.45, 1.0
    if c in ASCENDERS:
        return 0.0, 1.45
    return 0.0, 1.40  # digits and capitals


def _weigh(crop, weight: str):
    """Ink weight on the white-ground source raster: heavy = MinFilter(3) (ink spreads), light = MaxFilter(3)
    (ink thins) unless that would keep less than LIGHT_KEEP of the glyph's ink."""
    from PIL import ImageFilter, ImageOps

    if weight == "regular":
        return crop
    padded = ImageOps.expand(crop, border=1, fill=255)
    if weight == "heavy":
        return hw_glyphs.autocrop(padded.filter(ImageFilter.MinFilter(3)))
    thin = padded.filter(ImageFilter.MaxFilter(3))
    before = hw_glyphs.ink_pixels(hw_glyphs.ink_mask(padded))
    if hw_glyphs.ink_pixels(hw_glyphs.ink_mask(thin)) < LIGHT_KEEP * before:
        return crop
    return hw_glyphs.autocrop(thin)


def final_ink(mask, x: int, y: int) -> int:
    """Ink pixels a glyph mask pasted at (x, y) has after the page is reduced to the final resolution: the mask
    on a canvas aligned to the reduction grid, reduced exactly as the page is (Image.reduce), >= 50% coverage."""
    from PIL import Image

    f = REDUCE
    ox, oy = x - x % f, y - y % f
    w, h = -(-(x + mask.width - ox) // f) * f, -(-(y + mask.height - oy) // f) * f
    canvas = Image.new("L", (w, h), 0)
    canvas.paste(mask, (x - ox, y - oy))
    return sum(1 for v in canvas.reduce(f).tobytes() if v >= INK_FINAL_LEVEL)


def write_value(page, box, value: str, writer: str, source, grng: random.Random, lrng: random.Random,
                pen: tuple[int, int, int], weight: str) -> list[Placed]:
    """Composite `value` in `writer`'s hand into `box` (left, top, right, bottom at COMPOSE_DPI) on `page`."""
    from PIL import Image, ImageOps

    if not ALLOWED_VALUE.fullmatch(value):
        raise GenerationError(f"value {value!r} breaks the value policy")
    left, top, right, bottom = box
    dpi = COMPOSE_DPI
    x_h = min(X_HEIGHT_FRAC * (bottom - top), X_HEIGHT_MAX_IN * dpi)
    inner_l, inner_r = left + PAD_IN * dpi, right - PAD_IN * dpi
    zone_t, zone_b = top + LABEL_ZONE_IN * dpi, bottom - BOTTOM_PAD_IN * dpi

    # the natural (unshrunk) layout in pixels; every random draw happens here, in a fixed order
    glyphs: list[tuple[str, object, float, float, float, float]] = []  # char, crop, h, w, bottom, dy
    gaps: list[tuple[float, bool]] = []                                # after glyph i: (size, is word gap)
    words = value.split(" ")
    for wi, word in enumerate(words):
        for ci, ch in enumerate(word):
            try:
                crop = _weigh(hw_glyphs.autocrop(source.get(writer, ch, grng)), weight)
            except ValueError as e:
                raise GenerationError(f"{value!r}: {ch!r} from writer {writer}: {e}") from e
            lo, hi = char_metrics(ch)
            jit = lrng.uniform(1 - SIZE_JITTER, 1 + SIZE_JITTER)
            h = (hi - lo) * x_h * jit
            glyphs.append((ch, crop, h, h * crop.width / crop.height, lo * x_h * jit,
                           lrng.uniform(-BASELINE_JITTER_PX, BASELINE_JITTER_PX)))
            if ci < len(word) - 1:
                gaps.append((lrng.uniform(*LETTER_GAP) * x_h, False))
        if wi < len(words) - 1:
            gaps.append((lrng.uniform(*WORD_GAP) * x_h, True))
    u, v = lrng.random(), lrng.random()

    ascent = max(bot + h for _c, _g, h, _w, bot, _dy in glyphs) + BASELINE_JITTER_PX
    descent = max(0.0, max(-bot for _c, _g, _h, _w, bot, _dy in glyphs)) + BASELINE_JITTER_PX
    avail_w, avail_h = inner_r - inner_l, zone_b - zone_t
    widths = [w for _c, _g, _h, w, _b, _dy in glyphs]
    fit = min(1.0, avail_h / (ascent + descent), avail_w / (sum(widths) + sum(g for g, _word in gaps)))
    for _ in range(20):  # compose-then-fit: shrink the whole value until it fits; never truncate
        scaled_gaps = [max(MIN_LETTER_GAP_PX, g * fit) for g, _word in gaps]
        total = sum(widths) * fit + sum(scaled_gaps)
        if total <= avail_w:
            break
        fit *= avail_w / total
    else:
        raise GenerationError(f"{value!r} does not fit its box even shrunk")
    start = inner_l + u * min(START_JITTER_IN * dpi, avail_w - total)
    baseline = zone_t + ascent * fit + v * min(VERTICAL_SLACK_IN * dpi, max(0.0, avail_h - (ascent + descent) * fit))

    placed: list[Placed] = []
    x = start
    for i, (ch, crop, h, w, bot, dy) in enumerate(glyphs):
        gw, gh = max(1, round(w * fit)), max(1, round(h * fit))
        mask = ImageOps.invert(crop.resize((gw, gh), Image.Resampling.LANCZOS))
        x0, y1 = round(x), round(baseline - bot * fit + dy)
        page.paste(Image.new("RGB", (gw, gh), pen), (x0, y1 - gh), mask)
        placed.append(Placed(ch, (x0, y1 - gh, x0 + gw, y1), final_ink(mask, x0, y1 - gh)))
        if i < len(gaps):
            x += w * fit + scaled_gaps[i]
    check_placement(value, box, placed)
    return placed


def check_placement(value: str, box, placed: list[Placed]) -> None:
    """The hard checks: a paste per non-space character, all inside the box, visible ink at the final
    resolution, strictly positive letter gaps and word gaps >= MIN_WORD_GAP_RATIO x the mean letter gap."""
    if len(placed) != len(value) - value.count(" "):
        raise GenerationError(f"{value!r}: {len(placed)} glyphs pasted for {len(value) - value.count(' ')} characters")
    left, top, right, bottom = box
    border = round(2 * COMPOSE_DPI / ff.DPI)
    for p in placed:
        x0, y0, x1, y1 = p.box
        if not (left + border <= x0 and x1 <= right - border and top + border <= y0 and y1 <= bottom - border):
            raise GenerationError(f"{value!r}: {p.char!r} pasted at {p.box} outside its box {box}")
        floor = MIN_PUNCT_INK_PX if p.char in hw_glyphs.PUNCTUATION else MIN_INK_PX
        if p.ink < floor:
            raise GenerationError(f"{value!r}: {p.char!r} has {p.ink} ink pixels at 850x1100 (minimum {floor})")
    word_break: list[bool] = []  # for each consecutive pair of pasted glyphs: is there a space between them?
    for word in value.split(" "):
        word_break += [False] * (len(word) - 1) + [True]
    word_break = word_break[:-1]
    letter, word = [], []
    for i, is_word in enumerate(word_break):
        (word if is_word else letter).append(placed[i + 1].box[0] - placed[i].box[2])
    if any(g <= 0 for g in letter):
        raise GenerationError(f"{value!r}: letter gaps {letter} must all be > 0")
    if word and letter and min(word) < MIN_WORD_GAP_RATIO * (sum(letter) / len(letter)):
        raise GenerationError(f"{value!r}: word gap {min(word)} px < {MIN_WORD_GAP_RATIO}x mean letter gap "
                              f"{sum(letter) / len(letter):.2f} px")


# ------------------------------------------------------------------ distortion ladder

def perspective_corners(W: int, H: int, frac: float, rng: random.Random) -> list[tuple[float, float]]:
    """Where the page corners land in the photo: each moved by at most frac*W across and frac*H down."""
    return [(x + rng.uniform(-frac, frac) * W, y + rng.uniform(-frac, frac) * H)
            for x, y in ((0, 0), (W, 0), (W, H), (0, H))]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (pure python, no numpy)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("singular perspective system")
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for k in range(col, n + 1):
                    m[r][k] -= f * m[col][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / m[r][r]
    return x


def perspective_coefficients(dst, src) -> tuple[float, ...]:
    """The 8 Image.Transform.PERSPECTIVE coefficients (a..h) sending each output point dst[i] to input point
    src[i]: x = (aX + bY + c) / (gX + hY + 1), y = (dX + eY + f) / (gX + hY + 1); an 8x8 linear solve."""
    rows, rhs = [], []
    for (X, Y), (x, y) in zip(dst, src):
        rows.append([X, Y, 1.0, 0.0, 0.0, 0.0, -X * x, -Y * x])
        rhs.append(x)
        rows.append([0.0, 0.0, 0.0, X, Y, 1.0, -X * y, -Y * y])
        rhs.append(y)
    return tuple(_solve(rows, rhs))


def lighting_gradient(size: tuple[int, int], amplitude: float, angle_deg: float):
    """RGB multiplier: 255 at one side of the page falling linearly to 255 * (1 - amplitude) at the other,
    along `angle_deg`. Computed on a coarse grid and bilinear-upsampled (the field is linear, so smooth)."""
    from PIL import Image

    W, H = size
    gw, gh = 34, 44
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    proj = [x * c + y * s for x, y in ((0, 0), (W, 0), (W, H), (0, H))]
    lo, span = min(proj), (max(proj) - min(proj)) or 1.0
    data = bytes(round(255 * (1 - amplitude * (((i + 0.5) * W / gw * c + (j + 0.5) * H / gh * s) - lo) / span))
                 for j in range(gh) for i in range(gw))
    return Image.frombytes("L", (gw, gh), data).resize((W, H), Image.Resampling.BILINEAR).convert("RGB")


def draw_distortion(level: str, size: tuple[int, int], rng: random.Random) -> dict:
    """The L1/L2 parameters drawn before photo_effect (which then draws its rotation and noise)."""
    lv = LEVELS[level]
    return {"corners": perspective_corners(size[0], size[1], lv.perspective, rng),
            "lighting": rng.uniform(lv.lighting / 2, lv.lighting),
            "light_angle": rng.uniform(0.0, 360.0),
            "blur": rng.uniform(*lv.blur)}


def distort(page, level: str, rng: random.Random) -> bytes:
    """The 850x1100 page through distortion level `level`, JPEG-encoded. L0 is photo_effect unchanged."""
    from PIL import Image, ImageChops

    if level == "L0":
        return ff.photo_effect(page, rng)
    lv, (W, H) = LEVELS[level], page.size
    p = draw_distortion(level, (W, H), rng)
    coeffs = perspective_coefficients(p["corners"], [(0, 0), (W, 0), (W, H), (0, H)])
    page = page.transform((W, H), Image.Transform.PERSPECTIVE, coeffs, resample=Image.Resampling.BICUBIC,
                          fillcolor=PAGE_EDGE)
    page = ImageChops.multiply(page, lighting_gradient((W, H), p["lighting"], p["light_angle"]))
    return ff.photo_effect(page, rng, max_angle=lv.max_angle, blur=p["blur"], quality=lv.quality)


# ------------------------------------------------------------------ fixtures

def render(spec: Spec, values: dict[str, str | None], writer: str, source, seed: int) -> bytes:
    """Steps 3 and 4 for one fixture: composite every non-blank value, reduce to 850x1100, distort, encode."""
    page, boxes = ff.render_blank(spec.form_type, COMPOSE_DPI)
    grng = random.Random(f"{seed}:{spec.stem}:glyphs")
    lrng = random.Random(f"{seed}:{spec.stem}:layout")
    pen, weight = lrng.choice(PEN_COLORS), lrng.choice(INK_WEIGHTS)
    for name, box in boxes.items():
        if values[name] is not None:
            write_value(page, box, values[name], writer, source, grng, lrng, pen, weight)
    final = page.reduce(REDUCE)
    if final.size != (int(ff.PAGE_W_IN * ff.DPI), int(ff.PAGE_H_IN * ff.DPI)):
        raise GenerationError(f"final page is {final.size}, not 850x1100")
    return distort(final, spec.distortion, random.Random(f"{seed}:{spec.stem}:distortion"))


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def generate(out: Path, seed: int, source, count_per_form: int = len(DEFAULT_PLAN),
             only: set[str] | None = None) -> list[Path]:
    """Plan, values and writers for the whole set; then, for the selected stems, truth JSONs first and the
    images after. Returns the paths written."""
    specs = [s for ft in FORM_TYPES for s in plan(ft, count_per_form)]
    values = {s.stem: choose_values(s, seed) for s in specs}
    writers = assign_writers(specs, values, source, seed)
    chosen = [s for s in specs if only is None or s.stem in only]
    if only is not None and len(chosen) != len(only):
        raise Refused(f"unknown stem(s): {', '.join(sorted(only - {s.stem for s in chosen}))}")
    out.mkdir(parents=True, exist_ok=True)
    truths = {s.stem: truth_record(s, values[s.stem], seed, writers[s.stem]) for s in chosen}
    for stem, truth in truths.items():  # on disk before any glyph is picked
        _write_json(out / f"{stem}.json", truth)
    written: list[Path] = []
    for s in chosen:
        image = render(s, values[s.stem], writers[s.stem], source, seed)
        (out / f"{s.stem}.jpg").write_bytes(image)
        truths[s.stem]["image_sha256"] = hashlib.sha256(image).hexdigest()
        _write_json(out / f"{s.stem}.json", truths[s.stem])
        written += [out / f"{s.stem}.jpg", out / f"{s.stem}.json"]
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--glyph-root", type=Path, default=hw_glyphs.DEFAULT_CACHE)
    ap.add_argument("--only", metavar="STEM", help="render one fixture (the plan and writers stay the full set's)")
    ap.add_argument("--count-per-form", type=int, default=len(DEFAULT_PLAN))
    args = ap.parse_args(argv)
    try:
        source = hw_glyphs.SD19Glyphs(args.glyph_root)
        paths = generate(args.out, args.seed, source, args.count_per_form, {args.only} if args.only else None)
    except (hw_glyphs.Unverified, Refused, GenerationError) as e:
        print(f"hw_fixtures: REFUSED: {e}", file=sys.stderr)
        return 2
    for p in paths:
        if p.suffix == ".json":
            t = json.loads(p.read_text())
            print(f"{p.stem}  writer {t['writer']}  {t['distortion']}  {','.join(t['tags'])}  {t['image_sha256'][:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
