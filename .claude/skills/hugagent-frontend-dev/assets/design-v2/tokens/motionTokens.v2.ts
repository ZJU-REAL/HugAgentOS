// HugAgentOS motion tokens v2 — drop-in superset of src/frontend/src/utils/motionTokens.ts.
// New: SPRING.smooth / SPRING.snappy described as visualDuration + bounce (Motion >= 11 accepts both forms).
// Durations / easings are unchanged; keep them in sync with styles/variables.css (--motion-*).

export const DUR = { instant: 0.1, fast: 0.16, normal: 0.24, slow: 0.32 } as const;

export const EASE = {
  /** Enter / expand / rise */
  brandOut: [0.16, 1, 0.3, 1] as const,
  /** Two-way state changes (colour, opacity) */
  standard: [0.4, 0, 0.2, 1] as const,
  /** Exit: accelerate away, ~0.7x the enter duration */
  exit: [0.4, 0, 1, 1] as const,
};

export const SPRING = {
  /** Panels, drawers, collapse/expand, layout changes. No overshoot. (≈ stiffness 322, damping 36) */
  smooth: { type: 'spring', visualDuration: 0.35, bounce: 0 },
  /** Tab ink, toggles, badge confirmation. Slight overshoot. (≈ stiffness 632, damping 43) */
  snappy: { type: 'spring', visualDuration: 0.25, bounce: 0.15 },
  /** @deprecated use SPRING.snappy — 33% overshoot */
  pop: { type: 'spring', stiffness: 500, damping: 30 },
  /** @deprecated use SPRING.snappy */
  ink: { type: 'spring', stiffness: 480, damping: 38 },
  /** @deprecated use SPRING.smooth — 33% overshoot makes panels wobble */
  panel: { type: 'spring', stiffness: 320, damping: 24 },
} as const;

export const STAGGER_STEP = 0.06;
export const STAGGER_CAP = 8;
export const LAYOUT_ANIM_MAX_ITEMS = 20;
export const FRESH_ENTER_WINDOW_MS = 2000;
