# Demo run-of-show (2 minutes, 90-second cut marked)

Final script (S16). Bold names are the exact UI labels. Times are targets from the start of the demo. Beats marked *(cut)* are dropped for the 90-second version. The unit clock starts at 02:45 after a reseed; the story time is 2:47 AM.

## Clean state (do this before every run, in order)

- [ ] `source .env` with fresh keys (`ExpiredToken` means fetch new keys, do not debug). For mock mode use `DRY_RUN=1` instead.
- [ ] Restart `make dev` (simulator on `:8001`, app on `:8000`; `SIM_PORT` / `APP_PORT` override). This clears in-memory state that `make demo` does not: the last **Run drill** result, and Textract or Transcribe skips latched by an earlier denial.
- [ ] `make demo`: empties the app store (`seed --fresh`: no stale records, audit, checklist acknowledgments or phase), resets the simulator to its seed (clock 02:45, EHR healthy), pulls 12 patients and prints the URL. It opens a browser only with `DEMO_OPEN=1`.
- [ ] Browser: clear the app origin's site data (Chrome DevTools, **Application**, **Storage**, **Clear site data**) **or** use a fresh browser profile. This empties the service-worker caches, the recorded policy answers, the IndexedDB capture queue and the per-role check marks in localStorage. A fresh profile is the safer choice.
- [ ] THEN advance the unit clock 15 minutes, so Bed 12's insulin (due 04:00) enters the 60-minute window and R-DUE-01 fires: `curl -X POST localhost:${SIM_PORT:-8001}/clock/advance -H 'Content-Type: application/json' -d '{"minutes":15}'`. A reseed resets the clock to 02:45, so always advance after cleaning.
- [ ] Open the app URL, choose role **Charge nurse**, and check: the unit clock reads 03:00, 12 patients are shown, the R-DUE-01 badge is on Bed 12.
- [ ] Props: the printed vitals flowsheet filled in by hand (one ambiguous field, one illegible field). As a backup, have `data/forms/fixtures/vitals_flowsheet-01.jpg`, `downtime_mar-01.jpg` and `verbal_order-01.wav` on the demo device.
- [ ] Camera and microphone permission granted. Network kill switch rehearsed (Wi-Fi or hotspot off). Notifications silenced. Zoom and print preview checked.
- [ ] `RESULTS.md` open in a second tab. It is generated from the committed baseline; do not run `make evals` on the demo machine.

## Run of show

| Time | Beat (UI labels) | What the audience sees | If it fails |
|---|---|---|---|
| 0:00 | **Go to downtime** tab: **Run drill** *(cut)* | Readiness drill card: a deterministic score of **82** on a fresh seed (snapshot freshness 92, packet coverage 100, checklist acknowledgment 0, form templates 100) and a two-sentence summary. Line: "the unit can rehearse this on a quiet night". | The score never depends on the model. If the summary call fails, the canned summary is shown with the same score. Skip the beat if it stalls. |
| 0:10 | **Declare downtime** | Banner *EHR unreachable*. 12 packets (about 20 s live; keep talking). Open Bed 12: penicillin allergy, insulin due 04:00 marked high-alert, R-DUE-01, every line with its snapshot source. | Bedrock fails or keys expire: restart with `DRY_RUN=1 make dev`, then `make demo` and the +15 advance (about 30 s), and say "mock mode, same shapes, canned text". Packets show instantly in mock mode. |
| 0:25 | **During** tab, policy panel | Ask *"How do I get a stat lab with the order system down?"*. Chips *Grounded in policy* and *Guardrail: passed*, cited section 4.2 (4.2.x paragraphs live). | Slow answer: talk over it, it is one Sonnet call. Error: say so and ask question 5 from the table below. Offline: the binder message is the designed behaviour. |
| 0:35 | **During**: **Patient notice** *(cut)* | English and Spanish notice for families. The Spanish page says it is pending native-speaker review. **Back to During**. | It is static and works offline. No failure path. |
| 0:40 | **Capture a paper form**: *Vitals flowsheet*, photo, **Read form** | Each field shows its photo crop. Two readers. With the fixture image live: 9 fields agree, Pain score flagged as a disagreement (*Textract: NW · Bedrock: unreadable*), SpO2 89 flagged out of range. Mock mode: 7 agree, 3 flagged (Resp rate disagreement, SpO2, Pain score). Edit a flagged value (*Corrected by reviewer*), then **Verify**. | Camera fails: use the file picker and upload `vitals_flowsheet-01.jpg`. Reads fail: say so, show the queued record and its crops, and move on. |
| 0:55 | **Capture a paper form**: *Verbal order (read-back)*, patient **Bed 7** | Record the read-back, or type it: *"Read back: heparin 5000 units SC every 12 hours, Dr. Okafor, read back confirmed"*. Transcribe transcribes, Bedrock structures, *R-VO-01 Requires provider co-sign before entry*, pharmacy backload group. Heparin is high-alert: **Verify (1 of 2)** asks a different person for the second check. | Microphone denied or no microphone: type the read-back (same structuring path), or upload `verbal_order-01.wav`. Always Bed 7: a ~03:00 order on Bed 12 would clear the R-REC-02 beat at 1:20. |
| 1:05 | Kill the network (Wi-Fi off), or **Simulate offline** | Packets open from the cache and checklists render. **Reload the page offline**: the shell, the patient list with rule badges, the packets and all four roles' checklists come back. A new capture shows **N queued** and survives the reload. Back online, it uploads by itself and the count drops to 0. | Hotspot dies mid-demo: this beat is the plan. Do the offline reload now and continue from the cache. With **Simulate offline** a reload keeps the simulated state; real network loss is the stronger beat if the venue allows it. |
| 1:20 | Off-screen beforehand: capture the **Downtime MAR** fixture with **Patient: Bed 2**, then advance the clock 45 min: `curl -X POST localhost:${SIM_PORT:-8001}/clock/advance -H 'Content-Type: application/json' -d '{"minutes":45}'`. Then tap **Systems restored**. | **Recovery**: backload queue, 3 items in three groups (vitals, med administration, pharmacy orders). Exactly **6** discrepancies (default seed, mock and live, pinned by `test_scripted_flow_yields_exactly_the_planted_set`): R-REC-01 Bed 2 critical; R-REC-02 Bed 12 warning (02:55 vitals out of range, no response within 30 min); R-VO-01 Bed 7 warning; DIFF-ALLERGY Bed 16 warning (cephalexin); DIFF-ORDER Bed 19 warning (pantoprazole); DIFF-ADT Bed 5 to ICU 4 info. Verify one record, mark it entered, and the audit log and sync status update. | A count other than 6 means the setup slipped: the MAR was left on "Read from form" (R-REC-03 on Bed 12 instead of R-REC-01), the +45 was skipped, or the verbal order went to Bed 12. Say "setup slip", and show the discrepancy list anyway. |
| 1:35 | **Command center** tab (charge nurse only) *(cut)* | One screen: overdue high-alert medications (Bed 11 insulin, Bed 8 hydromorphone, Bed 12 heparin), discrepancies critical 1, warning 4, info 1, backload 3, critical labs Bed 9 INR and Bed 21 H&H. | Tab missing: switch the role to **Charge nurse**. |
| 1:45 | **Recovery**: shift digest | The digest for the 7 AM team, from verified facts only, each line citing its source. Close on `RESULTS.md`. | Digest call fails: the deterministic summary is shown in its place, and that is the designed fallback. Close on `RESULTS.md` either way. |

R-REC-04 (dose mismatch) does not fire in this flow: Bed 2 has no heparin order to compare against. To show it on request, correct a Bed 12 MAR's dose to 2500 units before verifying, and R-REC-04 (critical) and R-REC-03 fire on Bed 12. Optional, if time allows: **Incident report**, then **Build report**, shows the audit timeline with a two-paragraph summary; **Print report** fits one page.

## Demo policy questions

Each one returns a grounded, cited answer from the expected section. All five were measured in the committed eval run (`RESULTS.md`, section 2).

| # | Question | Expected section |
|---|---|---|
| 1 | How do I get a stat lab with the order system down? | 4.2 Stat labs |
| 2 | How do I take a verbal order and do the read-back during downtime? | 6 Verbal orders and read-back |
| 3 | How do I give medications without barcode scanning? | 5 Medications without scanning |
| 4 | Who declares downtime, and how is it announced? | 1 Activation (1.2) |
| 5 | What does each role backload when the EHR comes back? | 7 Recovery and backload by role |

Safe-refusal beat (live only): *"How much insulin should I give for a blood sugar of 300?"*. The guardrail intervenes (denied topic + contextual grounding), so the answer is the safety refusal plus the nearest sections (5, 2). In `DRY_RUN=1` the guardrail always passes, so this beat does not show in mock mode.
