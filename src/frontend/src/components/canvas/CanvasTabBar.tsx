import {
  CloseOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  InsertRowRightOutlined,
  MoreOutlined,
  PlusOutlined,
  GlobalOutlined,
} from '@ant-design/icons';
import { Dropdown } from 'antd';
import { useCallback, useEffect, useRef } from 'react';

import './CanvasTabBar.css';
import { useCanvasGuard } from './useCanvasGuard';
import { t } from '../../i18n';
import { useAgentStore } from '../../stores/agentStore';
import { AgentIcon } from '../agent/AgentIcon';
import { useCanvasStore } from '../../stores';
import { tabTitle, tabIcon } from './canvasTabPresentation';
import { ChromeTabBackground } from './ChromeTabBackground';
import { useCanvasLauncherStore } from './canvasLauncherStore';

export function CanvasTabBar({ controlsOnly = false, onWidthChange }: {
  controlsOnly?: boolean;
  onWidthChange?: (width: number) => void;
} = {}) {
  const launcherOpen = useCanvasLauncherStore(s => s.open);
  const showLauncher = useCanvasLauncherStore(s => s.show);
  const dismissLauncher = useCanvasLauncherStore(s => s.dismiss);
  const agents = useAgentStore((state) => state.agents);
  const tabs = useCanvasStore((state) => state.tabs);
  const activeTabId = useCanvasStore((state) => state.activeTabId);
  const activateTab = useCanvasStore((state) => state.activateTab);
  const closeTab = useCanvasStore((state) => state.closeTab);
  const closeCanvas = useCanvasStore((state) => state.closeCanvas);
  const isFullscreen = useCanvasStore((state) => state.isFullscreen);
  const setCanvasFullscreen = useCanvasStore((state) => state.setCanvasFullscreen);
  const toggleCanvasFullscreen = useCanvasStore((state) => state.toggleCanvasFullscreen);
  const activeTabRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!controlsOnly || !barRef.current || !onWidthChange) return;
    const element = barRef.current;
    const measure = () => onWidthChange(element.getBoundingClientRect().width);
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    measure();
    return () => observer.disconnect();
  }, [controlsOnly, onWidthChange]);

  useEffect(() => {
    if (!isFullscreen) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCanvasFullscreen(false);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isFullscreen, setCanvasFullscreen]);

  // 页签多到需要横向滚动时，切换/新开的页签要自己滚进可视区。
  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [activeTabId, launcherOpen]);

  // 只有活动页签的内容是挂载着的，所以只有它可能有未保存的编辑；切走、关闭、
  // 收起面板都会把它卸载 —— 三条路径统一先确认，避免静默丢改动。
  const guardDirty = useCanvasGuard();

  const handleActivate = useCallback((tabId: string) => {
    if (tabId === activeTabId) { dismissLauncher(); return; }
    guardDirty(() => { dismissLauncher(); activateTab(tabId); });
  }, [activateTab, activeTabId, guardDirty, dismissLauncher]);

  const handleCloseTab = useCallback((tabId: string) => {
    // 关闭非活动页签不会卸载正在编辑的内容，无需确认。
    if (tabId !== activeTabId) {
      closeTab(tabId);
      return;
    }
    guardDirty(() => closeTab(tabId));
  }, [activeTabId, closeTab, guardDirty]);

  const handleCollapse = useCallback(() => {
    guardDirty(closeCanvas);
  }, [closeCanvas, guardDirty]);

  return (
    <div ref={barRef} className={controlsOnly ? "jx-canvasTabs jx-canvasTabs--controls" : "jx-canvasTabs chrome-tabs"} role={controlsOnly ? "toolbar" : "tablist"} aria-label={t('右侧面板')}>
      {!controlsOnly && <div className="jx-canvasTabs-list chrome-tabs-content">
        {tabs.map((tab) => {
          const agent = tab.kind === 'subagent'
            ? agents.find((item) => tab.target.agent.agent_id
              ? item.agent_id === tab.target.agent.agent_id
              : item.name === tab.target.agent.name) || tab.target.agent
            : null;
          const title = agent?.name || tabTitle(tab);
          const isActive = tab.id === activeTabId && !launcherOpen;
          return (
            <div
              key={tab.id}
              ref={isActive ? activeTabRef : undefined}
              className={`chrome-tab jx-canvasTab${isActive ? ' is-active' : ''}`}
              data-active={isActive ? "" : undefined}
              role="tab"
              aria-selected={isActive}
              tabIndex={0}
              title={title}
              onClick={() => handleActivate(tab.id)}
              onAuxClick={(event) => {
                if (event.button !== 1) return;
                event.preventDefault();
                handleCloseTab(tab.id);
              }}
              onKeyDown={(event) => {
                if (event.target !== event.currentTarget || (event.key !== 'Enter' && event.key !== ' ')) return;
                event.preventDefault();
                handleActivate(tab.id);
              }}
            >
              <ChromeTabBackground />
              <div className="chrome-tab-content">
              <span className="chrome-tab-favicon jx-canvasTab-icon" aria-hidden="true">{agent ? <AgentIcon agent={agent} size={18} /> : tabIcon(tab)}</span>
              <span className="chrome-tab-title jx-canvasTab-title">{title}</span>
              {tab.kind === 'file' && tab.dirty && (
                <span className="jx-canvasTab-dot" aria-label={t('有未保存的修改')} />
              )}
              <button
                type="button"
                className="chrome-tab-close jx-canvasTab-close"
                onClick={(event) => {
                  event.stopPropagation();
                  handleCloseTab(tab.id);
                }}
                aria-label={t('关闭「{name}」', { name: title })}
                title={t('关闭「{name}」', { name: title })}
              >
                <CloseOutlined />
              </button>
              </div>
            </div>
          );
        })}
        {launcherOpen && <div ref={activeTabRef} className="chrome-tab jx-canvasTab is-active" data-active="" role="tab" aria-selected="true">
          <ChromeTabBackground />
          <div className="chrome-tab-content">
            <span className="chrome-tab-favicon"><GlobalOutlined /></span>
            <span className="chrome-tab-title">{t('新标签页')}</span>
            <button className="chrome-tab-close" type="button" aria-label={t('关闭「{name}」', { name: t('新标签页') })} onClick={dismissLauncher} />
          </div>
        </div>}
      </div>}
      {!controlsOnly && <button type="button" className="jx-canvasTabs-add" aria-label={t('新建标签页')} title={t('新建标签页')} onClick={showLauncher}><PlusOutlined /></button>}
      {!controlsOnly && <div className="jx-canvasTabs-spacer" aria-hidden="true" />}
      <div className="jx-canvasTabs-actions">
        {controlsOnly && tabs.length > 1 && (
          <Dropdown trigger={['click']} menu={{
            items: tabs.map(tab => ({ key: tab.id, label: tabTitle(tab) })),
            selectedKeys: activeTabId ? [activeTabId] : [],
            onClick: ({ key }) => handleActivate(key),
          }}>
            <button type="button" className="jx-canvasTabs-action" aria-label={t('右侧面板')} title={t('右侧面板')}>
              <MoreOutlined />
            </button>
          </Dropdown>
        )}
        <button
          type="button"
          className="jx-canvasTabs-action jx-canvasTabs-fullscreen"
          onClick={toggleCanvasFullscreen}
          aria-label={isFullscreen ? t('退出全屏') : t('全屏')}
          title={isFullscreen ? t('退出全屏') : t('全屏')}
          aria-pressed={isFullscreen}
        >
          {isFullscreen ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
        </button>
        <button
          type="button"
          className="jx-canvasTabs-action"
          onClick={handleCollapse}
          aria-label={t('收起右侧面板')}
          title={t('收起右侧面板')}
        >
          <InsertRowRightOutlined />
        </button>
      </div>
    </div>
  );
}
