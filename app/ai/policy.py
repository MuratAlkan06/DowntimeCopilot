"""Policy answers grounded in data/policy/downtime_policy.md (Bedrock job 3).

docs/decisions.md: full policy in context (KB retrieval is P2). Citations are checked against the
parsed section headers: an answer is `grounded` only if every citation names a real section (or a
numbered paragraph inside one, e.g. 4.2.1) and its quote appears in that section's text.
An ungrounded reply is never shown: the answer becomes a refusal plus the nearest sections (refusal()).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app import config
from app.ai import client as ai
from app.ai.guardrail import get_guardrail
from contracts import Citation, Cost, PolicyAnswer

_HEADER = re.compile(r"^#{2,3}\s+(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$")
_WORD = re.compile(r"[a-z0-9]+")
STOP = {"how", "do", "i", "a", "an", "the", "with", "is", "to", "get", "what", "when", "of", "in", "on", "for",
        "and", "or", "my", "we", "can", "does", "should", "who"}


@dataclass
class Section:
    section_id: str
    title: str
    text: str


def policy_text() -> str:
    return (config.data_dir() / "policy" / "downtime_policy.md").read_text()


def sections() -> dict[str, Section]:
    out: dict[str, Section] = {}
    current: Section | None = None
    for line in policy_text().splitlines():
        m = _HEADER.match(line)
        if m:
            current = Section(m.group(1), m.group(2), "")
            out[current.section_id] = current
        elif current is not None:
            current.text += line + "\n"
    return out


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _terms(s: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(s.lower()) if w not in STOP}


def retrieve(question: str) -> Section:
    """Keyword retrieval for the dry-run answer: title matches weigh 3x, subsections win ties."""
    q = _terms(question)
    def score(s: Section) -> tuple:
        return (3 * len(q & _terms(s.title)) + len(q & _terms(s.text)), s.section_id.count("."))
    return max(sections().values(), key=score)


def _mock_answer(question: str) -> str:
    s = retrieve(question)
    paras = [p.strip() for p in s.text.split("\n\n") if p.strip()]
    quote = re.sub(r"^\d+(\.\d+)*\s+", "", paras[0]) if paras else s.title
    answer = " ".join(re.sub(r"^\d+(\.\d+)*\s+", "", p) for p in paras[:2])
    return json.dumps({"answer": f"Per section {s.section_id} ({s.title}): {answer}",
                       "citations": [{"section_id": s.section_id, "title": s.title, "quote": quote}]})


def _resolve(section_id: str, secs: dict[str, Section]) -> Section | None:
    """A parsed header, or the nearest parsed ancestor of a numbered paragraph that exists in it (4.2.1 -> 4.2)."""
    if section_id in secs:
        return secs[section_id]
    parts = section_id.split(".")
    for i in range(len(parts) - 1, 0, -1):
        if (s := secs.get(".".join(parts[:i]))) is not None:
            return s if re.search(rf"^{re.escape(section_id)}\s", s.text, re.M) else None
    return None


def grounded(citations: list[Citation]) -> bool:
    secs = sections()
    return bool(citations) and all(
        (s := _resolve(c.section_id, secs)) is not None and _norm(c.quote) in _norm(s.text) for c in citations
    )


REFUSAL = "I can't find this in the downtime policy."


def _paragraphs(s: Section) -> list[str]:
    """Body paragraphs with their leading paragraph number stripped (still verbatim section text)."""
    return [re.sub(r"^\d+(\.\d+)*\s+", "", p.strip()) for p in s.text.split("\n\n") if p.strip()]


def nearest(question: str, k: int = 3) -> list[Section]:
    """Top-k sections by keyword overlap with the question (title terms weigh 3x, like retrieve()).
    Deterministic, no model call: only sections with body text and a nonzero score, ties in document order."""
    q = _terms(question)
    scored = [(3 * len(q & _terms(s.title)) + len(q & _terms(s.text)), i, s)
              for i, s in enumerate(sections().values()) if s.text.strip()]
    return [s for score, _, s in sorted(scored, key=lambda t: (-t[0], t[1])) if score > 0][:k]


def refusal(question: str, lead: str = REFUSAL) -> tuple[str, list[Citation]]:
    """Grounding-failure answer: the refusal plus the nearest sections, each cited with its best-matching
    paragraph (verbatim) so the nurse knows where to look in the paper binder."""
    q = _terms(question)
    near = nearest(question)
    cites = [Citation(section_id=s.section_id, title=s.title,
                      quote=max(_paragraphs(s), key=lambda p: len(q & _terms(p)), default=""))
             for s in near]
    if not near:
        return f"{lead} Check the paper downtime binder.", cites
    return f"{lead} Nearest sections: " + "; ".join(f"{s.section_id} {s.title}" for s in near) + ".", cites


def ask(question: str, actor_role: str = "system") -> PolicyAnswer:
    policy = policy_text()
    user = json.dumps({"question": question, "policy": policy})
    res = ai.call("policy", "policy", user, mock=lambda: _mock_answer(question), actor_role=actor_role)
    try:
        raw = ai.extract_json(res.text)
        answer = str(raw.get("answer", ""))
        citations = [Citation(**{k: str(c.get(k, "")) for k in ("section_id", "title", "quote")})
                     for c in raw.get("citations", []) if isinstance(c, dict)]
    except ValueError:
        answer, citations = res.text.strip(), []
    g = get_guardrail().check(answer, grounding=policy, query=question)
    ok = grounded(citations)
    if not ok:  # no citation resolves + quote-matches: never show the ungrounded text
        answer, citations = refusal(question)
    return PolicyAnswer(question=question, answer=answer, citations=citations, model_id=res.model_id,
                        grounded=ok, guardrail=g,
                        cost=Cost(tokens_in=res.tokens_in, tokens_out=res.tokens_out, usd=res.usd))
