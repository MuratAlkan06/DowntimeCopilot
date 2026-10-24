"""Verbal-order audio -> S3 -> Transcribe -> the same structuring as a typed read-back (S9).

Every test runs offline: S3 and Transcribe are a fake client pair, the clock and sleep are injected (no
real waits), and the Bedrock structuring call is stubbed at app.ai.client._converse.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import re
import wave
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app import sim_client
from app.ai import client as ai
from app.capture import pipeline, transcribe
from app.store import get_store, reset_store
from app.sync import s3 as s3sync
from contracts import BackloadGroup, CapturedRecord, FieldFlag, FormType
from simulator import main as sim

BUCKET = "downtime-copilot-audio-test"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt fake-read-back"
HEARD = ("Verbal order for bed 7: heparin 5000 units subcutaneous now. Ordering provider Doctor Quill. "
         "Read-back confirmed.")


class FakeAWS:
    """S3 + Transcribe in one object. `deny` = the step whose calls raise `code`; `statuses` = successive
    GetTranscriptionJob statuses (the last one repeats)."""

    STEP = {"head_bucket": "create", "create_bucket": "create", "put_bucket_encryption": "create",
            "put_public_access_block": "create", "put_bucket_lifecycle_configuration": "create",
            "put_object": "put", "start_transcription_job": "start", "get_transcription_job": "poll"}

    def __init__(self, deny=None, code="AccessDenied", statuses=("IN_PROGRESS", "COMPLETED"), exists=True,
                 cleanup_fails=False, failure_reason="Unsupported audio"):
        self.deny, self.code, self.statuses, self.exists = deny, code, list(statuses), exists
        self.cleanup_fails, self.failure_reason = cleanup_fails, failure_reason
        self.calls: list[tuple[str, dict]] = []

    def _call(self, op, kw):
        self.calls.append((op, kw))
        if self.deny and self.STEP.get(op) == self.deny:
            raise ClientError({"Error": {"Code": self.code, "Message": "nope"}}, op)
        if self.cleanup_fails and op.startswith("delete"):
            raise ClientError({"Error": {"Code": "InternalError", "Message": "x"}}, op)

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def kw(self, op) -> dict:
        return next(k for o, k in self.calls if o == op)

    def __getattr__(self, op):  # every other S3 / Transcribe call just records
        if op.startswith("_"):
            raise AttributeError(op)
        return lambda **kw: self._call(op, kw) or {}

    def head_bucket(self, **kw):
        self._call("head_bucket", kw)
        if not self.exists:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket")

    def get_transcription_job(self, **kw):
        self._call("get_transcription_job", kw)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        job = {"TranscriptionJobName": kw["TranscriptionJobName"], "TranscriptionJobStatus": status}
        if status == "COMPLETED":
            job["Transcript"] = {"TranscriptFileUri": "https://transcribe.example/t.json"}
        if status == "FAILED":
            job["FailureReason"] = self.failure_reason
        return {"TranscriptionJob": job}


class FakeClock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def transcript_doc(text=HEARD):
    return {"jobName": "x", "results": {"transcripts": [{"transcript": text}], "items": []}}


def run(aws, clock=None, fetch=None, **kw):
    clock = clock or FakeClock()
    return transcribe.run_job(WAV, "wav", s3=aws, tr=aws, bucket=BUCKET, clock=clock, sleep=clock.sleep,
                              fetch=fetch or (lambda uri: transcript_doc()), **kw)


@pytest.fixture(autouse=True)
def fresh_latches(monkeypatch):
    monkeypatch.setattr(transcribe, "_denied", None)
    monkeypatch.setattr(transcribe, "_bucket_ready", set())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("STORE_PATH", str(tmp_path / "test.sqlite"))
    monkeypatch.setenv("SNAPSHOT_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(sim_client, "make_client", lambda: TestClient(sim.app))
    sim.state.reset(7)
    reset_store()
    yield monkeypatch
    reset_store()


def live(monkeypatch, aws, structured: dict | None = None):
    """DRY_RUN=0 with the AWS pair, the clock and the Nova structuring call all stubbed."""
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("AUDIO_BUCKET", BUCKET)
    monkeypatch.setattr(transcribe, "_clients", lambda: (aws, aws))
    monkeypatch.setattr(transcribe, "fetch_json", lambda uri: transcript_doc())
    clock = FakeClock()
    monkeypatch.setattr(transcribe, "_now", clock)
    monkeypatch.setattr(transcribe, "_sleep", clock.sleep)
    reply = structured or {"fields": {"medication": "heparin", "dose": "5000 units", "route": "SC", "frequency": "now",
                                      "ordering_provider": "Dr. Quill", "read_back": "yes", "provider_cosign": "no"},
                           "confidence": {k: 0.9 for k in ("medication", "dose", "route", "frequency",
                                                           "ordering_provider", "read_back", "provider_cosign")}}
    seen: list[str] = []

    def fake(model_id, system, user_text, image, image_format, max_tokens):
        seen.append(json.loads(user_text)["transcript"])
        return {"output": {"message": {"content": [{"text": json.dumps(reply)}]}},
                "usage": {"inputTokens": 300, "outputTokens": 120}}

    monkeypatch.setattr(ai, "_converse", fake)
    return seen


# ------------------------------------------------------------------ call shapes

def test_media_format_from_content_type():
    assert transcribe.media_format("audio/webm;codecs=opus") == "webm"
    assert transcribe.media_format("audio/wav") == transcribe.media_format("audio/x-wav") == "wav"
    assert transcribe.media_format("audio/mp4") == "mp4" and transcribe.media_format("audio/mpeg") == "mp3"
    assert transcribe.media_format("audio/ogg; codecs=opus") == "ogg"
    for nope in ("image/jpeg", "text/plain", "application/octet-stream", "", None, "audio/x-unknown"):
        assert transcribe.media_format(nope) is None


def test_live_pipeline_call_shapes_and_cleanup():
    aws, clock = FakeAWS(statuses=("QUEUED", "IN_PROGRESS", "COMPLETED")), FakeClock()
    res = run(aws, clock)
    assert (res.outcome, res.text, res.fallback) == (transcribe.TRANSCRIBED, HEARD, False)
    put = aws.kw("put_object")
    assert put["Bucket"] == BUCKET and put["Body"] == WAV and put["ServerSideEncryption"] == "AES256"
    start = aws.kw("start_transcription_job")
    assert re.fullmatch(r"dtc-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", start["TranscriptionJobName"])
    assert start["LanguageCode"] == "en-US" and start["MediaFormat"] == "wav"
    assert start["Media"] == {"MediaFileUri": f"s3://{BUCKET}/{put['Key']}"} and put["Key"].endswith(".wav")
    assert res.job_name == start["TranscriptionJobName"]
    assert aws.ops().count("get_transcription_job") == 3 and clock.sleeps == [transcribe.POLL_INTERVAL_S] * 2
    assert aws.kw("delete_object") == {"Bucket": BUCKET, "Key": put["Key"]}
    assert aws.kw("delete_transcription_job") == {"TranscriptionJobName": res.job_name}
    assert res.cleanup == {"object_deleted": True, "job_deleted": True}
    assert "text" not in res.audit() and res.audit()["outcome"] == "transcribed"


def test_bucket_created_and_hardened_once_per_process():
    aws = FakeAWS(exists=False)
    run(aws)
    run(aws)
    assert aws.ops().count("head_bucket") == 1 and aws.ops().count("create_bucket") == 1
    assert "CreateBucketConfiguration" not in aws.kw("create_bucket")  # us-east-1 takes no LocationConstraint
    enc = aws.kw("put_bucket_encryption")["ServerSideEncryptionConfiguration"]["Rules"][0]
    assert enc["ApplyServerSideEncryptionByDefault"] == {"SSEAlgorithm": "AES256"}
    pab = aws.kw("put_public_access_block")["PublicAccessBlockConfiguration"]
    assert set(pab) == {"BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"}
    assert all(pab.values())
    (rule,) = aws.kw("put_bucket_lifecycle_configuration")["LifecycleConfiguration"]["Rules"]
    assert rule["Status"] == "Enabled" and rule["Expiration"] == {"Days": 1}


def test_ensure_bucket_location_constraint_and_already_owned():
    aws = FakeAWS(exists=False)
    assert s3sync.ensure_bucket(aws, BUCKET, "us-west-2") is True
    assert aws.kw("create_bucket")["CreateBucketConfiguration"] == {"LocationConstraint": "us-west-2"}

    class Owned(FakeAWS):
        def create_bucket(self, **kw):
            self.calls.append(("create_bucket", kw))
            raise ClientError({"Error": {"Code": "BucketAlreadyOwnedByYou"}}, "CreateBucket")

    owned = Owned(exists=False)
    assert s3sync.ensure_bucket(owned, BUCKET, "us-east-1") is True  # success, still hardened
    assert "put_bucket_lifecycle_configuration" in owned.ops()
    existing = FakeAWS()
    assert s3sync.ensure_bucket(existing, BUCKET, "us-east-1") is False and existing.ops() == ["head_bucket"]

    class Taken(FakeAWS):
        def create_bucket(self, **kw):
            raise ClientError({"Error": {"Code": "BucketAlreadyExists"}}, "CreateBucket")

    with pytest.raises(ClientError):
        s3sync.ensure_bucket(Taken(exists=False), BUCKET, "us-east-1")


def test_empty_and_delete_bucket():
    class Pager:
        def paginate(self, **kw):
            return [{"Versions": [{"Key": "audio/a.wav", "VersionId": "null"}],
                     "DeleteMarkers": [{"Key": "audio/b.wav", "VersionId": "v2"}]}]

    aws = FakeAWS()
    aws.get_paginator = lambda name: Pager()
    assert s3sync.empty_and_delete(aws, BUCKET) == 2
    assert aws.ops() == ["delete_objects", "delete_bucket"]
    assert len(aws.kw("delete_objects")["Delete"]["Objects"]) == 2


# ------------------------------------------------------------------ error taxonomy

@pytest.mark.parametrize("code", ["AccessDenied", "AccessDeniedException", "UnauthorizedOperation"])
@pytest.mark.parametrize("step", ["create", "put", "start", "poll"])
def test_access_denied_at_each_step_falls_back_and_latches(step, code, monkeypatch, caplog):
    aws = FakeAWS(deny=step, code=code, exists=step != "create")
    with caplog.at_level(logging.WARNING, logger="app.capture.transcribe"):
        res = run(aws)
    assert (res.outcome, res.step, res.detail, res.text) == (transcribe.DENIED, step, code, None) and res.fallback
    assert transcribe._denied == step
    # cleanup: the object only if it was uploaded, the job only if it was started
    assert ("delete_object" in aws.ops()) is (step in ("start", "poll"))
    assert ("delete_transcription_job" in aws.ops()) is (step == "poll")
    # later captures in this process: no AWS call, same DENIED, no second log line
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("AUDIO_BUCKET", BUCKET)
    untouched = FakeAWS()
    monkeypatch.setattr(transcribe, "_clients", lambda: (untouched, untouched))
    again = transcribe.transcribe(WAV, "audio/wav")
    assert again.outcome == transcribe.DENIED and again.step == step and untouched.calls == []
    assert sum("denied" in r.getMessage() for r in caplog.records) == 1


def test_head_bucket_403_counts_as_denied_at_create():
    aws = FakeAWS(deny="create", code="403")
    assert run(aws).outcome == transcribe.DENIED and aws.ops() == ["head_bucket"]


def test_job_failed_is_its_own_fallback_and_not_latched(caplog):
    aws = FakeAWS(statuses=("IN_PROGRESS", "FAILED"), failure_reason="The media format is not supported")
    with caplog.at_level(logging.WARNING, logger="app.capture.transcribe"):
        res = run(aws)
    assert (res.outcome, res.step, res.text) == (transcribe.FAILED, "poll", None) and res.fallback
    assert res.detail == "The media format is not supported" and transcribe._denied is None
    assert res.cleanup == {"object_deleted": True, "job_deleted": True}
    assert res.job_name in caplog.text and "FAILED" in caplog.text


def test_poll_timeout_with_injected_clock(caplog):
    aws, clock = FakeAWS(statuses=("IN_PROGRESS",)), FakeClock()
    with caplog.at_level(logging.WARNING, logger="app.capture.transcribe"):
        res = run(aws, clock, budget=45.0, interval=1.5)
    assert (res.outcome, res.step, res.text) == (transcribe.TIMEOUT, "poll", None) and res.fallback
    assert len(clock.sleeps) == 30 and sum(clock.sleeps) == 45.0  # 30 x 1.5 s; one more would pass 45 s
    assert res.seconds == pytest.approx(45.0) and res.detail == "last status IN_PROGRESS"
    assert res.job_name and res.job_name in caplog.text  # job name logged for follow-up
    assert transcribe._denied is None and res.cleanup["object_deleted"] is True


def test_other_errors_and_empty_transcript_fall_back_without_latching():
    res = run(FakeAWS(deny="start", code="LimitExceededException"))
    assert (res.outcome, res.detail) == (transcribe.ERROR, "LimitExceededException") and transcribe._denied is None
    assert run(FakeAWS(), fetch=lambda uri: transcript_doc("  ")).outcome == transcribe.EMPTY

    def boom(uri):
        raise OSError("connection reset")
    res = run(FakeAWS(), fetch=boom)
    assert (res.outcome, res.step, res.detail) == (transcribe.ERROR, "fetch", "OSError")
    assert res.cleanup == {"object_deleted": True, "job_deleted": True}


def test_cleanup_failure_is_not_fatal():
    res = run(FakeAWS(cleanup_fails=True))
    assert res.outcome == transcribe.TRANSCRIBED and res.text == HEARD
    assert res.cleanup == {"object_deleted": False, "job_deleted": False}


def test_fetch_json_refuses_non_https():
    with pytest.raises(ValueError):
        transcribe.fetch_json("file:///etc/hosts")


def test_unsupported_and_unconfigured_never_call_aws(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    aws = FakeAWS()
    monkeypatch.setattr(transcribe, "_clients", lambda: (aws, aws))
    assert transcribe.transcribe(b"\xff\xd8jpeg", "image/jpeg").outcome == transcribe.UNSUPPORTED
    monkeypatch.delenv("AUDIO_BUCKET", raising=False)
    assert transcribe.transcribe(WAV, "audio/wav").outcome == transcribe.UNCONFIGURED
    assert transcribe.transcribe(WAV, "audio/wav", typed=" typed wins ").outcome == transcribe.TYPED
    assert aws.calls == []


# ------------------------------------------------------------------ through the capture pipeline

def shape(rec: CapturedRecord) -> dict:
    return {"fields": [(f.name, f.value, f.flag, f.agreement) for f in rec.fields], "form_type": rec.form_type,
            "group": rec.backload_group, "image_ref": rec.image_ref, "has_audio": bool(rec.audio_ref),
            "textract": rec.reads.textract, "bedrock": rec.reads.bedrock, "patient": rec.patient_id}


def test_live_audio_uses_the_typed_structuring_path(env):
    aws = FakeAWS()
    seen = live(env, aws)
    info: dict = {}
    audio = pipeline.capture(WAV, "audio/wav", FormType.verbal_order, "pt-07", "rn", info=info)
    typed = pipeline.capture(HEARD.encode(), "text/plain", FormType.verbal_order, "pt-07", "rn", HEARD)
    assert seen == [HEARD, HEARD]  # same structuring call, same text
    assert shape(audio) == shape(typed) and info == {"transcribe": "transcribed", "text_source": "transcribed"}
    f = {x.name: x.value for x in audio.fields}
    assert (f["medication"], f["dose"], f["route"], f["ordering_provider"], f["read_back"]) == (
        "heparin", "5000 units", "SC", "Dr. Quill", "yes")
    assert f["provider_cosign"] == "no" and audio.backload_group == BackloadGroup.pharmacy_orders
    assert get_store().get_blob(audio.audio_ref) == (WAV, "audio/wav")  # audio kept locally; S3 copy deleted
    assert {"delete_object", "delete_transcription_job"} <= set(aws.ops())
    ev = next(e for e in get_store().list_audit() if e.action == "capture.transcribe" and e.target_id == audio.record_id)
    assert ev.details["outcome"] == "transcribed" and HEARD not in json.dumps(ev.details)


def test_live_denied_keeps_audio_upload_only_and_says_why(env):
    seen = live(env, FakeAWS(deny="start", code="AccessDeniedException"))
    info: dict = {}
    rec = pipeline.capture(WAV, "audio/webm", FormType.verbal_order, "pt-07", "rn", info=info)
    assert info == {"transcribe": "denied", "text_source": "audio_untranscribed"}
    assert seen == []  # no transcript, so no structuring call
    assert rec.audio_ref and get_store().get_blob(rec.audio_ref)[0] == WAV
    assert all(f.value is None and f.flag == FieldFlag.unreadable for f in rec.fields)  # nothing guessed
    assert {f.name for f in rec.fields} >= {"medication", "read_back", "provider_cosign"}
    assert rec.backload_group == BackloadGroup.pharmacy_orders
    ev = next(e for e in get_store().list_audit() if e.action == "capture.transcribe")
    assert ev.details["outcome"] == "denied" and ev.details["step"] == "start"


def test_dry_run_audio_and_typed_parity(env):
    info: dict = {}
    audio = pipeline.capture(WAV, "audio/webm;codecs=opus", FormType.verbal_order, "pt-07", "rn", info=info)
    typed_text = transcribe.MOCK_TRANSCRIPT
    typed = pipeline.capture(typed_text.encode(), "text/plain", FormType.verbal_order, "pt-07", "rn", typed_text)
    assert info == {"transcribe": "mock", "text_source": "transcribed"} and shape(audio) == shape(typed)
    assert audio.reads.bedrock.fields["medication"] == "ondansetron"


def test_api_reports_the_transcribe_outcome(env):
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/capture", files={"file": ("rb.webm", WAV, "audio/webm")}, data={"form_type": "verbal_order"})
        assert r.status_code == 201 and r.headers["x-transcribe"] == "mock"
        r = c.post("/capture", data={"form_type": "verbal_order", "readback_text": HEARD})
        assert r.status_code == 201 and r.headers["x-transcribe"] == "typed"
        r = c.post("/capture", files={"file": ("f.png", b"\x89PNG", "image/png")}, data={"form_type": "vitals_flowsheet"})
        assert r.status_code == 201 and "x-transcribe" not in r.headers


def test_text_source_from_every_transcribe_outcome():
    assert pipeline.text_source(transcribe.TYPED) == pipeline.TYPED
    assert pipeline.text_source(transcribe.MOCK) == pipeline.text_source(transcribe.TRANSCRIBED) == pipeline.TRANSCRIBED
    assert {pipeline.text_source(o) for o in transcribe.FALLBACKS} == {pipeline.AUDIO_UNTRANSCRIBED}
    assert pipeline.text_source("something new") == pipeline.AUDIO_UNTRANSCRIBED  # never claims a transcript


def source_of(c: TestClient, record_id: str) -> str | None:
    """What a fresh client (reload, another device) learns: the stored value via the verification call."""
    r = c.get(f"/backload/{record_id}/verification")
    assert r.status_code == 200, r.text
    return r.json()["text_source"]


def test_text_source_is_stored_at_capture_and_served_after_reload(env):
    """S9 re-check: a typed read-back over attached audio must not read as "transcribed from audio" once the
    capturing device's memory is gone. Each capture path stores its source; later saves keep it."""
    from app.main import app
    with TestClient(app) as c:
        audio = c.post("/capture", files={"file": ("rb.webm", WAV, "audio/webm")}, data={"form_type": "verbal_order"})
        typed_over_audio = c.post("/capture", files={"file": ("rb.webm", WAV, "audio/webm")},
                                  data={"form_type": "verbal_order", "readback_text": HEARD})
        typed_only = c.post("/capture", data={"form_type": "verbal_order", "readback_text": HEARD})
        photo = c.post("/capture", files={"file": ("f.png", b"\x89PNG", "image/png")},
                       data={"form_type": "vitals_flowsheet"})
        ids = [r.json()["record_id"] for r in (audio, typed_over_audio, typed_only, photo)]
        assert [source_of(c, i) for i in ids] == ["transcribed", "typed", "typed", "photo"]
        assert get_store().get_blob(typed_over_audio.json()["audio_ref"])[0] == WAV  # the audio is kept
        # Edit / verify / entered re-save the record without a source; the stored one stays.
        assert c.patch(f"/backload/{ids[1]}/fields/dose", json={"value": "5000 units SC"}).status_code == 200
        assert c.post(f"/backload/{ids[3]}/verify", json={}).json()["status"] == "verified"
        assert c.post(f"/backload/{ids[3]}/entered").json()["status"] == "entered"
        assert (source_of(c, ids[1]), source_of(c, ids[3])) == ("typed", "photo")
        assert "text_source" not in CapturedRecord.model_fields  # storage only: the contract is untouched
    reset_store()  # a new process on the same store file (reload) still has it
    assert get_store().text_source(ids[1]) == "typed" and get_store().text_source("rec-unknown") is None


def test_fallback_capture_stores_audio_untranscribed(env):
    seen = live(env, FakeAWS(deny="start", code="AccessDeniedException"))
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/capture", files={"file": ("rb.webm", WAV, "audio/webm")}, data={"form_type": "verbal_order"})
        assert r.status_code == 201 and r.headers["x-transcribe"] == "denied" and seen == []
        assert source_of(c, r.json()["record_id"]) == "audio_untranscribed"


# ------------------------------------------------------------------ speech fixture (tools/speech_fixture.py)

FIXTURES = Path(__file__).resolve().parent.parent / "data" / "forms" / "fixtures"


def speech_fixture() -> tuple[bytes, dict]:
    truth = json.loads((FIXTURES / "verbal_order-01.json").read_text())
    return (FIXTURES / truth["audio"]).read_bytes(), truth


def test_speech_fixture_is_small_synthetic_pcm_and_matches_the_generator():
    audio, truth = speech_fixture()
    assert len(audio) < 500_000 and truth["synthetic"] is True
    with wave.open(io.BytesIO(audio)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        assert 3 < w.getnframes() / w.getframerate() < 15
    spec = importlib.util.spec_from_file_location("speech_fixture", FIXTURES.parents[2] / "tools" / "speech_fixture.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)  # import only; generating needs macOS and never runs in CI
    assert truth["script"] == gen.SCRIPT and {k: truth[k] for k in gen.TRUTH} == gen.TRUTH
    assert "Quill" in truth["script"] and truth["bed"] == "7"


def test_dry_run_fixture_audio_gets_its_canned_transcript_and_typed_parity(env):
    audio, truth = speech_fixture()
    assert transcribe.canned_transcript(audio) == truth["transcript"]
    assert transcribe.canned_transcript(audio + b"\x00") == transcribe.MOCK_TRANSCRIPT  # any other audio
    info: dict = {}
    rec = pipeline.capture(audio, "audio/wav", FormType.verbal_order, truth["patient_id"], "rn", info=info)
    typed = pipeline.capture(truth["transcript"].encode(), "text/plain", FormType.verbal_order, truth["patient_id"],
                             "rn", truth["transcript"])
    assert info == {"transcribe": "mock", "text_source": "transcribed"} and shape(rec) == shape(typed)
    f = {x.name: x.value for x in rec.fields}
    want = truth["fields"]
    for name in ("medication", "dose", "frequency", "ordering_provider", "read_back", "provider_cosign"):
        assert f[name] == want[name], name
    assert f["route"].lower().startswith("subcut")  # the DRY_RUN pattern parse keeps the spoken route word
    assert rec.backload_group == BackloadGroup.pharmacy_orders and rec.patient_id == "pt-07"
