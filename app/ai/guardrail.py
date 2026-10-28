"""Guardrail adapter: Bedrock ApplyGuardrail, or a checker-prompt second model call, one interface.

ApplyGuardrail shape verified against the installed botocore service model (bedrock-runtime
ApplyGuardrail: guardrailIdentifier, guardrailVersion, source INPUT|OUTPUT,
content[{text:{text, qualifiers[grounding_source|query|guard_content]}}]; response action
NONE|GUARDRAIL_INTERVENED, assessments[], outputs[{text}]).
Selection: app.config.guardrail_mode() (docs/decisions.md). DRY_RUN always passes.

The published guardrail (downtime-copilot-v1) has a contextual grounding policy, which rejects a request
carrying a grounding source without a query (ValidationException, verified live). Its denied topic cannot
see the patient record either, so it flags doses the record itself states (verified live: packet text
restating a scheduled insulin dose INTERVENED). Hence record-grounded output without a query (packets,
digest) keeps the checker, which judges against the grounding source; policy answers (grounding + query)
go through ApplyGuardrail. Limits per the Bedrock user guide: grounding source 100,000 chars, query 1,000.
The denied topic evaluates the answer text only; the query and grounding source are excluded from topic evaluation.
"""
from __future__ import annotations

import json
from typing import Protocol

from app import config
from app.ai import client as ai
from contracts import Guardrail


class GuardrailAdapter(Protocol):
    def check(self, output: str, *, grounding: str | None = None, query: str | None = None) -> Guardrail: ...


class DryRunGuardrail:
    def check(self, output: str, *, grounding: str | None = None, query: str | None = None) -> Guardrail:
        return Guardrail(status="passed", reasons=[])


class BedrockGuardrail:
    def __init__(self, guardrail_id: str, version: str) -> None:
        self.guardrail_id, self.version = guardrail_id, version

    def check(self, output: str, *, grounding: str | None = None, query: str | None = None) -> Guardrail:
        if grounding and not query:  # packets, digest: see module docstring
            return CheckerGuardrail().check(output, grounding=grounding)
        content = []
        if grounding and query:  # contextual grounding needs both (see module docstring)
            content.append({"text": {"text": grounding[:100_000], "qualifiers": ["grounding_source"]}})
            content.append({"text": {"text": query[:1_000], "qualifiers": ["query"]}})
        content.append({"text": {"text": output, "qualifiers": ["guard_content"]}})
        resp = ai._runtime().apply_guardrail(  # shared client: called from packet worker threads
            guardrailIdentifier=self.guardrail_id, guardrailVersion=self.version, source="OUTPUT", content=content,
        )
        if resp.get("action") == "GUARDRAIL_INTERVENED":
            reasons = [k for a in resp.get("assessments", []) for k in a if k.endswith("Policy")]
            return Guardrail(status="intervened", reasons=reasons or [resp.get("actionReason", "intervened")])
        return Guardrail(status="passed", reasons=[])


class CheckerGuardrail:
    """Fallback: second Bedrock call with prompts/checker.md, strict JSON verdict."""

    def check(self, output: str, *, grounding: str | None = None, query: str | None = None) -> Guardrail:
        payload = json.dumps({"output": output, "grounding_source": grounding, "query": query})
        res = ai.call("checker", "checker", payload, mock=lambda: '{"verdict": "pass", "reasons": []}')
        try:
            verdict = ai.extract_json(res.text)
        except ValueError:
            return Guardrail(status="error", reasons=["checker output unparseable"])
        if str(verdict.get("verdict", "")).lower() == "pass":
            return Guardrail(status="passed", reasons=[])
        return Guardrail(status="intervened", reasons=[str(r) for r in verdict.get("reasons", [])] or ["checker failed"])


def get_guardrail() -> GuardrailAdapter:
    if config.dry_run():
        return DryRunGuardrail()
    published = config.guardrail_published()
    if config.guardrail_mode() == "bedrock" and published:
        return BedrockGuardrail(*published)
    return CheckerGuardrail()
