import { useState } from 'react';
import { useAuthStore } from '../../stores';
import { staggerStyle } from '../../utils/motionTokens';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { EDITION_LAB_ITEMS, SkillDistillPanel } from '../../labEdition';
import LoopPanel from '../loop/LoopPanel';
import { t } from '../../i18n';

const LAB_ITEMS = [
  ...EDITION_LAB_ITEMS,
  {
    id: 'autonomous_loop',
    name: t('自主循环'),
    icon: '/home/new-icons/more.svg',
    description: t('让智能体自我推进的长时运行任务：按可验证目标（环境验证）反复迭代、自我修正，达标或触预算即停'),
    enabled: true,
  },
  // 「站点」已从实验室提升为侧边栏一级入口（LAYOUT_ITEMS.sites → panel 'sites'），此处不再重复陈列。
  {
    id: 'coming_soon',
    name: t('更多实验'),
    icon: '/home/new-icons/more.svg',
    description: t('更多实验性 AI 能力将陆续开放'),
    enabled: false,
  },
];

export default function LabPanel() {
  const [subPanel, setSubPanel] = useState<'skill_distill' | 'autonomous_loop' | null>(null);
  const labEnabled = useAuthStore((s) => s.authUser?.lab_enabled);
  const { title: labTitle, subtitle: labSubtitle } = usePanelHeader('lab', {
    title: '实验室',
    subtitle: 'AI 能力实验性应用',
  });

  // Defensive permission check: if the admin has disabled this user's lab permission, show a no-permission notice inside the panel.
  // A default of undefined is treated as enabled (backward compatible).
  if (labEnabled === false) {
    return (
      <div className="jx-agentPage">
        <div className="jx-agentPage-header">
          <div>
            <div className="jx-agentPage-title">{labTitle}</div>
            <div className="jx-agentPage-subtitle">{t('暂无访问权限，请联系管理员开通实验室权限。')}</div>
          </div>
        </div>
      </div>
    );
  }

  const handleClick = (id: string) => {
    if (id === 'skill_distill') {
      setSubPanel('skill_distill');
    } else if (id === 'autonomous_loop') {
      setSubPanel('autonomous_loop');
    }
  };

  if (subPanel === 'skill_distill') {
    return <SkillDistillPanel onBack={() => setSubPanel(null)} />;
  }
  if (subPanel === 'autonomous_loop') {
    return (
      <div className="jx-agentPage">
        <LoopPanel onBack={() => setSubPanel(null)} />
      </div>
    );
  }

  return (
    <div className="jx-agentPage jx-labPage">
      <div className="jx-agentPage-header">
        <div>
          <div className="jx-agentPage-title">{labTitle}</div>
          {labSubtitle ? <div className="jx-agentPage-subtitle">{labSubtitle}</div> : null}
        </div>
      </div>

      <div
        className="jx-agentPage-grid jx-anim-stagger"
        style={{ '--stagger-step': '30ms' } as React.CSSProperties}
      >
        {LAB_ITEMS.map((app, idx) => (
          <div
            key={app.id}
            className={`jx-agentCard jx-card-lift${!app.enabled ? ' jx-agentCard--disabled' : ''}`}
            style={staggerStyle(idx)}
            onClick={() => app.enabled && handleClick(app.id)}
            role={app.enabled ? 'button' : undefined}
            tabIndex={app.enabled ? 0 : undefined}
          >
            <div className="jx-agentCard-body">
              <div className="jx-agentCard-head">
                <img
                  src={app.icon}
                  alt=""
                  width={28}
                  height={28}
                  style={{ borderRadius: '50%', objectFit: 'cover', display: 'block' }}
                />
                <div className="jx-agentCard-nameRow">
                  <span className="jx-agentCard-name">{app.name}</span>
                  <span className={`jx-agentCard-badge${app.enabled ? ' jx-agentCard-badge--enabled' : ''}`}>
                    {app.enabled ? t('可用') : t('即将上线')}
                  </span>
                </div>
              </div>
              <div className="jx-agentCard-desc">{app.description}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
