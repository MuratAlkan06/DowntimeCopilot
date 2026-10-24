"""Photo / audio -> CapturedRecord -> offline action queue (status needs_review)."""
from __future__ import annotations

import uuid

from app.ai import forms
from app.capture import crops, textract, transcribe
from app.capture.agreement import merge
from app.store import get_store
from app.sim_client import now as sim_now
from contracts import BackloadGroup, CapturedRecord, FormType, Reads

BACKLOAD_GROUP: dict[FormType, BackloadGroup] = {
    FormType.vitals_flowsheet: BackloadGroup.nursing_vitals,
    FormType.downtime_mar: BackloadGroup.nursing_med_admin,
    FormType.verbal_order: BackloadGroup.pharmacy_orders,
    FormType.admission_note: BackloadGroup.unit_registration,
}

IMAGE_FORMATS = {"image/jpeg": "jpeg", "image/jpg": "jpeg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def _resolve_patient(patient_id: str | None, bed: str | None) -> str | None:
    if patient_id:
        return patient_id
    if not bed:
        return None
    want = bed.strip().lower().removeprefix("bed").strip()
    for p in get_store().latest_snapshot():
        if p.bed.lower().removeprefix("bed").strip() == want:
            return p.patient_id
    return None


def capture(data: bytes, content_type: str, form_type: FormType, patient_id: str | None, role: str,
            transcript: str | None = None, info: dict | None = None) -> CapturedRecord:
    """Store the original, read it (Textract + Nova 2 Lite vision; canned under DRY_RUN), merge, and store
    one crop blob per field (crops.crop_ref). Unreadable fields carry no value (agreement.merge).
    Verbal order: typed read-back, else the audio through Transcribe; either text takes the same structuring
    call. No transcript (fallback) keeps the audio with every field unread. `info["transcribe"]` gets the
    outcome (app.capture.transcribe) for the caller."""
    store = get_store()
    record_id = f"rec-{uuid.uuid4().hex[:12]}"
    ref = store.save_blob(data, content_type)
    boxes: dict = {}
    if form_type == FormType.verbal_order:
        heard = transcribe.transcribe(data, content_type, typed=transcript)
        store.audit(role, "capture.transcribe", "captured_record", record_id, heard.audit())
        if info is not None:
            info["transcribe"] = heard.outcome
        reads = Reads(textract=None, bedrock=forms.structure_transcript(heard.text, actor_role=role)
                      if heard.text else forms.unread_verbal_order())
        image_ref, audio_ref = None, ref
    else:
        fmt = IMAGE_FORMATS.get(content_type.lower(), "jpeg")
        vision, model_boxes = forms.read_form_with_boxes(data, fmt, form_type, actor_role=role)
        second, textract_boxes = textract.read_with_boxes(data, form_type)
        reads = Reads(textract=second, bedrock=vision)
        image_ref, audio_ref = ref, None
        boxes, source = crops.choose_boxes(form_type, model_boxes, textract_boxes)
        rendered = crops.render_crops(data, boxes)
        for name, jpeg in rendered.items():
            store.save_blob(jpeg, "image/jpeg", ref=crops.crop_ref(record_id, name))
        store.audit(role, "capture.crops", "captured_record", record_id,
                    {"stored": sorted(rendered), "box_source": source})
    fields = merge(reads.textract, reads.bedrock, boxes)
    bed = next((f.value for f in fields if f.name == "bed"), None)
    return CapturedRecord(
        record_id=record_id, patient_id=_resolve_patient(patient_id, bed), form_type=form_type,
        image_ref=image_ref, audio_ref=audio_ref, captured_at=sim_now(), captured_by_role=role, reads=reads,
        fields=fields, backload_group=BACKLOAD_GROUP[form_type],
    )
