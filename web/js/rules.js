// Rule hits rendered against the unit (simulated) clock. Pure: no DOM imports, so tests can run it in node.
// Server alert text carries ABSOLUTE unit times only ("due 03:30"). The relative part ("in 43 min") is
// computed here at render time from /status now, and recomputed on every refresh, so it never freezes.

const MIN = 60000;
const HALF_DAY = 12 * 60 * MIN;

/** Due time of a hit: the structured `due_at` from GET /rules/current, else the "due HH:MM" in the alert
 *  text (packet alerts), placed on the unit date nearest to now so it survives midnight. */
export function dueAt(hit, nowIso) {
  if (hit?.due_at) return new Date(hit.due_at);
  const m = /\bdue (\d{1,2}):(\d{2})\b/.exec(hit?.text || '');
  const now = new Date(nowIso);
  if (!m || isNaN(now)) return null;
  const d = new Date(now);
  d.setUTCHours(Number(m[1]), Number(m[2]), 0, 0);
  if (d - now > HALF_DAY) d.setUTCDate(d.getUTCDate() - 1);
  else if (now - d > HALF_DAY) d.setUTCDate(d.getUTCDate() + 1);
  return d;
}

/** "in 43 min" / "due now" / "overdue 5 min"; '' when the unit clock or the due time is unknown. */
export function dueRelative(hit, nowIso) {
  if (!nowIso) return '';
  const now = new Date(nowIso);
  const due = dueAt(hit, nowIso);
  if (!due || isNaN(now) || isNaN(due)) return '';
  const ms = due - now;
  if (ms >= MIN) return `in ${Math.floor(ms / MIN)} min`;
  if (ms > -MIN) return 'due now';
  return `overdue ${Math.floor(-ms / MIN)} min`;
}

/** Whole minutes from `iso` to the unit clock, or null if either is unknown. */
export function ageMinutes(iso, nowIso) {
  const t = new Date(iso);
  const now = new Date(nowIso);
  if (!iso || !nowIso || isNaN(t) || isNaN(now)) return null;
  return Math.max(0, Math.floor((now - t) / MIN));
}
