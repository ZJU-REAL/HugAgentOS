// Shared motion variants —— aligned with the CSS primitives in styles/motion.css.
// Use these when you need exit animations / orchestration (stagger); for one-way entrance prefer the CSS primitive classes.
import type { Variants } from 'motion/react';
import { DUR, EASE, STAGGER_STEP } from './motionTokens';

export const fadeInUp: Variants = {
  hidden: { opacity: 0, y: 8 },
  visible: { opacity: 1, y: 0, transition: { duration: DUR.normal, ease: EASE.brandOut } },
  exit: { opacity: 0, y: 4, transition: { duration: DUR.fast, ease: EASE.exit } },
};

export const scaleIn: Variants = {
  hidden: { opacity: 0, y: 6, scale: 0.97 },
  visible: { opacity: 1, y: 0, scale: 1, transition: { duration: DUR.fast, ease: EASE.brandOut } },
  exit: { opacity: 0, y: 4, scale: 0.97, transition: { duration: DUR.fast, ease: EASE.exit } },
};

export const staggerContainer: Variants = {
  visible: { transition: { staggerChildren: STAGGER_STEP, delayChildren: 0.05 } },
};

/** List→detail drill-down transition: forward enters from the right / back enters from the left.
 * Usage: <motion.div {...(userNavigated ? DRILL_IN_DETAIL : { initial: false })}> */
export const DRILL_IN_DETAIL = {
  initial: { opacity: 0, x: 16 },
  animate: { opacity: 1, x: 0 },
  transition: { duration: DUR.normal, ease: EASE.brandOut },
} as const;

export const DRILL_IN_BACK = {
  initial: { opacity: 0, x: -12 },
  animate: { opacity: 1, x: 0 },
  transition: { duration: DUR.normal, ease: EASE.brandOut },
} as const;

/** List item delete exit (slide right + fade out; under popLayout, sibling items fill in via layout) */
export const LIST_ITEM_EXIT = {
  opacity: 0,
  x: 24,
  transition: { duration: DUR.fast, ease: EASE.exit },
} as const;
