import { CheckOutlined, CloseOutlined, LoadingOutlined, MinusOutlined } from '@ant-design/icons';
import { ElapsedTimer } from '../common';
import { t } from '../../i18n';
import { compactionLabel, type CompactionProgress } from '../../utils/compactionProgress';

export function CompactionStepRow({ progress }: { progress: CompactionProgress }) {
  const running = progress.status === 'running';
  const Icon = running ? LoadingOutlined : progress.status === 'completed' ? CheckOutlined
    : progress.status === 'skipped' ? MinusOutlined : CloseOutlined;
  return (
    <div className="jx-tcr" role="status" aria-live="polite">
      <div className="jx-tcr-header">
        <span className="jx-tcr-status">
          <Icon spin={running} className={`jx-tcr-icon${running ? ' jx-tcr-icon--running' : ''}`} />
        </span>
        <span className="jx-tcr-label">
          <span className={`jx-tcr-prefix${running ? ' jx-tcr-prefix--shimmer' : ''}`}>{t(compactionLabel(progress))}</span>
        </span>
        {running && <ElapsedTimer startTs={progress.started_at} className="jx-tcr-timer" />}
      </div>
      <div style={{ padding: '2px 0 6px 30px', fontSize: 12, color: 'var(--color-text-secondary)' }}>
        {t(running ? '整理较早的对话，保留关键内容。'
          : progress.status === 'completed' ? '已保留关键内容，可以继续对话。'
          : progress.status === 'failed' || progress.status === 'interrupted' ? '输入 /compact 可重试。' : '对话可以继续。')}
      </div>
    </div>
  );
}
