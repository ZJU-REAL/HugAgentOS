import { t } from '../i18n';

/** Elapsed seconds as M:SS — the subagent and run-monitor panel clocks. */
export function formatClock(totalSec: number): string {
  const sec = Math.max(0, Math.floor(totalSec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

/**
 * Whole seconds as "36秒" / "1分05秒". The live turn clock and the settled
 * "用时 …" share this shape so the number doesn't change form when a reply ends.
 */
export function formatTurnSeconds(totalSec: number): string {
  const sec = Math.max(0, Math.floor(totalSec));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m > 0 ? t('{m}分{s}秒', { m, s: String(s).padStart(2, '0') }) : t('{n}秒', { n: s });
}
