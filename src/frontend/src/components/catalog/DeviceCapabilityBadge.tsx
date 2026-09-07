import { useEffect } from 'react';
import { Button, Popconfirm, Tooltip, message } from 'antd';
import type { DeviceCapabilityKind } from '../../api';
import { t } from '../../i18n';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { canPrepareDeviceCapability, canToggleDeviceCapability, canRemoveDeviceFiles, canCreateDeviceLocalCopy, cloudRestoreTarget, deviceCapabilityState, deviceDependencyReason, DEVICE_SOURCE_LABEL } from '../../utils/deviceCapabilities';
import { DeviceCapabilityChoice } from './DeviceCapabilityChoice';

export function DeviceCapabilityBadge({ kind, runtimeName, installId }: {
  kind: DeviceCapabilityKind; runtimeName: string; installId?: string;
}) {
  const enabled = useDeploymentModeStore((s) => s.provisionMode === 'dual');
  const load = useDesktopCapabilityStore((s) => s.load);
  const state = useDesktopCapabilityStore((s) => s.kinds[kind]);
  const busy = useDesktopCapabilityStore((s) => s.busy);
  const setEnabled = useDesktopCapabilityStore((s) => s.setEnabled);
  const prepare = useDesktopCapabilityStore((s) => s.prepare);
  const choose = useDesktopCapabilityStore((s) => s.choose);
  const copyLocal = useDesktopCapabilityStore((s) => s.copyLocal);
  const removeFiles = useDesktopCapabilityStore((s) => s.removeFiles);
  useEffect(() => { if (enabled) void load(kind); }, [enabled, kind, load]);
  if (!enabled) return null;
  const name = state.byName[runtimeName] ? runtimeName
    : state.listing?.items.find((candidate) => candidate.server_id === runtimeName)?.runtime_name || runtimeName;
  const candidates = state.byName[name] || [];
  const item = installId ? candidates.find((c) => c.install_id === installId)
    : candidates.find((c) => c.resolution.outcome === 'chosen') || candidates[0];
  if (!item) return null;
  const allItems = state.listing?.items || [];
  const label = deviceCapabilityState(item, candidates);
  const restore = cloudRestoreTarget(item, allItems);
  const hasLocalCopy = allItems.some((candidate) => candidate.source === 'local' && candidate.derived_from === item.install_id);
  const stop = (event: React.SyntheticEvent) => event.stopPropagation();
  const report = (error: unknown) => { message.error((error as Error).message); };
  const tip = [
    item.last_error,
    item.revision,
    item.resolution.reason,
    ...(item.readiness?.missing_required || []).map((reason) => deviceDependencyReason(reason, t)),
  ].filter(Boolean).join('\n');
  return (
    <span className="jx-devcap" onClick={stop}>
      <Tooltip title={tip || undefined}>
        <span className={`jx-devcap-chip jx-devcap-src-${item.source}`}>{t(DEVICE_SOURCE_LABEL[item.source] || item.source)}</span>
      </Tooltip>
      <span className={`jx-devcap-chip jx-devcap-${label.tone}`}>{t(label.text)}</span>
      <DeviceCapabilityChoice kind={kind} runtimeName={name} candidates={candidates}
        preference={state.listing?.preferences[name]} disabled={state.loading}
        onChoose={(id) => { void choose(kind, name, id).catch(report); }} />
      <span className="jx-devcap-actions">
        {canToggleDeviceCapability(item) && <Button type="link" size="small" loading={!!busy[item.install_id]}
          title={t('仅修改这台设备的启用偏好，使用云端能力仍需要账号授权')}
          onClick={() => { void setEnabled(kind, item.install_id, !item.enabled).catch(report); }}>
          {item.enabled ? t('在本机停用') : t('在本机启用')}
        </Button>}
        {canCreateDeviceLocalCopy(item) && !hasLocalCopy && <Button type="link" size="small" loading={!!busy[item.install_id]}
          title={t('复制当前技能并在此设备使用；云端更新不会覆盖本机副本')}
          onClick={() => { void copyLocal(kind, item.install_id).then(() => message.success(t('已创建并选用本机副本'))).catch(report); }}>
          {t('创建本机副本')}
        </Button>}
        {restore && item.resolution.outcome === 'chosen' && <Button type="link" size="small" disabled={state.loading}
          title={t('重新选用云端原版，并保留本机副本文件')}
          onClick={() => { void choose(kind, restore.runtime_name, restore.install_id).then(() => message.success(t('已恢复云端版本，本机副本已保留'))).catch(report); }}>
          {t('恢复云端版本')}
        </Button>}
        {canPrepareDeviceCapability(item) && <Button type="link" size="small" loading={!!busy[item.install_id]}
          onClick={() => { void prepare(kind, [item.install_id]).catch(report); }}>{item.files_ready ? t('重新检查本机依赖') : t('在本机准备')}</Button>}
        {canRemoveDeviceFiles(item) && <Popconfirm title={t('移除这台机器上的文件？账号里仍保留，之后可重新准备')}
          okText={t('移除')} cancelText={t('取消')} onConfirm={() => removeFiles(kind, item.install_id).catch(report)}>
          <Button type="link" size="small" danger disabled={!!busy[item.install_id]}>{t('移除本机文件')}</Button>
        </Popconfirm>}
      </span>
    </span>
  );
}
