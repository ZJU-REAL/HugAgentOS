import { useEffect, useState, type ReactNode } from 'react';
import { Dropdown } from 'antd';
import { LockOutlined, SafetyOutlined, ThunderboltOutlined } from '@ant-design/icons';
import {
  getLocalApprovalMode,
  setLocalApprovalMode,
  type LocalApprovalMode,
} from '../../api';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { ChipChevron } from '../common/ChipChevron';
import LocalPermissionsModal from './LocalPermissionsModal';

// 图标与工具行其他入口保持同一套 antd 线性风格（单色、随文字色），不用彩色 emoji。
const APPROVAL_META: Record<LocalApprovalMode, { icon: ReactNode; label: string; desc: string }> = {
  strict: { icon: <LockOutlined />, label: '只读/严格', desc: '仅允许读取工作区/授权目录，所有写入一律拦截' },
  standard: { icon: <SafetyOutlined />, label: '标准', desc: '写系统目录/提权拦截，删除/外联需确认' },
  full: { icon: <ThunderboltOutlined />, label: '放开', desc: '全部放行，仅记录审计（谨慎使用）' },
};
const APPROVAL_ORDER: LocalApprovalMode[] = ['strict', 'standard', 'full'];

/**
 * 本机操作权限档胶囊：紧挨工具栏「项目」选择框展示（桌面壳 + 本机模式时才渲染），
 * 下拉切换严格度档位，底部入口打开授权目录 / 细粒度策略弹窗。
 */
export default function LocalApprovalPill() {
  const isDesktop = useDeploymentModeStore((s) => s.isDesktop);
  const activeLocal = useDeploymentModeStore((s) => s.activeLocal);
  const provisionMode = useDeploymentModeStore((s) => s.provisionMode);
  // 混合架构：双模式下本机执行面常驻，本地操作权限档同样有意义。
  const localCapable = activeLocal || provisionMode === 'dual';
  const [approval, setApproval] = useState<LocalApprovalMode | null>(null);
  const [permOpen, setPermOpen] = useState(false);
  const [approvalOpen, setApprovalOpen] = useState(false);

  useEffect(() => {
    if (isDesktop && localCapable) {
      getLocalApprovalMode().then(setApproval).catch(() => {});
    }
  }, [isDesktop, localCapable]);

  if (!isDesktop || !localCapable || !approval) return null;

  const applyApproval = (mode: LocalApprovalMode) => {
    setApproval(mode);
    setLocalApprovalMode(mode).catch(() => {});
  };

  const items = [
    {
      key: 'approval-group',
      type: 'group' as const,
      label: '本机操作权限',
      children: APPROVAL_ORDER.map((m) => ({
        key: m,
        label: (
          <div className="jx-modeOption">
            <div className="jx-modeOptionHead">
              <span className="jx-modeOptionTitle">
                {APPROVAL_META[m].icon} {APPROVAL_META[m].label}
              </span>
              {approval === m && <img src="/home/check.svg" alt="" className="jx-modeCheckIcon" />}
            </div>
            <div className="jx-modeOptionDesc">{APPROVAL_META[m].desc}</div>
          </div>
        ),
        onClick: () => applyApproval(m),
      })),
    },
    { type: 'divider' as const },
    {
      key: 'perm-detail',
      label: <span className="jx-approvalDetailEntry">授权目录与细粒度策略…</span>,
      onClick: () => setPermOpen(true),
    },
  ];

  return (
    <>
      <Dropdown
        trigger={['click']}
        placement="topLeft"
        overlayClassName="jx-modeMenu jx-approvalMenu"
        onOpenChange={setApprovalOpen}
        menu={{ items }}
      >
        <button
          type="button"
          className={`jx-composerChip jx-projectDropBtn jx-approvalPillBtn${approvalOpen ? ' open' : ''}`}
          title="本机操作权限档（对本机文件操作的严格度）"
          aria-label={`本机操作权限：${APPROVAL_META[approval].label}，点击切换`}
        >
          <span className="jx-approvalPillIcon">{APPROVAL_META[approval].icon}</span>
          <span className="jx-projectDropName jx-composerChip-label">{APPROVAL_META[approval].label}</span>
          <ChipChevron />
        </button>
      </Dropdown>
      <LocalPermissionsModal open={permOpen} onClose={() => setPermOpen(false)} />
    </>
  );
}
