# Durability CI runs: per-run evidence

Generated from the committed [`ci-runs/<run-id>/report.json`](ci-runs/) copies. Do not hand-edit.
Every CI count on this page is computed from those files alone and can be recomputed from them. The
few local, non-CI figures in the totals are cited from their PRs and marked as such.

- **The copies.** All 24 `durability-report-<run-id>` artifacts the CI "Durability harness"
  job uploaded through the `main` run at `6eb38a4`: the full set as of 2026-10-27 (artifacts
  are kept 14 days). Each `report.json` is byte-identical to its artifact (sha256-checked against a
  fresh download on 2026-10-27). Nothing is redacted: the reports hold no ARN, account ID, hostname
  or path.
- **Identity columns** (date, event, branch, PR, head, run conclusion) come from the GitHub Actions
  run record (`https://github.com/MuratAlkan06/DowntimeCopilot/actions/runs/<run-id>`). **Code state**
  is read from each head commit's source: the #54 fix (`_write_with_outbox` in `app/store/sqlite.py`)
  and the #55 fix (`retryDead` in `web/js/queue.js`).
- **Code state.** `pre-#54-fix`: neither fix (both defects present). `pre-#55-fix`: #54 fixed, #55
  not. `post-both-fix`: both fixed, one of the 7 runs in the frozen 387. `post-close`: both fixed, run
  after the durability contract record closed; not part of any frozen figure.
- **Run conclusion.** `green`: the run and its durability job succeeded. `failed-node-step`: the
  "Node cycles" step crashed on a relative `--run-dir` (`ValueError: relative path can't be expressed
  as a file URI`; fixed at `b1849f0`), so the report has a server side only. The server cycles ran
  and their report was uploaded, but the run never went green.
- **`expected_fail` tag.** `F6-rollback` is `S5: F6-rollback (SIMULATED transaction rollback under a 201; fix issue to be filed)`: the #54 defect,
  recorded by design on pre-fix code.
- **#55 duplicates** are the node side's `known_windows.f2_resubmit_duplicates`. After the #55 fix
  the same event is a violation instead, and no run records one.
- The master seed of every run is its run id (`report.json` `master_seed`).

## Totals

### #54: F6-rollback losses (`expected_fail`, server side)

| Run | Code state | Run conclusion | F6-rollback |
|---|---|---|---|
| 40574792412 | pre-#54-fix | failed-node-step | 5 |
| 40574822851 | pre-#54-fix | failed-node-step | 5 |
| 40575279968 | pre-#54-fix | green | 2 |
| 40575290603 | pre-#54-fix | green | 5 |
| 40576027397 | pre-#54-fix | green | 2 |

- CI: **19** across 5 pre-fix runs (2 of them server-side only).
- Local: 1, from PR #53's local stratified run (seed 51051), documented in PR #53's body (not a CI artifact).
- **Total: 19 + 1 = 20.**

### #55: F2 resubmit duplicates (`known_windows.f2_resubmit_duplicates`, node side)

| Run | Code state | Duplicates |
|---|---|---|
| 40575279968 | pre-#54-fix | 2 |
| 40575290603 | pre-#54-fix | 4 |
| 40576027397 | pre-#54-fix | 3 |
| 40576905693 | pre-#55-fix | 4 |
| 40576946764 | pre-#55-fix | 6 |
| 40577506992 | pre-#55-fix | 4 |

- CI: **23** across 6 pre-#55-fix runs. The 2 server-side-only runs have no node side and add nothing.
- Local: 1, from PR #53's local stratified run, documented in PR #53's body, plus S5a's node cycles (PR #57), whose F2 count was not
  itemized.
- **Total: 23 + 1 + the un-itemized S5a observation = 24+.**

### Frozen headline figures

**769 cycles with zero I1/I2 violations** are the fully completed runs enumerated as of contract
close: 13 green CI runs (650 cycles, computed here) plus 119 local cycles (not CI artifacts):

| Component | Cycles |
|---|---|
| 13 green CI runs, 40575279968 through 40582707480 | 650 |
| S2 calibration, server (PR #53) | 30 |
| S2 stratified, 15 server + 12 node (PR #53) | 27 |
| S5a F6rb-focused, server (PR #57) | 5 |
| S5a `make durability CYCLES=10`, 10 server + 10 node (PR #57) | 20 |
| S5b `CYCLES=10`, 10 server + 10 node (PR #58) | 20 |
| Pre-live regression at #60, server, seed 51 (PR #60) | 9 |
| S4 live run against the real table ([`live-s4/`](live-s4/report.md)) | 8 |
| **Total** | **769** |

**387 cycles on code with both fixes, zero loss, zero duplication and zero expected failures** are the enumerated post-fix set:

| Component | Cycles |
|---|---|
| 7 `post-both-fix` CI runs | 350 |
| S5b `CYCLES=10`, 10 server + 10 node (PR #58) | 20 |
| Pre-live regression at #60, server, seed 51 (PR #60) | 9 |
| S4 live run against the real table ([`live-s4/`](live-s4/report.md)) | 8 |
| **Total** | **387** |

Across the 7 CI runs: violations 0, lost 0, duplicated 0, `expected_fail` 0, #55 duplicates 0. The only known windows they record are the by-design `not_mirrored`, `record_without_create_audit`, `unacked_inflight`.

### Not part of the frozen figures

- **2 partial pre-fix runs** (40574792412, 40574822851): 50 server cycles, never green. Their F6-rollback events are counted in the #54 total above.
- **9 post-close runs** (40584091782 through 40586907234): 450 cycles, violations 0, `expected_fail` 0.
- Across all 24 reports: 1150 cycles, violations 0.

## Runs

| Run | Created (UTC) | Event | Branch (PR) | Head | Code state | Run conclusion | Frozen figures | Cycles server + node | Violations | Lost / duplicated | `expected_fail` | #55 duplicates |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| [40574792412](ci-runs/40574792412/report.json) | 2026-10-27 20:04 | push | `ci/durability-harness-job` (#56) | `1a615c5` | pre-#54-fix | failed-node-step | none (partial) | 25 + 0 | none | 0 / 0 | F6-rollback x5 | n/a (no node side) |
| [40574822851](ci-runs/40574822851/report.json) | 2026-10-27 20:04 | PR | `ci/durability-harness-job` (#56) | `1a615c5` | pre-#54-fix | failed-node-step | none (partial) | 25 + 0 | none | 0 / 0 | F6-rollback x5 | n/a (no node side) |
| [40575279968](ci-runs/40575279968/report.json) | 2026-10-27 20:07 | push | `ci/durability-harness-job` (#56) | `b1849f0` | pre-#54-fix | green | 769 | 25 + 25 | none | 0 / 0 | F6-rollback x2 | 2 |
| [40575290603](ci-runs/40575290603/report.json) | 2026-10-27 20:08 | PR | `ci/durability-harness-job` (#56) | `b1849f0` | pre-#54-fix | green | 769 | 25 + 25 | none | 0 / 0 | F6-rollback x5 | 4 |
| [40576027397](ci-runs/40576027397/report.json) | 2026-10-27 20:12 | push | `main` (#56 merge) | `7aff95a` | pre-#54-fix | green | 769 | 25 + 25 | none | 0 / 0 | F6-rollback x2 | 3 |
| [40576905693](ci-runs/40576905693/report.json) | 2026-10-27 20:18 | push | `fix/outbox-rollback-save-loss` (#57) | `0069d07` | pre-#55-fix | green | 769 | 25 + 25 | none | 0 / 0 | none | 4 |
| [40576946764](ci-runs/40576946764/report.json) | 2026-10-27 20:19 | PR | `fix/outbox-rollback-save-loss` (#57) | `0069d07` | pre-#55-fix | green | 769 | 25 + 25 | none | 0 / 0 | none | 6 |
| [40577506992](ci-runs/40577506992/report.json) | 2026-10-27 20:22 | push | `main` (#57 merge) | `2efc72b` | pre-#55-fix | green | 769 | 25 + 25 | none | 0 / 0 | none | 4 |
| [40578638173](ci-runs/40578638173/report.json) | 2026-10-27 20:30 | push | `fix/queue-on-5xx-dead-letter` (#58) | `d1eb851` | post-both-fix | green | 769, 387 | 25 + 25 | none | 0 / 0 | none | 0 |
| [40578688056](ci-runs/40578688056/report.json) | 2026-10-27 20:30 | PR | `fix/queue-on-5xx-dead-letter` (#58) | `d1eb851` | post-both-fix | green | 769, 387 | 25 + 25 | none | 0 / 0 | none | 0 |
| [40579463460](ci-runs/40579463460/report.json) | 2026-10-27 20:35 | push | `fix/queue-on-5xx-dead-letter` (#58) | `7126739` | post-both-fix | green | 769, 387 | 25 + 25 | none | 0 / 0 | none | 0 |
| [40579472681](ci-runs/40579472681/report.json) | 2026-10-27 20:35 | PR | `fix/queue-on-5xx-dead-letter` (#58) | `7126739` | post-both-fix | green | 769, 387 | 25 + 25 | none | 0 / 0 | none | 0 |
| [40580784310](ci-runs/40580784310/report.json) | 2026-10-27 20:44 | push | `main` (#58 merge) | `e5fbb16` | post-both-fix | green | 769, 387 | 25 + 25 | none | 0 / 0 | none | 0 |
| [40582667673](ci-runs/40582667673/report.json) | 2026-10-27 20:57 | push | `test/durability-live-run` (#60) | `33b0081` | post-both-fix | green | 769, 387 | 25 + 25 | none | 0 / 0 | none | 0 |
| [40582707480](ci-runs/40582707480/report.json) | 2026-10-27 20:58 | PR | `test/durability-live-run` (#60) | `33b0081` | post-both-fix | green | 769, 387 | 25 + 25 | none | 0 / 0 | none | 0 |
| [40584091782](ci-runs/40584091782/report.json) | 2026-10-27 21:07 | push | `test/durability-live-run` (#60) | `30a0a63` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40584100755](ci-runs/40584100755/report.json) | 2026-10-27 21:07 | PR | `test/durability-live-run` (#60) | `30a0a63` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40584722202](ci-runs/40584722202/report.json) | 2026-10-27 21:11 | push | `main` (#60 merge) | `ef11370` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40585661181](ci-runs/40585661181/report.json) | 2026-10-27 21:18 | push | `docs/durability-record-fixes` (#62) | `7dadf6f` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40585670442](ci-runs/40585670442/report.json) | 2026-10-27 21:18 | PR | `docs/durability-record-fixes` (#62) | `7dadf6f` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40586042288](ci-runs/40586042288/report.json) | 2026-10-27 21:21 | push | `main` (#62 merge) | `b4b86de` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40586534380](ci-runs/40586534380/report.json) | 2026-10-27 21:24 | push | `docs/durability-attribution-fix` (#63) | `89accaf` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40586543296](ci-runs/40586543296/report.json) | 2026-10-27 21:24 | PR | `docs/durability-attribution-fix` (#63) | `89accaf` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |
| [40586907234](ci-runs/40586907234/report.json) | 2026-10-27 21:27 | push | `main` (#63 merge) | `6eb38a4` | post-close | green | none (post-close) | 25 + 25 | none | 0 / 0 | none | 0 |

## Fault modes injected

| Run | Server | Node |
|---|---|---|
| 40574792412 | F3 x2, F4 x4, F4fd x3, F5 x1, F6 x2, F6rb x5, F7a x2, F7b x4, none x2 | no node side |
| 40574822851 | F3 x1, F4 x1, F4fd x4, F5 x2, F6 x2, F6rb x5, F7a x4, F7b x4, none x2 | no node side |
| 40575279968 | F3 x2, F4 x2, F4fd x2, F5 x4, F6 x4, F6rb x2, F7a x3, F7b x3, none x3 | F1 x1, F1die x3, F2 x2, none x4, offline x3, poison x5, tabdeath x4, twotab x3 |
| 40575290603 | F3 x2, F4 x3, F4fd x1, F5 x1, F6 x2, F6rb x5, F7a x2, F7b x5, none x4 | F1 x5, F1die x1, F2 x4, none x1, offline x2, poison x4, tabdeath x4, twotab x4 |
| 40576027397 | F3 x3, F4 x2, F4fd x2, F5 x3, F6 x3, F6rb x2, F7a x4, F7b x2, none x4 | F1 x4, F1die x5, F2 x3, none x3, offline x2, poison x3, tabdeath x2, twotab x3 |
| 40576905693 | F3 x3, F4 x3, F4fd x2, F5 x3, F6 x2, F6rb x3, F7a x2, F7b x4, none x3 | F1 x2, F1die x2, F2 x4, none x4, offline x4, poison x5, tabdeath x2, twotab x2 |
| 40576946764 | F3 x4, F4 x3, F4fd x2, F5 x3, F6 x2, F6rb x2, F7a x2, F7b x2, none x5 | F1 x3, F1die x2, F2 x6, none x3, offline x1, poison x3, tabdeath x5, twotab x2 |
| 40577506992 | F3 x4, F4 x3, F4fd x3, F5 x3, F6 x2, F6rb x3, F7a x3, F7b x2, none x2 | F1 x2, F1die x3, F2 x4, none x3, offline x2, poison x4, tabdeath x3, twotab x4 |
| 40578638173 | F3 x3, F4 x2, F4fd x1, F5 x3, F6 x3, F6rb x4, F7a x3, F7b x4, none x2 | F1 x4, F1die x4, F2 x3, none x4, offline x2, poison x2, tabdeath x1, twotab x5 |
| 40578688056 | F3 x4, F4 x2, F4fd x3, F5 x2, F6 x3, F6rb x4, F7a x2, F7b x4, none x1 | F1 x3, F1die x1, F2 x4, none x3, offline x4, poison x1, tabdeath x4, twotab x5 |
| 40579463460 | F3 x1, F4 x4, F4fd x4, F5 x1, F6 x4, F6rb x2, F7a x2, F7b x4, none x3 | F1 x4, F1die x3, F2 x3, none x3, offline x2, poison x1, tabdeath x7, twotab x2 |
| 40579472681 | F3 x2, F4 x3, F4fd x3, F5 x6, F6 x3, F6rb x3, F7a x1, F7b x2, none x2 | F1 x2, F1die x5, F2 x5, none x3, offline x2, poison x2, tabdeath x3, twotab x3 |
| 40580784310 | F3 x2, F4 x5, F4fd x2, F5 x3, F6 x3, F6rb x3, F7a x3, F7b x3, none x1 | F1 x2, F1die x1, F2 x1, none x3, offline x4, poison x3, tabdeath x9, twotab x2 |
| 40582667673 | F3 x5, F4 x3, F4fd x1, F5 x3, F6 x1, F6rb x1, F7a x3, F7b x3, none x5 | F1 x2, F1die x4, F2 x1, none x5, offline x1, poison x3, tabdeath x6, twotab x3 |
| 40582707480 | F3 x3, F4 x1, F4fd x4, F5 x1, F6 x4, F6rb x4, F7a x4, F7b x2, none x2 | F1 x5, F1die x6, F2 x4, none x2, offline x2, poison x2, tabdeath x1, twotab x3 |
| 40584091782 | F3 x4, F4 x1, F4fd x5, F5 x3, F6 x4, F6rb x1, F7a x1, F7b x3, none x3 | F1 x1, F1die x1, F2 x5, none x4, offline x4, poison x6, tabdeath x3, twotab x1 |
| 40584100755 | F3 x2, F4 x3, F4fd x4, F5 x3, F6 x2, F6rb x3, F7a x3, F7b x2, none x3 | F1 x3, F1die x6, F2 x2, none x3, offline x5, poison x2, tabdeath x1, twotab x3 |
| 40584722202 | F3 x4, F4 x1, F4fd x2, F5 x3, F6 x3, F6rb x3, F7a x4, F7b x2, none x3 | F1 x3, F1die x3, F2 x3, none x4, offline x4, poison x1, tabdeath x1, twotab x6 |
| 40585661181 | F3 x3, F4 x4, F4fd x2, F5 x3, F6 x1, F6rb x3, F7a x2, F7b x4, none x3 | F1 x4, F1die x4, F2 x4, none x1, offline x4, poison x3, tabdeath x3, twotab x2 |
| 40585670442 | F3 x3, F4 x2, F4fd x3, F5 x2, F6 x5, F6rb x2, F7a x3, F7b x4, none x1 | F1 x4, F1die x3, F2 x2, none x3, offline x2, poison x2, tabdeath x5, twotab x4 |
| 40586042288 | F3 x3, F4 x5, F4fd x2, F5 x3, F6 x2, F6rb x2, F7a x4, F7b x1, none x3 | F1 x4, F1die x4, F2 x3, none x1, offline x2, poison x4, tabdeath x4, twotab x3 |
| 40586534380 | F3 x2, F4 x4, F4fd x3, F5 x1, F6 x3, F6rb x4, F7a x2, F7b x4, none x2 | F1 x4, F1die x2, F2 x5, none x1, offline x3, poison x3, tabdeath x4, twotab x3 |
| 40586543296 | F3 x3, F4 x3, F4fd x1, F5 x5, F6 x2, F6rb x4, F7a x3, F7b x2, none x2 | F1 x4, F1die x1, F2 x2, none x3, offline x5, poison x3, tabdeath x3, twotab x4 |
| 40586907234 | F3 x3, F4 x3, F4fd x2, F5 x4, F6 x2, F6rb x2, F7a x3, F7b x2, none x4 | F1 x3, F1die x4, F2 x3, none x2, offline x6, poison x2, tabdeath x3, twotab x2 |

## Known windows (not violations)

| Run | Server | Node |
|---|---|---|
| 40574792412 | not_mirrored x2, record_without_create_audit x1 | no node side |
| 40574822851 | not_mirrored x2, record_without_create_audit x2 | no node side |
| 40575279968 | not_mirrored x4, record_without_create_audit x4 | f2_resubmit_duplicates x2, stall x5, unacked_inflight x3 |
| 40575290603 | not_mirrored x2, record_without_create_audit x1 | f2_resubmit_duplicates x4, stall x4 |
| 40576027397 | not_mirrored x3, record_without_create_audit x3 | f2_resubmit_duplicates x3, stall x3, unacked_inflight x3 |
| 40576905693 | not_mirrored x5, record_without_create_audit x3 | f2_resubmit_duplicates x4, stall x5, unacked_inflight x2 |
| 40576946764 | not_mirrored x4, record_without_create_audit x3 | f2_resubmit_duplicates x6, stall x3, unacked_inflight x1 |
| 40577506992 | not_mirrored x5, record_without_create_audit x3 | f2_resubmit_duplicates x4, stall x4 |
| 40578638173 | not_mirrored x7, record_without_create_audit x3 | unacked_inflight x3 |
| 40578688056 | not_mirrored x7, record_without_create_audit x2 | unacked_inflight x1 |
| 40579463460 | not_mirrored x6, record_without_create_audit x1 | unacked_inflight x2 |
| 40579472681 | not_mirrored x6, record_without_create_audit x6 | unacked_inflight x3 |
| 40580784310 | not_mirrored x6, record_without_create_audit x3 | unacked_inflight x1 |
| 40582667673 | not_mirrored x2, record_without_create_audit x3 | unacked_inflight x3 |
| 40582707480 | not_mirrored x8, record_without_create_audit x1 | unacked_inflight x3 |
| 40584091782 | not_mirrored x5, record_without_create_audit x3 | none |
| 40584100755 | not_mirrored x5, record_without_create_audit x3 | unacked_inflight x4 |
| 40584722202 | not_mirrored x6, record_without_create_audit x3 | unacked_inflight x2 |
| 40585661181 | not_mirrored x4, record_without_create_audit x3 | none |
| 40585670442 | not_mirrored x7, record_without_create_audit x2 | unacked_inflight x1 |
| 40586042288 | not_mirrored x4, record_without_create_audit x3 | unacked_inflight x1 |
| 40586534380 | not_mirrored x7, record_without_create_audit x1 | unacked_inflight x2 |
| 40586543296 | not_mirrored x6, record_without_create_audit x5 | none |
| 40586907234 | not_mirrored x4, record_without_create_audit x4 | unacked_inflight x3 |

## Server drain to empty, by fault

Seconds, `p50 / max (n cycles)`; blank where the fault was not drawn. The node side records no drain timings (0 entries across all reports).

| Run | F3 | F4 | F4fd | F5 | F6 | F6rb | F7a | F7b | none |
|---|---|---|---|---|---|---|---|---|---|
| 40574792412 | 0.504 / 1.262 (2) | 0.504 / 0.769 (4) | 0.504 / 0.507 (3) | 1.514 / 1.514 (1) | 0.756 / 0.769 (2) | 0.757 / 0.77 (5) | 0.252 / 1.009 (2) | 0.504 / 0.504 (4) | 0.756 / 0.757 (2) |
| 40574822851 | 1.512 / 1.512 (1) | 0.755 / 0.755 (1) | 1.007 / 1.007 (4) | 0.252 / 0.755 (2) | 0.756 / 1.006 (2) | 0.756 / 1.01 (5) | 1.258 / 1.51 (4) | 0.757 / 0.757 (4) | 1.012 / 1.26 (2) |
| 40575279968 | 1.005 / 1.51 (2) | 0.755 / 1.007 (2) | 0.503 / 0.756 (2) | 1.007 / 1.008 (4) | 1.01 / 1.013 (4) | 0.503 / 0.755 (2) | 1.259 / 1.513 (3) | 0.504 / 0.762 (3) | 1.006 / 1.007 (3) |
| 40575290603 | 0.85 / 1.259 (2) | 0.756 / 1.007 (3) | 0.504 / 0.504 (1) | 0.504 / 0.504 (1) | 0.51 / 0.756 (2) | 0.756 / 0.757 (5) | 1.26 / 1.763 (2) | 0.505 / 0.755 (5) | 1.014 / 1.259 (4) |
| 40576027397 | 1.264 / 1.513 (3) | 1.009 / 1.514 (2) | 0.504 / 0.757 (2) | 0.504 / 1.767 (3) | 1.261 / 1.513 (3) | 1.01 / 1.262 (2) | 1.513 / 1.77 (4) | 0.516 / 0.771 (2) | 1.014 / 1.023 (4) |
| 40576905693 | 1.01 / 1.256 (3) | 0.754 / 1.016 (3) | 0.503 / 0.503 (2) | 1.004 / 1.006 (3) | 1.006 / 1.256 (2) | 1.006 / 1.256 (3) | 1.008 / 1.257 (2) | 0.503 / 0.505 (4) | 0.754 / 1.256 (3) |
| 40576946764 | 1.01 / 1.512 (4) | 0.505 / 1.26 (3) | 0.756 / 0.756 (2) | 0.757 / 1.765 (3) | 1.263 / 1.513 (2) | 0.757 / 1.01 (2) | 0.758 / 1.009 (2) | 0.507 / 0.518 (2) | 1.01 / 1.262 (5) |
| 40577506992 | 1.51 / 2.019 (4) | 0.76 / 1.285 (3) | 0.755 / 0.756 (3) | 0.252 / 1.008 (3) | 0.503 / 1.007 (2) | 0.755 / 2.283 (3) | 1.016 / 1.261 (3) | 0.504 / 0.504 (2) | 0.755 / 1.008 (2) |
| 40578638173 | 1.005 / 1.255 (3) | 0.503 / 0.754 (2) | 0.503 / 0.503 (1) | 0.754 / 1.005 (3) | 0.757 / 1.005 (3) | 0.754 / 0.754 (4) | 1.005 / 1.256 (3) | 0.754 / 0.754 (4) | 1.005 / 1.005 (2) |
| 40578688056 | 1.011 / 1.513 (4) | 0.504 / 1.261 (2) | 1.009 / 1.513 (3) | 0.758 / 1.261 (2) | 1.019 / 1.277 (3) | 1.262 / 1.262 (4) | 1.261 / 1.514 (2) | 0.76 / 1.018 (4) | 1.013 / 1.013 (1) |
| 40579463460 | 0.756 / 0.756 (1) | 1.008 / 1.009 (4) | 1.01 / 1.261 (4) | 1.514 / 1.514 (1) | 1.261 / 1.514 (4) | 1.009 / 1.263 (2) | 1.01 / 1.262 (2) | 0.757 / 0.775 (4) | 1.022 / 1.517 (3) |
| 40579472681 | 1.009 / 1.766 (2) | 1.011 / 1.26 (3) | 1.261 / 1.262 (3) | 0.757 / 2.271 (6) | 1.261 / 1.261 (3) | 1.512 / 1.513 (3) | 1.263 / 1.263 (1) | 0.524 / 0.764 (2) | 0.759 / 1.014 (2) |
| 40580784310 | 0.757 / 0.758 (2) | 0.504 / 0.76 (5) | 0.254 / 0.505 (2) | 0.756 / 0.76 (3) | 0.504 / 0.758 (3) | 0.504 / 0.505 (3) | 1.01 / 1.012 (3) | 0.505 / 0.507 (3) | 0.76 / 0.76 (1) |
| 40582667673 | 1.01 / 1.514 (5) | 1.009 / 1.265 (3) | 0.756 / 0.756 (1) | 0.756 / 1.26 (3) | 1.261 / 1.261 (1) | 1.263 / 1.263 (1) | 1.008 / 1.009 (3) | 0.508 / 0.757 (3) | 1.008 / 1.013 (5) |
| 40582707480 | 1.009 / 1.259 (3) | 0.755 / 0.755 (1) | 0.504 / 0.755 (4) | 1.009 / 1.009 (1) | 0.768 / 1.007 (4) | 1.011 / 1.26 (4) | 1.259 / 1.263 (4) | 0.504 / 0.507 (2) | 1.01 / 1.258 (2) |
| 40584091782 | 1.009 / 1.261 (4) | 0.756 / 0.756 (1) | 0.757 / 1.261 (5) | 1.264 / 1.515 (3) | 1.272 / 1.517 (4) | 1.262 / 1.262 (1) | 2.017 / 2.017 (1) | 0.518 / 0.763 (3) | 1.26 / 1.268 (3) |
| 40584100755 | 1.26 / 1.264 (2) | 0.757 / 0.77 (3) | 0.766 / 1.259 (4) | 1.763 / 1.763 (3) | 0.757 / 1.008 (2) | 1.51 / 2.361 (3) | 1.513 / 1.763 (3) | 0.503 / 0.504 (2) | 1.511 / 1.853 (3) |
| 40584722202 | 1.264 / 1.511 (4) | 0.755 / 0.755 (1) | 0.504 / 0.504 (2) | 0.755 / 1.764 (3) | 0.765 / 1.009 (3) | 1.007 / 1.009 (3) | 1.511 / 1.762 (4) | 0.504 / 0.755 (2) | 1.008 / 1.259 (3) |
| 40585661181 | 1.261 / 2.018 (3) | 0.757 / 1.008 (4) | 0.504 / 1.009 (2) | 0.504 / 0.756 (3) | 1.26 / 1.26 (1) | 1.26 / 1.512 (3) | 1.008 / 1.009 (2) | 0.757 / 1.011 (4) | 1.008 / 1.261 (3) |
| 40585670442 | 1.139 / 1.259 (3) | 0.503 / 0.505 (2) | 1.008 / 1.259 (3) | 0.504 / 1.01 (2) | 1.009 / 1.511 (5) | 1.01 / 1.011 (2) | 1.511 / 1.514 (3) | 0.507 / 0.758 (4) | 1.007 / 1.007 (1) |
| 40586042288 | 0.757 / 1.512 (3) | 0.504 / 0.506 (5) | 0.252 / 0.504 (2) | 1.011 / 1.264 (3) | 0.504 / 0.504 (2) | 0.504 / 0.757 (2) | 1.007 / 1.261 (4) | 0.504 / 0.504 (1) | 0.504 / 0.756 (3) |
| 40586534380 | 1.513 / 1.515 (2) | 1.008 / 1.261 (4) | 1.008 / 1.009 (3) | 1.011 / 1.011 (1) | 1.262 / 1.513 (3) | 1.26 / 1.263 (4) | 1.513 / 1.766 (2) | 0.757 / 0.757 (4) | 1.01 / 1.26 (2) |
| 40586543296 | 1.26 / 1.263 (3) | 1.26 / 1.261 (3) | 1.011 / 1.011 (1) | 0.758 / 2.017 (5) | 1.262 / 1.265 (2) | 1.01 / 1.261 (4) | 1.009 / 1.26 (3) | 0.525 / 1.015 (2) | 1.009 / 1.01 (2) |
| 40586907234 | 0.756 / 1.258 (3) | 0.755 / 0.756 (3) | 0.252 / 0.755 (2) | 1.007 / 1.008 (4) | 0.503 / 1.011 (2) | 0.756 / 0.77 (2) | 0.757 / 1.762 (3) | 0.504 / 0.504 (2) | 1.259 / 1.261 (4) |

## Excluded and elsewhere

- **Excluded:** the orchestrator-side independent calibration run (8 fault-free cycles, seed 424242, DynamoDB Local, all `violations=0`). Its evidence is in session task logs only; it was never committed and has no CI artifact, so it is in no total.
- **The S4 live run** (8 cycles against the real sync table) has its own generator-written report in [`live-s4/`](live-s4/report.md) and is not repeated here.
- The local runs in the frozen figures (PR #53, S5a, S5b, the pre-live regression) are recorded in their PRs and in [`docs/decisions.md`](../decisions.md).
