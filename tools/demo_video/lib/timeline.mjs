// Pure timeline logic for the demo video (no I/O, no Playwright, no ffmpeg): which waits get cut, the kept segments,
// final-cut time, per-beat budgets and caption reading time. record.mjs paces each beat with it while recording;
// postprocess.mjs (through lib/post.mjs) cuts with the same functions, so both see the same plan.
//
// The timeline (timeline.json from record.mjs) holds wall-clock milliseconds: per beat its start/end, markers and
// trim waits (`a` / `b` = the still windows right after the trigger and once the wait is done), plus every still
// window the recorder held.

export const TOLERANCE_S = 0.75; // a cut point must sit this far inside a still window on both sides
export const LABEL_MIN_S = 2; // a cut that removes more than this gets the burned "sped up" label
export const MIN_CUT_S = 1; // shorter waits are kept whole (no cut, no label)
export const TARGET_MARGIN_S = 0.25; // record.mjs pads each beat to max_s minus this
export const READ_S_PER_CHAR = 0.05; // a caption stays up at least this long per character (20 characters a second)
export const READ_MIN_S = 2; // and never less than this
export const STILL_JOIN_MS = 30; // stills this close together, with no action between them, are one window

/** Reading time of a caption or notice: record.mjs keeps each one on screen (post-cut) at least this long. */
export const readS = (text) => Math.max(READ_MIN_S, String(text).length * READ_S_PER_CHAR);

const mid = ([s, e]) => (s + e) / 2;

/** Still windows from timeline.stills, back-to-back ones joined (the pre-roll's last still and the first beat's
 *  opening pause, the last hold and the tail still). */
export function stillWindows(timeline) {
  const out = [];
  for (const w of [...(timeline.stills || [])].sort((a, b) => a.start_ms - b.start_ms)) {
    const last = out[out.length - 1];
    if (last && w.start_ms - last.end_ms <= STILL_JOIN_MS) last.end_ms = Math.max(last.end_ms, w.end_ms);
    else out.push({ start_ms: w.start_ms, end_ms: w.end_ms });
  }
  return out;
}

/** The still window that holds [t - tol, t + tol], or null. */
export function stillAround(timeline, t, tolMs = TOLERANCE_S * 1000) {
  return stillWindows(timeline).find((w) => w.start_ms <= t - tolMs && t + tolMs <= w.end_ms) || null;
}

function cutFor(beat, wait) {
  if (!wait.a || !wait.b) return null; // the wait ended inside its first still window: nothing to cut
  const inMs = mid(wait.a);
  const outMs = mid(wait.b);
  return { beat: beat.id, wait: wait.id, in_ms: inMs, out_ms: outMs, removed_s: (outMs - inMs) / 1000 };
}

export function speedupLabel(template, removedS) {
  const t = template || 'Sped up: {s} s of waiting removed';
  return t.replace('{s}', String(Math.round(removedS)));
}

/** Planned cuts for a whole timeline (or one beat while it is recorded): each runs from the centre of the still window
 *  after a wait's trigger to the centre of the one after it is done. Only waits of beats with `speedup`, only cuts of
 *  at least MIN_CUT_S, and each cut point must sit inside a still window with TOLERANCE_S to spare on both sides
 *  (throws otherwise: the recording broke the "cut only inside still windows" rule). */
export function planCuts(timeline, { beats = timeline.beats, labels = {} } = {}) {
  const cuts = [];
  for (const beat of beats) {
    if (!beat.speedup) continue;
    for (const wait of beat.waits || []) {
      const c = cutFor(beat, wait);
      if (!c || c.removed_s < MIN_CUT_S) continue;
      for (const p of [c.in_ms, c.out_ms]) {
        if (!stillAround(timeline, p)) {
          throw new Error(`cut point ${p} (${beat.id}/${wait.id}) is not inside a still window with ±${TOLERANCE_S} s to spare`);
        }
      }
      c.label = c.removed_s > LABEL_MIN_S ? speedupLabel(labels[beat.id], c.removed_s) : null;
      cuts.push(c);
    }
  }
  return cuts.sort((x, y) => x.in_ms - y.in_ms);
}

/** Post-cut seconds between two wall times: the span minus the cuts that fall inside it. */
export function postCutSpanS(cuts, fromMs, untilMs) {
  const removed = cuts.filter((c) => c.in_ms >= fromMs && c.out_ms <= untilMs).reduce((s, c) => s + c.removed_s, 0);
  return (untilMs - fromMs) / 1000 - removed;
}

/** Post-cut seconds of one beat: its wall span minus what its cuts remove. `untilMs` measures a beat still being
 *  recorded (record.mjs pads with it). */
export function beatPostCutS(beat, cuts, untilMs = beat.end_ms) {
  return postCutSpanS(cuts.filter((c) => c.beat === beat.id), beat.start_ms, untilMs);
}

/** Kept wall-time segments: from the first beat's start to the last beat's end, minus every cut. The head (pre-roll)
 *  and tail trims are cut points too, so they must also sit inside still windows. */
export function keepSegments(timeline, cuts) {
  const beats = timeline.beats;
  const head = beats[0].start_ms;
  const tail = beats[beats.length - 1].end_ms;
  for (const [name, p] of [['head', head], ['tail', tail]]) {
    if (!stillAround(timeline, p)) throw new Error(`${name} trim point ${p} is not inside a still window with ±${TOLERANCE_S} s to spare`);
  }
  const segs = [];
  let at = head;
  for (const c of cuts) {
    if (c.in_ms < at || c.out_ms > tail) throw new Error(`cut ${c.beat}/${c.wait} overlaps another cut or the trims`);
    segs.push([at, c.in_ms]);
    at = c.out_ms;
  }
  segs.push([at, tail]);
  return segs;
}

/** Wall ms -> seconds in the final cut. A time inside a removed span maps to the cut itself. */
export function outputTime(ms, segments) {
  let acc = 0;
  for (const [s, e] of segments) {
    if (ms < s) return acc / 1000;
    if (ms <= e) return (acc + ms - s) / 1000;
    acc += e - s;
  }
  return acc / 1000;
}

export const totalS = (segments) => segments.reduce((s, [a, b]) => s + (b - a), 0) / 1000;

/** Per-beat report: post-cut seconds against max_s. */
export function budgetReport(timeline, cuts) {
  return timeline.beats.map((b) => {
    const post = beatPostCutS(b, cuts);
    return { id: b.id, wall_s: +((b.end_ms - b.start_ms) / 1000).toFixed(2), post_cut_s: +post.toFixed(2), max_s: b.max_s, over: post > b.max_s + 1e-9 };
  });
}
