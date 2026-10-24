"""Printable downtime form templates, their layout maps, and seeded fixture photos.

One spec (FORMS below) is the single source of truth. From it this script writes, under data/forms/:
  <form_type>.html          browser-printable US Letter template (print CSS, absolutely placed boxes)
  <form_type>.layout.json   field name -> normalized box {x, y, w, h} on the printed page
  fixtures/<form_type>-01.jpg   the template "filled in by hand", slightly rotated, with sensor noise
  fixtures/<form_type>-01.json  ground truth for that fixture (null = deliberately unreadable)

The HTML places every box with the same percentages as the layout map, so the map matches the print
by construction. Images are deterministic for a given Pillow + font (fixed seed, no wall clock).

  .venv/bin/python tools/form_fixtures.py            # regenerate everything in data/forms/
  .venv/bin/python tools/form_fixtures.py --out DIR  # write elsewhere (tests)

Synthetic data only. Owner deferral: real phone photos of printed forms come later (docs/decisions.md).
"""
from __future__ import annotations

import argparse
import html
import io
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "forms"
SEED = 20261023

PAGE_W_IN, PAGE_H_IN = 8.5, 11.0
MARGIN_IN = 0.5
TOP_IN = 1.9          # first field row starts below the header block
GAP_IN = 0.15
DPI = 100             # fixture photo resolution (850 x 1100 px)

# form_type -> (title, [(row height in inches, [(field, printed label, width fraction)])], ground truth)
# Field names are the CapturedRecord field schema per form type (app/ai/forms.py MOCK_READS keys).
FORMS: dict[str, dict] = {
    "vitals_flowsheet": {
        "title": "Downtime Vital Signs Flowsheet",
        "rows": [
            (1.0, [("bed", "Bed", 0.25), ("time", "Time (24 h)", 0.35), ("initials", "RN initials", 0.40)]),
            (1.0, [("temp_c", "Temperature (°C)", 1 / 3), ("heart_rate", "Heart rate (/min)", 1 / 3),
                   ("resp_rate", "Resp. rate (/min)", 1 / 3)]),
            (1.0, [("bp_systolic", "BP systolic (mmHg)", 0.5), ("bp_diastolic", "BP diastolic (mmHg)", 0.5)]),
            (1.0, [("spo2", "SpO2 (%)", 0.5), ("pain_score", "Pain score (0-10)", 0.5)]),
        ],
        # pain_score is written then scribbled out: ground truth is unreadable (null), never a guess.
        "truth": {"bed": "12", "time": "02:55", "temp_c": "37.8", "heart_rate": "104", "resp_rate": "22",
                  "bp_systolic": "148", "bp_diastolic": "86", "spo2": "89", "pain_score": None, "initials": "PN"},
        "scribbled": {"pain_score": "4"},
    },
    "downtime_mar": {
        "title": "Downtime Medication Administration Record",
        "rows": [
            (1.0, [("bed", "Bed", 0.3), ("time_given", "Time given (24 h)", 0.7)]),
            (1.0, [("medication", "Medication", 1.0)]),
            (1.0, [("dose", "Dose + unit", 0.5), ("route", "Route", 0.5)]),
            (1.0, [("initials", "Given by (initials)", 0.5),
                   ("second_verifier", "Second verifier (high-alert meds)", 0.5)]),
        ],
        # heparin is high-alert: the blank second_verifier is what the two-person verify catches.
        "truth": {"bed": "12", "medication": "heparin", "dose": "5000 units", "route": "SC", "time_given": "02:50",
                  "initials": "PN", "second_verifier": None},
        "scribbled": {},
    },
    "admission_note": {
        "title": "Downtime Admission Note",
        "rows": [
            (1.0, [("bed", "Bed", 0.3), ("height_cm", "Height (cm)", 0.35), ("weight_kg", "Weight (kg)", 0.35)]),
            (1.2, [("allergies", "Allergies (substance + reaction)", 1.0)]),
            (2.0, [("history", "Past medical history", 1.0)]),
        ],
        "truth": {"bed": "12", "allergies": "penicillin (hives)", "height_cm": "178", "weight_kg": "92.4",
                  "history": "type 2 diabetes, hypertension"},
        "scribbled": {},
    },
}

FOOTER = ("Write one value per box in pen. Cross out errors with a single line; do not erase. "
          "Keep this original for backload. Synthetic demo form, not for clinical use.")
HANDWRITING_FONTS = (  # first one present wins; else the bundled font, sheared into a distinct italic
    "/System/Library/Fonts/Noteworthy.ttc",
    "/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf",
    "/System/Library/Fonts/MarkerFelt.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
)


def _r(v: float) -> float:
    return round(v, 4)


def layout(form_type: str) -> dict[str, dict[str, float]]:
    """Field -> normalized box {x, y, w, h} (fractions of the US Letter page, origin top-left)."""
    boxes: dict[str, dict[str, float]] = {}
    y = TOP_IN
    content_w = PAGE_W_IN - 2 * MARGIN_IN
    for height, cells in FORMS[form_type]["rows"]:
        x = MARGIN_IN
        usable = content_w - GAP_IN * (len(cells) - 1)
        for name, _label, frac in cells:
            w = usable * frac
            boxes[name] = {"x": _r(x / PAGE_W_IN), "y": _r(y / PAGE_H_IN), "w": _r(w / PAGE_W_IN),
                           "h": _r(height / PAGE_H_IN)}
            x += w + GAP_IN
        y += height + GAP_IN
    return boxes


def labels(form_type: str) -> dict[str, str]:
    return {name: label for _h, cells in FORMS[form_type]["rows"] for name, label, _f in cells}


# ------------------------------------------------------------------ HTML template

CSS = """
@page { size: letter; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; background: #e8e8e8; font-family: Helvetica, Arial, sans-serif; color: #111; }
.page { position: relative; width: 8.5in; height: 11in; margin: 0.25in auto; background: #fff;
        box-shadow: 0 1px 6px rgba(0,0,0,.25); }
.header { position: absolute; left: 5.8824%; top: 4.5455%; width: 88.2353%; height: 11.3636%;
          border-bottom: 2px solid #111; }
.org { font-size: 10pt; letter-spacing: .04em; text-transform: uppercase; }
.title { font-size: 20pt; font-weight: 700; margin: 4pt 0 2pt; }
.tag { display: inline-block; font-size: 9pt; font-weight: 700; border: 1.5px solid #111; padding: 1pt 6pt; }
.meta { font-size: 9pt; margin-top: 4pt; }
.box { position: absolute; border: 1.5px solid #111; }
.box .label { position: absolute; left: 6pt; top: 4pt; font-size: 8.5pt; font-weight: 700;
              text-transform: uppercase; letter-spacing: .03em; }
.footer { position: absolute; left: 5.8824%; bottom: 4.5455%; width: 88.2353%; font-size: 8.5pt;
          border-top: 1px solid #111; padding-top: 4pt; }
@media print { html, body { background: #fff; } .page { margin: 0; box-shadow: none; } }
"""


def render_html(form_type: str) -> str:
    spec, boxes, names = FORMS[form_type], layout(form_type), labels(form_type)
    cells = "\n".join(
        f'    <div class="box" data-field="{n}" style="left:{b["x"] * 100:.2f}%;top:{b["y"] * 100:.2f}%;'
        f'width:{b["w"] * 100:.2f}%;height:{b["h"] * 100:.2f}%"><span class="label">{html.escape(names[n])}</span></div>'
        for n, b in boxes.items())
    return f"""<!doctype html>
<!-- Generated by tools/form_fixtures.py from the same spec as {form_type}.layout.json. Do not hand-edit. -->
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(spec["title"])} (printable)</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="page" data-form-type="{form_type}">
    <div class="header">
      <div class="org">Mercy Ridge Medical Center (synthetic) &middot; Unit 4 West</div>
      <div class="title">{html.escape(spec["title"])}</div>
      <span class="tag">EHR DOWNTIME &middot; PAPER RECORD</span>
      <div class="meta">Form {form_type} &middot; v1 &middot; print on US Letter, 100% scale</div>
    </div>
{cells}
    <div class="footer">{html.escape(FOOTER)}</div>
  </div>
</body>
</html>
"""


# ------------------------------------------------------------------ fixture photo

def _fonts():
    from PIL import ImageFont

    label = ImageFont.load_default(size=13)
    title = ImageFont.load_default(size=26)
    small = ImageFont.load_default(size=11)
    for path in HANDWRITING_FONTS:
        if Path(path).exists():
            return label, title, small, (lambda size, p=path: ImageFont.truetype(p, size)), False
    return label, title, small, (lambda size: ImageFont.load_default(size=size)), True


def _ink_text(page, xy, text, font, shear: bool, ink=(28, 48, 125)):
    """Draw handwriting-like text. Without a handwriting font, shear a bundled-font rendering (italic)."""
    from PIL import Image, ImageDraw

    bbox = font.getbbox(text)
    w, h = bbox[2] + 12, bbox[3] + 12
    mask = Image.new("L", (w + (h // 3 if shear else 0), h), 0)
    ImageDraw.Draw(mask).text((4, 4), text, font=font, fill=255)
    if shear:
        mask = mask.transform(mask.size, Image.Transform.AFFINE, (1, 0.3, -h * 0.3, 0, 1, 0),
                              resample=Image.Resampling.BICUBIC)
    page.paste(Image.new("RGB", mask.size, ink), (int(xy[0]), int(xy[1])), mask)
    return mask.size


def render_fixture(form_type: str, seed: int) -> bytes:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    rng = random.Random(f"{seed}:{form_type}")
    spec, boxes, names = FORMS[form_type], layout(form_type), labels(form_type)
    W, H = int(PAGE_W_IN * DPI), int(PAGE_H_IN * DPI)
    page = Image.new("RGB", (W, H), (250, 248, 241))
    d = ImageDraw.Draw(page)
    label_font, title_font, small_font, hand, shear = _fonts()

    # header, matching the HTML template's header block
    x0, y0 = MARGIN_IN * DPI, MARGIN_IN * DPI
    d.text((x0, y0), "MERCY RIDGE MEDICAL CENTER (SYNTHETIC) · UNIT 4 WEST", font=small_font, fill=(20, 20, 20))
    d.text((x0, y0 + 18), spec["title"], font=title_font, fill=(10, 10, 10))
    d.rectangle((x0, y0 + 54, x0 + 250, y0 + 72), outline=(10, 10, 10), width=2)
    d.text((x0 + 8, y0 + 57), "EHR DOWNTIME · PAPER RECORD", font=small_font, fill=(10, 10, 10))
    d.line((x0, (0.5 + 1.25) * DPI, W - x0, (0.5 + 1.25) * DPI), fill=(10, 10, 10), width=2)

    for name, b in boxes.items():
        left, top, right, bottom = b["x"] * W, b["y"] * H, (b["x"] + b["w"]) * W, (b["y"] + b["h"]) * H
        d.rectangle((left, top, right, bottom), outline=(15, 15, 15), width=2)
        d.text((left + 8, top + 6), names[name].upper(), font=label_font, fill=(15, 15, 15))
        value = spec["truth"].get(name)
        written = value if value is not None else spec["scribbled"].get(name)
        if written is None:
            continue  # left blank on paper
        size = rng.randint(30, 36)
        tx = left + 14 + rng.randint(0, 18)
        ty = top + 24 + rng.randint(0, max(1, int((bottom - top) - 70)))
        tw, th = _ink_text(page, (tx, ty), written, hand(size), shear)
        if value is None:  # scribble over it: deliberately unreadable
            for _ in range(7):
                d.line((tx + rng.randint(-4, 6), ty + rng.randint(4, th), tx + tw + rng.randint(-6, 4),
                        ty + rng.randint(4, th)), fill=(28, 48, 125), width=4)
    d.text((x0, H - MARGIN_IN * DPI - 14), FOOTER, font=small_font, fill=(40, 40, 40))

    # photo-ish: slight rotation, sensor noise, softness (all from the seeded RNG)
    angle = rng.uniform(-1.6, 1.6)
    page = page.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=(196, 192, 184))
    noise = Image.frombytes("L", (W, H), bytes(b % 22 for b in rng.randbytes(W * H))).convert("RGB")
    page = ImageChops.subtract(page, noise).filter(ImageFilter.GaussianBlur(0.6))
    buf = io.BytesIO()
    page.save(buf, "JPEG", quality=70, optimize=True)
    return buf.getvalue()


def ground_truth(form_type: str, image_name: str, seed: int) -> dict:
    return {"form_type": form_type, "image": image_name, "seed": seed, "synthetic": True,
            "fields": dict(FORMS[form_type]["truth"])}


def _dump(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def generate(out: Path = OUT, seed: int = SEED) -> list[Path]:
    (out / "fixtures").mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for ft in FORMS:
        files = {
            out / f"{ft}.html": render_html(ft).encode(),
            out / f"{ft}.layout.json": _dump({"form_type": ft, "version": 1,
                                              "page": {"width_in": PAGE_W_IN, "height_in": PAGE_H_IN},
                                              "fields": layout(ft)}).encode(),
            out / "fixtures" / f"{ft}-01.jpg": render_fixture(ft, seed),
            out / "fixtures" / f"{ft}-01.json": _dump(ground_truth(ft, f"{ft}-01.jpg", seed)).encode(),
        }
        for path, data in files.items():
            path.write_bytes(data)
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)
    for p in generate(args.out, args.seed):
        print(f"{p.relative_to(args.out) if p.is_relative_to(args.out) else p}  {p.stat().st_size} B")
    _l, _t, _s, _h, shear = _fonts()
    print("handwriting:", "bundled font, sheared italic" if shear else "system handwriting font")
    return 0


if __name__ == "__main__":
    sys.exit(main())
