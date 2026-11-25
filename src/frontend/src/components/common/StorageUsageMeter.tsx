import { t } from '../../i18n';
import type { StorageUsage } from '../../types';

const MB = 1024 * 1024;

export function formatStorageSize(bytes: number): string {
  if (bytes >= 1024 * MB) return `${+(bytes / (1024 * MB)).toFixed(2)} GB`;
  if (bytes >= MB) return `${+(bytes / MB).toFixed(1)} MB`;
  return `${Math.ceil(bytes / 1024)} KB`;
}

/**
 * Why these files cannot be stored, or null when they fit: the first file over the per-file cap,
 * else the batch overrunning the remaining quota. The server enforces both; this only saves a
 * doomed upload.
 */
export function storageRejection(files: File[], usage: StorageUsage | null): string | null {
  if (!usage) return null;
  const { file_limit_mb: fileLimit, quota_mb: quota, used_bytes: used } = usage;
  if (fileLimit) {
    const big = files.find(file => file.size > fileLimit * MB);
    if (big) return t('「{name}」超过单个文件 {n} MB 的上传上限', { name: big.name, n: fileLimit });
  }
  if (quota && used !== null) {
    const incoming = files.reduce((sum, file) => sum + file.size, 0);
    const left = Math.max(quota * MB - used, 0);
    if (incoming > left) return t('存储空间不足：剩余 {left}，本次需要 {need}', { left: formatStorageSize(left), need: formatStorageSize(incoming) });
  }
  return null;
}

/** A usage bar when a quota applies. The per-file cap is not shown; an oversized upload is refused with its own message. */
export function StorageUsageMeter({ usage, label }: { usage: StorageUsage | null; label: string }) {
  if (!usage?.quota_mb) return null;
  const quota = usage.quota_mb;
  const used = usage.used_bytes ?? 0;
  const ratio = Math.min(used / (quota * MB), 1);
  const tone = ratio >= 0.95 ? ' is-full' : ratio >= 0.8 ? ' is-high' : '';
  const summary = t('已用 {used} / {total}', { used: formatStorageSize(used), total: formatStorageSize(quota * MB) });
  // The title keeps the figure reachable where a narrow toolbar hides or truncates the line.
  return <div className={'jx-storageUsage' + tone} role="group" aria-label={t('{label}存储用量', { label })} title={summary}>
    <div className="jx-storageUsage-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={Math.round(ratio * 100)} aria-label={t('{label}已用容量', { label })}>
      <span className="jx-storageUsage-fill" style={{ transform: `scaleX(${ratio})` }} />
    </div>
    <span className="jx-storageUsage-text">{summary}</span>
  </div>;
}
