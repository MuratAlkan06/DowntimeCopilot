// Shared app state + a minimal event bus. Role, phase and downtime markers persist in localStorage.

export const ROLES = [
  { id: 'charge_nurse', label: 'Charge nurse' },
  { id: 'rn', label: 'RN' },
  { id: 'pharmacy', label: 'Pharmacy' },
  { id: 'unit_clerk', label: 'Unit clerk' },
];

// Grouped the way Epic groups backload (README "Recovery").
export const GROUPS = [
  { id: 'nursing_med_admin', label: 'Nursing · Medication administration' },
  { id: 'nursing_vitals', label: 'Nursing · Vitals' },
  { id: 'nursing_io', label: 'Nursing · Intake & output' },
  { id: 'pharmacy_orders', label: 'Pharmacy · Orders' },
  { id: 'unit_registration', label: 'Unit · Allergies, history, height/weight' },
];

// Which backload groups each role owns (highlighted + sorted first).
export const ROLE_GROUPS = {
  charge_nurse: GROUPS.map((g) => g.id),
  rn: ['nursing_med_admin', 'nursing_vitals', 'nursing_io'],
  pharmacy: ['pharmacy_orders'],
  unit_clerk: ['unit_registration'],
};

export const STALE_MINUTES = 30; // R-STALE-01

const ls = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

export const store = {
  role: ls.get('dc.role') || 'charge_nurse',
  phase: ls.get('dc.phase') || 'downtime',
  downtimeStartedAt: ls.get('dc.downtimeStartedAt'),
  restoredAt: ls.get('dc.restoredAt'),
  snapshotTime: ls.get('dc.snapshotTime'),
  patients: new Map(),
};

const PERSISTED = { role: 'dc.role', phase: 'dc.phase', downtimeStartedAt: 'dc.downtimeStartedAt', restoredAt: 'dc.restoredAt', snapshotTime: 'dc.snapshotTime' };

const listeners = new Map();
export function on(evt, fn) {
  if (!listeners.has(evt)) listeners.set(evt, new Set());
  listeners.get(evt).add(fn);
}
export function emit(evt, payload) {
  listeners.get(evt)?.forEach((fn) => { try { fn(payload); } catch (e) { console.error(`[dc] ${evt} handler`, e); } });
}

export function set(key, value) {
  store[key] = value;
  if (PERSISTED[key]) ls.set(PERSISTED[key], value);
  emit(key, value);
}

/** Record the newest snapshot time seen in any response. */
export function noteSnapshot(...times) {
  const valid = times.flat().filter(Boolean).map((t) => new Date(t)).filter((d) => !isNaN(d));
  if (!valid.length) return;
  const newest = new Date(Math.max(...valid));
  if (!store.snapshotTime || newest > new Date(store.snapshotTime)) set('snapshotTime', newest.toISOString());
}

export function patientLabel(id) {
  const p = id && store.patients.get(id);
  if (!p) return id || 'Unassigned';
  return `${p.bed} · ${p.name}`;
}

export const roleLabel = (id) => ROLES.find((r) => r.id === id)?.label || id;
