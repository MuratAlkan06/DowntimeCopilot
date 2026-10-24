"""RESULTS.md generator: a PURE function of one artifact bundle (evals/runs/<run-id>/ or the newest committed
evals/baseline/<run-id>/) plus evals/NOTES.md. The timestamp and git SHA come from the bundle's run.json
(recorded at run time), never from now() or HEAD, so regenerating from the same bundle is byte-identical.

    python -m evals.report [BUNDLE] [--notes evals/NOTES.md] [--out RESULTS.md]   (`make results`)

Refuses (nonzero, nothing written) a bundle with dry-run/mock calls, /costs sums that do not reconcile, a null
cost per outage-hour, or a NOTES.md section keyed by an unknown metric.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

from evals.run import ROOT, Refused, check_costs, digest, guardrail_apply, loud

NOTES = ROOT / "evals" / "NOTES.md"
BASELINE = ROOT / "evals" / "baseline"
METRICS = ("faithfulness", "grounding", "extraction", "false_flags", "speech", "latency_cost", "outage_cost")
HEADER = "<!-- GENERATED - DO NOT EDIT. `make results` regenerates this file from the bundle named below. -->"


def pct(k: int, n: int, zero: str = "n/a (0 measured)") -> str:
    """'83.3% (5/6)'; a zero denominator is never a number."""
    return f"{100 * k / n:.1f}% ({k}/{n})" if n else zero


def _norm(v) -> str | None:
    return None if v is None else " ".join(str(v).lower().split())


def words(text: str | None) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()


def word_errors(ref: list[str], hyp: list[str]) -> list[tuple[str, str | None, str | None]]:
    """Minimal edit script (substitute / delete / insert) turning ref into hyp."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    ops, i, j = [], n, m
    while i or j:
        if i and j and d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            if ref[i - 1] != hyp[j - 1]:
                ops.append(("substitute", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i and d[i][j] == d[i - 1][j] + 1:
            ops.append(("delete", ref[i - 1], None))
            i -= 1
        else:
            ops.append(("insert", None, hyp[j - 1]))
            j -= 1
    return ops[::-1]


def load_notes(path: Path) -> dict[str, str]:
    """'## <metric>' sections of NOTES.md, bodies verbatim. Unknown metric keys refuse."""
    if not path.exists():
        return {}
    notes: dict[str, list[str]] = {}
    key = None
    for line in path.read_text().splitlines():
        m = re.match(r"^## (\S+)\s*$", line)
        if m:
            key = m.group(1)
            if key not in METRICS:
                raise Refused(f"NOTES.md section '{key}' is not a metric ({', '.join(METRICS)})")
            notes[key] = []
        elif key:
            notes[key].append(line)
    return {k: "\n".join(v).strip() for k, v in notes.items() if "\n".join(v).strip()}


def _load(bundle: Path, name: str):
    return json.loads((bundle / name).read_text())


def _in_range(name: str, value, ranges: dict) -> bool:
    if name not in ranges or value is None:
        return True
    try:
        lo, hi = ranges[name]
        return lo <= float(value) <= hi
    except ValueError:
        return True


# ------------------------------------------------------------------ metrics (pure)

def faithfulness(bundle: Path) -> dict:
    from app.ai.packets import SECTIONS, faithful, is_omission, resolve
    from contracts import SbarItem

    snaps = {p["patient_id"]: p for p in _load(bundle, "outputs/snapshot.json")}
    passes = _load(bundle, "outputs/packet_passes.json")
    first_k = first_n = final_k = final_n = omitted = regenerated = unparseable = 0
    misses = []
    for pk in _load(bundle, "outputs/packets.json"):
        pid, snap = pk["patient_id"], snaps[pk["patient_id"]]
        runs = passes.get(pid, [])
        regenerated += len(runs) > 1
        unparseable += any("unparseable" in r for r in pk["guardrail"]["reasons"])
        if runs:
            kept, rejected = faithful(runs[0], snap)
            k = sum(len(v) for v in kept.values())
            first_k, first_n = first_k + k, first_n + k + len(rejected)
            misses += [{"patient": pid, "pass": "first", **r} for r in rejected]
        for sec in SECTIONS:
            for item in pk["sbar"][sec]:
                final_n += 1
                if is_omission(SbarItem(**item)):
                    omitted += 1
                    misses.append({"patient": pid, "pass": "final (omitted)", "section": sec, "text": item["text"],
                                   "source_path": item["source_path"]})
                elif resolve(snap, item["source_path"]):
                    final_k += 1
                else:
                    misses.append({"patient": pid, "pass": "final", "section": sec, **item})
    return {"packets": len(snaps), "first": (first_k, first_n), "final": (final_k, final_n), "omitted": omitted,
            "regenerated": regenerated, "unparseable": unparseable, "misses": misses}


def grounding(bundle: Path, manifest: dict) -> dict:
    rows = []
    for q, ans in zip(manifest["questions"], _load(bundle, "outputs/policy.json")):
        exp = q["expected_section"]
        cited = [c["section_id"] for c in ans["citations"]]
        hit = ans["grounded"] and any(c == exp or c.startswith(exp + ".") for c in cited)
        rows.append({"question": q["question"], "expected": exp, "cited": cited, "grounded": ans["grounded"],
                     "guardrail": ans["guardrail"]["status"], "correct": hit})
    return {"rows": rows, "grounded": sum(r["grounded"] for r in rows), "correct": sum(r["correct"] for r in rows),
            "n": len(rows)}


def extraction(bundle: Path, meta: dict) -> dict:
    ranges = meta["vital_ranges"]
    per = {"nova": [0, 0], "textract": [0, 0], "combined": [0, 0]}
    rows, misses = [], []
    clean = false_flags = caught = silent = 0
    for cap in _load(bundle, "outputs/captures.json"):
        stem, rec = cap["fixture"], cap["record"]
        truth = meta["truth"][stem]["fields"]
        reads = {"nova": rec["reads"]["bedrock"], "textract": rec["reads"]["textract"]}
        merged = {f["name"]: f for f in rec["fields"]}
        row = {"fixture": stem}
        for reader, out in [*reads.items(), ("combined", {"fields": {k: f["value"] for k, f in merged.items()}})]:
            if out is None:
                row[reader] = None
                continue
            hits = 0
            for name, want in truth.items():
                got = out["fields"].get(name)
                if _norm(got) == _norm(want):
                    hits += 1
                else:
                    misses.append({"fixture": stem, "reader": reader, "field": name, "expected": want, "got": got})
            row[reader] = (hits, len(truth))
            per[reader][0] += hits
            per[reader][1] += len(truth)
        for name, want in truth.items():
            f = merged.get(name, {})
            right = _norm(f.get("value")) == _norm(want)
            if want is not None and _in_range(name, want, ranges):  # clean: legible on paper and in range
                clean += 1
                false_flags += bool(f.get("flag")) and right
            caught += bool(f.get("flag")) and not right
            silent += not f.get("flag") and not right
        rows.append(row)
    return {"rows": rows, "per": per, "clean": clean, "false_flags": false_flags, "caught": caught,
            "silent": silent, "misses": misses}


def speech(bundle: Path, meta: dict) -> dict:
    sp = _load(bundle, "outputs/speech.json")
    truth = meta["truth"][sp["fixture"]]
    ref, hyp = words(truth["transcript"]), words((sp.get("transcribe") or {}).get("text"))
    errs = word_errors(ref, hyp)
    structured = sp["record"]["reads"]["bedrock"] or {"fields": {}}
    s_misses = [{"field": k, "expected": v, "got": structured["fields"].get(k)}
                for k, v in truth["fields"].items() if _norm(structured["fields"].get(k)) != _norm(v)]
    return {"outcome": (sp.get("transcribe") or {}).get("outcome"), "n": len(ref),
            "correct": max(len(ref) - len(errs), 0), "errors": errs, "fields_n": len(truth["fields"]),
            "fields_k": len(truth["fields"]) - len(s_misses), "field_misses": s_misses}


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


# ------------------------------------------------------------------ render

def render(bundle: Path, notes_path: Path = NOTES) -> str:
    meta, costs = _load(bundle, "run.json"), _load(bundle, "costs.json")
    by_job = check_costs(costs)
    notes = load_notes(notes_path)
    manifest = meta["manifest"]
    fa, gr, ex, sp = faithfulness(bundle), grounding(bundle, manifest), extraction(bundle, meta), speech(bundle, meta)
    lat = {j: statistics.median([c["latency_ms"] for c in costs["calls"] if c["job"] == j]) for j in by_job}
    label = bundle.resolve().relative_to(ROOT).as_posix() if bundle.resolve().is_relative_to(ROOT) else bundle.name
    syn = "synthetic fixtures"
    L: list[str] = []
    add = L.append

    def note(key: str) -> None:
        if key in notes:
            add("")
            add(f"Notes (evals/NOTES.md, `{key}`):")
            add("")
            add(notes[key])

    add(HEADER)
    add("# RESULTS")
    add("")
    add("GENERATED - DO NOT EDIT. Every number below is computed from one recorded live run (the bundle named here); "
        "commentary comes verbatim from `evals/NOTES.md`.")
    add("")
    add(f"- Run id: `{meta['run_id']}`")
    add(f"- Run started / finished (UTC, recorded at run time): {meta['started_at']} / {meta['finished_at']}")
    add(f"- Git SHA at run time: `{meta['git_sha']}`{' (working tree dirty)' if meta['git_dirty'] else ''}")
    add(f"- Artifact bundle: `{label}/`")
    add(f"- Artifact digest (sha256 over the bundle, `evals.run.digest`): `{digest(bundle)}`")
    add(f"- Inputs fixed before the run (`evals/manifest.json`): {manifest['seed_patients']} seeded patients, "
        f"{len(manifest['questions'])} DEMO.md policy questions, form fixtures: {len(manifest['form_fixtures'])}, "
        f"speech fixtures: 1, simulated clock +{manifest['clock_advance_minutes']['before_downtime']} min then "
        f"+{manifest['clock_advance_minutes']['before_costs']} min")
    add("")
    add("## Headline")
    add("")
    add("| Metric | Value |")
    add("|---|---|")
    add(f"| Packet faithfulness, final lines with a resolving source_path | {pct(*fa['final'])} |")
    add(f"| Packet faithfulness, first-pass model lines | {pct(*fa['first'])} |")
    add(f"| Packet omissions (unverifiable lines withheld) | {fa['omitted']} |")
    add(f"| Policy grounding rate | {pct(gr['grounded'], gr['n'])} |")
    add(f"| Policy expected-section correctness | {pct(gr['correct'], gr['n'])} |")
    for reader, name in (("nova", "Nova 2 Lite vision"), ("textract", "Textract"), ("combined", "combined (agreement)")):
        add(f"| Form field accuracy, {name} ({syn}) | {pct(*ex['per'][reader])} |")
    add(f"| False-flag rate on clean fields ({syn}) | {pct(ex['false_flags'], ex['clean'], 'n/a (0 clean fields)')} |")
    add(f"| Speech transcription word accuracy (1 {syn[:-1]}) | {pct(sp['correct'], sp['n'])} |")
    add(f"| Model spend, this run (`/costs`, Bedrock only) | ${costs['total_usd']:.4f} |")
    add(f"| Cost per simulated outage-hour | ${costs['usd_per_outage_hour']:.4f} "
        f"({costs['outage_hours']} simulated h) |")
    add("")
    add(f"Photographed forms: {meta['photographed_fixtures']} fixtures — pending")
    add("")
    add("## Paths that ran")
    add("")
    routes = meta["paths"]["routes"]
    for job in sorted(by_job):
        models = sorted({c["model_id"] for c in costs["calls"] if c["job"] == job})
        add(f"- `{job}`: {', '.join(models)} (route {routes.get(job, '?')})")
    add(f"- Textract second read: {'on' if meta['paths']['textract_enabled'] else 'off'}; "
        f"guardrail: {meta['paths']['guardrail_mode']}; transcription: {sp['outcome']}; "
        f"region {meta['paths']['aws_region']}")
    add("")
    add("## Services (from docs/decisions.md at run time)")
    add("")
    add("| Service | Probe |")
    add("|---|---|")
    for s in meta["services"]:
        add(f"| {s['service']} | {s['probe']} |")
    add("")
    add("## 1. Packet faithfulness")
    add("")
    add(f"Packets: {fa['packets']}. Final: {pct(*fa['final'])} of shown lines resolve to a snapshot path, "
        f"{fa['omitted']} omitted. First pass: {pct(*fa['first'])}. Regenerated once: {fa['regenerated']}; "
        f"unparseable model output: {fa['unparseable']}.")
    note("faithfulness")
    add("")
    add("## 2. Policy grounding")
    add("")
    add("| # | Question | Expected | Cited | Grounded | Guardrail | Expected section |")
    add("|---|---|---|---|---|---|---|")
    for i, r in enumerate(gr["rows"], 1):
        add(f"| {i} | {r['question']} | {r['expected']} | {', '.join(r['cited']) or '-'} | "
            f"{'yes' if r['grounded'] else 'no'} | {r['guardrail']} | {'yes' if r['correct'] else 'no'} |")
    add("")
    add(f"Grounding rate {pct(gr['grounded'], gr['n'])}; expected-section correctness {pct(gr['correct'], gr['n'])}.")
    note("grounding")
    add("")
    add(f"## 3. Form extraction ({syn})")
    add("")
    add("| Fixture | Nova 2 Lite | Textract | Combined |")
    add("|---|---|---|---|")
    for r in ex["rows"]:
        cells = [pct(*r[k]) if r.get(k) else "n/a (reader did not run)" for k in ("nova", "textract", "combined")]
        add(f"| {r['fixture']} | {' | '.join(cells)} |")
    add(f"| **All ({syn})** | {pct(*ex['per']['nova'])} | {pct(*ex['per']['textract'])} | "
        f"{pct(*ex['per']['combined'])} |")
    add("")
    add(f"Photographed forms: {meta['photographed_fixtures']} fixtures — pending")
    note("extraction")
    add("")
    add(f"## 4. Flags on clean fields ({syn})")
    add("")
    add(f"Clean field: legible on paper (ground truth not blank) and inside the unit's normal range. False flag: a "
        f"clean field flagged for review although the combined value was right. False-flag rate "
        f"{pct(ex['false_flags'], ex['clean'], 'n/a (0 clean fields)')}. Flags that caught a wrong value: "
        f"{ex['caught']}. Wrong values with no flag (silent errors): {ex['silent']}.")
    note("false_flags")
    add("")
    add("## 5. Speech transcription (1 synthetic fixture)")
    add("")
    add(f"Word accuracy {pct(sp['correct'], sp['n'])} against the fixture transcript (lowercase, punctuation "
        f"ignored; accuracy = 1 - (substitutions + deletions + insertions) / reference words). Verbal-order "
        f"structuring from the live transcript: {pct(sp['fields_k'], sp['fields_n'])} fields.")
    note("speech")
    add("")
    add("## 6. Latency and cost per call type (`/costs`)")
    add("")
    add("| Call type | Calls | Median latency (ms) | Tokens in / out | USD |")
    add("|---|---|---|---|---|")
    for job in sorted(by_job):
        j = by_job[job]
        add(f"| {job} | {j['calls']} | {_num(lat[job])} | {j['tokens_in']} / {j['tokens_out']} | {j['usd']:.6f} |")
    add(f"| **Total** | {len(costs['calls'])} | {_num(statistics.median([c['latency_ms'] for c in costs['calls']]))} | "
        f"{costs['tokens_in']} / {costs['tokens_out']} | {costs['total_usd']:.6f} |")
    add("")
    um = meta["unmetered"]
    # Derived from the bundle, not run.json's tally (the first baseline's tally counted packets; see NOTES.md).
    apply_n = guardrail_apply(meta["paths"]["guardrail_mode"], len(_load(bundle, "outputs/policy.json")))
    add(f"Per-call-type sums equal the `/costs` totals (checked at run time and again here). Not metered by the app, "
        f"so not in these sums: Textract AnalyzeDocument {um['textract_analyze_document']} page(s), Transcribe "
        f"{um['transcribe_jobs']} job(s), ApplyGuardrail {apply_n} check(s); see AWS billing. Budget "
        f"guard: limit ${meta['budget']['limit_usd']:.2f}, max projected ${meta['budget']['max_projected_usd']:.4f}.")
    note("latency_cost")
    add("")
    add("## 7. Cost per simulated outage-hour")
    add("")
    add(f"${costs['usd_per_outage_hour']:.4f} per simulated outage-hour: ${costs['total_usd']:.6f} of model calls over "
        f"{costs['outage_hours']} simulated hours (outage declared after +"
        f"{manifest['clock_advance_minutes']['before_downtime']} min, `/costs` read after +"
        f"{manifest['clock_advance_minutes']['before_costs']} min).")
    note("outage_cost")
    add("")
    add("## FAILURES")
    add("")
    add("Every miss in this run.")
    add("")
    add("Packet lines (first pass rejected, or omitted / unresolved in the final packet):")
    add("")
    for m in fa["misses"]:
        add(f"- {m['patient']} {m['pass']} [{m['section']}] source_path `{m['source_path']}`: {m['text']}")
    if not fa["misses"]:
        add("- none")
    add("")
    add("Policy questions without a grounded answer from the expected section:")
    add("")
    for r in gr["rows"]:
        if not r["correct"]:
            add(f"- \"{r['question']}\": expected {r['expected']}, got {', '.join(r['cited']) or 'no citation'} "
                f"(grounded {'yes' if r['grounded'] else 'no'}, guardrail {r['guardrail']})")
    if gr["correct"] == gr["n"]:
        add("- none")
    add("")
    add(f"Form fields ({syn}; field, expected, got):")
    add("")
    for m in ex["misses"]:
        add(f"- {m['fixture']} / {m['reader']} / `{m['field']}`: expected {json.dumps(m['expected'])}, "
            f"got {json.dumps(m['got'])}")
    if not ex["misses"]:
        add("- none")
    add("")
    add("Speech words (operation, expected, got):")
    add("")
    for op, want, got in sp["errors"]:
        add(f"- {op}: expected {json.dumps(want)}, got {json.dumps(got)}")
    if not sp["errors"]:
        add("- none")
    add("")
    add("Verbal-order structured fields (field, expected, got):")
    add("")
    for m in sp["field_misses"]:
        add(f"- `{m['field']}`: expected {json.dumps(m['expected'])}, got {json.dumps(m['got'])}")
    if not sp["field_misses"]:
        add("- none")
    add("")
    return "\n".join(L)


def latest_baseline() -> Path:
    """The newest committed bundle. Run ids start with the UTC start time, so they sort chronologically;
    older bundles stay under evals/baseline/ as the history of record."""
    found = sorted(p for p in BASELINE.glob("*") if p.is_dir()) if BASELINE.exists() else []
    if not found:
        raise Refused(f"no bundle under {BASELINE}; pass BUNDLE explicitly")
    return found[-1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle", type=Path, nargs="?")
    ap.add_argument("--notes", type=Path, default=NOTES)
    ap.add_argument("--out", type=Path, default=ROOT / "RESULTS.md")
    args = ap.parse_args(argv)
    try:
        text = render(args.bundle or latest_baseline(), args.notes)
    except Refused as exc:
        loud(str(exc))
        return 3
    args.out.write_text(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
