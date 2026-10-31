import { Button } from 'antd';
import { t } from '../../i18n';

interface SkeletonLinesProps {
  rows?: number;
  className?: string;
}

/** Placeholder text lines for a section whose own data is loading; the last line is shorter. */
export function SkeletonLines({ rows = 3, className }: SkeletonLinesProps) {
  return (
    <div className={`jx-skeletonLines${className ? ` ${className}` : ''}`} role="status" aria-label={t('加载中…')} aria-busy="true">
      {Array.from({ length: rows }, (_, index) => <div className="jx-skeletonBlock jx-skeletonLines-line" key={index} aria-hidden="true" />)}
    </div>
  );
}

interface LoadErrorProps {
  /** What failed, e.g. 「项目资料加载失败」; defaults to a generic title. */
  title?: string;
  /** The reason from the server or network. */
  reason?: string;
  onRetry: () => void;
  className?: string;
}

/** A failed load: what failed, why, and a way to try again. */
export function LoadError({ title, reason, onRetry, className }: LoadErrorProps) {
  return (
    <div className={`jx-loadError${className ? ` ${className}` : ''}`} role="alert">
      <div className="jx-loadError-title">{title || t('加载失败')}</div>
      {reason && <div className="jx-loadError-reason">{reason}</div>}
      <Button size="small" onClick={onRetry}>{t('重试')}</Button>
    </div>
  );
}
