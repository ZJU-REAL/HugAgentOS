import {
  ApartmentOutlined,
  CloseOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  InsertRowRightOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { Modal } from 'antd';
import { useCallback, useEffect, useRef, type ReactNode } from 'react';

import { t } from '../../i18n';
import { useCanvasStore, usePluginUiStore } from '../../stores';
import type { CanvasTab } from '../../stores/canvasStore';
import { getFileIconSrc } from '../../utils/fileIcon';

function tabTitle(tab: CanvasTab): string {
  if (tab.kind === 'file') return tab.artifact.name;
  // A plugin tab is labelled by the plugin's own declaration; the generic
  // fallback only shows if that declaration carried no title.
  if (tab.kind === 'plugin') return tab.target.title || t('插件视图');
  return t('本体校验');
}

function tabIcon(tab: CanvasTab): ReactNode {
  if (tab.kind === 'file') {
    return <img src={getFileIconSrc(tab.artifact.name)} width="17" height="17" alt="" aria-hidden="true" />;
  }
  if (tab.kind === 'plugin') {
    // The contributed canvas view / module may declare its own icon; the glyph
    // is only the fallback for declarations that ship none.
    const { target } = tab;
    const store = usePluginUiStore.getState();
    const icon = store.findCanvas(target.slug, target.canvasId)?.contribution.icon
      || store.findModule(target.slug, target.canvasId)?.contribution.icon;
    return icon
      ? <img src={icon} width="17" height="17" alt="" aria-hidden="true" />
      : <ApartmentOutlined />;
  }
  return <SafetyCertificateOutlined />;
}

export function CanvasTabBar() {
  const tabs = useCanvasStore((state) => state.tabs);
  const activeTabId = useCanvasStore((state) => state.activeTabId);
  const activateTab = useCanvasStore((state) => state.activateTab);
  const closeTab = useCanvasStore((state) => state.closeTab);
  const closeCanvas = useCanvasStore((state) => state.closeCanvas);
  const isFullscreen = useCanvasStore((state) => state.isFullscreen);
  const setCanvasFullscreen = useCanvasStore((state) => state.setCanvasFullscreen);
  const toggleCanvasFullscreen = useCanvasStore((state) => state.toggleCanvasFullscreen);
  const activeTabRef = useRef<HTMLDivElement>(null);

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
  }, [activeTabId]);

  // 只有活动页签的内容是挂载着的，所以只有它可能有未保存的编辑；切走、关闭、
  // 收起面板都会把它卸载 —— 三条路径统一先确认，避免静默丢改动。
  const guardDirty = useCallback((action: () => void) => {
    const state = useCanvasStore.getState();
    const active = state.tabs.find((tab) => tab.id === state.activeTabId);
    if (!active || active.kind !== 'file' || !active.dirty) {
      action();
      return;
    }
    Modal.confirm({
      title: t('有未保存的修改'),
      content: t('离开后编辑内容将丢失，确定继续？'),
      okText: t('放弃修改'),
      cancelText: t('取消'),
      okButtonProps: { danger: true },
      onOk: action,
    });
  }, []);

  const handleActivate = useCallback((tabId: string) => {
    if (tabId === activeTabId) return;
    guardDirty(() => activateTab(tabId));
  }, [activateTab, activeTabId, guardDirty]);

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
    <div className="jx-canvasTabs" role="tablist" aria-label={t('右侧面板')}>
      <div className="jx-canvasTabs-list">
        {tabs.map((tab) => {
          const title = tabTitle(tab);
          const isActive = tab.id === activeTabId;
          return (
            <div
              key={tab.id}
              ref={isActive ? activeTabRef : undefined}
              className={`jx-canvasTab${isActive ? ' is-active' : ''}`}
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
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                handleActivate(tab.id);
              }}
            >
              <span className="jx-canvasTab-icon" aria-hidden="true">{tabIcon(tab)}</span>
              <span className="jx-canvasTab-title">{title}</span>
              {tab.kind === 'file' && tab.dirty && (
                <span className="jx-canvasTab-dot" aria-label={t('有未保存的修改')} />
              )}
              <button
                type="button"
                className="jx-canvasTab-close"
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
          );
        })}
      </div>
      <div className="jx-canvasTabs-spacer" aria-hidden="true" />
      <div className="jx-canvasTabs-actions">
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
