"""Model comparison harness (#76): form reading, SBAR packets and policy answers on Nova 2 Lite, Claude Haiku 4.5 and
Claude Sonnet 4.6 with identical inputs, scored with the evals metrics plus latency and cost per job.

  python -m tools.model_compare --out RUN_DIR [--models nova,haiku,sonnet] [--budget 5.00]   # DRY_RUN mocks only
  python -m tools.model_compare --out RUN_DIR --live [--models ...] [--budget 5.00]          # real Bedrock calls

Frozen design: issue #76. One pass per model, cheapest first, each on a fresh, empty scratch store, in-process
(FastAPI TestClient on app.main:app without its lifespan: no scheduler, no sync worker), PACKET_CONCURRENCY=1. Each
job section runs under app.ai.client.route_override({job: model}), so the job never falls back to Haiku. Per pass:
simulator POST /reset -> /snapshot/pull (12 patients) -> clock +15 min -> /downtime/start; /policy/ask per manifest
question; forms.read_form_with_boxes on the 39 fixture images (3 synthetic + 36 photographed: the model input of
/capture, no Textract call); clock +45 min and /costs before the next pass. Form reads are re-merged with the fixed
Textract reads of the pinned baseline bundle (BASELINE, BASELINE_DIGEST) and scored by the evals scorers.

A pass whose overridden-job rows are not all the requested model, whose checker rows are not all Nova, or that hits
a call failure surviving one same-model retry (transient errors only) is discarded and rerun once on a fresh store;
a second failure marks the pass failed. Discarded spend is still counted. Writes RUN_DIR/report.json + report.md
(generated only) and RUN_DIR/passes/<model>/attempt-<n>/ (the raw outputs every metric is computed from), all
scrubbed with the evals writers.

Without --live, DRY_RUN=1 is forced: canned mocks, zero network, and the report says so on top.
Refuses (exit 3, nothing written): a non-empty RUN_DIR, an unknown or unpriced model, missing fixture images or an
image that fails its sha256, a baseline bundle that does not match its pinned digest or lacks a Textract read for any
of the 39 fixtures, truth or vital ranges that differ from the baseline's, an unreachable simulator (SIM_URL, as for
`make evals`); with --live also DRY_RUN=1, no published guardrail or a failed 1-token probe. Budget (exit 4): the
projected spend over --budget before any call (nothing written) or before any later step (the report is written,
marked incomplete). Any pass not published leaves the report incomplete (exit 3).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # `python tools/model_compare.py` as well as `python -m tools.model_compare`
    sys.path.insert(0, str(ROOT))

from app.ai import client as ai  # noqa: E402
from evals import report as evals_report  # noqa: E402
from evals import run as evals_run  # noqa: E402
from evals.run import BudgetExceeded, Refused  # noqa: E402

MODELS: dict[str, str] = {"nova": ai.NOVA_2_LITE, "haiku": ai.HAIKU_4_5, "sonnet": ai.SONNET_4_6}  # cheapest first
NAMES = {"nova": "Nova 2 Lite", "haiku": "Claude Haiku 4.5", "sonnet": "Claude Sonnet 4.6"}
STEPS = (("packets", "packet"), ("policy", "policy"), ("forms", "form_read"))  # run order; forms (costliest) last
CHECKER_MODEL = ai.NOVA_2_LITE  # the packet checker stays Nova in every pass (never overridden)
ROLE = "charge_nurse"  # evals_run.ROLE
PRODUCTION_PACKET_CONCURRENCY = 4  # app.config.packet_concurrency() default; disclosed next to the measured 1

# The one committed bundle with reads.textract for all 39 fixtures (3 synthetic in captures.json, the 36
# photographed in photographed.json), pinned by evals.run.digest.
BASELINE = ROOT / "evals" / "baseline" / "20261028T060034Z-609f90e"
BASELINE_DIGEST = "137d625b84c377968f726d05183782112c845922ad8a9c97412113de6a2e6150"
PRICING_VERIFIED = "2026-10-24"  # app/ai/client.py PRICING

# Bedrock Runtime Converse errors worth one same-model retry (installed botocore service model: 429/408/500/503),
# plus botocore connection and HTTP client errors. Anything else propagates at once.
TRANSIENT_CODES = frozenset({"ThrottlingException", "ModelTimeoutException", "InternalServerException",
                             "ServiceUnavailableException", "ModelNotReadyException"})

# Budget projection only (spend is metered from /costs): conservative per-call token envelopes (input, output).
# The pinned baseline's /costs averages ~960/800 per packet call, ~2,170/390 per policy call, ~840/25 per checker
# call and ~580/490 per Nova form read; form_read is sized for a Claude image (up to ~1,600 tokens) plus the prompt.
EST_TOKENS: dict[str, tuple[int, int]] = {"form_read": (2_500, 1_000), "packet": (2_000, 1_600),
                                          "policy": (3_000, 800), "checker": (1_500, 100)}

RULE = ("A route changes only via its own slice, and only if: forms (primary dataset: the photographed set, "
        "combined-with-fixed-Textract accuracy): candidate strictly better by >=3 fields, no worse on silent errors, "
        "p50 model latency <=1.5x incumbent; or equal-within-3-fields AND >=40% cheaper per job with the same guards. "
        "Packets (n=12) and policy (n=5) are UNDER-POWERED: this run may recommend but never auto-trigger a "
        "packet/policy route change; that requires the owner's recorded sign-off.")
COUNTERFACTUAL = ("\"all-Sonnet\" = form_read + packet + policy on Sonnet; the Nova checker and ApplyGuardrail are "
                  "identical in both arms and excluded from the difference; stated on the fixed eval workload, not "
                  "production.")
HEADER = "<!-- GENERATED - DO NOT EDIT. tools/model_compare.py writes this file from report.json. -->"


def loud(msg: str) -> None:
    bar = "!" * 72
    print(f"\n{bar}\nMODEL COMPARISON NOT RUN: {msg}\n{bar}\n", file=sys.stderr)


# ------------------------------------------------------------------ fixed inputs

def load_baseline(manifest: dict, bundle: Path | None = None, pinned: str | None = None) -> dict:
    """The pinned baseline: digest, a Textract read for every manifest fixture, truth equal to the committed truth
    (form_type + fields), and the vital ranges its records were merged under. Pure, offline (committed files only)."""
    from app import config
    bundle = BASELINE if bundle is None else bundle
    pinned = BASELINE_DIGEST if pinned is None else pinned
    if not (bundle / "run.json").is_file():
        raise Refused(f"baseline bundle {bundle.name} not found under evals/baseline/")
    if (got := evals_run.digest(bundle)) != pinned:
        raise Refused(f"baseline bundle {bundle.name} digest {got} != pinned {pinned}")
    meta = json.loads((bundle / "run.json").read_text())
    sets = {"synthetic": (manifest["form_fixtures"], "captures.json", meta.get("truth", {}), evals_run.FIXTURES),
            "photographed": (manifest.get("photographed_fixtures", []), "photographed.json",
                             meta.get("photographed_truth", {}), evals_run.PHOTO_FIXTURES)}
    out: dict = {"id": bundle.name, "digest": pinned, "textract": {}, "form_type": {}, "truth": {},
                 "vital_ranges": meta["vital_ranges"]}
    for name, (stems, file, truth, folder) in sets.items():
        caps = json.loads((bundle / "outputs" / file).read_text()) if (bundle / "outputs" / file).exists() else []
        if [c["fixture"] for c in caps] != list(stems):
            raise Refused(f"baseline {bundle.name} outputs/{file} fixtures != the manifest's {name} set")
        out["truth"][name] = {}
        for c in caps:
            stem, tx = c["fixture"], c["record"]["reads"]["textract"]
            if tx is None:
                raise Refused(f"baseline {bundle.name}: no Textract read for {stem}")
            committed = json.loads((folder / f"{stem}.json").read_text())
            if stem not in truth or {k: truth[stem].get(k) for k in ("form_type", "fields")} != \
                    {k: committed.get(k) for k in ("form_type", "fields")}:
                raise Refused(f"baseline {bundle.name}: truth for {stem} differs from the committed {folder.name}/"
                              f"{stem}.json")
            out["textract"][stem], out["form_type"][stem] = tx, committed["form_type"]
            out["truth"][name][stem] = {"fields": committed["fields"]}
    ranges = {k: list(v) for k, v in config.vital_ranges().items()}
    if ranges != {k: list(v) for k, v in meta["vital_ranges"].items()}:
        raise Refused(f"vital ranges {ranges} differ from the baseline's {meta['vital_ranges']} (VITAL_RANGE_*)")
    return out


def preflight(live: bool, keys: list[str]) -> tuple[dict, dict]:
    """Every refusal before any model call. Returns (manifest, baseline)."""
    from app import config
    manifest = evals_run.load_manifest(fixtures=evals_run.FIXTURES)  # synthetic images + declared photographed truth
    evals_run.check_photographed_images(manifest.get("photographed_fixtures", []), evals_run.PHOTO_FIXTURES)
    if missing := sorted({MODELS[k] for k in keys} - set(ai.PRICING)):
        raise Refused(f"PRICING has no rate for {', '.join(missing)}")
    if live:
        if config.dry_run():
            raise Refused("--live with DRY_RUN=1 would measure canned mocks; unset DRY_RUN")
        if config.guardrail_published() is None:
            raise Refused("no published guardrail (GUARDRAIL_ID + GUARDRAIL_VERSION); every pass must run the "
                          "same published guardrail")
    return manifest, load_baseline(manifest)


def step_estimates(keys: list[str], manifest: dict, mode: str, allowance: float) -> dict[str, float]:
    """Projected USD per step '<model>:<step>': every packet regenerated once, one checker call per packet (and per
    policy answer in checker mode), one ApplyGuardrail allowance per policy answer in bedrock mode."""
    n_forms = len(manifest["form_fixtures"]) + len(manifest.get("photographed_fixtures", []))
    n_pk, n_q = manifest["seed_patients"], len(manifest["questions"])

    def usd(model: str, job: str, calls: int) -> float:
        return ai.estimate_usd(model, *EST_TOKENS[job]) * calls

    out = {}
    for k in keys:
        m = MODELS[k]
        out[f"{k}:packets"] = usd(m, "packet", 2 * n_pk) + usd(CHECKER_MODEL, "checker", n_pk)
        out[f"{k}:policy"] = usd(m, "policy", n_q) + (n_q * allowance if mode == "bedrock"
                                                       else usd(CHECKER_MODEL, "checker", n_q))
        out[f"{k}:forms"] = usd(m, "form_read", n_forms)
    return {k: round(v, 6) for k, v in out.items()}


# ------------------------------------------------------------------ hooks (observation and one retry)

def transient(exc: BaseException) -> bool:
    from botocore.exceptions import ClientError, ConnectionError, HTTPClientError
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") in TRANSIENT_CODES
    return isinstance(exc, (ConnectionError, HTTPClientError))


@contextlib.contextmanager
def retry_once(log: list[str]):
    """One same-model retry of a transient Bedrock error (app.ai.client._converse, every job). Never another model."""
    orig = ai._converse

    def converse(model_id, *args, **kw):
        try:
            return orig(model_id, *args, **kw)
        except Exception as exc:  # noqa: BLE001 - classified below; anything else propagates unchanged
            if not transient(exc):
                raise
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")
            log.append(f"{model_id}: {code or type(exc).__name__}")
            return orig(model_id, *args, **kw)

    ai._converse = converse
    try:
        yield
    finally:
        ai._converse = orig


@contextlib.contextmanager
def packet_timer(rows: list[dict]):
    """Per-packet wall-clock around packets.generate_packet (model call, faithfulness check, the regeneration if
    any, the checker call). Observation only; the Hooks precedent in evals.run."""
    from app.ai import packets
    orig, lock = packets.generate_packet, threading.Lock()

    def timed(p, alerts, now, actor_role="system"):
        t0 = time.perf_counter()
        out = orig(p, alerts, now, actor_role=actor_role)
        with lock:
            rows.append({"patient_id": p.patient_id, "wall_ms": _ms(t0)})
        return out

    packets.generate_packet = timed
    try:
        yield
    finally:
        packets.generate_packet = orig


@contextlib.contextmanager
def scoped_env(keys: tuple[str, ...]):
    """Restore `keys` in os.environ on exit (the harness sets them per pass)."""
    old = {k: os.environ.get(k) for k in keys}
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ------------------------------------------------------------------ checks (pure)

def pass_problems(rows: list[dict], model: str, live: bool, counts: dict[str, int]) -> list[str]:
    """Why a pass's /costs rows cannot be published (empty: clean). `counts`: forms, packets, questions run.
    Every row of an overridden job is the requested model; every checker row is Nova; no dry-run row in a live pass
    (no live row in a dry run); the call counts match the work done."""
    want = model if live else f"dry-run:{model}"
    checker = CHECKER_MODEL if live else f"dry-run:{CHECKER_MODEL}"
    problems = []
    for _, job in STEPS:
        if bad := sorted({r["model_id"] for r in rows if r["job"] == job} - {want}):
            problems.append(f"contaminated: {job} rows from {', '.join(bad)} (requested {want})")
    if bad := sorted({r["model_id"] for r in rows if r["job"] == "checker"} - {checker}):
        problems.append(f"checker not constant: checker rows from {', '.join(bad)} (must be {checker})")
    if other := sorted({r["job"] for r in rows} - {job for _, job in STEPS} - {"checker"}):
        problems.append(f"unexpected job rows: {', '.join(other)}")
    if live and (leak := [r for r in rows if r["dry_run"] or str(r["model_id"]).startswith("dry-run")]):
        problems.append(f"dry-run leak: {len(leak)} dry-run row(s) in a live pass")
    if not live and (leak := [r for r in rows if not r["dry_run"]]):
        problems.append(f"live leak: {len(leak)} live row(s) in a dry run")
    n = {job: sum(r["job"] == job for r in rows) for job in ("form_read", "packet", "policy", "checker")}
    if n["form_read"] != counts["forms"]:
        problems.append(f"{n['form_read']} form_read row(s) for {counts['forms']} form reads")
    if n["policy"] != counts["questions"]:
        problems.append(f"{n['policy']} policy row(s) for {counts['questions']} questions")
    if not counts["packets"] <= n["packet"] <= 2 * counts["packets"]:
        problems.append(f"{n['packet']} packet row(s) for {counts['packets']} packets (one regeneration each at most)")
    if live and n["checker"] < counts["packets"]:
        problems.append(f"checker ran {n['checker']} time(s) for {counts['packets']} packets")
    return problems


def identical(blobs: dict[str, bytes]) -> bool:
    """True when every pass saw the same bytes (the three passes' snapshot JSON)."""
    return len(set(blobs.values())) <= 1


# ------------------------------------------------------------------ one pass

class Context:
    """Run-wide state: mode, inputs, the budget guard and the running spend (finished attempts + the current one)."""

    def __init__(self, live: bool, keys: list[str], manifest: dict, baseline: dict, budget: float):
        from app import config
        self.live, self.keys, self.manifest, self.baseline = live, keys, manifest, baseline
        self.mode = config.guardrail_mode()
        self.allowance = manifest["budget"]["unmetered_allowance_usd"]["guardrail_check"]
        self.estimates = step_estimates(keys, manifest, self.mode, self.allowance)
        self.guard = evals_run.BudgetGuard(budget, self.estimates)
        self.closed_usd = self.probe_usd = 0.0
        self.closed_checks = self.answers = 0
        self.store = None
        self.attempts: list[dict] = []
        self.stopped: str | None = None
        self.started = datetime.now(timezone.utc)

    def guardrail_checks(self, answers: int) -> int:
        """Unmetered ApplyGuardrail calls (evals.run.guardrail_apply); none in DRY_RUN (the adapter never calls it)."""
        return evals_run.guardrail_apply(self.mode, answers) if self.live else 0

    def spent(self) -> float:
        metered = sum(c["usd"] for c in self.store.list_costs()) if self.store is not None else 0.0
        checks = self.closed_checks + self.guardrail_checks(self.answers)
        return self.closed_usd + metered + checks * self.allowance + self.probe_usd


def form_record(stem: str, set_name: str, form_type: str, read, textract: dict) -> dict:
    """The model read re-merged with the baseline's fixed Textract read (agreement.merge), in the captured-record
    shape evals.report._score_captures scores."""
    from app.capture.agreement import merge
    from contracts import ReaderOutput

    tx = ReaderOutput.model_validate(textract)
    return {"fixture": stem, "set": set_name,
            "record": {"form_type": form_type,
                       "reads": {"bedrock": read.model_dump(mode="json"), "textract": tx.model_dump(mode="json")},
                       "fields": [f.model_dump(mode="json") for f in merge(tx, read)]}}


def run_attempt(ctx: Context, key: str, attempt: int, scratch: Path, out: Path) -> dict:
    """One pass of one model on a fresh store. Never raises for a model/protocol failure (the attempt is discarded
    with its reason); BudgetExceeded propagates after the attempt's spend is closed."""
    from fastapi.testclient import TestClient

    from app import config, sim_client
    from app.ai import forms
    from app.main import app
    from app.store import get_store, reset_store
    from contracts import FormType, ReaderOutput

    model, m = MODELS[key], ctx.manifest
    rel = f"passes/{key}/attempt-{attempt}"
    gid, version = config.guardrail_published() or (None, None)
    att = {"model": key, "model_id": model, "attempt": attempt, "dir": rel, "status": "ok", "reasons": [],
           "retries": [], "guardrail": {"mode": config.guardrail_mode(), "version": version},
           "_guardrail_id": gid}  # the id is compared across passes, never written
    os.environ["STORE_PATH"] = str(scratch / f"{key}-{attempt}.sqlite")
    reset_store()
    store = ctx.store = get_store()
    if store.list_costs() or store.list_audit(1) or store.list_records() or store.list_packets():
        raise Refused(f"scratch store {store.path} is not empty")
    ctx.answers = 0
    api = TestClient(app, headers=evals_run.ROLE)  # no `with`: no lifespan, no scheduler or sync worker
    sim = sim_client.make_client()
    hooks, packet_ms = evals_run.Hooks(), []
    out_: dict = {"forms": []}
    timings: dict = {"packets": packet_ms, "policy": [], "forms": []}

    def packets_step() -> None:
        sim.post("/reset").raise_for_status()
        pulled = api.post("/snapshot/pull")
        pulled.raise_for_status()
        if len(pulled.json()) != m["seed_patients"]:
            raise RuntimeError(f"snapshot has {len(pulled.json())} patients, the manifest fixes {m['seed_patients']}")
        out_["snapshot"] = pulled.json()
        sim.post("/clock/advance", json={"minutes": m["clock_advance_minutes"]["before_downtime"]}).raise_for_status()
        r = api.post("/downtime/start", json={"simulate_outage": True})
        r.raise_for_status()
        out_["packets"] = r.json()

    def policy_step() -> None:
        out_["policy"] = []
        for i, q in enumerate(m["questions"], 1):
            t0 = time.perf_counter()
            r = api.post("/policy/ask", json={"question": q["question"]})
            r.raise_for_status()
            timings["policy"].append({"question": i, "wall_ms": _ms(t0)})
            out_["policy"].append(r.json())
            ctx.answers += 1

    def forms_step() -> None:
        for set_name, stems, folder in (("synthetic", m["form_fixtures"], evals_run.FIXTURES),
                                        ("photographed", m.get("photographed_fixtures", []),
                                         evals_run.PHOTO_FIXTURES)):
            for stem in stems:
                img = evals_run.image_of(folder, stem)
                ft = ctx.baseline["form_type"][stem]
                before = len(store.list_costs())
                t0 = time.perf_counter()
                unparseable = None
                try:
                    read, _ = forms.read_form_with_boxes(img.read_bytes(), evals_run.IMAGE_EXT[img.suffix.lower()],
                                                         FormType(ft), actor_role=ROLE)
                except Exception as exc:  # noqa: BLE001 - a reply that came back but did not parse is a result
                    if len(store.list_costs()) == before:
                        raise  # the call itself failed: a pass failure
                    read, unparseable = ReaderOutput(), f"{type(exc).__name__}: {exc}"
                wall = _ms(t0)
                rows = store.list_costs()[before:]
                out_["forms"].append(form_record(stem, set_name, ft, read, ctx.baseline["textract"][stem])
                                     | {"unparseable": unparseable})
                timings["forms"].append({"fixture": stem, "set": set_name, "wall_ms": wall,
                                         "model_ms": [r["latency_ms"] for r in rows],
                                         "usd": round(sum(r["usd"] for r in rows), 6)})

    step, costs = "setup", None
    try:
        with hooks.installed(), packet_timer(packet_ms), retry_once(att["retries"]):
            for (step, job), fn in zip(STEPS, (packets_step, policy_step, forms_step)):
                ctx.guard.check(ctx.spent())
                with ai.route_override({job: model}):
                    fn()
                ctx.guard.check(ctx.spent(), done=f"{key}:{step}")
            step = "costs"
            sim.post("/clock/advance", json={"minutes": m["clock_advance_minutes"]["before_costs"]}).raise_for_status()
            costs = api.get("/costs").json()
    except BudgetExceeded as exc:
        att["status"], att["reasons"] = "stopped", [f"{step}: {exc}"]
        raise
    except Exception as exc:  # noqa: BLE001 - any failure discards the pass (rerun once on a fresh store)
        att["status"], att["reasons"] = "discarded", [f"{step}: {type(exc).__name__}: {exc}"]
    finally:
        rows = store.list_costs()
        att["calls"] = len(rows)
        att["metered_usd"] = round(sum(r["usd"] for r in rows), 6)
        att["guardrail_apply"] = ctx.guardrail_checks(ctx.answers)
        ctx.closed_usd += att["metered_usd"]
        ctx.closed_checks += att["guardrail_apply"]
        ctx.store, ctx.answers = None, 0
        ctx.attempts.append(att)
        _write_attempt(out / rel, out_, hooks, timings, costs if costs is not None else {"calls": rows})
    if att["status"] == "ok":
        counts = {"forms": len(out_["forms"]), "packets": len(out_.get("packets", [])),
                  "questions": len(out_.get("policy", []))}
        att["reasons"] = pass_problems(costs["calls"], model, ctx.live, counts)
        if hooks.textract_calls:
            att["reasons"].append(f"{hooks.textract_calls} Textract call(s); the harness reads no form through it")
        if ctx.live:
            try:
                evals_run.check_costs(costs)
            except Refused as exc:
                att["reasons"].append(str(exc))
        if att["reasons"]:
            att["status"] = "discarded"
    return att


def _write_attempt(adir: Path, out_: dict, hooks, timings: dict, costs: dict) -> None:
    w = evals_run.write_json
    for name in ("snapshot", "packets", "policy", "forms"):
        if name in out_:
            w(adir / "outputs" / f"{name}.json", out_[name])
    if "packets" in out_:
        w(adir / "outputs" / "packet_passes.json", hooks.passes)
    w(adir / "timings.json", timings)
    w(adir / "costs.json", costs)


def run_pass(ctx: Context, key: str, scratch: Path, out: Path) -> None:
    """The pass of one model: one attempt, rerun once on a fresh store if it is discarded."""
    steps = [f"{key}:{s}" for s, _ in STEPS]
    for attempt in (1, 2):
        if attempt == 2:
            ctx.guard.remaining.update({s: ctx.estimates[s] for s in steps})  # the rerun is projected in full
        if run_attempt(ctx, key, attempt, scratch, out)["status"] == "ok":
            return
    for s in steps:  # failed twice: its estimates no longer belong to the projection
        ctx.guard.remaining.pop(s, None)


# ------------------------------------------------------------------ scoring (pure, from the written files)

def _pct_pair(k: int, n: int) -> list[int]:
    return [int(k), int(n)]


def lat(xs: list[int | float]) -> dict:
    """p50 = median (as RESULTS.md); p95 = nearest rank, ceil(0.95 n) (the maximum for n <= 20)."""
    s = sorted(xs)
    if not s:
        return {"n": 0, "p50": None, "p95": None}
    return {"n": len(s), "p50": statistics.median(s), "p95": s[math.ceil(0.95 * len(s)) - 1]}


def score_attempt(adir: Path, manifest: dict, baseline: dict) -> dict:
    """Every metric of one published pass, recomputed from its written files with the evals scorers."""
    fa = evals_report.faithfulness(adir)
    gr = evals_report.grounding(adir, manifest)
    forms = json.loads((adir / "outputs" / "forms.json").read_text())
    timings = json.loads((adir / "timings.json").read_text())
    calls = json.loads((adir / "costs.json").read_text())["calls"]
    rows = {job: [c for c in calls if c["job"] == job] for job in ("packet", "policy", "checker")}
    out: dict = {"forms": {}}
    for set_name in ("photographed", "synthetic"):
        caps = [c for c in forms if c["set"] == set_name]
        x = evals_report._score_captures(caps, baseline["truth"][set_name], baseline["vital_ranges"])
        t = [r for r in timings["forms"] if r["set"] == set_name]
        usd = round(sum(r["usd"] for r in t), 6)
        out["forms"][set_name] = {
            "n": len(caps), "per": {"model": x["per"]["nova"], "textract": x["per"]["textract"],
                                    "combined": x["per"]["combined"]},
            "clean": x["clean"], "false_flags": x["false_flags"], "caught": x["caught"], "silent": x["silent"],
            "misses": len(x["misses"]), "unparseable": sum(bool(c.get("unparseable")) for c in caps),
            "latency_ms": {"model_call": lat([v for r in t for v in r["model_ms"]]),
                           "wall_clock": lat([r["wall_ms"] for r in t])},
            "usd": usd, "usd_per_job": round(usd / len(caps), 6) if caps else None}
    pk_usd = round(sum(c["usd"] for c in rows["packet"]), 6)
    out["packets"] = {
        "n": fa["packets"], "first": _pct_pair(*fa["first"]), "final": _pct_pair(*fa["final"]),
        "omitted": fa["omitted"], "regenerated": fa["regenerated"], "unparseable": fa["unparseable"],
        "misses": len(fa["misses"]),
        "latency_ms": {"model_call": lat([c["latency_ms"] for c in rows["packet"]]),
                       "wall_clock": lat([r["wall_ms"] for r in timings["packets"]])},
        "usd": pk_usd, "usd_per_job": round(pk_usd / fa["packets"], 6) if fa["packets"] else None,
        "checker": {"calls": len(rows["checker"]), "models": sorted({c["model_id"] for c in rows["checker"]}),
                    "usd": round(sum(c["usd"] for c in rows["checker"]), 6),
                    "latency_ms": lat([c["latency_ms"] for c in rows["checker"]])}}
    po_usd = round(sum(c["usd"] for c in rows["policy"]), 6)
    out["policy"] = {
        "n": gr["n"], "grounded": gr["grounded"], "correct": gr["correct"],
        "interventions": sum(r["guardrail"] == "intervened" for r in gr["rows"]),
        "rows": [{k: r[k] for k in ("expected", "cited", "grounded", "guardrail", "correct")} for r in gr["rows"]],
        "latency_ms": {"model_call": lat([c["latency_ms"] for c in rows["policy"]]),
                       "wall_clock": lat([r["wall_ms"] for r in timings["policy"]])},
        "usd": po_usd, "usd_per_job": round(po_usd / gr["n"], 6) if gr["n"] else None}
    return out


def job_usd(metrics: dict) -> dict[str, float]:
    """Metered USD of the three compared jobs on the fixed workload (checker and ApplyGuardrail excluded)."""
    f = metrics["forms"]
    return {"form_read": round(f["photographed"]["usd"] + f["synthetic"]["usd"], 6),
            "packet": metrics["packets"]["usd"], "policy": metrics["policy"]["usd"]}


def counterfactual(metrics: dict[str, dict]) -> dict:
    """Routed (app ROUTES) vs all-Sonnet on the fixed workload, from the measured passes (pre-registered, #76)."""
    by_model = {MODELS[k]: k for k in metrics}
    jobs = [job for _, job in STEPS]
    need = {ai.ROUTES[j] for j in jobs} | {ai.SONNET_4_6}
    if missing := sorted(need - set(by_model)):
        return {"available": False, "reason": f"no published pass for {', '.join(missing)}"}
    routed = {j: {"model": ai.ROUTES[j], "usd": job_usd(metrics[by_model[ai.ROUTES[j]]])[j]} for j in jobs}
    sonnet = job_usd(metrics[by_model[ai.SONNET_4_6]])
    r, s = round(sum(v["usd"] for v in routed.values()), 6), round(sum(sonnet.values()), 6)
    return {"available": True, "routed": routed, "routed_usd": r, "all_sonnet": sonnet, "all_sonnet_usd": s,
            "difference_usd": round(s - r, 6), "routed_share": round(r / s, 4) if s else None}


def rule_inputs(metrics: dict[str, dict]) -> list[dict]:
    """The measured inputs the pre-registered forms rule names, per candidate vs the incumbent route (photographed
    set). Numbers only: the verdict slots stay empty in this harness (S1 of #76)."""
    inc = next((k for k in metrics if MODELS[k] == ai.ROUTES["form_read"]), None)
    if inc is None:
        return []
    i = metrics[inc]["forms"]["photographed"]
    out = []
    for k in metrics:
        if k == inc:
            continue
        c = metrics[k]["forms"]["photographed"]
        ip50, cp50 = i["latency_ms"]["model_call"]["p50"], c["latency_ms"]["model_call"]["p50"]
        out.append({"route": "form_read", "incumbent": inc, "candidate": k,
                    "combined_fields_delta": c["per"]["combined"][0] - i["per"]["combined"][0],
                    "silent_errors": {"incumbent": i["silent"], "candidate": c["silent"]},
                    "p50_model_call_ratio": round(cp50 / ip50, 3) if ip50 and cp50 is not None else None,
                    "usd_per_job_ratio": (round(c["usd_per_job"] / i["usd_per_job"], 3)
                                          if i["usd_per_job"] else None),
                    "verdict": None})
    return out


# ------------------------------------------------------------------ report

def build_report(out: Path, ctx: Context) -> dict:
    """report.json: run metadata from `ctx`, every metric from the written attempt files."""
    from app import config
    published = [a for a in ctx.attempts if a["status"] == "ok"]
    metrics = {a["model"]: score_attempt(out / a["dir"], ctx.manifest, ctx.baseline) for a in published}
    snaps = {a["model"]: (out / a["dir"] / "outputs" / "snapshot.json").read_bytes() for a in published}
    guardrails = {(a["guardrail"]["mode"], a["guardrail"]["version"], a["_guardrail_id"]) for a in published}
    passes = {}
    for k in ctx.keys:
        tries = [a for a in ctx.attempts if a["model"] == k]
        ok = next((a for a in tries if a["status"] == "ok"), None)
        status = "ok" if ok else ("not run" if not tries else
                                  "stopped" if tries[-1]["status"] == "stopped" else "failed")
        passes[k] = {"model_id": MODELS[k], "status": status, "published_attempt": ok["dir"] if ok else None,
                     "attempts": [{x: a[x] for x in ("attempt", "dir", "status", "reasons", "retries", "calls",
                                                       "metered_usd", "guardrail_apply", "guardrail")}
                                  for a in tries]}
    invalid = []  # the passes are not comparable: nothing in this report may be published
    if not identical(snaps):
        invalid.append("snapshots differ across passes: the passes did not see identical inputs")
    if len(guardrails) > 1:
        invalid.append("guardrail mode/id/version differs across passes")
    problems = [*invalid, *(f"{k}: pass {p['status']}" for k, p in passes.items() if p["status"] != "ok")]
    if ctx.stopped:
        problems.append(f"budget: {ctx.stopped}")
    status = "invalid" if invalid else "incomplete" if problems else "complete"
    metered = round(sum(a["metered_usd"] for a in ctx.attempts), 6)
    checks = sum(a["guardrail_apply"] for a in ctx.attempts)
    abandoned = round(sum(a["metered_usd"] + a["guardrail_apply"] * ctx.allowance
                          for a in ctx.attempts if a["status"] != "ok"), 6)
    sha = evals_run.git("rev-parse", "HEAD")
    m = ctx.manifest
    return {
        "schema": 1, "run_id": f"{ctx.started:%Y%m%dT%H%M%SZ}-{sha[:7] or 'nogit'}",
        "mode": "live" if ctx.live else "dry-run", "status": status, "problems": problems,
        "started_at": ctx.started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": sha, "git_dirty": bool(evals_run.git("status", "--porcelain", "--untracked-files=no")),
        "models": {k: MODELS[k] for k in ctx.keys}, "order": list(ctx.keys),
        "workload": {"form_reads": {"synthetic": len(m["form_fixtures"]),
                                    "photographed": len(m.get("photographed_fixtures", []))},
                     "packets": m["seed_patients"], "policy_questions": len(m["questions"]),
                     "clock_advance_minutes": m["clock_advance_minutes"]},
        "baseline": {"bundle": f"evals/baseline/{ctx.baseline['id']}", "digest": ctx.baseline["digest"],
                     "textract_reads": len(ctx.baseline["textract"]), "vital_ranges": ctx.baseline["vital_ranges"]},
        "lifecycle": {"app": "in-process TestClient, no lifespan (no scheduler, no sync worker)",
                      "store": "fresh empty scratch store per attempt", "packet_concurrency": 1,
                      "production_packet_concurrency": PRODUCTION_PACKET_CONCURRENCY,
                      "guardrail_mode": ctx.mode, "checker_model": CHECKER_MODEL,
                      "snapshots_identical": identical(snaps), "snapshots_compared": len(snaps),
                      "aws_region": config.aws_region()},
        "pricing": {"verified": PRICING_VERIFIED, "source": "app/ai/client.py PRICING (us. geographic profiles)",
                    "usd_per_1m_tokens": {mid: {"input": p[0], "output": p[1]} for mid, p in ai.PRICING.items()}},
        "budget": {"limit_usd": ctx.guard.limit, "max_projected_usd": round(ctx.guard.max_projected, 6),
                   "step_estimates_usd": ctx.estimates,
                   "spent": {"metered_usd": metered, "guardrail_apply": checks,
                             "guardrail_allowance_usd": round(checks * ctx.allowance, 6),
                             "probes_usd": round(ctx.probe_usd, 6), "abandoned_usd": abandoned,
                             "total_usd": round(metered + checks * ctx.allowance + ctx.probe_usd, 6)}},
        "passes": passes, "metrics": metrics, "counterfactual": counterfactual(metrics),
        "rule": {"text": RULE, "frozen_in": "#76", "inputs": rule_inputs(metrics),
                 "verdicts": {"form_read": None, "packet": None, "policy": None}},
        "counterfactual_rule": COUNTERFACTUAL,
    }


def _lat(x: dict) -> str:
    if not x["n"]:
        return "n/a (0 measured)"
    return f"{evals_report._num(x['p50'])} / {evals_report._num(x['p95'])} (n={x['n']})"


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:.6f}"


def render_md(r: dict) -> str:
    """report.md: a pure function of report.json."""
    pct = evals_report.pct
    L: list[str] = []
    add = L.append
    names = ", ".join(NAMES[k] for k in r["order"])
    add(HEADER)
    add(f"# Model comparison: {names}")
    add("")
    if r["mode"] == "dry-run":
        add("**DRY RUN: canned mocks, zero network. These are not measurements; never publish them.**")
        add("")
    add(f"Status: **{r['status']}**" + (f" ({'; '.join(r['problems'])})" if r["problems"] else ""))
    add("")
    w, b, lc = r["workload"], r["baseline"], r["lifecycle"]
    add(f"- Run id: `{r['run_id']}` ({r['mode']}); started / finished (UTC): {r['started_at']} / {r['finished_at']}")
    add(f"- Git SHA at run time: `{r['git_sha']}`{' (working tree dirty)' if r['git_dirty'] else ''}")
    add(f"- Fixed eval workload per model: {w['form_reads']['synthetic'] + w['form_reads']['photographed']} form "
        f"reads ({w['form_reads']['photographed']} photographed + {w['form_reads']['synthetic']} synthetic), "
        f"{w['packets']} packets, {w['policy_questions']} policy answers (`evals/manifest.json`)")
    add(f"- Forms re-merged with the fixed Textract reads of `{b['bundle']}/` (digest `{b['digest']}`, "
        f"{b['textract_reads']} reads); no Textract call in this run")
    add(f"- {lc['app']}; {lc['store']}; packets at PACKET_CONCURRENCY={lc['packet_concurrency']} (production "
        f"{lc['production_packet_concurrency']}); guardrail mode `{lc['guardrail_mode']}`; packet checker "
        f"`{lc['checker_model']}` in every pass")
    add(f"- Snapshots byte-identical across the {lc['snapshots_compared']} published pass(es): "
        f"{'yes' if lc['snapshots_identical'] else 'NO'}")
    add("")
    met = r["metrics"]
    add("## Comparison")
    add("")
    add("| Model | Job | Accuracy | Model call p50 / p95 ms (n) | Wall-clock p50 / p95 ms (n) | USD per job |")
    add("|---|---|---|---|---|---|")
    for k in r["order"]:
        if k not in met:
            add(f"| {NAMES[k]} | all | pass {r['passes'][k]['status']}: no metrics (see Spend) | - | - | - |")
            continue
        x = met[k]
        for set_name in ("photographed", "synthetic"):
            f = x["forms"][set_name]
            add(f"| {NAMES[k]} | Form reading, {set_name} ({f['n']}) | combined {pct(*f['per']['combined'])}; "
                f"model alone {pct(*f['per']['model'])} | {_lat(f['latency_ms']['model_call'])} | "
                f"{_lat(f['latency_ms']['wall_clock'])} | {_usd(f['usd_per_job'])} |")
        p = x["packets"]
        add(f"| {NAMES[k]} | SBAR packet ({p['n']}) | final {pct(*p['final'])}; first pass {pct(*p['first'])} | "
            f"{_lat(p['latency_ms']['model_call'])} | {_lat(p['latency_ms']['wall_clock'])} | "
            f"{_usd(p['usd_per_job'])} |")
        q = x["policy"]
        add(f"| {NAMES[k]} | Policy answer ({q['n']}) | expected section {pct(q['correct'], q['n'])}; grounded "
            f"{pct(q['grounded'], q['n'])} | {_lat(q['latency_ms']['model_call'])} | "
            f"{_lat(q['latency_ms']['wall_clock'])} | {_usd(q['usd_per_job'])} |")
    add("")
    add("Accuracy: forms = field accuracy against the committed truth (combined = the model read merged with the fixed "
        "Textract read, as /capture does); packets = SBAR lines whose source_path resolves in the snapshot; policy = "
        "grounded answers citing the expected section. USD per job: metered model calls of that job only (the packet "
        "checker and ApplyGuardrail are reported apart).")
    add("")
    for set_name, label in (("photographed", "photographed set (primary)"), ("synthetic", "synthetic set")):
        add(f"## Forms: {label}")
        add("")
        add("| Model | Model alone | Textract (fixed) | Combined | False flags on clean | Flags that caught a wrong "
            "value | Silent errors | Unparseable replies |")
        add("|---|---|---|---|---|---|---|---|")
        for k in r["order"]:
            if k in met:
                f = met[k]["forms"][set_name]
                add(f"| {NAMES[k]} | {pct(*f['per']['model'])} | {pct(*f['per']['textract'])} | "
                    f"{pct(*f['per']['combined'])} | {pct(f['false_flags'], f['clean'], 'n/a (0 clean fields)')} | "
                    f"{f['caught']} | {f['silent']} | {f['unparseable']} |")
        add("")
    add("An unparseable form reply is scored as a read with no fields (the model alone misses every field the truth "
        "fills; combined falls back to the Textract read) and counted above.")
    add("")
    add("## Packets")
    add("")
    add("| Model | First pass | Final | Omitted | Regenerated | Unparseable | Checker calls | Checker model | "
        "Checker USD |")
    add("|---|---|---|---|---|---|---|---|---|")
    for k in r["order"]:
        if k in met:
            p = met[k]["packets"]
            add(f"| {NAMES[k]} | {pct(*p['first'])} | {pct(*p['final'])} | {p['omitted']} | {p['regenerated']} | "
                f"{p['unparseable']} | {p['checker']['calls']} | {', '.join(p['checker']['models']) or '-'} | "
                f"{_usd(p['checker']['usd'])} |")
    add("")
    add("## Policy")
    add("")
    add("| Model | Grounded | Expected section | Guardrail interventions |")
    add("|---|---|---|---|")
    for k in r["order"]:
        if k in met:
            q = met[k]["policy"]
            add(f"| {NAMES[k]} | {pct(q['grounded'], q['n'])} | {pct(q['correct'], q['n'])} | "
                f"{q['interventions']} |")
    add("")
    add("## Latency instruments")
    add("")
    add("- Model call: `latency_ms` of each Converse call as logged in `/costs` (one call; a transient same-model "
        "retry, if any, is inside it).")
    add("- Wall-clock: per packet, `packets.generate_packet` (model call, faithfulness check, the one regeneration if "
        "any, checker call); per policy answer, `POST /policy/ask` in-process (model call, ApplyGuardrail, grounding "
        "check); per form, `forms.read_form_with_boxes` (model call and parse).")
    add(f"- Packets ran one at a time (PACKET_CONCURRENCY={lc['packet_concurrency']}; production runs "
        f"{lc['production_packet_concurrency']} in flight), so these are uncontended per-packet latencies, not the "
        f"production `/downtime/start` time.")
    add("- p50 = median; p95 = nearest rank, ceil(0.95 n): with n <= 20 the p95 is the maximum.")
    add("")
    add("## Spend")
    add("")
    add("| Model | Attempt | Status | Calls | Metered USD | ApplyGuardrail checks | Transient retries | Reasons |")
    add("|---|---|---|---|---|---|---|---|")
    for k in r["order"]:
        for a in r["passes"][k]["attempts"]:
            add(f"| {NAMES[k]} | {a['attempt']} | {a['status']} | {a['calls']} | {_usd(a['metered_usd'])} | "
                f"{a['guardrail_apply']} | {len(a['retries'])} | {'; '.join(a['reasons']) or '-'} |")
        if not r["passes"][k]["attempts"]:
            add(f"| {NAMES[k]} | - | not run | 0 | $0.000000 | 0 | 0 | - |")
    s, bu = r["budget"]["spent"], r["budget"]
    add("")
    add(f"Metered {_usd(s['metered_usd'])} + ApplyGuardrail allowance {_usd(s['guardrail_allowance_usd'])} "
        f"({s['guardrail_apply']} checks, unmetered by the app) + probes {_usd(s['probes_usd'])} = "
        f"**{_usd(s['total_usd'])}**, of which {_usd(s['abandoned_usd'])} in discarded or stopped attempts. Budget "
        f"limit ${bu['limit_usd']:.2f}, max projected ${bu['max_projected_usd']:.4f}.")
    add("")
    add("## Pricing provenance")
    add("")
    add(f"{r['pricing']['source']}, verified {r['pricing']['verified']}; USD per 1M tokens.")
    add("")
    add("| Model id | Input | Output |")
    add("|---|---|---|")
    for mid, p in sorted(r["pricing"]["usd_per_1m_tokens"].items(), key=lambda kv: (kv[1]["input"], kv[0])):
        add(f"| `{mid}` | {p['input']:.2f} | {p['output']:.2f} |")
    add("")
    add("## All-Sonnet cost counterfactual (pre-registered)")
    add("")
    add(r["counterfactual_rule"])
    add("")
    c = r["counterfactual"]
    if c["available"]:
        routed = ", ".join(f"{j} on `{c['routed'][j]['model']}` {_usd(c['routed'][j]['usd'])}"
                           for j in ("form_read", "packet", "policy"))
        add(f"On the fixed eval workload ({w['form_reads']['synthetic'] + w['form_reads']['photographed']} form "
            f"reads + {w['packets']} packets + {w['policy_questions']} policy answers): routed {_usd(c['routed_usd'])} "
            f"({routed}); all-Sonnet {_usd(c['all_sonnet_usd'])}; difference {_usd(c['difference_usd'])}.")
    else:
        add(f"Not available: {c['reason']}.")
    add("")
    add("## Pre-registered route-change rule (frozen in #76 before any live run)")
    add("")
    add(r["rule"]["text"])
    add("")
    add("| Route | Incumbent | Candidate | Combined fields vs incumbent | Silent errors (incumbent / candidate) | "
        "p50 model call ratio | USD per job ratio | Verdict |")
    add("|---|---|---|---|---|---|---|---|")
    for x in r["rule"]["inputs"]:
        add(f"| {x['route']} | {NAMES[x['incumbent']]} | {NAMES[x['candidate']]} | {x['combined_fields_delta']:+d} | "
            f"{x['silent_errors']['incumbent']} / {x['silent_errors']['candidate']} | "
            f"{x['p50_model_call_ratio'] if x['p50_model_call_ratio'] is not None else 'n/a'} | "
            f"{x['usd_per_job_ratio'] if x['usd_per_job_ratio'] is not None else 'n/a'} | |")
    for job, n in (("packet", w["packets"]), ("policy", w["policy_questions"])):
        incumbent = next((NAMES[k] for k, v in MODELS.items() if v == ai.ROUTES[job]), ai.ROUTES[job])
        add(f"| {job} | {incumbent} | any | under-powered (n={n}): may recommend, never auto-trigger; a change "
            f"needs the owner's recorded sign-off | | | | |")
    add("")
    add("Verdict slots are empty in this harness version (S1 of #76); the live run (S2) records each verdict from "
        "these numbers under the rule exactly as frozen.")
    add("")
    return "\n".join(L)


def write_report(out: Path, report: dict) -> None:
    evals_run.write_json(out / "report.json", report)
    text = evals_run.scrub(render_md(json.loads((out / "report.json").read_text())))  # from the file as written
    if evals_run.guard_hits(text):
        raise Refused("scrub left guard-pattern matches in report.md")
    (out / "report.md").write_text(text)


# ------------------------------------------------------------------ run

def probe(model: str) -> float:
    """1-token Converse call (access, quota, agreement) before any pass; returns its estimated USD."""
    try:
        resp = ai._converse(model, "Reply with the single word OK.", "ping", None, "jpeg", 1)
    except Exception as exc:  # noqa: BLE001 - any failure refuses the run before a pass starts
        raise Refused(f"1-token probe of {model} failed: {type(exc).__name__}: {exc}") from None
    return ai.estimate_usd(model, resp["usage"]["inputTokens"], resp["usage"]["outputTokens"])


def compare(out: Path, keys: list[str], budget: float, live: bool) -> dict:
    """Preflight, then one pass per model; writes out/report.json + report.md. Returns the report."""
    if out.exists() and any(out.iterdir()):
        raise Refused(f"--out {out} exists and is not empty (one directory per run)")
    env_keys = ("DRY_RUN", "STORE_PATH", "PACKET_CONCURRENCY", "SNAPSHOT_INTERVAL_SECONDS", "SYNC_INTERVAL_SECONDS")
    with scoped_env(env_keys), tempfile.TemporaryDirectory(prefix="model-compare-") as scratch:
        if not live:
            os.environ["DRY_RUN"] = "1"
        os.environ.update(PACKET_CONCURRENCY="1", SNAPSHOT_INTERVAL_SECONDS="0", SYNC_INTERVAL_SECONDS="0")
        manifest, baseline = preflight(live, keys)
        ctx = Context(live, keys, manifest, baseline, budget)
        ctx.guard.check(0.0)  # the whole run projected before any call: refused here, nothing is written
        from app import config, sim_client
        try:
            sim_client.make_client().post("/reset").raise_for_status()
        except Exception as exc:  # noqa: BLE001 - any simulator failure is a refusal
            raise Refused(f"simulator unreachable at {config.sim_url()}: {exc}") from None
        if live:
            for k in keys:
                ctx.probe_usd += probe(MODELS[k])
            ctx.guard.check(ctx.spent())
        from app.store import reset_store
        try:
            for k in keys:
                run_pass(ctx, k, Path(scratch), out)
        except BudgetExceeded as exc:
            ctx.stopped = str(exc)
        finally:
            ai.set_route_override(None)
            reset_store()
        report = build_report(out, ctx)
        write_report(out, report)
        reset_store()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="run directory (must be new or empty)")
    ap.add_argument("--models", default=",".join(MODELS), help=f"comma list of {', '.join(MODELS)} (cheapest first)")
    ap.add_argument("--budget", type=float, default=5.00, help="USD cap on the projected spend")
    ap.add_argument("--live", action="store_true", help="real Bedrock calls (without it: DRY_RUN mocks only)")
    args = ap.parse_args(argv)
    asked = [k.strip() for k in args.models.split(",") if k.strip()]
    if not asked or set(asked) - set(MODELS):
        loud(f"--models must name some of {', '.join(MODELS)} (got {args.models!r})")
        return 3
    keys = [k for k in MODELS if k in asked]  # cheapest first, whatever the order given
    try:
        report = compare(args.out, keys, args.budget, args.live)
    except Refused as exc:
        loud(str(exc))
        return 4 if isinstance(exc, BudgetExceeded) else 3
    print(f"wrote {args.out / 'report.json'} and report.md: {report['mode']}, status {report['status']}, "
          f"spend ${report['budget']['spent']['total_usd']:.4f}")
    if report["status"] == "complete":
        return 0
    return 4 if any(p.startswith("budget") for p in report["problems"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())
