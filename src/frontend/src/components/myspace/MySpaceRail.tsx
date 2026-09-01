import { useState } from 'react';
import { Badge } from 'antd';
import {
  BellOutlined,
  BookOutlined,
  CloudOutlined,
  SearchOutlined,
  ShareAltOutlined,
  StarOutlined,
} from '@ant-design/icons';
import type { KbTabKey, MySpaceTab } from '../../types';
import { useMySpaceStore } from '../../stores/mySpaceStore';
import { useCatalogStore, useEditionStore } from '../../stores';
import { t } from '../../i18n';

const NAV_ITEMS: Array<{ key: MySpaceTab; label: string; icon: React.ReactNode }> = [
  { key: 'favorites', label: t('会话收藏'), icon: <StarOutlined /> },
  { key: 'shares', label: t('分享记录'), icon: <ShareAltOutlined /> },
  { key: 'notifications', label: t('消息通知'), icon: <BellOutlined /> },
];

/**
 * 「我的空间」的二级边栏：贴在主侧边栏右侧的一条独立导航列。
 * 挂在 App 外壳而不是面板内部——面板落在 .jx-panel 里带内边距且居中，栏塞进去会与主侧边栏之间露缝。
 * 云文档与知识库沿用侧边栏「能力中心」的分组交互：点整行即展开/收起，收起态点它顺带进入该页。
 * 窄屏隐藏，改由面板顶部拍平成一排的 Tab 承担同一套导航（同一份 store 状态，两种画法）。
 */
export function MySpaceRail() {
  const tab = useMySpaceStore((s) => s.tab);
  const setTab = useMySpaceStore((s) => s.setTab);
  const assetScope = useMySpaceStore((s) => s.assetScope);
  const setAssetScope = useMySpaceStore((s) => s.setAssetScope);
  const notifUnreadCount = useMySpaceStore((s) => s.notifUnreadCount);
  const openSearch = useMySpaceStore((s) => s.openSearch);
  const railCollapsed = useMySpaceStore((s) => s.railCollapsed);
  const kbTab = useCatalogStore((s) => s.kbTab);
  const setKbTab = useCatalogStore((s) => s.setKbTab);
  const multiTenancy = useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true));
  const isCE = useEditionStore((s) => s.edition === 'ce');

  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  /** 点分组标题：折叠/展开；原本是折叠的，顺带进入该页（否则收起状态下就进不去了） */
  const toggleGroup = (key: MySpaceTab) => {
    const wasCollapsed = !!collapsed[key];
    setCollapsed((prev) => ({ ...prev, [key]: !wasCollapsed }));
    if (wasCollapsed) setTab(key);
  };

  const cloudScopes = ([
    { key: 'personal', label: t('个人文件夹') },
    { key: 'team', label: t('团队文件夹') },
  ] as const).filter((s) => multiTenancy || s.key !== 'team');

  const kbScopes = ([
    { key: 'public', label: t('公共知识库') },
    { key: 'private', label: t('私有知识库') },
  ] as const).filter((s) => !isCE || s.key !== 'public');

  return (
    <aside
      className={`jx-msRail${railCollapsed ? ' is-collapsed' : ''}`}
      aria-label={t('我的空间')}
      aria-hidden={railCollapsed}
    >
      <div className="jx-msRail-inner">
      <div className="jx-msRail-title">{t('我的空间')}</div>

      <div className="jx-msRail-nav">
        <button type="button" className="jx-msRail-item" onClick={openSearch}>
          <SearchOutlined className="jx-msRail-icon" />
          <span>{t('搜索')}</span>
        </button>

        <Group
          label={t('云文档')}
          icon={<CloudOutlined />}
          collapsed={!!collapsed.assets}
          active={tab === 'assets'}
          onToggle={() => toggleGroup('assets')}
        >
          {cloudScopes.map((s) => (
            <button
              key={s.key}
              type="button"
              className={`jx-msRail-subItem${tab === 'assets' && assetScope === s.key ? ' active' : ''}`}
              onClick={() => { setAssetScope(s.key); setTab('assets'); }}
            >
              <span>{s.label}</span>
            </button>
          ))}
        </Group>

        <Group
          label={t('知识库')}
          icon={<BookOutlined />}
          collapsed={!!collapsed.kb}
          active={tab === 'kb'}
          onToggle={() => toggleGroup('kb')}
        >
          {kbScopes.map((s) => (
            <button
              key={s.key}
              type="button"
              className={`jx-msRail-subItem${tab === 'kb' && kbTab === s.key ? ' active' : ''}`}
              onClick={() => { setKbTab(s.key as KbTabKey); setTab('kb'); }}
            >
              <span>{s.label}</span>
            </button>
          ))}
        </Group>

        {NAV_ITEMS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`jx-msRail-item${tab === item.key ? ' active' : ''}`}
            onClick={() => setTab(item.key)}
          >
            <span className="jx-msRail-icon">{item.icon}</span>
            <span>{item.label}</span>
            {item.key === 'notifications' && notifUnreadCount > 0 && (
              <Badge count={notifUnreadCount} size="small" overflowCount={99} />
            )}
          </button>
        ))}
      </div>
      </div>
    </aside>
  );
}

/** 一级行退化为可折叠的分组标题：高亮只在折叠态出现，展开时交给选中的二级项，避免父子同时点亮 */
function Group({ label, icon, collapsed, active, onToggle, children }: {
  label: string;
  icon: React.ReactNode;
  collapsed: boolean;
  active: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="jx-msRail-group">
      <button
        type="button"
        className={`jx-msRail-item${collapsed && active ? ' active' : ''}`}
        aria-expanded={!collapsed}
        onClick={onToggle}
      >
        <span className="jx-msRail-icon">{icon}</span>
        <span>{label}</span>
      </button>
      <div className={`jx-expandWrap${collapsed ? '' : ' jx-expandWrap--open'}`}>
        <div className="jx-msRail-subList">{children}</div>
      </div>
    </div>
  );
}
