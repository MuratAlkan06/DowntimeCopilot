# Durability harness (frozen design, #51)

Proves that captured records are never lost or duplicated across network cuts and process death, on both
capture paths: the browser queue (`web/js/queue.js`, client-ID retries, server dedupe on `captured.client_id`)
and the SQLite sync outbox (`app/store/sqlite.py`) drained by `app/sync/worker.py` to DynamoDB. The harness
lives in `tools/durability/` (Python stdlib plus the app's own pinned `boto3`, no new dependency). It changes
nothing under `app/` or `web/`: server faults come from a shim injected from outside, browser faults from a
fake `fetch` around the real modules.

Run: `make durability` (`CYCLES?=25`, `SEED?=auto`). It needs `DDB_LOCAL_DIR` (an unpacked DynamoDB Local,
see `tools/durability/ddb_local.py`) and java, or `DDB_LOCAL_ENDPOINT`; without them the server cycles print
SKIP, the same idea as the node `skipif` in `tests/test_web_queue.py`.

## Invariants

- **I1 zero lost.** Every capture acked to the client (200/201) or persisted in the queue (store `put`
  completed) is in SQLite after quiesce. Every SQLite record and audit row is in DynamoDB by trailing id of
  `sk`, or accounted as `not_mirrored`, or in the denied-latch JSONL export.
- **I2 zero duplicated.** At most one SQLite record per `client_id`; at most one DynamoDB item per id; at most
  one `capture.create` audit per `client_id` in DynamoDB (`CapturedRecord` has no `client_id`, so the
  logical-record key in DynamoDB is the `client_id` in that audit's details). Same-key re-puts from the crash
  window between `put_item` and `outbox_done` are overwrites, not duplicates. Since #55, I2 covers F2 too.
- **I3 bounded drain.** `pending` reaches 0 within 90 s after the faults stop. Poison items are excluded: they
  dead-letter (#55) instead of draining.
- Unacked in-flight submits (killed before any response and before a queue put) are reported as the known
  unacked window, not as I1 violations.

## Server cycles (`python -m tools.durability.harness`)

Each cycle: a fresh `STORE_PATH` and a fresh table `dur-<runid>-<n>` in DynamoDB Local (in memory,
`-sharedDb`, free high port); one `python -m uvicorn app.main:app` subprocess in its own process group
(killed in `try/finally` and `atexit`); captures through `POST /capture` multipart, typed verbal order,
`client_id` `dur-<runid>-<cycle>-<n>`; the fault; then the quiesce protocol: poll ONLY `GET /sync/status`
until `pending == 0` on 3 consecutive polls, SIGTERM, and reconcile the DEAD SQLite file against a
paginated, consistent DynamoDB scan.

| Env | Value |
|---|---|
| `DRY_RUN` | `0` (the DynamoDB mirror runs for real; Bedrock is replaced by the shim) |
| `TEXTRACT_ENABLED` | `0` |
| `AUDIO_BUCKET` | unset (no S3 snapshot step) |
| `SYNC_TABLE` | `dur-<runid>-<n>` |
| `SYNC_ENDPOINT_URL` | `http://127.0.0.1:<proxyport>` |
| AWS credentials | dummy values in env; every other `AWS_*` is stripped |
| `SNAPSHOT_INTERVAL_SECONDS` | `0` |
| `SYNC_INTERVAL_SECONDS` | `0.25` in CI runs, `1.0` for the live run (S4) |
| `SIM_URL` | a closed port: no simulator, wall-clock fallback |
| `DURABILITY_SHIM` | `1`, with `PYTHONPATH` = `tools/durability/shim` then the repo root |

### Proxy modes (`tools/durability/proxy.py`)

A threaded TCP proxy in front of DynamoDB Local. The mode is chosen per connection. A mode change also cuts
every live connection, so a pooled keep-alive socket cannot carry traffic past it.

- `pass`: forward both ways.
- `refuse`: listener closed, so connections are refused (endpoint down).
- `drop`: accept and close at once.
- `hold`: accept and never forward. The worker freezes inside `put_item` for about 14 s (botocore
  `total_max_attempts=2`, connect 2 s, read 5 s), a deterministic mid-batch SIGKILL window.
- `fwd_drop`: forward the request, let it land, drop the response. The put is applied, the client errors,
  and the outbox row survives. The restart then re-puts the same key: an overwrite, which is excluded from I2.
- `deny`: an AWS-shaped `AccessDeniedException` (HTTP 400, awsJson1.0), the tiny DynamoDB fake for F7b.

### Shim (`tools/durability/shim/sitecustomize.py`)

The shim is a no-op unless `DURABILITY_SHIM=1` (pytest proves it). It always chains to any interpreter-level
`sitecustomize` it shadows. With the flag:

- `app.ai.client._converse` returns a canned, deterministic Converse dict whose text is the app's own
  `mock_verbal_order` of the typed read-back.
- F5: `Store.save_record` writes a signal file after the save COMMITS for one `client_id`, then blocks. The
  harness SIGKILLs it there, between the save and the `capture.create` audit.
- F6: the Nth `CapturedRecord` enqueue fails. With `statement`, the outbox INSERT raises inside the real
  `_enqueue`, so the app's own failure path runs. With `rollback` (labeled SIMULATED), the outbox INSERT
  first rolls the whole transaction back and then raises, standing in for SQLITE_FULL/IOERR/BUSY
  auto-rollback; the app's real `_enqueue` failure path runs here too.

## Node cycles (`python -m tools.durability.queue_cycles`)

The real `queue.js` and `api.js` (copied with `state.js`) run in `node --input-type=module` subprocesses, with
a per-life timeout (precedent: `tests/test_web_queue.py:21`). They talk to one real uvicorn app (`DRY_RUN=1`,
`SYNC_INTERVAL_SECONDS=0`): the server is the dedupe oracle, and the outbox is the server cycles' job.

- A file-backed store implements the queue store interface (`put`, `list`, `delete`).
- The driver holds replicas of `buildForm`, `sendQueued`, the 3-line `flushQueue` gate and the
  `submitCapture` branches, each with a comment that names the drift risk.
- A fake `fetch` wraps the real one with a per-cycle schedule keyed by (client_id, attempt):
  `cut_pre` (drop before send), `cut_post` (drop the response after the commit), `real_5xx` (commit, then
  answer 500), `offline_result` (a service-worker `x-dc-offline` answer), and `die_pre`/`die_post`.
- "Tab death" is a fresh subprocess over the persisted store file.
- Every cycle ends with a fault-free heal life that polls and drains until no live item is left, which is
  enough drains for a poison item to reach the `queue.js` dead-letter cap (`MAX_ATTEMPTS`, 8).
- `locks` is always passed explicitly (`null`).
- Stall is counted as skipped `flushQueue` attempts (the `fetchFailed` latch) until a non-cached 2xx clears it.

## Expected outcomes per fault

| Fault | Side | Expected outcome |
|---|---|---|
| F1 cut mid-upload | node | Pre-ack and never queued: `known_windows.unacked_inflight`. Otherwise retried and deduped, 0 violations |
| F2 5xx after commit | node | (#55 fixed) The first-submit 5xx is queued under the same id and lands exactly once; the queued-item 5xx retries the same id too. A second record for the paper is `violations.dup_paper` plus `violations.f2_not_exactly_once` |
| F3 proxy cut mid-drain | server | `violations` if anything is lost or duplicated |
| F4 hold + SIGKILL mid-batch | server | `violations` if lost or dup (`F4fd`: fwd_drop re-put = overwrite, excluded) |
| F5 SIGKILL after commit, before `capture.create` | server | `known_windows.record_without_create_audit`, including the 200-replay case |
| F6 outbox statement fault | server | `not_mirrored` accounting. The reconciliation-derived missing set is ground truth; the S1 counter is a cross-check |
| F6-rollback (SIMULATED) | server | Save survives (fixed, [#54](https://github.com/MuratAlkan06/DowntimeCopilot/issues/54)): `known_windows.not_mirrored`; a lost save is a `violations.lost_acked` |
| F7a endpoint down across a restart | server | Backoff, then drain. `violations` if lost |
| F7b AccessDenied | server | Denied latch, then the JSONL fallback. Lines are deduped by (pk, sk) and never count as violations. A restart drops the latch |
| poison (422: legacy vitals item, blob null) | node | (#55 fixed) Dead-lettered after 8 answered failures: the active queue reaches 0, the item stays in the store, counted as `dead_letter` (report: "Dead-lettered poison items"). Still live after the heal: `violations.queue_not_drained`; any other item dead-lettered: `violations.dead_letter_unexpected`. Excluded from I3 |
| two tabs (two module instances, shared store) | node | Server dedupe holds |

## Metrics and report

- `t0` is the first `/sync/status` 200 after the heal or restart. `t1` is the first `pending == 0` poll
  (polls every 250 ms at most). The report gives p50 and max of `t1 - t0` per fault type.
- A cycle is a seeded scenario. The master seed and each cycle seed are printed. Scheduling is stratified:
  at least one cycle per fault type, then a random mix.
- `--replay FAULT:CYCLE_SEED` reruns one cycle.
- `python -m tools.durability.report RUN_DIR` is a pure generator: run dir in, `report.json` and `report.md`
  out. Never hand-edit either file.
- The report lists violations, known windows, expected failures, `not_mirrored`, drain p50/max and stall
  counts for each side separately.

## Out of scope

Real IndexedDB semantics, multi-worker uvicorn, simulated-clock eras, fake timers, and live F3/F6 (the S4
live run uses fault-free cycles against the real table).
