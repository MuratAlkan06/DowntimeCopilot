"""Per-field crops of the source photo, stored as blobs beside the original image.

Box choice per field: Textract's answer box (measured OCR geometry), else the vision model's box, each
only when it lands on the printed field (its centre inside the template box, with slack for a tilted
photo), else the template layout map
(data/forms/<form_type>.layout.json). DRY_RUN reads return no boxes, so dry runs crop from the layout
map through the same code. `CapturedField.crop` is the frozen contract's normalized box; the cropped
image itself is the blob `crop_ref(record_id, field)` (GET /blobs/{ref}).
"""
from __future__ import annotations

import io
import logging

from app.capture.layouts import layout
from contracts import Crop, FormType

log = logging.getLogger(__name__)

PAD = 0.012        # padding around a box, fraction of the page (tilt and handwriting overflow)
SLACK = 0.04       # how far outside the printed box a model box centre may land and still count
MAX_CROP_W = 640   # px; crops are review thumbnails, not archives (the original image is kept)


def crop_ref(record_id: str, field: str) -> str:
    return f"crop:{record_id}:{field}"


def _centre_inside(box: Crop, printed: Crop) -> bool:
    cx, cy = box.left + box.width / 2, box.top + box.height / 2
    return (printed.left - SLACK <= cx <= printed.left + printed.width + SLACK
            and printed.top - SLACK <= cy <= printed.top + printed.height + SLACK)


def choose_boxes(form_type: FormType, model_boxes: dict[str, Crop] | None,
                 textract_boxes: dict[str, Crop] | None = None) -> tuple[dict[str, Crop], dict[str, str]]:
    """-> (field -> box, field -> source "textract" | "model" | "layout")."""
    printed = layout(form_type)
    boxes: dict[str, Crop] = {}
    source: dict[str, str] = {}
    for name in set(printed) | set(model_boxes or {}) | set(textract_boxes or {}):
        t, m, p = (textract_boxes or {}).get(name), (model_boxes or {}).get(name), printed.get(name)
        if t is not None and (p is None or _centre_inside(t, p)):
            boxes[name], source[name] = t, "textract"
        elif m is not None and (p is None or _centre_inside(m, p)):
            boxes[name], source[name] = m, "model"
        elif p is not None:
            boxes[name], source[name] = p, "layout"
    return boxes, source


def render_crops(image: bytes, boxes: dict[str, Crop]) -> dict[str, bytes]:
    """field -> JPEG crop. {} when the upload is not a decodable image (crops are best effort)."""
    try:
        from PIL import Image, ImageOps

        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image)))
        img = img.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any undecodable upload just has no crops
        log.info("no crops: image not decodable (%s)", exc)
        return {}
    W, H = img.size
    out: dict[str, bytes] = {}
    for name, b in boxes.items():
        left, top = max(0.0, b.left - PAD) * W, max(0.0, b.top - PAD) * H
        right, bottom = min(1.0, b.left + b.width + PAD) * W, min(1.0, b.top + b.height + PAD) * H
        if right - left < 2 or bottom - top < 2:
            continue
        piece = img.crop((int(left), int(top), int(right), int(bottom)))
        if piece.width > MAX_CROP_W:
            piece = piece.resize((MAX_CROP_W, max(1, round(piece.height * MAX_CROP_W / piece.width))))
        buf = io.BytesIO()
        piece.save(buf, "JPEG", quality=80)
        out[name] = buf.getvalue()
    return out
