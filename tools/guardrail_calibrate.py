"""Guardrail calibration replay (#80 S3): re-send the refusal eval's ApplyGuardrail checks to one guardrail version
and report what each policy saw, so the contextual grounding threshold comes from measured scores.

    ( set -a; source .env; set +a; DRY_RUN=0 .venv/bin/python tools/guardrail_calibrate.py \
        [--version N] [--bundle evals/baseline/<run-id>] [--raw-out DIR] )

Inputs are committed data only: the policy text (data/policy/downtime_policy.md, the grounding source), each
refusal question of evals/manifest.json (the query) and the model's answer as the guardrail received it
(`pre_guardrail` in the bundle's outputs/refusals.json). Every check goes through app.ai.guardrail.BedrockGuardrail,
so block order, qualifiers, caps and source OUTPUT are the app's own. The one addition is outputScope FULL, which
returns the filters that did not intervene too (the passing scores); it does not change the verdict.

Needs DRY_RUN=0, GUARDRAIL_ID and a version: --version, default GUARDRAIL_VERSION. DRAFT runs only when asked for
by name (--version DRAFT), never by default. Prints per entry the denied-topic hit, every contextual grounding
filter, the overall action and a usage-derived cost, then the threshold the frozen #80 S3 formula gives and the
forecast at it: never a raw response, the guardrail id or any account id. Raw responses are written only with
--raw-out, a new or empty directory outside the repo or git-ignored, scrubbed and with the guardrail id masked.
Exit 0 when the replay completed and the formula gave a threshold, 1 when it completed without one (missing
scores, or the formula's STOP), 2 when it could not run (stopped at the first error, never retried).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # `python tools/guardrail_calibrate.py` as well as `python -m tools.guardrail_calibrate`
    sys.path.insert(0, str(ROOT))

from app.ai import client as ai  # noqa: E402
from app.ai.guardrail import BedrockGuardrail  # noqa: E402
from evals import run as evals_run  # noqa: E402
from evals.run import Refused  # noqa: E402
from scripts.check_notice_guardrail import PRICE_PER_1K  # noqa: E402

POLICY = ROOT / "data" / "policy" / "downtime_policy.md"
# The refusal eval's first live run (#80 S2), pinned by evals.run.digest (docs/decisions.md, #80 S2).
BASELINE = ROOT / "evals" / "baseline" / "20261028T152718Z-336ee8a"
BASELINE_DIGEST = "d05fdbe37299954a68f3f5daf691cd74b9843058308d8f184713c1e211a8cf80"
SHOULD_ANSWER, SHOULD_REFUSE = "should_answer", "should_refuse"

# The frozen #80 S3 formula: T = min(CAP, floor_to_STEP(min(S) - MARGIN)); below FLOOR, recompute once without the
# single lowest score (the 4-of-5 acceptance rule); still below FLOOR is a STOP (no threshold change).
CAP, FLOOR, STEP, MARGIN = Decimal("0.75"), Decimal("0.40"), Decimal("0.05"), Decimal("0.05")


# ------------------------------------------------------------------ inputs (committed data, offline)

def load_entries(bundle: Path = BASELINE, pinned: str | None = BASELINE_DIGEST) -> list[dict]:
    """One entry per refusal question: id, category, subcategory, question (the manifest's), the bundle's
    pre_guardrail text and baseline outcome. Refuses a bundle that fails its pin or does not match the manifest."""
    refusals = bundle / "outputs" / "refusals.json"
    if not refusals.is_file():
        raise Refused(f"{bundle} has no outputs/refusals.json")
    if pinned is not None and (got := evals_run.digest(bundle)) != pinned:
        raise Refused(f"bundle {bundle.name} digest {got} != pinned {pinned}")
    declared = json.loads(evals_run.MANIFEST.read_text()).get("refusal_questions", [])
    recorded = json.loads(refusals.read_text())
    if [q["id"] for q in declared] != [r["id"] for r in recorded]:
        raise Refused(f"bundle {bundle.name} refusal ids differ from evals/manifest.json refusal_questions")
    out = []
    for q, r in zip(declared, recorded):
        if r["response"]["question"] != q["question"]:
            raise Refused(f"{q['id']}: the bundle's question differs from the manifest's")
        if not str(r.get("pre_guardrail", "")).strip():
            raise Refused(f"{q['id']}: no pre_guardrail text in the bundle")
        out.append({"id": q["id"], "category": q["category"], "subcategory": q["subcategory"],
                    "question": q["question"], "pre_guardrail": r["pre_guardrail"], "baseline": r["outcome"]})
    return out


def target(version_arg: str | None) -> tuple[str, str]:
    """(guardrail id, version). The version is --version, else GUARDRAIL_VERSION; DRAFT only when named."""
    if os.environ.get("DRY_RUN") != "0":
        raise Refused("needs DRY_RUN=0 (source .env in a subshell): this replay makes live ApplyGuardrail calls")
    gid = os.environ.get("GUARDRAIL_ID", "").strip()
    version = (version_arg or os.environ.get("GUARDRAIL_VERSION") or "").strip()
    if not gid or not version:
        raise Refused("needs GUARDRAIL_ID and a version (--version, or GUARDRAIL_VERSION in the environment)")
    if version.upper() == "DRAFT":
        if (version_arg or "").strip().upper() != "DRAFT":
            raise Refused("GUARDRAIL_VERSION is DRAFT: a DRAFT replay must be asked for with --version DRAFT")
        version = "DRAFT"
    elif not version.isdigit():
        raise Refused(f"version {version!r} is neither a published version number nor DRAFT")
    return gid, version


def check_raw_out(path: Path | None) -> Path | None:
    """--raw-out must be a new or empty directory outside the repo, or one git ignores."""
    if path is None:
        return None
    path = path.resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise Refused(f"--raw-out {path} must be a new or empty directory")
    if path.is_relative_to(ROOT):
        ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(path)], capture_output=True)
        if ignored.returncode != 0:
            raise Refused(f"--raw-out {path} is inside the repo and not git-ignored")
    return path


# ------------------------------------------------------------------ the replay

class Recorder:
    """Stands in for the bedrock-runtime client inside BedrockGuardrail.check: forwards the app's request unchanged
    plus outputScope FULL, and keeps each request and response."""

    def __init__(self, client) -> None:
        self.client = client
        self.calls: list[tuple[dict, dict]] = []

    def apply_guardrail(self, **request):
        resp = self.client.apply_guardrail(**request, outputScope="FULL")
        self.calls.append((request, resp))
        return resp


@contextlib.contextmanager
def recording(client):
    rec, orig = Recorder(client), ai._runtime
    ai._runtime = lambda: rec
    try:
        yield rec
    finally:
        ai._runtime = orig


def check_one(entry: dict, policy: str, client, gid: str, version: str) -> tuple[dict, dict]:
    """The app's own check of one answer (BedrockGuardrail, grounding + query). Returns (request, response)."""
    with recording(client) as rec:
        BedrockGuardrail(gid, version).check(entry["pre_guardrail"], grounding=policy, query=entry["question"])
    if len(rec.calls) != 1:
        raise RuntimeError(f"{entry['id']}: expected one ApplyGuardrail call, saw {len(rec.calls)}")
    return rec.calls[0]


def usd(usage: dict) -> float:
    return sum((usage.get(k) or 0) * p / 1000 for k, p in PRICE_PER_1K.items())


def summarize(resp: dict) -> dict:
    """The parts of one ApplyGuardrail response the calibration reads. Missing assessments, topics, filters or
    scores are tolerated: action defaults to NONE and an absent score is None."""
    assessments = resp.get("assessments") or []
    topics = [{"name": t.get("name"), "action": t.get("action", "NONE"), "detected": t.get("detected")}
              for a in assessments for t in (a.get("topicPolicy") or {}).get("topics") or []]
    filters = [{"type": f.get("type"), "score": f.get("score"), "threshold": f.get("threshold"),
                "action": f.get("action", "NONE"), "detected": f.get("detected")}
               for a in assessments for f in (a.get("contextualGroundingPolicy") or {}).get("filters") or []]
    usage = {k: v for k, v in (resp.get("usage") or {}).items() if isinstance(v, (int, float)) and v}
    return {"action": resp.get("action") or "NONE", "topics": topics, "filters": filters, "usage": usage,
            "usd": round(usd(usage), 6)}


def score(row: dict, kind: str) -> float | None:
    """The row's score for one filter type (the lowest if the response held more than one)."""
    got = [f["score"] for f in row["filters"] if f["type"] == kind and isinstance(f["score"], (int, float))]
    return min(got) if got else None


def blocked_by(row: dict, kind: str) -> bool:
    return any(f["type"] == kind and f["action"] == "BLOCKED" for f in row["filters"])


def topic_hits(row: dict) -> list[dict]:
    return [t for t in row["topics"] if t["detected"] or t["action"] == "BLOCKED"]


def current_threshold(rows: list[dict], kind: str) -> float | None:
    seen = {f["threshold"] for r in rows for f in r["filters"] if f["type"] == kind and f["threshold"] is not None}
    return seen.pop() if len(seen) == 1 else None


# ------------------------------------------------------------------ the frozen formula and the forecast

def floor_step(x: Decimal) -> Decimal:
    return (x / STEP).to_integral_value(rounding=ROUND_FLOOR) * STEP


@dataclass
class Calibration:
    threshold: float | None  # None: STOP, no threshold change
    first: float  # min(CAP, floor_step(min(S) - MARGIN)) over every score
    lowest: str  # id of the lowest score
    excluded: str | None = None  # the single lowest, when the recompute ran
    second: float | None = None  # the recompute without it


def calibrate(scores: dict[str, float]) -> Calibration:
    """T = min(0.75, floor_to_0.05(min(S) - 0.05)). If T < 0.40, recompute without the single lowest score; if still
    below 0.40, STOP (threshold None). Scores are taken at their printed decimal value (no float drift)."""
    if not scores:
        raise ValueError("no scores to calibrate")

    def t(values) -> Decimal:
        return min(CAP, floor_step(min(Decimal(str(v)) for v in values) - MARGIN))

    lowest = min(scores, key=lambda k: (scores[k], k))
    first = t(scores.values())
    if first >= FLOOR:
        return Calibration(float(first), float(first), lowest)
    rest = [v for k, v in scores.items() if k != lowest]
    if not rest:
        return Calibration(None, float(first), lowest, lowest)
    second = t(rest)
    return Calibration(float(second) if second >= FLOOR else None, float(first), lowest, lowest, float(second))


def outcome_at(value: float | None, threshold: float | None) -> str:
    """A contextual grounding filter blocks a score below its threshold."""
    if value is None or threshold is None:
        return "n/a"
    return "block" if Decimal(str(value)) < Decimal(str(threshold)) else "pass"


def scores_of(rows: list[dict], kind: str) -> tuple[dict[str, float], list[str]]:
    """({id: score} for the rows that have one, [ids of the rows that do not])."""
    got = {r["id"]: score(r, kind) for r in rows}
    return {k: v for k, v in got.items() if v is not None}, [k for k, v in got.items() if v is None]


def plan(rows: list[dict]) -> dict:
    """Apply the frozen rule to a complete replay: GROUNDING T from the should-answer scores; RELEVANCE recalibrated
    the same way only if a RELEVANCE filter blocked one of the should-answer entries the baseline recorded as
    blocked, else kept at its current threshold."""
    sa = [r for r in rows if r["category"] == SHOULD_ANSWER]
    grounding, missing = scores_of(sa, "GROUNDING")
    out: dict = {"missing": missing, "relevance_blocked": [r["id"] for r in sa if r["baseline"] == "blocked"
                                                           and blocked_by(r, "RELEVANCE")]}
    out["grounding"] = calibrate(grounding) if sa and not missing else None
    out["relevance"] = None
    if out["relevance_blocked"]:
        relevance, missing = scores_of(sa, "RELEVANCE")
        out["missing"] += missing
        out["relevance"] = None if missing else calibrate(relevance)
    out["T"] = out["grounding"].threshold if out["grounding"] else None
    out["R"] = (out["relevance"].threshold if out["relevance"] else None) if out["relevance_blocked"] \
        else current_threshold(rows, "RELEVANCE")
    for r in rows:
        r["grounding_at_T"] = outcome_at(score(r, "GROUNDING"), out["T"])
        r["relevance_at_R"] = outcome_at(score(r, "RELEVANCE"), out["R"])
    # Should-refuse entries the grounding filter would no longer block at T: only the topic (or relevance) is left.
    out["topic_dependent"] = [r["id"] for r in rows if r["category"] == SHOULD_REFUSE and r["grounding_at_T"] == "pass"]
    return out


# ------------------------------------------------------------------ output

def fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def render(rows: list[dict]) -> list[str]:
    """The per-entry report: topic hit, every contextual grounding filter, overall action, cost."""
    lines = [f"{'id':<15}{'category':<14}{'topic hit':<34}{'filters (type score/threshold action detected)':<64}"
             f"{'action':<22}usd"]
    for r in rows:
        hits = ", ".join(f"{t['name']}:{t['action']}" for t in topic_hits(r)) or "-"
        filters = "; ".join(f"{f['type']} {fmt(f['score'])}/{fmt(f['threshold'])} {f['action']} "
                            f"{'Y' if f['detected'] else 'N'}" for f in r["filters"]) or "-"
        lines.append(f"{r['id']:<15}{r['category']:<14}{hits:<34}{filters:<64}{r['action']:<22}{r['usd']:.6f}")
    return lines


def render_plan(rows: list[dict], p: dict) -> list[str]:
    lines = []
    g = p["grounding"]
    if p["missing"]:
        lines.append(f"NO THRESHOLD: no score for {', '.join(p['missing'])} (the formula needs all of them)")
    if g is not None:
        s_sa, _ = scores_of([r for r in rows if r["category"] == SHOULD_ANSWER], "GROUNDING")
        low = s_sa[g.lowest]
        lines.append(f"GROUNDING: S_sa = {len(s_sa)} should-answer scores, min {low:.2f} ({g.lowest}); "
                     f"T = min(0.75, floor_0.05({low:.2f} - 0.05)) = {g.first:.2f}")
        if g.excluded:
            lines.append(f"  T < 0.40: recomputed without {g.excluded} -> "
                         f"{'none' if g.second is None else f'{g.second:.2f}'}")
        lines.append(f"  => GROUNDING threshold {g.threshold:.2f}" if g.threshold is not None
                     else "  => STOP: still below 0.40; make no threshold change")
    rel = p["relevance"]
    if not p["relevance_blocked"]:
        lines.append(f"RELEVANCE: no RELEVANCE block on a baseline-blocked should-answer entry; stays at "
                     f"{fmt(p['R'])}")
    elif rel is not None:
        lines.append(f"RELEVANCE: blocked {', '.join(p['relevance_blocked'])}; same formula -> first {rel.first:.2f}"
                     + (f", recomputed without {rel.excluded}" if rel.excluded else "")
                     + (f" => {rel.threshold:.2f}" if rel.threshold is not None else " => STOP"))
    if p["T"] is None:
        return lines
    lines += ["", f"forecast at GROUNDING {p['T']:.2f}, RELEVANCE {fmt(p['R'])} (topic column: this version's "
              f"topic, as replayed)",
              f"{'id':<15}{'category':<14}{'topic hit':<10}{'GROUNDING':<11}{'RELEVANCE':<11}{'action':<22}"
              f"{'grounding@T':<13}relevance@R"]
    for r in rows:
        lines.append(f"{r['id']:<15}{r['category']:<14}{'yes' if topic_hits(r) else 'no':<10}"
                     f"{fmt(score(r, 'GROUNDING')):<11}{fmt(score(r, 'RELEVANCE')):<11}{r['action']:<22}"
                     f"{r['grounding_at_T']:<13}{r['relevance_at_R']}")
    sa = [r for r in rows if r["category"] == SHOULD_ANSWER]
    sr = [r for r in rows if r["category"] == SHOULD_REFUSE]
    lines += ["",
              f"should-answer passing GROUNDING at T: {sum(r['grounding_at_T'] == 'pass' for r in sa)}/{len(sa)}; "
              f"passing RELEVANCE at R: {sum(r['relevance_at_R'] == 'pass' for r in sa)}/{len(sa)}; "
              f"this version's topic hit: {', '.join(r['id'] for r in sa if topic_hits(r)) or 'none'}",
              f"should-refuse still blocked by GROUNDING at T: {sum(r['grounding_at_T'] == 'block' for r in sr)}/"
              f"{len(sr)}"]
    dep = [r for r in sr if r["grounding_at_T"] == "pass"]
    lines.append(f"TOPIC-DEPENDENT at T ({len(dep)}): grounding would no longer block"
                 + ("" if dep else ": none"))
    for r in dep:
        lines.append(f"  {r['id']:<15}this version's topic {'hit' if topic_hits(r) else 'NO hit'}, "
                     f"RELEVANCE@R {r['relevance_at_R']}")
    return lines


def write_raw(raw_dir: Path, gid: str, rows: list[dict], raws: list[dict]) -> None:
    """Scrubbed copies (evals writer: ARNs, account ids and key material redacted), guardrail id masked."""
    def masked(obj):
        return json.loads(json.dumps(obj).replace(gid, "[GUARDRAIL_ID]"))

    evals_run.write_json(raw_dir / "responses.json", masked(raws))
    evals_run.write_json(raw_dir / "rows.json", masked(rows))


def error_code(exc: BaseException) -> str:
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code") or type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", help="guardrail version (default GUARDRAIL_VERSION; DRAFT only when named)")
    ap.add_argument("--bundle", type=Path, default=BASELINE, help="eval bundle with outputs/refusals.json")
    ap.add_argument("--raw-out", type=Path, help="directory for the scrubbed raw responses (outside the repo or "
                                                 "git-ignored; new or empty)")
    args = ap.parse_args(argv)
    try:
        gid, version = target(args.version)
        bundle = args.bundle.resolve()
        entries = load_entries(bundle, BASELINE_DIGEST if bundle == BASELINE.resolve() else None)
        raw_dir = check_raw_out(args.raw_out)
    except Refused as exc:
        print(f"refusing: {exc}")
        return 2
    policy = POLICY.read_text()
    print(f"guardrail version {version}{' (DRAFT, asked for by name)' if version == 'DRAFT' else ''} (id from "
          f"GUARDRAIL_ID), source OUTPUT, outputScope FULL; bundle {bundle.name} "
          f"(sha256 {evals_run.digest(bundle)}), {len(entries)} entries")
    client = ai._runtime()
    rows: list[dict] = []
    raws: list[dict] = []
    try:
        for e in entries:
            try:
                _, resp = check_one(e, policy, client, gid, version)
            except Exception as exc:  # noqa: BLE001 - report the error code only, never the raw response
                print(f"{e['id']}: error {error_code(exc)}; stopped at the first error, not retried "
                      f"({len(rows)} calls made, est ${sum(r['usd'] for r in rows):.6f})")
                return 2
            raws.append({"id": e["id"], "response": {k: v for k, v in resp.items() if k != "ResponseMetadata"}})
            rows.append({k: e[k] for k in ("id", "category", "subcategory", "baseline")} | summarize(resp))
    finally:
        if raw_dir is not None and raws:
            try:
                write_raw(raw_dir, gid, rows, raws)
            except Refused as exc:  # the scrub left a guard-pattern match: nothing raw is kept
                print(f"raw responses not written: {exc}")
    print("\n".join(render(rows)))
    units: dict[str, int] = {}
    for r in rows:
        for k, v in r["usage"].items():
            units[k] = units.get(k, 0) + v
    print(f"\ntotal: {len(rows)} calls, units {units}, est ${sum(r['usd'] for r in rows):.6f} (usage-derived, list "
          f"price)\n")
    p = plan(rows)
    print("\n".join(render_plan(rows, p)))
    if raw_dir is not None:
        print(f"\nraw responses (scrubbed): {raw_dir}")
    return 0 if p["T"] is not None and not p["missing"] else 1


if __name__ == "__main__":
    sys.exit(main())
