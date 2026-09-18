import { CloudSyncOutlined, LoadingOutlined } from '@ant-design/icons';
import { useCapabilitySyncStore } from '../stores/capabilitySyncStore';
import { t } from '../i18n';

/**
 * 云端能力有改动时才出现的同步入口，和「下载更新」同一个位置、同一套交互：
 * 系统只负责告诉用户有得同步，点不点、什么时候点由用户决定。
 */
export function CapabilitySyncEntry({ className }: { className: string }) {
  const pending = useCapabilitySyncStore((s) => s.pending);
  const busy = useCapabilitySyncStore((s) => s.busy);
  const run = useCapabilitySyncStore((s) => s.run);
  if (!pending) return null;
  const label = t('同步云端能力');
  return (
    <button type="button" className={className} title={label} aria-label={label}
      disabled={busy} onClick={() => { void run(); }}>
      {busy ? <LoadingOutlined spin /> : <CloudSyncOutlined />}
    </button>
  );
}
