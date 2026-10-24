"""Bedrock Converse wrapper with model routing (README "Model routing") and cost logging.

Request shape verified against the installed botocore service model (bedrock-runtime Converse:
modelId, messages[{role, content[{text}|{image:{format, source:{bytes}}}]}], system[{text}],
inferenceConfig{maxTokens, temperature, topP, stopSequences}; response output.message.content[],
usage{inputTokens, outputTokens}) and the AWS Bedrock user guide examples (context7
/websites/aws_amazon_bedrock, "conversation-inference").

DRY_RUN=1: every call returns the caller's canned, contract-shaped text with zero network.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app import config
from app.store import get_store

log = logging.getLogger(__name__)

NOVA_2_LITE = "us.amazon.nova-2-lite-v1:0"
SONNET_4_6 = "us.anthropic.claude-sonnet-4-6"
HAIKU_4_5 = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
FALLBACK_MODEL = HAIKU_4_5

# Job -> inference profile. us-east-1 / us-west-2 serve these only via us./global. profiles.
ROUTES: dict[str, str] = {
    "form_read": NOVA_2_LITE,
    "triage": NOVA_2_LITE,
    "structure": NOVA_2_LITE,
    "checker": NOVA_2_LITE,
    "packet": SONNET_4_6,
    "policy": SONNET_4_6,
    "digest": SONNET_4_6,
    "incident": SONNET_4_6,
}

# USD per 1M tokens (input, output). ESTIMATES for the cost widget.
# TODO(role 5): confirm against the Bedrock pricing page before RESULTS.md quotes cost per outage-hour.
PRICING: dict[str, tuple[float, float]] = {
    NOVA_2_LITE: (0.30, 2.50),
    SONNET_4_6: (3.00, 15.00),
    HAIKU_4_5: (1.00, 5.00),
}

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass
class AIResult:
    text: str
    model_id: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    usd: float
    dry_run: bool


def load_prompt(name: str) -> str:
    """Prompts are versioned files, read at call time (edit without restarting)."""
    return (PROMPTS_DIR / f"{name}.md").read_text()


def estimate_usd(model_id: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICING.get(model_id, PRICING[SONNET_4_6])
    return round(tokens_in / 1e6 * pin + tokens_out / 1e6 * pout, 6)


def extract_json(text: str) -> Any:
    """Parse the first JSON object in a model reply (tolerates ```json fences and prose)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


_bedrock = None
_bedrock_lock = threading.Lock()


def _runtime():
    """One shared bedrock-runtime client. Clients are thread-safe, but creating them from the default
    session is not (boto3 docs), so creation is locked: packets are generated on worker threads."""
    global _bedrock
    with _bedrock_lock:
        if _bedrock is None:
            import boto3
            _bedrock = boto3.client("bedrock-runtime", region_name=config.aws_region())
    return _bedrock


def _converse(model_id: str, system: str, user_text: str, image: bytes | None, image_format: str,
              max_tokens: int) -> dict:
    content: list[dict] = [{"text": user_text}]
    if image is not None:
        content.append({"image": {"format": image_format, "source": {"bytes": image}}})
    return _runtime().converse(
        modelId=model_id,
        messages=[{"role": "user", "content": content}],
        system=[{"text": system}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
    )


def call(job: str, prompt: str, user_text: str, *, mock: Callable[[], str], image: bytes | None = None,
         image_format: str = "jpeg", max_tokens: int = 2000, actor_role: str = "system") -> AIResult:
    model_id = ROUTES[job]
    system = load_prompt(prompt)
    t0 = time.perf_counter()
    if config.dry_run():
        text = mock()
        tokens_in, tokens_out = (len(system) + len(user_text)) // 4, len(text) // 4
        used, usd = f"dry-run:{model_id}", 0.0
    else:
        used = model_id
        try:
            resp = _converse(model_id, system, user_text, image, image_format, max_tokens)
        except Exception as exc:  # noqa: BLE001 - any Bedrock failure falls back once to Haiku
            if model_id == FALLBACK_MODEL:
                raise
            log.warning("bedrock %s failed for %s (%s); falling back to %s", model_id, job, exc, FALLBACK_MODEL)
            used = FALLBACK_MODEL
            resp = _converse(used, system, user_text, image, image_format, max_tokens)
        text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
        tokens_in, tokens_out = resp["usage"]["inputTokens"], resp["usage"]["outputTokens"]
        usd = estimate_usd(used, tokens_in, tokens_out)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    store = get_store()
    store.record_cost(job, used, tokens_in, tokens_out, latency_ms, usd, config.dry_run())
    store.audit(actor_role, "ai.call", "model", used,
                {"job": job, "prompt": prompt, "tokens_in": tokens_in, "tokens_out": tokens_out,
                 "latency_ms": latency_ms, "usd": usd, "dry_run": config.dry_run()})
    return AIResult(text, used, tokens_in, tokens_out, latency_ms, usd, config.dry_run())
