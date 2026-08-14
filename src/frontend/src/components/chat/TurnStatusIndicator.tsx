import { useEffect, useState } from 'react';
import { BrandLoader, ElapsedTimer } from '../common';
import { t } from '../../i18n';

interface TurnStatusIndicatorProps {
  /** Wall-clock ms the current wait began — anchors the elapsed clock. */
  startTs: number;
}

/** Short waits keep the bare label; the clock only appears once the turn has clearly been running a while. */
const CLOCK_AFTER_MS = 15_000;

/**
 * Turn-level "model at work" signal shown before anything visible has
 * streamed in (no text, no tool run yet). A single line of brand-gradient
 * shimmer text instead of a full ToolRunShell card — at this point we don't
 * yet know whether the model will call a tool at all, so "执行中 / 正在准备
 * 调用工具" both over-promises and reads heavy. Once a real tool run starts,
 * the shell (and its in-shell pending row) takes over.
 */
export function TurnStatusIndicator({ startTs }: TurnStatusIndicatorProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const showClock = now - startTs >= CLOCK_AFTER_MS;
  return (
    <div className="jx-turnStatus" role="status" aria-live="polite">
      <BrandLoader size={18} done={false} label={t('深度拥抱中…')} />
      {/* jx-anim-keep: "system at work" indicator; under reduced-motion the shimmer slows down but is preserved */}
      <span className="jx-turnStatus-label jx-anim-keep">{t('深度拥抱中…')}</span>
      {showClock && <ElapsedTimer startTs={startTs} className="jx-turnStatus-clock" />}
    </div>
  );
}
