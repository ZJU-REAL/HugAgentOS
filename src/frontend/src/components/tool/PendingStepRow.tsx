import { LoadingOutlined } from '@ant-design/icons';
import { ElapsedTimer } from '../common';
import { t } from '../../i18n';

interface PendingStepRowProps {
  /** Timestamp the stall/pending state began — drives the inline timer. */
  startTs: number;
}

/**
 * Pending row shown inside the ToolRunShell when the model is buffering a
 * tool-call payload (backend `tool_pending` or frontend stall detection).
 *
 * The configured LLM emits the tool-call `arguments` JSON in one chunk after
 * a multi-second server-side buffer, so this row is the only signal we can
 * give the user during that gap. It replaces the old free-floating
 * "Preparing to call a tool" inline indicator, keeping the shell as the single home
 * for any agent-side work.
 */
export function PendingStepRow({ startTs }: PendingStepRowProps) {
  return (
    <div className="jx-tcr">
      <div className="jx-tcr-header">
        <span className="jx-tcr-status">
          <LoadingOutlined spin className="jx-tcr-icon jx-tcr-icon--running" />
        </span>
        <span className="jx-tcr-label">
          {/* jx-anim-keep: "system at work" indicator; under reduced-motion it slows down but is preserved rather than frozen */}
          <span className="jx-tcr-prefix jx-tcr-prefix--shimmer jx-anim-keep">{t('正在准备调用工具…')}</span>
        </span>
        <ElapsedTimer startTs={startTs} className="jx-tcr-timer" />
      </div>
    </div>
  );
}
