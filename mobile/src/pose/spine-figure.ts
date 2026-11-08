/**
 * Geometry for the little side-view spine drawn from the wearable's live angles. Pure: no React, no DOM.
 *
 * The wearable sends the upper back's lean (`imu_pitch_deg`, BNO085) and the bend WITHIN each section
 * (upper = BNO - A, mid = A - B, lumbar = B - C; + = forward). From those, each sensor's own tilt from the
 * calibrated neutral posture is: BNO = pitch, A = pitch - upper, B = A - mid, C = B - lumbar. The figure
 * is four segments stacked bottom-up (sacrum to shoulders), each drawn at its sensor's tilt, with a head on top.
 */

import type { SpineData } from './wearable';

export type Segment = { key: 'lumbar' | 'mid' | 'upper' | 'neck'; cx: number; cy: number; length: number; angleDeg: number; bendDeg: number | null };
export type Figure = { segments: Segment[]; head: { cx: number; cy: number; r: number }; leanDeg: number };

export const ROUNDING_DEG = 15; // a section bent past this is drawn in the warning colour

const n = (v: number | null | undefined) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);

/** Tilt of each section, sacrum first, from the wearable's fields (missing ones count as 0). */
export function sectionTilts(d: SpineData): { lumbar: number; mid: number; upper: number; neck: number } {
  const bno = n(d.imu_pitch_deg);
  const a = bno - n(d.spine_upper_deg);
  const b = a - n(d.spine_mid_deg);
  const c = b - n(d.spine_lumbar_deg);
  // Drawn segments go from C up: the lumbar segment sits between C and B (tilt midway), and so on. The top
  // segment (shoulders to head) follows the BNO.
  return { lumbar: (c + b) / 2, mid: (b + a) / 2, upper: (a + bno) / 2, neck: bno };
}

/** Lay the figure out inside a square of `size` px, standing on its bottom edge, leaning forward to the right. */
export function layoutFigure(d: SpineData, size: number): Figure {
  const tilts = sectionTilts(d);
  const seg = size * 0.17;
  const baseX = size * 0.46;
  const baseY = size * 0.86;
  let x = baseX;
  let y = baseY;
  const segments: Segment[] = [];
  const order: { key: Segment['key']; tilt: number; bend: number | null }[] = [
    { key: 'lumbar', tilt: tilts.lumbar, bend: typeof d.spine_lumbar_deg === 'number' ? d.spine_lumbar_deg : null },
    { key: 'mid', tilt: tilts.mid, bend: typeof d.spine_mid_deg === 'number' ? d.spine_mid_deg : null },
    { key: 'upper', tilt: tilts.upper, bend: typeof d.spine_upper_deg === 'number' ? d.spine_upper_deg : null },
    { key: 'neck', tilt: tilts.neck, bend: null },
  ];
  for (const s of order) {
    const a = clamp(s.tilt, -90, 90);
    const rad = (a * Math.PI) / 180;
    const nx = x + Math.sin(rad) * seg;
    const ny = y - Math.cos(rad) * seg;
    segments.push({ key: s.key, cx: (x + nx) / 2, cy: (y + ny) / 2, length: seg, angleDeg: a, bendDeg: s.bend });
    x = nx;
    y = ny;
  }
  const r = size * 0.075;
  const last = segments[segments.length - 1];
  const rad = (last.angleDeg * Math.PI) / 180;
  return { segments, head: { cx: x + Math.sin(rad) * r * 1.1, cy: y - Math.cos(rad) * r * 1.1, r }, leanDeg: n(d.imu_pitch_deg) };
}

export function isRounding(bendDeg: number | null): boolean {
  return bendDeg !== null && Math.abs(bendDeg) >= ROUNDING_DEG;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
