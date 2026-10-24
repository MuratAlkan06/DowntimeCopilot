"""Transcribe adapter: verbal-order read-back audio -> transcript.

Real call shape verified against the installed botocore service model (transcribe
StartTranscriptionJob: TranscriptionJobName, Media{MediaFileUri}, MediaFormat, LanguageCode;
async job, poll GetTranscriptionJob, transcript JSON at TranscriptFileUri). Media must be in S3.
docs/decisions.md: Transcribe ALLOWED (list only); typed read-back stays the fallback.
"""
from __future__ import annotations

from app import config

MOCK_TRANSCRIPT = ("Verbal order read back: ondansetron 4 milligrams IV every 6 hours as needed for nausea, "
                   "ordered by Doctor Quill. Read back and confirmed.")


def transcribe(audio: bytes, media_format: str = "webm", typed: str | None = None) -> str:
    """Typed read-back wins when given (Transcribe-denied fallback, README fallbacks table)."""
    if typed and typed.strip():
        return typed.strip()
    if config.dry_run():
        return MOCK_TRANSCRIPT
    # TODO(role 4, P2): upload audio to S3, start_transcription_job(TranscriptionJobName=...,
    # Media={"MediaFileUri": s3_uri}, MediaFormat=media_format, LanguageCode="en-US"), poll
    # get_transcription_job until COMPLETED, fetch Transcript.TranscriptFileUri.
    raise NotImplementedError("live Transcribe path not wired yet; send a typed read-back `transcript`")
