import { Grits } from '@/constants/grits';

/** Charcoal / black with electric green: History, workout detail, sign-in and the pose lab. Games now use GG below. */
export const G = {
  bg: '#0b0d0c',
  panel: '#14181a',
  panelHi: '#1b2124',
  line: '#283135',
  green: '#39ff14',
  greenDim: '#1f9d0a',
  greenInk: '#06210a',
  text: '#f2f5f3',
  muted: '#98a5a1',
  red: '#ff5252',
  amber: '#ffb020',
  blue: '#4cc9f0',
} as const;

/**
 * The same keys as G, in the GRIT look (constants/grits.ts: peach page, cream cards, sage greens, tan ink) so the
 * Games screens match Home and Exercise. Only components/games/ uses it; History keeps G.
 * Greens and amber are taken a shade darker than the brand swatches so text stays readable on cream.
 */
export const GG = {
  bg: Grits.peach,
  panel: Grits.cream,
  panelHi: Grits.pillText,
  line: Grits.pill,
  green: '#3F7A4C', // sage, deepened: fills (with greenInk text) and small labels
  greenDim: Grits.sage,
  greenInk: Grits.pillText,
  text: Grits.ink,
  muted: Grits.inkSoft,
  red: '#B5402F',
  amber: '#B8650F',
  blue: '#2F7FA6',
} as const;

export const MAX_W = 1120;
