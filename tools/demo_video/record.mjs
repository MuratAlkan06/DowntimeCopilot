#!/usr/bin/env node
// Records DEMO.md's 90-second cut with Playwright, beat by beat from beats.json, and writes the raw video plus
// timeline.json for postprocess.mjs. It starts its own simulator and app (uvicorn, no --reload) on non-default
// ports with a scratch store, seeds them with `make demo`, advances the unit clock 15 minutes and asserts the
// DEMO.md clean state before any beat runs. Use the make targets: `make demo-video-dryrun` (DRY_RUN=1, footage is
// never a deliverable) or `make demo-video` (live, after `source .env`).
//
//   node record.mjs --mode dry_run|live [--headed] [--shots] [--simulate-latency S (dry run only)]
//
// Output: tools/demo_video/out/<UTC stamp>-<mode>/ (raw.webm, timeline.json, service and browser logs, the scratch
// store; --shots adds a screenshot per caption switch and per beat end). Exit 1 on any failed assert or a beat over
// budget, 2 on a refusal before anything starts. Env: VIDEO_APP_PORT (8800), VIDEO_SIM_PORT (8801), PY (the repo's
// .venv/bin/python).
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { chromium } from 'playwright';
import { DIR, loadManifest, expected } from './lib/manifest.mjs';
import { planCuts, beatPostCutS, postCutSpanS, budgetReport, readS, TARGET_MARGIN_S } from './lib/timeline.mjs';

const ROOT = path.resolve(DIR, '..', '..');
const args = process.argv.slice(2);
const flag = (name) => args.includes(`--${name}`);
const opt = (name, dflt) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : dflt; };
const MODE = opt('mode');
// Development aid, dry run only: hold each model-backed request this long, so a dry run exercises the trim and cut
// path the way live latency does (the mocks answer in milliseconds). Recorded in timeline.json.
const LATENCY_S = Number(opt('simulate-latency', 0));
const APP_PORT = Number(process.env.VIDEO_APP_PORT || 8800);
const SIM_PORT = Number(process.env.VIDEO_SIM_PORT || 8801);
const PY = process.env.PY || path.join(ROOT, '.venv', 'bin', 'python');
const APP = `http://localhost:${APP_PORT}`;
const SIM = `http://127.0.0.1:${SIM_PORT}`;
const API = `http://127.0.0.1:${APP_PORT}`;
const HOLD_MIN_S = 1.0; // the shortest end-of-beat hold: what the viewer gets even when a beat runs long
const INSTANT_MS = 120; // a trimWait satisfied this fast was already done: no still windows, nothing to cut
const IDLE_MS = 500;

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));
const log = (...m) => console.log('[record]', ...m);
const fail = (msg) => { throw new Error(msg); };

// ---------------------------------------------------------------- refusals before anything starts

function refuse(msg) {
  console.error(`record.mjs: ${msg}`);
  process.exit(2);
}

if (!['live', 'dry_run'].includes(MODE)) refuse('pass --mode dry_run or --mode live (use the make targets)');
const truthy = (v) => !['', '0', 'false', 'no'].includes(String(v ?? '').trim().toLowerCase());
if (MODE === 'live') {
  if (!process.env.AWS_PROFILE) refuse('live mode needs AWS_PROFILE: `source .env` first (make infra-env writes it)');
  if (truthy(process.env.DRY_RUN)) refuse('live mode with DRY_RUN set would record mock footage as live; unset DRY_RUN');
}
if (!fs.existsSync(PY)) refuse(`no python at ${PY}: run \`make setup\` (or set PY)`);
if (!(LATENCY_S >= 0)) refuse('--simulate-latency takes seconds');
if (LATENCY_S && MODE !== 'dry_run') refuse('--simulate-latency is a dry-run development aid; live footage keeps its real latency');

const { manifest } = (() => { try { return loadManifest(); } catch (e) { return refuse(e.message); } })();

function portBusy(port) {
  const connect = (host) => new Promise((resolve) => {
    const s = net.connect({ host, port });
    s.once('connect', () => { s.destroy(); resolve(true); });
    s.once('error', () => resolve(false));
    s.setTimeout(500, () => { s.destroy(); resolve(false); });
  });
  const bind = () => new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(true));
    srv.listen(port, '127.0.0.1', () => srv.close(() => resolve(false)));
  });
  return Promise.all([connect('127.0.0.1'), connect('::1'), bind()]).then((r) => r.some(Boolean));
}

// ---------------------------------------------------------------- run directory and services

const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
const RUN = path.join(DIR, 'out', `${stamp}-${MODE}`);
const SHOTS = path.join(RUN, 'shots');
const children = [];

function childEnv() {
  return {
    ...process.env,
    APP_PORT: String(APP_PORT), SIM_PORT: String(SIM_PORT), SIM_URL: `http://localhost:${SIM_PORT}`,
    STORE_PATH: path.join(RUN, 'store.sqlite'),
    DRY_RUN: MODE === 'dry_run' ? '1' : '0',
    // The in-app scheduler would re-pull the snapshot at 03:00, right after the +15 advance, and the banner would no
    // longer read DEMO.md's "02:45 AM · 15 min ago". The scripted harnesses (evals/run.py) turn it off the same way.
    SNAPSHOT_INTERVAL_SECONDS: '0',
  };
}

function startService(name, target, port) {
  const out = fs.openSync(path.join(RUN, `${name}.log`), 'a');
  const child = spawn(PY, ['-m', 'uvicorn', target, '--port', String(port)], { cwd: ROOT, env: childEnv(), stdio: ['ignore', out, out] });
  children.push({ name, child });
  child.once('exit', (code, sig) => { if (!child.stopping) log(`${name} exited early (code ${code}, signal ${sig})`); });
  return child;
}

async function waitHttp(url, timeoutMs = 20000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    try { if ((await fetch(url)).ok) return; } catch { /* not up yet */ }
    await sleep(200);
  }
  fail(`${url} did not answer within ${timeoutMs / 1000} s`);
}

async function stopServices() {
  for (const { child } of children.reverse()) {
    if (child.exitCode != null || child.signalCode) continue;
    child.stopping = true;
    child.kill('SIGTERM');
    const gone = await Promise.race([new Promise((r) => child.once('exit', () => r(true))), sleep(5000).then(() => false)]);
    if (!gone) child.kill('SIGKILL');
  }
}

async function json(url, init) {
  const r = await fetch(url, { ...init, headers: { 'Content-Type': 'application/json', 'X-Role': 'charge_nurse', ...(init?.headers || {}) } });
  if (!r.ok) fail(`${init?.method || 'GET'} ${url} answered ${r.status}`);
  return r.json();
}

const advanceClock = (minutes) => json(`${SIM}/clock/advance`, { method: 'POST', body: JSON.stringify({ minutes }) });

// ---------------------------------------------------------------- timeline

const timeline = {
  schema: 1,
  mode: MODE,
  recorded_utc: new Date().toISOString(),
  git: (() => {
    try {
      const sha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT }).toString().trim();
      const dirty = execFileSync('git', ['status', '--porcelain', '--untracked-files=no'], { cwd: ROOT }).toString().trim() !== '';
      return { sha, dirty };
    } catch { return null; }
  })(),
  playwright: JSON.parse(fs.readFileSync(path.join(DIR, 'node_modules', 'playwright', 'package.json'), 'utf8')).version,
  viewport: { width: 1280, height: 800 },
  app_url: `${APP}/`,
  ports: { app: APP_PORT, sim: SIM_PORT },
  simulated_latency_s: LATENCY_S || null,
  video: 'raw.webm',
  context_start_ms: null,
  context_end_ms: null,
  stills: [],
  beats: [],
};

const still = (id, beat, start, end) => timeline.stills.push({ id, beat, start_ms: start, end_ms: end });

// ---------------------------------------------------------------- browser helpers

let browser; let context; let page;
let inflight = 0; let lastNet = Date.now();
let offlineNow = false;

const MODEL_BACKED = /\/(downtime\/start|policy\/ask|capture|recovery\/restore|digest)(\?|$)/;

async function simulateLatency(ctx) {
  await ctx.route(MODEL_BACKED, async (route) => {
    if (!offlineNow) await sleep(LATENCY_S * 1000);
    await route.fallback();
  });
}

function trackNetwork(ctx) {
  const done = () => { inflight = Math.max(0, inflight - 1); lastNet = Date.now(); };
  ctx.on('request', () => { inflight += 1; lastNet = Date.now(); });
  ctx.on('requestfinished', done);
  ctx.on('requestfailed', done);
}

/** Network idle: no request in flight for IDLE_MS (page and service worker requests both count). */
async function idle(timeoutMs = 30000) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (inflight === 0 && Date.now() - lastNet >= IDLE_MS) return;
    await sleep(50);
  }
  fail(`network not idle within ${timeoutMs / 1000} s (${inflight} in flight)`);
}

const loc = (sel, text) => (text ? page.locator(sel, { hasText: text }) : page.locator(sel)).first();

async function waitFor({ sel, text, state = 'visible', timeout_s: t = 30 }) {
  try {
    await loc(sel, text).waitFor({ state, timeout: t * 1000 });
  } catch {
    fail(`timed out after ${t} s waiting for ${sel}${text ? ` with "${text}"` : ''} (${state})`);
  }
}

async function assertText(a) {
  const want = expected(a, MODE);
  const el = page.locator(a.sel).first();
  try { await el.waitFor({ state: 'visible', timeout: 5000 }); } catch { fail(`assertText: ${a.sel} is not visible (wanted "${want}")`); }
  const got = await el.innerText();
  if (!got.includes(want)) fail(`assertText: ${a.sel} reads "${got.trim().slice(0, 200)}", not "${want}"`);
}

/** Smooth scroll. "start" lands the element just under the sticky phase bar, "center" centres it, "top" goes home. */
async function scroll({ sel, block = 'start' }) {
  await page.locator(sel).first().evaluate((el, b) => {
    if (b === 'top') { window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
    if (b === 'center') { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
    const bar = document.querySelector('.phase-nav')?.getBoundingClientRect().height || 0;
    window.scrollTo({ top: Math.max(0, el.getBoundingClientRect().top + window.scrollY - bar - 12), behavior: 'smooth' });
  }, block);
  // Settle: the scroll position (window and any scrolled pane) unchanged for 150 ms, at most 1.5 s.
  const end = Date.now() + 1500;
  let last = null; let since = Date.now();
  while (Date.now() < end) {
    const pos = await page.evaluate(() => [window.scrollY, ...[...document.querySelectorAll('.list-pane')].map((e) => e.scrollTop)].join(','));
    if (pos !== last) { last = pos; since = Date.now(); } else if (Date.now() - since >= 150) break;
    await sleep(40);
  }
}

// ---------------------------------------------------------------- named checks (structural, never model prose)

const CHECKS = {
  async preflight() {
    const pf = manifest.preflight;
    await waitFor({ sel: '#snapshot-banner', text: pf.banner, timeout_s: 15 });
    await waitFor({ sel: `button.patient-row[data-id="pt-12"] .rule-badge`, text: pf.rule_on_bed12, timeout_s: 15 });
    const rows = await page.locator('button.patient-row').count();
    if (rows !== pf.patients) fail(`preflight: ${rows} patient rows, expected ${pf.patients}`);
    await CHECKS.modeGuard();
  },

  /** No-mock guard, both directions: live footage must come from real services (DynamoDB sync, no canned model call
   *  logged), dry-run footage from the mocks (dry-run sync, every logged model call canned). */
  async modeGuard() {
    const sync = await json(`${API}/sync/status`);
    const calls = (await json(`${API}/costs`)).calls || [];
    const mocked = calls.filter((c) => c.dry_run).length;
    if (MODE === 'live' && (sync.mode !== 'dynamodb' || mocked)) {
      fail(`no-mock guard: live run but sync mode is "${sync.mode}" and ${mocked} of ${calls.length} model calls were canned`);
    }
    if (MODE === 'dry_run' && (sync.mode !== 'dry_run' || mocked !== calls.length)) {
      fail(`dry-run guard: dry run but sync mode is "${sync.mode}" and ${calls.length - mocked} model calls were real`);
    }
  },

  async policy(a) {
    const chips = (await page.locator('#policy-answer .policy-answer .chips .chip').allInnerTexts()).map((t) => t.trim());
    for (const c of a.chips) if (!chips.includes(c)) fail(`policy: chip "${c}" missing (chips: ${chips.join(' | ')})`);
    const badges = (await page.locator('#policy-answer .citations .section-badge').allInnerTexts()).map((t) => t.trim());
    const re = new RegExp(`^§ ${a.section.replace(/\./g, '\\.')}(\\.\\d+)*$`);
    if (!badges.some((b) => re.test(b))) fail(`policy: no citation at section ${a.section} (got ${badges.join(', ') || 'none'})`);
  },

  async offlineShell() {
    await waitFor({ sel: '#offline-banner', timeout_s: 15 });
    await waitFor({ sel: '#checklist-items .checklist-item', timeout_s: 15 });
    await waitFor({ sel: '#queue-count', text: '1 queued', timeout_s: 15 });
    await waitFor({ sel: '#checklist-cached', text: 'all 4 roles', timeout_s: 15 });
  },

  async cachedList() {
    await waitFor({ sel: 'button.patient-row[data-id="pt-12"] .rule-badge', text: 'R-DUE-01', timeout_s: 15 });
    await waitFor({ sel: '#patient-list .cached-note', timeout_s: 5 });
    const rows = await page.locator('button.patient-row').count();
    if (rows !== manifest.preflight.patients) fail(`cachedList: ${rows} cached patient rows`);
  },

  /** The offline capture is fully processed: chip gone, its record card on screen, and the server holds the Bed 2
   *  MAR in the backload queue. Moving on before this loses R-REC-01 at restore. */
  async drained() {
    if (await page.locator('#queue-count').isVisible()) return false;
    const card = page.locator('#capture-result article.record .record-title', { hasText: 'Downtime MAR' });
    if (!(await card.count())) return false;
    const queue = await json(`${API}/backload`);
    return (queue.nursing_med_admin || []).some((r) => r.form_type === 'downtime_mar' && r.patient_id === 'pt-02');
  },

  async planted(a) {
    const items = await page.locator('li.disc-item').count();
    const rules = (await page.locator('li.disc-item > .rule-badge').allInnerTexts()).map((t) => t.trim()).sort();
    const groups = await page.locator('#backload .bl-group:not(.empty)').count();
    if (items !== a.items || JSON.stringify(rules) !== JSON.stringify([...a.rules].sort()) || groups !== a.groups) {
      fail(`planted-set guard: ${items} discrepancies [${rules.join(', ')}] in ${groups} backload groups; `
        + `expected ${a.items} [${a.rules.join(', ')}] in ${a.groups} (DEMO.md 1:20 "setup slip")`);
    }
  },

  async syncChip() {
    await waitFor({ sel: '#sync-chip .chip', timeout_s: 15 });
    const text = (await page.locator('#sync-chip .chip').innerText()).trim();
    if (MODE === 'live' && (/dry run|local export/i.test(text) || !/DynamoDB/.test(text))) fail(`no-mock guard: sync chip reads "${text}"`);
    if (MODE === 'dry_run' && !/dry run/.test(text)) fail(`dry-run guard: sync chip reads "${text}"`);
    await CHECKS.modeGuard();
  },
};

async function check(a) {
  const fn = CHECKS[a.name] || fail(`unknown check ${a.name}`);
  return fn(a);
}

async function until(name, timeoutMs) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    if (await check({ name })) return;
    await sleep(250);
  }
  fail(`timed out after ${timeoutMs / 1000} s waiting for ${name}`);
}

// ---------------------------------------------------------------- beat actions

const stillMs = () => manifest.still_s * 1000;

async function pause(beat, s, id) {
  const t0 = Date.now();
  await sleep(s * 1000);
  still(id, beat.id, t0, Date.now());
}

/** A wait the cut may shorten: a still window right after the trigger (the loading state), the wait, a still window
 *  once it is done (the result). The cut runs between the two windows' centres. A wait that is over before the first
 *  window ends gets no windows and no cut. */
async function trimWait(beat, a) {
  const t0 = Date.now();
  const cond = a.check ? until(a.check, a.timeout_s * 1000) : waitFor(a);
  const done = cond.then(() => Date.now());
  done.catch(() => {});
  const early = await Promise.race([done, sleep(INSTANT_MS).then(() => null)]);
  if (early) { beat.waits.push({ id: a.id, start_ms: t0, end_ms: early, a: null, b: null }); return; }
  const aEnd = t0 + stillMs();
  const inside = await Promise.race([done, sleep(aEnd - Date.now()).then(() => null)]);
  if (inside) { beat.waits.push({ id: a.id, start_ms: t0, end_ms: inside, a: null, b: null }); return; }
  still(`${a.id}:a`, beat.id, t0, aEnd);
  const tDone = await done;
  await sleep(stillMs());
  const bEnd = Date.now();
  still(`${a.id}:b`, beat.id, tDone, bEnd);
  beat.waits.push({ id: a.id, start_ms: t0, end_ms: tDone, a: [t0, aEnd], b: [tDone, bEnd] });
  log(`  ${beat.id}/${a.id}: waited ${((tDone - t0) / 1000).toFixed(1)} s`);
}

const beatCuts = (beat) => planCuts(timeline, { beats: [beat], labels: { [beat.id]: beat.speedup_label } });

/** Seconds a caption (or notice) shown since `from` still needs to reach its reading time, post-cut. */
const unread = (beat, shown, now = Date.now()) => (shown ? readS(shown.text) - postCutSpanS(beatCuts(beat), shown.from, now) : 0);

/** A mark switches the caption (or starts a notice) that names it: the outgoing caption first gets its reading time,
 *  as a still pause, so no caption leaves the screen before it can be read. */
async function mark(beat, id) {
  const mb = manifest.beats.find((b) => b.id === beat.id);
  const next = mb.captions.find((c) => c.at === id);
  if (next) {
    const wait = unread(beat, beat.caption);
    if (wait > 0) await pause(beat, wait, `${beat.id}:read`);
  }
  beat.markers[id] = Date.now();
  if (next) beat.caption = { from: beat.markers[id], text: (MODE === 'dry_run' && next.text_dry_run) || next.text };
  if (mb.notice?.at === id) beat.notice = { from: beat.markers[id], text: (MODE === 'dry_run' && mb.notice.text_dry_run) || mb.notice.text };
}

/** End of beat: pad to max_s minus a margin, never less than HOLD_MIN_S or what the last caption and a late notice
 *  still need to be read. */
async function hold(beat) {
  if (flag('shots')) await page.screenshot({ path: path.join(SHOTS, `${beat.id}.png`) });
  const now = Date.now();
  const post = beatPostCutS(beat, beatCuts(beat), now);
  const target = beat.max_s - TARGET_MARGIN_S;
  const s = Math.max(HOLD_MIN_S, target - post, unread(beat, beat.caption, now), unread(beat, beat.notice, now));
  if (post + s > beat.max_s) log(`  ${beat.id}: OVER BUDGET, ${(post + s).toFixed(1)} s against ${beat.max_s} s`);
  await pause(beat, s, `${beat.id}:hold`);
}

async function run(beat, a) {
  switch (a.do) {
    case 'pause': return pause(beat, a.s, `${beat.id}:pause`);
    case 'hold': return hold(beat);
    case 'mark':
      if (flag('shots')) await page.screenshot({ path: path.join(SHOTS, `${beat.id}-${a.id}.png`) });
      return mark(beat, a.id);
    case 'click': {
      const el = loc(a.sel, a.text);
      await el.waitFor({ state: 'visible', timeout: 10000 });
      return el.click();
    }
    case 'select': return page.selectOption(a.sel, a.value);
    case 'upload': return page.setInputFiles(a.sel, path.join(ROOT, a.file));
    case 'type': {
      const el = page.locator(a.sel).first();
      await el.fill('');
      return el.pressSequentially(a.value, { delay: 90 });
    }
    case 'scroll': return scroll(a);
    case 'idle': return idle();
    case 'waitFor': return waitFor(a);
    case 'trimWait': return trimWait(beat, a);
    case 'assertText': return assertText(a);
    case 'check': return check(a);
    case 'offline': offlineNow = a.value; return context.setOffline(a.value);
    case 'reload': return page.reload({ waitUntil: 'domcontentloaded' });
    case 'clock': {
      const r = await advanceClock(a.minutes);
      beat.markers[`clock+${a.minutes}`] = Date.now();
      return log(`  unit clock advanced ${a.minutes} min (off screen): ${r.now}`);
    }
    default: return fail(`unknown action ${a.do}`);
  }
}

// ---------------------------------------------------------------- main

async function main() {
  for (const port of [APP_PORT, SIM_PORT]) {
    if (await portBusy(port)) refuse(`port ${port} is in use; this never stops a process it did not start. Free it or set VIDEO_APP_PORT / VIDEO_SIM_PORT`);
  }
  fs.mkdirSync(flag('shots') ? SHOTS : RUN, { recursive: true });
  log(`mode ${MODE}, run ${path.relative(ROOT, RUN)}, app :${APP_PORT}, sim :${SIM_PORT}`);

  startService('sim', 'simulator.main:app', SIM_PORT);
  await waitHttp(`${SIM}/clock`);
  startService('app', 'app.main:app', APP_PORT);
  await waitHttp(`${API}/openapi.json`);
  execFileSync('make', ['demo', `APP_PORT=${APP_PORT}`, `SIM_PORT=${SIM_PORT}`, `PY=${PY}`], { cwd: ROOT, env: childEnv(), stdio: ['ignore', 'inherit', 'inherit'] });
  const clock = await advanceClock(15);
  log(`unit clock ${clock.now} (+15 after the reseed)`);

  browser = await chromium.launch({ headless: !flag('headed') });
  context = await browser.newContext({
    viewport: timeline.viewport,
    locale: 'en-US',
    recordVideo: { dir: path.join(RUN, 'video-tmp'), size: { ...timeline.viewport } },
  });
  trackNetwork(context);
  if (LATENCY_S) await simulateLatency(context);
  const logFile = path.join(RUN, 'browser.log');
  timeline.context_start_ms = Date.now();
  page = await context.newPage();
  page.on('console', (m) => fs.appendFileSync(logFile, `[${m.type()}] ${m.text()}\n`));
  page.on('pageerror', (e) => fs.appendFileSync(logFile, `[pageerror] ${e.message}\n`));

  let aborted = null;
  try {
    // Pre-roll (trimmed off by postprocess): the bare app URL, the service worker in control, the role, and the
    // DEMO.md clean-state checks. Nothing costs money before Declare, so a failure here is cheap.
    await page.goto(`${APP}/`, { waitUntil: 'load' });
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.waitForFunction(() => navigator.serviceWorker.controller, null, { timeout: 15000 });
    await page.selectOption('#role-select', 'charge_nurse');
    await idle();
    await CHECKS.preflight();
    log('preflight ok: banner, 12 patients, R-DUE-01 on Bed 12, mode guard');
    await page.evaluate(() => window.scrollTo(0, 0));
    await pause({ id: 'preroll' }, manifest.still_s, 'preroll:edge');

    for (const mb of manifest.beats) {
      const beat = { id: mb.id, max_s: mb.max_s, speedup: !!mb.speedup, speedup_label: mb.speedup_label, start_ms: Date.now(), end_ms: null, markers: {}, waits: [] };
      const first = mb.captions[0];
      beat.caption = { from: beat.start_ms, text: (MODE === 'dry_run' && first.text_dry_run) || first.text };
      if (mb.notice && !mb.notice.at) beat.notice = { from: beat.start_ms, text: mb.notice.text };
      timeline.beats.push(beat);
      log(`beat ${mb.id} (DEMO.md ${mb.demo.time}, budget ${mb.max_s} s)`);
      for (const a of mb.actions) await run(beat, a);
      beat.end_ms = Date.now();
      delete beat.caption;
      delete beat.notice;
    }
    await pause({ id: 'tail' }, manifest.still_s, 'tail:edge');
    await CHECKS.modeGuard();
  } catch (e) {
    aborted = e.message;
    console.error(`record.mjs ABORT: ${aborted}`);
    try { await page.screenshot({ path: path.join(RUN, 'abort.png') }); } catch { /* page gone */ }
  }

  timeline.context_end_ms = Date.now();
  await context.close();
  await page.video().saveAs(path.join(RUN, 'raw.webm'));
  await page.video().delete();
  await browser.close();
  fs.rmSync(path.join(RUN, 'video-tmp'), { recursive: true, force: true });

  if (aborted) {
    timeline.aborted = aborted;
    fs.writeFileSync(path.join(RUN, 'timeline.json'), `${JSON.stringify(timeline, null, 2)}\n`);
    return 1;
  }
  const labels = Object.fromEntries(manifest.beats.map((b) => [b.id, b.speedup_label]));
  const cuts = planCuts(timeline, { labels });
  const report = budgetReport(timeline, cuts);
  timeline.planned = { cuts, budget: report, total_s: +report.reduce((s, r) => s + r.post_cut_s, 0).toFixed(2) };
  fs.writeFileSync(path.join(RUN, 'timeline.json'), `${JSON.stringify(timeline, null, 2)}\n`);
  fs.writeFileSync(path.join(DIR, 'out', `latest-${MODE}`), `${path.basename(RUN)}\n`);

  console.log('\nbeat         wall s   post-cut s   budget s');
  for (const r of report) console.log(`${r.id.padEnd(12)} ${r.wall_s.toFixed(1).padStart(6)}   ${r.post_cut_s.toFixed(1).padStart(10)}   ${String(r.max_s).padStart(8)}${r.over ? '   OVER' : ''}`);
  console.log(`total post-cut ${timeline.planned.total_s} s; ${cuts.length} cuts, ${cuts.filter((c) => c.label).length} labelled "sped up"`);
  console.log(`timeline: ${path.relative(ROOT, path.join(RUN, 'timeline.json'))}`);
  return report.some((r) => r.over) ? 1 : 0;
}

let code = 1;
try {
  code = await main();
} catch (e) {
  console.error(`record.mjs: ${e.message}`);
} finally {
  try { await browser?.close(); } catch { /* already closed */ }
  await stopServices();
}
process.exit(code);
