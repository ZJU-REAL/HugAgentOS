import { t } from '../../i18n';

interface BrandLoaderProps {
  /** Visual size (px), default 16 */
  size?: number;
  /** Accessibility text; when omitted, chosen automatically based on the done state */
  label?: string;
  /** true = the frozen brand mark (gray); false = the GIF being drawn (blue) */
  done?: boolean;
}

/**
 * Unified "in-progress / completed" inline indicator.
 * - active: `/loader.gif` brand-blue dynamic drawing
 * - done: `/loader-done.png` slate-gray frozen final state + a brief fade-in
 *
 * Used at inline summary positions like ThinkingInline, ToolProgressInline,
 * replacing the earlier two scattered visuals breathingOrbs + pulseDot.
 */
export function BrandLoader({ size = 16, label, done = false }: BrandLoaderProps) {
  return (
    <span
      className={`jx-brandLoader${done ? ' jx-brandLoader--done' : ''}`}
      role="img"
      aria-label={label ?? (done ? t('已完成') : t('加载中'))}
      style={{ width: size, height: size }}
    />
  );
}

export default BrandLoader;
