"""Transcribe adapter: verbal-order read-back audio -> transcript (docs/decisions.md S9).

Call shapes checked against the installed botocore models (1.43.x). Transcribe reads media from S3 only:
  1. create   the audio bucket (config.audio_bucket) if missing, then harden it (app.sync.s3.ensure_bucket);
              once per process
  2. put      PutObject audio/dtc-<uuid4>.<fmt> with ServerSideEncryption AES256
  3. start    StartTranscriptionJob(TranscriptionJobName=dtc-<uuid4>, LanguageCode en-US,
              MediaFormat <from the upload's content type>, Media{MediaFileUri s3://bucket/key})
  4. poll     GetTranscriptionJob until COMPLETED, within POLL_BUDGET_S of the start of step 1. Clock and
              sleep are injected, so tests never sleep
  5. fetch    the transcript JSON at Transcript.TranscriptFileUri (https, presigned, service-managed)
  finally     best-effort DeleteObject + DeleteTranscriptionJob; failures are logged, never raised
              (the bucket's 1-day expiry is the backstop for the object)

Fallbacks (typed / upload-only): the record keeps the audio blob locally, nothing is guessed, and the
response says why (X-Transcribe). AccessDenied / AccessDeniedException / UnauthorizedOperation at any
step: logged once, DENIED for the rest of the process (no retry). Job status FAILED, poll timeout (job
name logged), an empty transcript, other AWS errors, an unsupported format or no bucket configured are
separate outcomes and are not latched. A typed read-back always wins and Transcribe is not called.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field

from app import config
from app.sync import s3 as s3sync

log = logging.getLogger(__name__)

MOCK_TRANSCRIPT = ("Verbal order read back: ondansetron 4 milligrams IV every 6 hours as needed for nausea, "
                   "ordered by Doctor Quill. Read back and confirmed.")

LANGUAGE_CODE = "en-US"
POLL_BUDGET_S = 45.0    # whole pipeline, upload to COMPLETED; the capture then takes the fallback
POLL_INTERVAL_S = 1.5
FETCH_TIMEOUT_S = 10.0

# Outcomes (X-Transcribe header and the capture.transcribe audit). FALLBACKS mean no transcript.
TYPED, MOCK, TRANSCRIBED = "typed", "mock", "transcribed"
DENIED, FAILED, TIMEOUT, EMPTY, ERROR, UNSUPPORTED, UNCONFIGURED = (
    "denied", "failed", "timeout", "empty", "error", "unsupported", "unconfigured")
FALLBACKS = frozenset({DENIED, FAILED, TIMEOUT, EMPTY, ERROR, UNSUPPORTED, UNCONFIGURED})

# Upload subtype (content type without parameters) -> Transcribe MediaFormat enum.
MEDIA_FORMATS = {"wav": "wav", "x-wav": "wav", "wave": "wav", "vnd.wave": "wav", "webm": "webm", "ogg": "ogg",
                 "mpeg": "mp3", "mp3": "mp3", "mp4": "mp4", "m4a": "m4a", "x-m4a": "m4a", "flac": "flac",
                 "x-flac": "flac", "amr": "amr"}

_now, _sleep = time.monotonic, time.sleep  # injected into run_job; tests replace them (no real waits)
_denied: str | None = None       # step of the first AccessDenied; the process then stays on the fallback
_bucket_ready: set[str] = set()  # buckets ensured (created/hardened if missing) by this process


@dataclass
class Result:
    text: str | None
    outcome: str
    step: str | None = None       # pipeline step reached (create/put/start/poll/fetch) on a live run
    job_name: str | None = None
    seconds: float | None = None
    detail: str = ""              # error code, FailureReason, last status; never transcript text
    cleanup: dict = field(default_factory=dict)

    @property
    def fallback(self) -> bool:
        return self.outcome in FALLBACKS

    def audit(self) -> dict:
        """Audit details: no transcript text (PHI stays in the record, not the log)."""
        return {k: v for k, v in asdict(self).items() if k != "text" and v not in (None, "", {})}


def media_format(content_type: str | None) -> str | None:
    """'audio/webm;codecs=opus' -> 'webm'. None for anything Transcribe cannot take (images, text)."""
    main, _, sub = (content_type or "").split(";")[0].strip().lower().partition("/")
    return MEDIA_FORMATS.get(sub) if main in ("audio", "video") else None


def canned_transcript(audio: bytes) -> str:
    """DRY_RUN: the ground-truth transcript of a known fixture recording (data/forms/fixtures/*.json with
    `audio` + `transcript`), else MOCK_TRANSCRIPT."""
    digest = hashlib.sha256(audio).hexdigest()
    for truth_file in sorted((config.data_dir() / "forms" / "fixtures").glob("*.json")):
        truth = json.loads(truth_file.read_text())
        wav = truth_file.parent / str(truth.get("audio") or "")
        if truth.get("transcript") and wav.is_file() and hashlib.sha256(wav.read_bytes()).hexdigest() == digest:
            return truth["transcript"]
    return MOCK_TRANSCRIPT


def transcribe(audio: bytes, content_type: str, typed: str | None = None) -> Result:
    """Typed read-back wins when given (no Transcribe call). DRY_RUN: canned, zero network."""
    if typed and typed.strip():
        return Result(typed.strip(), TYPED)
    if config.dry_run():
        return Result(canned_transcript(audio), MOCK)
    fmt = media_format(content_type)
    if fmt is None:
        return Result(None, UNSUPPORTED, detail=(content_type or "").split(";")[0][:60])
    if _denied:
        return Result(None, DENIED, step=_denied, detail="denied earlier in this process")
    bucket = config.audio_bucket()
    if not bucket:
        return Result(None, UNCONFIGURED, detail="AUDIO_BUCKET not set")
    s3, tr = _clients()
    return run_job(audio, fmt, s3=s3, tr=tr, bucket=bucket, region=config.aws_region(), clock=_now, sleep=_sleep,
                   fetch=fetch_json)


def _clients():
    import boto3
    region = config.aws_region()
    return boto3.client("s3", region_name=region), boto3.client("transcribe", region_name=region)


def fetch_json(uri: str) -> dict:
    if not uri.startswith("https://"):
        raise ValueError("transcript URI is not https")
    with urllib.request.urlopen(uri, timeout=FETCH_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def transcript_text(doc: dict) -> str:
    return " ".join(t.get("transcript", "") for t in (doc.get("results") or {}).get("transcripts") or []).strip()


def run_job(audio: bytes, fmt: str, *, s3, tr, bucket: str, region: str = "us-east-1",
            clock=time.monotonic, sleep=time.sleep, fetch=fetch_json,
            budget: float = POLL_BUDGET_S, interval: float = POLL_INTERVAL_S) -> Result:
    """Steps 1-5 + cleanup (module docstring). Never raises for AWS, network or parse errors."""
    global _denied
    from botocore.exceptions import BotoCoreError, ClientError

    t0 = clock()
    uid = uuid.uuid4()
    job, key = f"dtc-{uid}", f"audio/dtc-{uid}.{fmt}"
    step, uploaded, started = "create", False, False
    res = Result(None, ERROR, step)
    try:
        if bucket not in _bucket_ready:
            s3sync.ensure_bucket(s3, bucket, region)
            _bucket_ready.add(bucket)
        step = "put"
        s3.put_object(Bucket=bucket, Key=key, Body=audio, ServerSideEncryption="AES256", ContentType=f"audio/{fmt}")
        uploaded = True
        step = "start"
        tr.start_transcription_job(TranscriptionJobName=job, LanguageCode=LANGUAGE_CODE, MediaFormat=fmt,
                                   Media={"MediaFileUri": f"s3://{bucket}/{key}"})
        started = True
        step = "poll"
        while True:
            info = tr.get_transcription_job(TranscriptionJobName=job)["TranscriptionJob"]
            status = info.get("TranscriptionJobStatus")
            if status == "COMPLETED":
                break
            if status == "FAILED":
                reason = str(info.get("FailureReason") or "")[:200]
                log.warning("Transcribe job %s FAILED (%s); typed/upload-only fallback", job, reason)
                res = Result(None, FAILED, step, job, detail=reason)
                return res
            if clock() - t0 + interval > budget:
                log.warning("Transcribe job %s not COMPLETED within %.0f s (last status %s); typed/upload-only "
                            "fallback", job, budget, status)
                res = Result(None, TIMEOUT, step, job, detail=f"last status {status}")
                return res
            sleep(interval)
        step = "fetch"
        text = transcript_text(fetch(info["Transcript"]["TranscriptFileUri"]))
        res = Result(text, TRANSCRIBED, step, job) if text else Result(None, EMPTY, step, job, detail="no speech")
        return res
    except ClientError as exc:
        code = s3sync.error_code(exc)
        if s3sync.is_denied(exc):
            _denied = step
            log.warning("Transcribe pipeline denied at %s (%s); typed/upload-only fallback for this process",
                        step, code)
            res = Result(None, DENIED, step, job if started else None, detail=code)
        else:
            log.warning("Transcribe pipeline error at %s (%s); typed/upload-only fallback", step, code)
            res = Result(None, ERROR, step, job if started else None, detail=code)
        return res
    except (BotoCoreError, OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Transcribe pipeline error at %s (%s); typed/upload-only fallback", step, type(exc).__name__)
        res = Result(None, ERROR, step, job if started else None, detail=type(exc).__name__)
        return res
    finally:
        res.seconds = round(clock() - t0, 2)
        res.cleanup = _cleanup(s3, tr, bucket, key if uploaded else None, job if started else None)


def _cleanup(s3, tr, bucket: str, key: str | None, job: str | None) -> dict:
    """Best-effort, non-fatal: the uploaded object and the job (with its service-managed transcript)."""
    done: dict = {}
    if key:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            done["object_deleted"] = True
        except Exception as exc:  # cleanup never fails a capture
            done["object_deleted"] = False
            log.warning("could not delete s3://%s/%s (%s); the 1-day expiry removes it", bucket, key,
                        s3sync.error_code(exc) or type(exc).__name__)
    if job:
        try:
            tr.delete_transcription_job(TranscriptionJobName=job)
            done["job_deleted"] = True
        except Exception as exc:  # cleanup never fails a capture
            done["job_deleted"] = False
            log.warning("could not delete Transcribe job %s (%s)", job, s3sync.error_code(exc) or type(exc).__name__)
    return done
