import { useEffect, useState } from 'react';
import { Button, Empty, Modal, message } from 'antd';
import type { DeviceCapabilityKind, DeviceCapabilityItem } from '../../api';
import { t } from '../../i18n';
import { useDesktopCapabilityStore } from '../../stores/desktopCapabilityStore';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { canPrepareDeviceCapability } from '../../utils/deviceCapabilities';
import { DeviceCapabilityBadge } from './DeviceCapabilityBadge';
import { DeviceSkillEditor } from './DeviceSkillEditor';

/** 账号清单之外的本机候选同样可见；后台同步后刷新状态，错误时保留恢复入口。 */
export function DeviceCapabilityPanel({ kind }: { kind: DeviceCapabilityKind }) {
  const enabled = useDeploymentModeStore((s) => s.provisionMode === 'dual');
  const state = useDesktopCapabilityStore((s) => s.kinds[kind]);
  const load = useDesktopCapabilityStore((s) => s.load);
  const refresh = useDesktopCapabilityStore((s) => s.refresh);
  const syncing = useDesktopCapabilityStore((s) => s.syncing);
  const prepare = useDesktopCapabilityStore((s) => s.prepare);
  const busy = useDesktopCapabilityStore((s) => s.busy);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<DeviceCapabilityItem | null>(null);
  useEffect(() => {
    if (!enabled) return;
    void load(kind, true);
    const update = () => { if (!document.hidden) void load(kind, true); };
    const timer = window.setInterval(update, 15000);
    window.addEventListener('focus', update);
    return () => { window.clearInterval(timer); window.removeEventListener('focus', update); };
  }, [enabled, kind, load]);
  if (!enabled) return null;
  const items = state.listing?.items || [];
  const pending = items.filter(canPrepareDeviceCapability);
  const conflicts = Object.keys(state.listing?.conflicts || {});
  const report = (error: unknown) => { message.error((error as Error).message); };
  return (
    <>
      <div className="jx-devcap-panel">
        <Button type="link" size="small" onClick={() => setOpen(true)}>{t('本机能力')} ({items.length})</Button>
        {state.error && <span role="alert" className="jx-devcap-panel-err">{state.error}</span>}
        {pending.length > 0 && <span>{t('待下载')} {pending.length}
          <Button type="link" size="small" loading={pending.some((item) => busy[item.install_id])}
            onClick={() => { void prepare(kind, pending.map((item) => item.install_id)).catch(report); }}>{t('全部在本机准备')}</Button>
        </span>}
        {conflicts.length > 0 && <span>{t('同名冲突')} {conflicts.length}</span>}
        <Button type="link" size="small" loading={syncing || state.loading}
          onClick={() => { void refresh().catch(report); }}>{t('同步云端清单')}</Button>
      </div>
      <Modal title={t('本机能力')} open={open} onCancel={() => setOpen(false)} footer={null} width={720}>
        {items.length === 0 ? <Empty description={t('暂无本机能力')} /> : <div className="jx-devcap-list">
          {items.map((item) => <div className="jx-devcap-row" key={item.install_id}>
            <strong>{item.display_name || item.runtime_name}</strong>
            <DeviceCapabilityBadge kind={kind} runtimeName={item.runtime_name} installId={item.install_id} />
            {kind === 'skill' && item.source === 'local' && item.derived_from && item.state === 'ready' &&
              <Button size="small" onClick={() => setEditing(item)}>{t('编辑技能')}</Button>}
            {item.last_error && <div role="alert" className="jx-devcap-panel-err">{item.last_error}</div>}
            {!!item.readiness?.missing_required.length && <div>{t('缺少必要组件')}：{item.readiness.missing_required.join('、')}</div>}
          </div>)}
        </div>}
      </Modal>
      {editing && <DeviceSkillEditor item={editing} onClose={() => setEditing(null)} />}
    </>
  );
}
