"""`make evals`: ONE live run of the fixed inputs in evals/manifest.json through the app, written to an
artifact bundle evals/runs/<run-id>/ (git-ignored): run.json (manifest, ground truth, run-time git SHA and
timestamps, paths that ran, the ALLOWED/DENIED table from docs/decisions.md), outputs/*.json (raw packets,
faithfulness passes, policy answers, captured records, transcript) and costs.json (the GET /costs dump).
Every file is scrubbed with the tools/guard.sh patterns before it is written. RESULTS.md is then generated
from the bundle alone (evals/report.py).

The app runs in-process (FastAPI TestClient on app.main:app, no lifespan: no scheduler, no sync worker, so
nothing is mirrored to the cloud) on a scratch STORE_PATH that must start empty, so /costs holds ONLY this
run's calls. The simulator is the real one at SIM_URL. Two observation hooks record what packets.faithful
and transcribe.transcribe saw and returned; they never change a result.

Refuses (exit nonzero, loud, no bundle, no RESULTS.md): DRY_RUN=1, missing fixtures, TEXTRACT_ENABLED off,
STORE_PATH unset or non-empty, simulator unreachable, a dry-run/mock call in the run, a projected spend over
the manifest budget (checked before and after every step), or a /costs inconsistency.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "evals" / "manifest.json"
FIXTURES = ROOT / "data" / "forms" / "fixtures"
PHOTO_FIXTURES = ROOT / "evals" / "fixtures"
RUNS = ROOT / "evals" / "runs"
IMAGE_EXT = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png"}
ROLE = {"X-Role": "charge_nurse"}
LIVE_TRANSCRIBE = frozenset({"transcribed"})  # X-Transcribe outcomes that count as a live transcript

# Same patterns as tools/guard.sh (credentials, ARNs with an account segment, 12-digit account ids).
SCRUB: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"(?i)aws_secret_access_key *= *[A-Za-z0-9/+=]{40}"), "aws_secret_access_key=[REDACTED]"),
    (re.compile(r"arn:aws:[^\s\"']*"), "arn:aws:[REDACTED]"),
    (re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])"), "[REDACTED]"),
)
GUARD_PATTERNS = (re.compile(r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"),
                  re.compile(r"(?i)aws_secret_access_key *= *[A-Za-z0-9/+=]{40}"),
                  re.compile(r"arn:aws:[^ ]*:[0-9]{12}|(^|[^0-9])[0-9]{12}([^0-9]|$)", re.M))


class Refused(RuntimeError):
    """A precondition or consistency check failed: nothing is written."""


class BudgetExceeded(Refused):
    pass


def loud(msg: str) -> None:
    bar = "!" * 72
    print(f"\n{bar}\nEVALS NOT RUN: {msg}\nRESULTS.md was NOT written. No numbers were produced.\n{bar}\n", file=sys.stderr)


# ------------------------------------------------------------------ fixed inputs

def load_manifest(path: Path = MANIFEST, fixtures: Path = FIXTURES) -> dict:
    """The manifest fixes the inputs before the run. Raises Refused if any input is missing."""
    m = json.loads(path.read_text())
    if not m.get("questions") or any(not q.get("question") or not q.get("expected_section") for q in m["questions"]):
        raise Refused("manifest questions need question + expected_section")
    missing = [s for s in m["form_fixtures"] if not (fixtures / f"{s}.json").exists()
               or not any((fixtures / f"{s}{ext}").exists() for ext in IMAGE_EXT)]
    sp = m["speech_fixture"]["stem"]
    if not (fixtures / f"{sp}.json").exists() or not (fixtures / f"{sp}.wav").exists():
        missing.append(sp)
    if missing:
        raise Refused(f"fixtures missing in {fixtures}: {', '.join(missing)} (need <stem>.jpg|png|wav + <stem>.json)")
    return m


def image_of(fixtures: Path, stem: str) -> Path:
    return next(fixtures / f"{stem}{ext}" for ext in IMAGE_EXT if (fixtures / f"{stem}{ext}").exists())


def find_pairs(fixtures: Path) -> list[tuple[Path, Path]]:
    """Photographed-form fixtures (evals/fixtures): image + ground truth with the same stem."""
    return [(img, img.with_suffix(".json")) for img in sorted(fixtures.glob("*"))
            if img.suffix.lower() in IMAGE_EXT and img.with_suffix(".json").exists()]


def services_table(decisions: Path = ROOT / "docs" / "decisions.md") -> list[dict]:
    """The first '| Service | Probe |' table in docs/decisions.md: [{service, probe}] (copied into the bundle)."""
    rows, inside = [], False
    for line in decisions.read_text().splitlines():
        if line.startswith("| Service | Probe"):
            inside = True
            continue
        if inside:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and not set(cells[0]) <= {"-", ":"}:
                rows.append({"service": cells[0], "probe": cells[1]})
    return rows


# ------------------------------------------------------------------ scoring hook (S8), kept for callers

def _norm(v) -> str | None:
    return None if v is None else " ".join(str(v).lower().split())


def accuracy(predicted: dict, truth: dict) -> tuple[int, int]:
    hits = sum(_norm(predicted.get(k)) == _norm(v) for k, v in truth.items())
    return hits, len(truth)


def score_reads(textract, bedrock, truth: dict) -> dict:
    """Per-reader scoring hook: field accuracy of reads.textract and reads.bedrock against ground truth,
    of the merged record (agreement.merge), and the agreement split over the merged fields.
    A reader that did not run (None) scores None, never 0/0 counted as measured."""
    from app.capture.agreement import merge

    merged = merge(textract, bedrock)
    split = {"agree": 0, "disagree": 0, "single": 0}
    for f in merged:
        split[f.agreement.value] += 1
    return {
        "textract": accuracy(textract.fields, truth) if textract is not None else None,
        "bedrock": accuracy(bedrock.fields, truth) if bedrock is not None else None,
        "combined": accuracy({f.name: f.value for f in merged}, truth),
        "agreement": split,
    }


# ------------------------------------------------------------------ budget guard

class BudgetGuard:
    """Projected spend = spent so far (metered + unmetered allowances) + the estimates of the steps not yet run.
    Checked before the first step and after every step; over the limit -> BudgetExceeded (the run aborts)."""

    def __init__(self, limit_usd: float, step_estimates: dict[str, float]):
        self.limit = float(limit_usd)
        self.remaining = dict(step_estimates)
        self.max_projected = 0.0

    def check(self, spent: float, done: str | None = None) -> float:
        if done is not None:
            self.remaining.pop(done, None)
        projected = spent + sum(self.remaining.values())
        self.max_projected = max(self.max_projected, projected)
        if projected > self.limit:
            raise BudgetExceeded(f"projected spend ${projected:.4f} > budget ${self.limit:.2f} "
                                 f"(spent ${spent:.4f}; steps left: {', '.join(self.remaining) or 'none'})")
        return projected


def execute(steps: list[tuple[str, Callable[[], None]]], guard: BudgetGuard, spent: Callable[[], float]) -> None:
    """Run the named steps in order; the guard sees the cumulative spend before the first and after each."""
    guard.check(spent())
    for name, fn in steps:
        fn()
        guard.check(spent(), done=name)


# ------------------------------------------------------------------ bundle

def scrub(text: str) -> str:
    for pat, repl in SCRUB:
        text = pat.sub(repl, text)
    return text


def guard_hits(text: str) -> int:
    return sum(len(p.findall(text)) for p in GUARD_PATTERNS)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = scrub(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    if guard_hits(text):
        raise Refused(f"scrub left guard-pattern matches in {path.name}")
    path.write_text(text)


def digest(bundle: Path) -> str:
    """sha256 over every file in the bundle: sorted relative path, NUL, file sha256, newline."""
    h = hashlib.sha256()
    for f in sorted(p for p in bundle.rglob("*") if p.is_file()):
        rel = f.relative_to(bundle).as_posix()
        h.update(f"{rel}\0{hashlib.sha256(f.read_bytes()).hexdigest()}\n".encode())
    return h.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


# ------------------------------------------------------------------ live run

def preflight(fixtures: Path) -> dict:
    """Every refusal that needs no network. Returns the manifest."""
    from app import config
    if config.dry_run():
        raise Refused("DRY_RUN=1 would measure canned mocks, not Bedrock/Textract/Transcribe. "
                      "Run with live credentials and DRY_RUN=0.")
    manifest = load_manifest(fixtures=fixtures)
    if not config.textract_enabled():
        raise Refused("TEXTRACT_ENABLED is off; the manifest measures both readers (set TEXTRACT_ENABLED=1)")
    if not os.environ.get("STORE_PATH", "").strip():
        raise Refused("STORE_PATH is unset; point it at a fresh scratch sqlite file (the run's /costs must hold "
                      "only eval calls)")
    return manifest


class Hooks:
    """Observation only: records each packets.faithful input (per patient, per pass) and each transcript."""

    def __init__(self):
        self.lock = threading.Lock()
        self.passes: dict[str, list[dict]] = {}
        self.transcripts: list[dict] = []
        self.textract_calls = 0

    @contextlib.contextmanager
    def installed(self):
        from app.ai import packets
        from app.capture import textract, transcribe
        orig_faithful, orig_tr, orig_tx = packets.faithful, transcribe.transcribe, textract.read_with_boxes

        def faithful(sbar_raw, snapshot):
            with self.lock:
                self.passes.setdefault(str(snapshot.get("patient_id")), []).append(json.loads(json.dumps(sbar_raw)))
            return orig_faithful(sbar_raw, snapshot)

        def heard(audio, content_type, typed=None):
            res = orig_tr(audio, content_type, typed=typed)
            with self.lock:
                self.transcripts.append({"outcome": res.outcome, "text": res.text, "seconds": res.seconds,
                                         "detail": res.detail})
            return res

        def second_read(data, form_type):
            with self.lock:
                self.textract_calls += 1
            return orig_tx(data, form_type)

        packets.faithful, transcribe.transcribe, textract.read_with_boxes = faithful, heard, second_read
        try:
            yield self
        finally:
            packets.faithful, transcribe.transcribe, textract.read_with_boxes = orig_faithful, orig_tr, orig_tx


def check_costs(costs: dict) -> dict[str, dict]:
    """Per-call-type sums must equal the /costs totals; cost per outage-hour must be non-null."""
    by_job: dict[str, dict] = {}
    for c in costs["calls"]:
        j = by_job.setdefault(c["job"], {"calls": 0, "usd": 0.0, "tokens_in": 0, "tokens_out": 0})
        j["calls"] += 1
        j["usd"] += c["usd"]
        j["tokens_in"] += c["tokens_in"]
        j["tokens_out"] += c["tokens_out"]
    if round(sum(j["usd"] for j in by_job.values()), 6) != round(costs["total_usd"], 6):
        raise Refused(f"per-call-type USD sum != /costs total_usd ({costs['total_usd']})")
    if sum(j["tokens_in"] for j in by_job.values()) != costs["tokens_in"] or \
            sum(j["tokens_out"] for j in by_job.values()) != costs["tokens_out"]:
        raise Refused("per-call-type token sums != /costs totals")
    if costs.get("usd_per_outage_hour") is None:
        raise Refused("/costs usd_per_outage_hour is null (no outage started, or zero simulated hours)")
    if costs.get("dry_run") or any(c["dry_run"] or str(c["model_id"]).startswith("dry-run") for c in costs["calls"]):
        raise Refused("a dry-run call is in /costs; canned mocks are not measurements")
    return by_job


def live_run(fixtures: Path, runs: Path) -> Path:
    """One live run of the manifest -> a scrubbed bundle under `runs`. Returns the bundle path."""
    manifest = preflight(fixtures)
    os.environ["SNAPSHOT_INTERVAL_SECONDS"] = "0"
    os.environ["SYNC_INTERVAL_SECONDS"] = "0"
    from fastapi.testclient import TestClient

    from app import config, sim_client
    from app.ai.client import ROUTES
    from app.store import get_store, reset_store

    reset_store()
    store = get_store()
    if store.list_costs() or store.list_audit(1) or store.list_records() or store.list_packets():
        raise Refused(f"store {store.path} is not empty; the run's /costs must hold only eval calls")
    sim = sim_client.make_client()
    try:
        sim.post("/reset").raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any simulator failure is a refusal
        raise Refused(f"simulator unreachable at {config.sim_url()}: {exc}") from None

    from app.main import app
    api = TestClient(app, headers=ROLE)
    started = datetime.now(timezone.utc)
    sha = git("rev-parse", "HEAD")
    run_id = f"{started:%Y%m%dT%H%M%SZ}-{sha[:7] or 'nogit'}"
    budget = manifest["budget"]
    allow = budget["unmetered_allowance_usd"]
    hooks = Hooks()
    out: dict = {}

    def spent() -> float:
        metered = sum(c["usd"] for c in store.list_costs())
        checks = len(out.get("packets", [])) + len(out.get("policy", []))
        unmetered = (hooks.textract_calls * allow["textract_page"] + len(hooks.transcripts) * allow["transcribe_job"]
                     + (checks * allow["guardrail_check"] if config.guardrail_mode() == "bedrock" else 0))
        return metered + unmetered

    def advance(minutes: int) -> None:
        sim.post("/clock/advance", json={"minutes": minutes}).raise_for_status()

    def packets_step() -> None:
        pulled = api.post("/snapshot/pull")
        pulled.raise_for_status()
        if len(pulled.json()) != manifest["seed_patients"]:
            raise Refused(f"seed gave {len(pulled.json())} patients, manifest fixes {manifest['seed_patients']}")
        out["snapshot"] = pulled.json()
        advance(manifest["clock_advance_minutes"]["before_downtime"])
        r = api.post("/downtime/start", json={"simulate_outage": True})
        r.raise_for_status()
        out["packets"] = r.json()

    def policy_step() -> None:
        out["policy"] = []
        for q in manifest["questions"]:
            r = api.post("/policy/ask", json={"question": q["question"]})
            r.raise_for_status()
            out["policy"].append(r.json())

    def forms_step() -> None:
        out["captures"] = []
        for stem in manifest["form_fixtures"]:
            img = image_of(fixtures, stem)
            ft = json.loads((fixtures / f"{stem}.json").read_text())["form_type"]
            r = api.post("/capture", data={"form_type": ft},
                         files={"file": (img.name, img.read_bytes(), f"image/{IMAGE_EXT[img.suffix.lower()]}")})
            r.raise_for_status()
            out["captures"].append({"fixture": stem, "record": r.json()})

    def speech_step() -> None:
        sp = manifest["speech_fixture"]
        wav = fixtures / f"{sp['stem']}.wav"
        r = api.post("/capture", data={"form_type": "verbal_order", "patient_id": sp["patient_id"]},
                     files={"file": (wav.name, wav.read_bytes(), "audio/wav")})
        r.raise_for_status()
        out["speech"] = {"fixture": sp["stem"], "x_transcribe": r.headers.get("X-Transcribe"),
                         "record": r.json(), "transcribe": hooks.transcripts[-1] if hooks.transcripts else None}
        if out["speech"]["x_transcribe"] not in LIVE_TRANSCRIBE:
            raise Refused(f"speech fixture was not transcribed live (X-Transcribe: {out['speech']['x_transcribe']})")

    guard = BudgetGuard(budget["limit_usd"], budget["step_estimates_usd"])
    with hooks.installed():
        execute([("packets", packets_step), ("policy", policy_step), ("forms", forms_step),
                 ("speech", speech_step)], guard, spent)
    advance(manifest["clock_advance_minutes"]["before_costs"])
    costs = api.get("/costs").json()
    check_costs(costs)
    finished = datetime.now(timezone.utc)

    truth = {s: json.loads((fixtures / f"{s}.json").read_text())
             for s in [*manifest["form_fixtures"], manifest["speech_fixture"]["stem"]]}
    run_meta = {
        "run_id": run_id, "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"), "git_sha": sha,
        "git_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "manifest": manifest, "truth": truth, "vital_ranges": {k: list(v) for k, v in config.vital_ranges().items()},
        "services": services_table(), "photographed_fixtures": len(find_pairs(PHOTO_FIXTURES)),
        "paths": {"routes": dict(ROUTES), "textract_enabled": config.textract_enabled(),
                  "guardrail_mode": config.guardrail_mode(), "aws_region": config.aws_region()},
        "budget": {"limit_usd": budget["limit_usd"], "max_projected_usd": round(guard.max_projected, 6),
                   "spent_with_allowances_usd": round(spent(), 6)},
        "unmetered": {"textract_analyze_document": hooks.textract_calls, "transcribe_jobs": len(hooks.transcripts),
                      "guardrail_apply": (len(out["packets"]) + len(out["policy"]))
                      if config.guardrail_mode() == "bedrock" else 0},
    }
    bundle = runs / run_id
    write_json(bundle / "run.json", run_meta)
    write_json(bundle / "costs.json", costs)
    write_json(bundle / "outputs" / "snapshot.json", out["snapshot"])
    write_json(bundle / "outputs" / "packets.json", out["packets"])
    write_json(bundle / "outputs" / "packet_passes.json", hooks.passes)
    write_json(bundle / "outputs" / "policy.json", out["policy"])
    write_json(bundle / "outputs" / "captures.json", out["captures"])
    write_json(bundle / "outputs" / "speech.json", out["speech"])
    return bundle


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixtures", type=Path, default=FIXTURES, help="synthetic form + speech fixtures")
    ap.add_argument("--runs", type=Path, default=RUNS, help="where the run bundle is written (git-ignored)")
    ap.add_argument("--out", type=Path, default=ROOT / "RESULTS.md")
    args = ap.parse_args(argv)
    try:
        bundle = live_run(args.fixtures, args.runs)
    except Refused as exc:
        loud(str(exc))
        return 4 if isinstance(exc, BudgetExceeded) else 3
    print(f"bundle {bundle} (sha256 {digest(bundle)})")
    from evals import report
    try:
        text = report.render(bundle)
    except Refused as exc:
        loud(f"{exc} (the bundle stays at {bundle})")
        return 3
    args.out.write_text(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
