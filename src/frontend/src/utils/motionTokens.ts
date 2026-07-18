// Motion design tokens (source of truth on the TS side, one-to-one with styles/variables.css's --motion-*).
// The motion library's duration unit is seconds; the CSS side uses ms —— the values on both sides must be kept in sync.
import type { CSSProperties } from 'react';

export const DUR = {
  instant: 0.1,
  fast: 0.16,
  normal: 0.24,
  slow: 0.32,
} as const;

export const EASE = {
  /** Brand curve: enter / expand / float up (strong deceleration with a slight overshoot feel) */
  brandOut: [0.16, 1, 0.3, 1] as const,
  /** Bidirectional state switching (color / opacity, etc.) */
  standard: [0.4, 0, 0.2, 1] as const,
  /** Exit: accelerated departure, duration ~0.7x of enter */
  exit: [0.4, 0, 1, 1] as const,
};

export const STAGGER_STEP = 0.06;

/** Backward-compatible alias: the SLIDE_EASE formerly inlined in App.tsx */
export const SLIDE_EASE = EASE.brandOut;

/** Three spring presets (unified feel across the whole app, don't write ad-hoc stiffness/damping anymore) */
export const SPRING = {
  /** Light bounce confirmation for badges / counts / status dots */
  pop: { type: 'spring', stiffness: 500, damping: 30 },
  /** Tab sliding indicator bar (layoutId ink) */
  ink: { type: 'spring', stiffness: 480, damping: 38 },
  /** Panel / hint card enter */
  panel: { type: 'spring', stiffness: 320, damping: 24 },
} as const;

/** CSS stagger cap: no more delay is added after the Nth item */
export const STAGGER_CAP = 8;

/** Inline style for the --stagger-i of .jx-anim-stagger children (after the cap, they appear in the same frame) */
export const staggerStyle = (i: number, cap: number = STAGGER_CAP): CSSProperties =>
  ({ '--stagger-i': Math.min(i, cap) }) as CSSProperties;

/** Scale upper bound for list layout/popLayout animations —— beyond it, degrade to avoid O(n) FLIP measurement */
export const LAYOUT_ANIM_MAX_ITEMS = 20;

/** "New message" enter window: play the enter animation only if ts at mount is less than this value from now (history replay is silent) */
export const FRESH_ENTER_WINDOW_MS = 2000;
