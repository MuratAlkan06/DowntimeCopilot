"""During: photograph a paper form (or record a verbal-order read-back) -> CapturedRecord."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from app.api import actor_role
from app.capture.pipeline import capture as run_capture
from app.store import get_store
from contracts import CapturedRecord, FormType

router = APIRouter(tags=["capture"])


@router.post("/capture", response_model=CapturedRecord, status_code=201)
async def capture(
    file: UploadFile = File(...),
    form_type: FormType = Form(...),
    patient_id: str | None = Form(default=None),
    transcript: str | None = Form(default=None),
    role: str = Depends(actor_role),
) -> CapturedRecord:
    data = await file.read()
    if not data:
        raise HTTPException(422, "empty upload")
    try:
        record = run_capture(data, file.content_type or "application/octet-stream", form_type, patient_id or None,
                             role, transcript)
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc)) from exc
    store = get_store()
    store.save_record(record)
    store.audit(role, "capture.create", "captured_record", record.record_id,
                {"form_type": form_type.value, "patient_id": record.patient_id,
                 "flags": [f.name for f in record.fields if f.flag]})
    return record


@router.get("/blobs/{ref}")
def blob(ref: str, role: str = Depends(actor_role)) -> Response:
    found = get_store().get_blob(ref)
    get_store().audit(role, "blob.view", "blob", ref, {"found": found is not None})
    if found is None:
        raise HTTPException(404, "unknown blob")
    return Response(content=found[0], media_type=found[1])
