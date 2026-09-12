import { useEffect, useState } from 'react';
import { Button, Tooltip } from 'antd';
import { CloudUploadOutlined } from '@ant-design/icons';
import { CapabilityChangesModal } from './CapabilityChangesModal';
import type { DeviceCapabilityKind } from '../../api';
import { t } from '../../i18n';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { DEVICE_SOURCE_LABEL } from '../../utils/deviceCapabilities';

/** Source and local-change actions share the current account's device listing. */
export function DeviceCapabilityBadge({ kind, runtimeName }: {
  kind: DeviceCapabilityKind; runtimeName: string;
}) {
  const enabled = useDeploymentModeStore((s) => s.provisionMode === 'dual');
  const load = useDesktopCapabilityStore((s) => s.load);
  const state = useDesktopCapabilityStore((s) => s.kinds[kind]);
  const [open, setOpen] = useState(false);
  useEffect(() => { if (enabled) void load(kind); }, [enabled, kind, load]);
  if (!enabled) return null;
  const item = state.byName[runtimeName];
  if (!item) return null;
  return (
    <span onClick={(event) => event.stopPropagation()}>
      <span className={`jx-devcap-chip jx-devcap-src-${item.source}`}>
        {t(DEVICE_SOURCE_LABEL[item.source] || item.source)}
        {item.change_state === 'modified' && <span> · {t('有本地变更')}</span>}
        {item.change_state === 'new' && <span> · {t('未提交')}</span>}
      </span>
      {item.change_state && item.change_state !== 'unavailable' && <Tooltip
        title={t(item.change_state === 'new' ? '本地新增，可提交到云端' : item.change_state === 'modified' ? '本地有变更，可提交到云端' : '比较云端版本')}>
        <Button type="text" size="small" icon={<CloudUploadOutlined />}
          aria-label={t('比较云端版本')} onClick={() => setOpen(true)}
          style={{ color: item.change_state === 'modified' || item.change_state === 'new' ? 'var(--color-primary)' : undefined }} />
      </Tooltip>}
      {open && <CapabilityChangesModal key={item.install_id} installId={item.install_id} onClose={() => setOpen(false)} />}
    </span>
  );
}
