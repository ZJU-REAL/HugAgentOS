import { useState, type ReactNode } from 'react';
import { Dropdown, message } from 'antd';
import { SafetyOutlined, ThunderboltOutlined, UnlockOutlined } from '@ant-design/icons';
import { ChipChevron } from '../../common/ChipChevron';
import { personalApi, errorText, type ApprovalMode } from '../api';
import { t } from '../../../i18n';

// Same three levels, icons and names as the HugAgentOS composer (ApprovalPill).
const META: Record<ApprovalMode, { icon: ReactNode; label: string; desc: string }> = {
  ask: { icon: <SafetyOutlined />, label: '逐项确认', desc: '操作电脑、管理会话、发邮件前都先问你一句' },
  auto: { icon: <ThunderboltOutlined />, label: '替我批准', desc: '电脑和会话操作直接执行，发邮件、删除会话仍会问你' },
  full: { icon: <UnlockOutlined />, label: '完全放开', desc: '所有操作一律不再询问' },
};
const ORDER: ApprovalMode[] = ['ask', 'auto', 'full'];

/** Permission level chip in the Personal Agent composer. */
export function PersonalApprovalPill({ mode, onChange }: { mode?: ApprovalMode; onChange: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const current = mode || 'ask';

  async function apply(next: ApprovalMode) {
    if (next === current || saving) return;
    setSaving(true);
    try { await personalApi.settings({ approval_mode: next }); await onChange(); }
    catch (exc) { message.error(errorText(exc)); }
    finally { setSaving(false); }
  }

  const items = [{
    key: 'approval-group', type: 'group' as const, label: t('操作权限'),
    children: ORDER.map(item => ({
      key: item,
      label: <div className="jx-modeOption">
        <div className="jx-modeOptionHead">
          <span className="jx-modeOptionTitle">{META[item].icon} {t(META[item].label)}</span>
          {current === item && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
        </div>
        <div className="jx-modeOptionDesc">{t(META[item].desc)}</div>
      </div>,
      onClick: () => void apply(item),
    })),
  }];
  const label = saving ? t('正在保存权限…') : t(META[current].label);

  return <Dropdown disabled={saving} trigger={['click']} placement="topLeft" overlayClassName="jx-modeMenu jx-approvalMenu" onOpenChange={setOpen} menu={{ items }}>
    <button type="button" disabled={saving} aria-busy={saving} className={'jx-composerChip jx-projectDropBtn jx-approvalPillBtn' + (open ? ' open' : '')}
      title={t('操作权限（操作前要不要先问你一句）')} aria-label={t('操作权限：{label}，点击切换', { label })}>
      <span className="jx-approvalPillIcon">{META[current].icon}</span>
      <span className="jx-projectDropName jx-composerChip-label">{label}</span>
      <ChipChevron />
    </button>
  </Dropdown>;
}
