#!/usr/bin/env node
// Cuts, captions and encodes a recording from record.mjs into the deliverables, then enforces the mechanical gates:
// final MP4 85-95 s, MP4 <= 10 MB, GIF <= 5 MB (nonzero exit on any violation).
//
//   node postprocess.mjs [--run <run dir> | --latest live|dry_run] [--gif-beat <id>] [--gif-from <marker>] [--gif-seconds N]
//
// Writes into the run directory: captions.ass, plan.json (cuts, kept segments, the exact ffmpeg commands), then
// demo.mp4 + demo.gif (live) or demo-dryrun.mp4 + demo-dryrun.gif (DRY_RUN footage, never a deliverable).
// FFMPEG / FFPROBE override the binaries. Without them the plan and captions are still written, the commands are
// printed, and the exit code is 3 (pending ffmpeg).
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { DIR, loadManifest } from './lib/manifest.mjs';
import {
  planCuts, keepSegments, totalS, budgetReport, buildAss, mp4Args, gifArgs, gifWindow, firstFramePts, probeDuration,
  probeFirstFrameArgs, probeDurationArgs, checkGates, GATES,
} from './lib/post.mjs';

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : dflt; };
const FFMPEG = process.env.FFMPEG || 'ffmpeg';
const FFPROBE = process.env.FFPROBE || 'ffprobe';
const PENDING = 3;

function die(msg, code = 1) {
  console.error(`postprocess: ${msg}`);
  process.exit(code);
}

const has = (bin) => spawnSync(bin, ['-version'], { stdio: 'ignore' }).status === 0;
const quote = (a) => (/^[\w@%+=:,./-]+$/.test(a) ? a : `'${a.replace(/'/g, "'\\''")}'`);
const shell = (bin, argv) => [bin, ...argv].map(quote).join(' ');

function runDir() {
  if (opt('run')) return path.resolve(opt('run'));
  const mode = opt('latest', 'live');
  const pointer = path.join(DIR, 'out', `latest-${mode}`);
  if (!fs.existsSync(pointer)) die(`no ${mode} recording yet (${path.relative(process.cwd(), pointer)} missing); run the recorder first`);
  return path.join(DIR, 'out', fs.readFileSync(pointer, 'utf8').trim());
}

function probe(argv) {
  return JSON.parse(execFileSync(FFPROBE, argv, { encoding: 'utf8' }));
}

const RUN = runDir();
const timeline = JSON.parse(fs.readFileSync(path.join(RUN, 'timeline.json'), 'utf8'));
if (timeline.aborted) die(`the recording aborted (${timeline.aborted}); record again`);
const { manifest } = loadManifest();
const mode = timeline.mode;
const base = mode === 'live' ? 'demo' : 'demo-dryrun';
const raw = path.join(RUN, timeline.video);
if (!fs.existsSync(raw)) die(`no raw video at ${raw}`);

const labels = Object.fromEntries(manifest.beats.map((b) => [b.id, b.speedup_label]));
const cuts = planCuts(timeline, { labels });
const segments = keepSegments(timeline, cuts);
const report = budgetReport(timeline, cuts);
const over = report.filter((r) => r.over);
if (over.length) die(`beats over budget: ${over.map((r) => `${r.id} ${r.post_cut_s} s > ${r.max_s} s`).join(', ')}`);
const planned = totalS(segments);
if (planned < GATES.minDurationS || planned > GATES.maxDurationS) {
  die(`the planned cut is ${planned.toFixed(2)} s, outside ${GATES.minDurationS}-${GATES.maxDurationS} s; record again`);
}

const tools = { ffmpeg: has(FFMPEG), ffprobe: has(FFPROBE) };
// Global offset: the raw video's first-frame PTS (0 until ffprobe can read it).
const ptsOffset = tools.ffprobe ? firstFramePts(probe(probeFirstFrameArgs(raw))) : 0;

const ass = buildAss({ manifest, mode, timeline, segments, cuts });
fs.writeFileSync(path.join(RUN, 'captions.ass'), ass);
const mp4 = `${base}.mp4`;
const gif = `${base}.gif`;
const gifBeat = opt('gif-beat', manifest.gif.beat);
const gifFrom = opt('gif-from', gifBeat === manifest.gif.beat ? manifest.gif.from : undefined);
const win = gifWindow({ timeline, segments, beat: gifBeat, from: gifFrom, seconds: Number(opt('gif-seconds', manifest.gif.seconds)) });
const mp4Argv = mp4Args({ input: timeline.video, ass: 'captions.ass', output: mp4, segments, timeline, ptsOffset });
const gifArgv = gifArgs({ input: mp4, output: gif, startS: win.startS, seconds: win.seconds });
const plan = {
  mode, run: path.basename(RUN), pts_offset_s: ptsOffset, pts_offset_source: tools.ffprobe ? 'ffprobe first frame' : 'pending ffprobe (0 assumed)',
  planned_s: +planned.toFixed(2), budget: report, cuts, segments,
  gif: { beat: gifBeat, from: gifFrom || null, start_s: +win.startS.toFixed(3), seconds: +win.seconds.toFixed(3) },
  commands: { cwd: RUN, mp4: shell(FFMPEG, mp4Argv), gif: shell(FFMPEG, gifArgv) },
};
fs.writeFileSync(path.join(RUN, 'plan.json'), `${JSON.stringify(plan, null, 2)}\n`);
console.log(`plan: ${planned.toFixed(2)} s after ${cuts.length} cuts (${cuts.filter((c) => c.label).length} labelled "sped up"); captions.ass and plan.json written to ${path.relative(process.cwd(), RUN) || '.'}`);

if (!tools.ffmpeg || !tools.ffprobe) {
  const missing = Object.entries(tools).filter(([, ok]) => !ok).map(([k]) => k).join(' and ');
  console.error(`\npostprocess: ${missing} not found. Install it with \`brew install ffmpeg\` (or set FFMPEG / FFPROBE), then run`);
  console.error(`  make demo-video-post RUN=${path.relative(path.resolve(DIR, '..', '..'), RUN)}`);
  console.error('The encoder stage is pending. It will run, from the run directory:');
  console.error(`  ${plan.commands.mp4}`);
  console.error(`  ${plan.commands.gif}`);
  console.error('(the trim times above assume a first-frame PTS of 0 until ffprobe reads the real one)');
  process.exit(PENDING);
}

for (const [what, argv] of [['MP4', mp4Argv], ['GIF', gifArgv]]) {
  console.log(`encoding ${what}…`);
  const r = spawnSync(FFMPEG, argv, { cwd: RUN, stdio: ['ignore', 'inherit', 'inherit'] });
  if (r.status !== 0) die(`ffmpeg failed on the ${what} (exit ${r.status})`);
}

const mp4DurationS = probeDuration(probe(probeDurationArgs(path.join(RUN, mp4))));
const sizes = { mp4: fs.statSync(path.join(RUN, mp4)).size, gif: fs.statSync(path.join(RUN, gif)).size };
const violations = checkGates({ mp4DurationS, mp4Bytes: sizes.mp4, gifBytes: sizes.gif });
plan.result = { mp4_duration_s: mp4DurationS, mp4_bytes: sizes.mp4, gif_bytes: sizes.gif, violations };
fs.writeFileSync(path.join(RUN, 'plan.json'), `${JSON.stringify(plan, null, 2)}\n`);
console.log(`${mp4}: ${mp4DurationS.toFixed(2)} s, ${sizes.mp4} bytes; ${gif}: ${sizes.gif} bytes`);
if (Math.abs(mp4DurationS - planned) > 0.5) console.warn(`postprocess: the MP4 is ${mp4DurationS.toFixed(2)} s against ${planned.toFixed(2)} s planned`);
if (mode !== 'live') console.log('DRY RUN footage: development only, never a deliverable.');
if (violations.length) die(`gate violations:\n  ${violations.join('\n  ')}`);
console.log('gates passed: duration, MP4 size, GIF size');
