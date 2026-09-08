import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Dropdown, message } from 'antd';
import { SafetyOutlined, ThunderboltOutlined, UnlockOutlined } from '@ant-design/icons';
import { getToolApprovalMode, setToolApprovalMode, type ToolApprovalMode } from '../../api';
import { t } from '../../i18n';
import { useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { ChipChevron } from '../common/ChipChevron';
import LocalPermissionsModal from './LocalPermissionsModal';

// 图标与工具行其他入口保持同一套 antd 线性风格（单色、随文字色），不用彩色 emoji。
const APPROVAL_META: Record<ToolApprovalMode, { icon: ReactNode; label: string; desc: string }> = {
  ask: {
    icon: <SafetyOutlined />,
    label: t('逐项确认'),
    desc: t('写文件、动本机、跑命令前都先问你一句'),
  },
  auto: {
    icon: <ThunderboltOutlined />,
    label: t('替我批准'),
    desc: t('写入类操作直接放行，删除等危险操作仍会问你'),
  },
  full: {
    icon: <UnlockOutlined />,
    label: t('完全放开'),
    desc: t('所有工具调用一律不再询问'),
  },
};
const APPROVAL_ORDER: ToolApprovalMode[] = ['ask', 'auto', 'full'];

/**
 * 工具执行权限档胶囊：紧挨工具栏「项目」选择框展示，下拉切换逐项确认 / 替我批准 / 完全放开。
 * 网页端与桌面端共用这一档（桌面端的本机策略由后端按它翻译），桌面本机模式下额外
 * 在底部提供授权目录 / 细粒度策略入口。
 */
export default function ApprovalPill() {
  const isDesktop = useDeploymentModeStore((s) => s.isDesktop);
  const localReady = useDeploymentModeStore((s) => s.localReady);
  const activeLocal = useDeploymentModeStore((s) => s.activeLocal);
  const provisionMode = useDeploymentModeStore((s) => s.provisionMode);
  // 只用来决定要不要露出「授权目录」入口：混合架构下本机执行面常驻，同样需要它。
  const localCapable = isDesktop && (activeLocal || provisionMode === 'dual');
  const [approval, setApproval] = useState<ToolApprovalMode | null>(null);
  const [permOpen, setPermOpen] = useState(false);
  const [approvalOpen, setApprovalOpen] = useState(false);

  const [saving, setSaving] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const requestVersion = useRef(0);
  const saveInFlight = useRef(false);

  useEffect(() => {
    if (saveInFlight.current) return;
    setApproval(null);
    setLoadFailed(false);
    let cancelled = false;
    const version = ++requestVersion.current;
    getToolApprovalMode()
      .then((mode) => {
        if (cancelled || version !== requestVersion.current) return;
        setApproval(mode);
        setLoadFailed(false);
      })
      .catch(() => {
        if (cancelled || version !== requestVersion.current) return;
        setApproval(null);
        setLoadFailed(true);
      });
    return () => { cancelled = true; };
  }, [localReady]);

  const applyApproval = async (mode: ToolApprovalMode) => {
    if (saveInFlight.current) return;
    saveInFlight.current = true;
    const version = ++requestVersion.current;
    setSaving(true);
    try {
      await setToolApprovalMode(mode);
      if (version !== requestVersion.current) return;
      setApproval(mode);
      setLoadFailed(false);
    } catch (error) {
      if (version !== requestVersion.current) return;
      // Either backend may have saved already. Never show the old preset as if
      // it described both backends; the user can retry any choice from the menu.
      setApproval(null);
      setLoadFailed(true);
      message.error(t('权限设置未完成同步，请重新选择后重试：{msg}', {
        msg: error instanceof Error ? error.message : String(error),
      }));
    } finally {
      saveInFlight.current = false;
      setSaving(false);
    }
  };

  const label = saving ? t('正在保存权限…')
    : approval ? APPROVAL_META[approval].label
      : loadFailed ? t('权限未同步') : t('正在读取权限…');

  const items = [
    {
      key: 'approval-group',
      type: 'group' as const,
      label: t('工具执行权限'),
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
    ...(localCapable
      ? [
          { type: 'divider' as const },
          {
            key: 'perm-detail',
            label: <span className="jx-approvalDetailEntry">{t('授权目录与细粒度策略…')}</span>,
            onClick: () => setPermOpen(true),
          },
        ]
      : []),
  ];

  return (
    <>
      <Dropdown
        disabled={saving}
        trigger={['click']}
        placement="topLeft"
        overlayClassName="jx-modeMenu jx-approvalMenu"
        onOpenChange={setApprovalOpen}
        menu={{ items }}
      >
        <button
          type="button"
          disabled={saving}
          aria-busy={saving}
          className={`jx-composerChip jx-projectDropBtn jx-approvalPillBtn${approvalOpen ? ' open' : ''}`}
          title={t('工具执行权限档（工具调用要不要先问你一句）')}
          aria-label={t('工具执行权限：{label}，点击切换', { label })}
        >
          <span className="jx-approvalPillIcon">{approval ? APPROVAL_META[approval].icon : <SafetyOutlined />}</span>
          <span className="jx-projectDropName jx-composerChip-label">{label}</span>
          <ChipChevron />
        </button>
      </Dropdown>
      {localCapable && <LocalPermissionsModal open={permOpen} onClose={() => setPermOpen(false)} />}
    </>
  );
}
