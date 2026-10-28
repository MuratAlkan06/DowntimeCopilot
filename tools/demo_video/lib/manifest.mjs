// beats.json (the single source of truth for the video) and ui-strings.json (the caption allowlist). The same rules
// are pinned without node by tests/test_demo_beats.py.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readS } from './timeline.mjs';

export const DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const MODES = ['live', 'dry_run'];

/** “…” fragments of a caption: each quotes a UI string and must be on the allowlist. */
export const quotes = (text) => [...String(text || '').matchAll(/“([^”]+)”/g)].map((m) => m[1]);

export function loadManifest(dir = DIR) {
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'beats.json'), 'utf8'));
  const allow = JSON.parse(fs.readFileSync(path.join(dir, 'ui-strings.json'), 'utf8'));
  const problems = validate(manifest, allow);
  if (problems.length) throw new Error(`beats.json is invalid:\n  ${problems.join('\n  ')}`);
  return { manifest, allow };
}

/** An assertText action's expected string in a mode. */
export const expected = (action, mode) => (mode === 'dry_run' && action.text_dry_run) || action.text;

/** Every caption and notice text of a beat in a mode. */
export function beatTexts(beat, mode) {
  const out = beat.captions.map((c) => (mode === 'dry_run' && c.text_dry_run) || c.text);
  if (beat.notice) out.push((mode === 'dry_run' && beat.notice.text_dry_run) || beat.notice.text);
  return out;
}

export function validate(manifest, allow) {
  const p = [];
  const strings = new Set(allow?.strings || []);
  if (!manifest.opening?.live || !manifest.opening?.dry_run) p.push('opening needs both the live and the dry_run text');
  const ids = new Set();
  for (const b of manifest.beats || []) {
    if (ids.has(b.id)) p.push(`duplicate beat id ${b.id}`);
    ids.add(b.id);
    if (!(b.max_s > 0)) p.push(`${b.id}: max_s must be positive`);
    if (!Array.isArray(b.captions) || !b.captions.length) p.push(`${b.id}: needs at least one caption`);
    if (b.speedup && !/sped up/i.test(b.speedup_label || '')) p.push(`${b.id}: speedup_label must say "sped up"`);
    const marks = new Set((b.actions || []).filter((a) => a.do === 'mark').map((a) => a.id));
    for (const c of [...(b.captions || []), ...(b.notice ? [b.notice] : [])]) {
      if (c.at && !marks.has(c.at)) p.push(`${b.id}: caption marker ${c.at} has no mark action`);
    }
    for (const mode of MODES) {
      const caps = (b.captions || []).map((c) => (mode === 'dry_run' && c.text_dry_run) || c.text);
      const reading = caps.reduce((s, t) => s + readS(t), 0);
      if (reading > b.max_s) p.push(`${b.id} (${mode}): its captions need ${reading.toFixed(1)} s of reading time, over ${b.max_s} s`);
      const asserted = (b.actions || []).filter((a) => a.do === 'assertText').map((a) => expected(a, mode));
      for (const text of beatTexts(b, mode)) {
        for (const q of quotes(text)) {
          if (!strings.has(q)) p.push(`${b.id} (${mode}): quoted “${q}” is not in ui-strings.json`);
          if (!asserted.some((s) => s.includes(q))) p.push(`${b.id} (${mode}): quoted “${q}” is never asserted on screen`);
        }
      }
    }
    if ((b.actions || []).at(-1)?.do !== 'hold') p.push(`${b.id}: the last action must be hold (pads to the budget)`);
  }
  return p;
}
