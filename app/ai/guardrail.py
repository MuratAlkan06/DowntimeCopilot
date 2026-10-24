"""Guardrail adapter: Bedrock ApplyGuardrail, or a checker-prompt second model call, one interface.

ApplyGuardrail shape verified against the installed botocore service model (bedrock-runtime
ApplyGuardrail: guardrailIdentifier, guardrailVersion, source INPUT|OUTPUT,
content[{text:{text, qualifiers[grounding_source|query|guard_content]}}]; response action
NONE|GUARDRAIL_INTERVENED, assessments[], outputs[{text}]).
Selection: app.config.guardrail_mode() (docs/decisions.md). DRY_RUN always passes.
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
        import boto3
        content = []
        if grounding:
            content.append({"text": {"text": grounding, "qualifiers": ["grounding_source"]}})
        if query:
            content.append({"text": {"text": query, "qualifiers": ["query"]}})
        content.append({"text": {"text": output, "qualifiers": ["guard_content"]}})
        resp = boto3.client("bedrock-runtime", region_name=config.aws_region()).apply_guardrail(
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
    import os
    if config.dry_run():
        return DryRunGuardrail()
    if config.guardrail_mode() == "bedrock":
        return BedrockGuardrail(os.environ["GUARDRAIL_ID"], os.environ.get("GUARDRAIL_VERSION", "DRAFT"))
    return CheckerGuardrail()
