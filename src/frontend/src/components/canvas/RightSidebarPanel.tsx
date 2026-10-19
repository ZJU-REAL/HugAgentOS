import { useCanvasPane } from './useCanvasPane';
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useCanvasStore } from '../../stores/canvasStore';
import { CanvasPaneContext } from './CanvasPaneContext';
import { useCanvasPaneId } from './useCanvasPane';
import './CanvasSplit.css';
import { CanvasLauncher } from './CanvasLauncher';
import { useCanvasLauncherStore } from './canvasLauncherStore';
import { InsertRowRightOutlined } from '@ant-design/icons';

import { t } from '../../i18n';

import { ContentErrorBoundary } from '../common';
import { SubagentSidebarPanel } from './SubagentSidebarPanel';
import { CanvasPanel } from './CanvasPanel';
import { CanvasTabBar } from './CanvasTabBar';
import { OntologySidebarPanel } from './OntologySidebarPanel';
import { PluginCanvasPanel } from './PluginCanvasPanel';
import { canvasRenderer } from './customCanvasRegistry';

export function RightSidebarPanel() {
  const isSplit = useCanvasStore(s => s.isSplit);
  const focusPane = useCanvasStore(s => s.focusPane);
  const root = useRef<HTMLDivElement>(null);
  const cleanup = useRef<(() => void) | null>(null);
  const [fraction, setFraction] = useState(.5);
  const [dragging, setDragging] = useState(false);
  const [width, setWidth] = useState(0);
  const minimum = Math.max(.2, Math.min(.5, 200 / (width || 400)));
  const clamp = (value: number) => Math.max(minimum, Math.min(1 - minimum, value));
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const observer = new ResizeObserver(entries => setWidth(entries[0].contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => () => cleanup.current?.(), []);
  useEffect(() => { if (!isSplit) cleanup.current?.(); }, [isSplit]);
  return <div ref={root} className={'jx-canvasSplit' + (isSplit ? ' is-split' : '')}
    style={{ '--canvas-split': clamp(fraction) } as CSSProperties}>
    <section className="jx-canvasSplit-pane" aria-label={t('左侧分屏')}
      onPointerDownCapture={() => focusPane('primary')} onFocusCapture={() => focusPane('primary')}>
      <CanvasPaneContext.Provider value="primary"><CanvasPane /></CanvasPaneContext.Provider>
    </section>
    {isSplit && <>
      <div className="jx-canvasSplit-divider" role="separator" aria-orientation="vertical"
        aria-label={t('调整分屏大小')} aria-valuemin={Math.round(minimum * 100)} aria-valuemax={Math.round((1 - minimum) * 100)}
        aria-valuenow={Math.round(clamp(fraction) * 100)} tabIndex={0}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          setFraction(value => event.key === 'Home' ? minimum : event.key === 'End' ? 1 - minimum
            : clamp(value + (event.key === 'ArrowLeft' ? -.05 : .05)));
        }}
        onPointerDown={event => {
          event.preventDefault();
          const rect = root.current?.getBoundingClientRect();
          if (!rect) return;
          const move = (e: PointerEvent) => setFraction(clamp((e.clientX - rect.left) / rect.width));
          const stop = () => {
            window.removeEventListener('pointermove', move);
            window.removeEventListener('pointerup', stop);
            window.removeEventListener('pointercancel', stop);
            setDragging(false); cleanup.current = null;
          };
          cleanup.current?.(); cleanup.current = stop; setDragging(true);
          window.addEventListener('pointermove', move);
          window.addEventListener('pointerup', stop);
          window.addEventListener('pointercancel', stop);
        }} onDoubleClick={() => setFraction(.5)} />
      <section className="jx-canvasSplit-pane" aria-label={t('右侧分屏')}
        onPointerDownCapture={() => focusPane('secondary')} onFocusCapture={() => focusPane('secondary')}>
        <CanvasPaneContext.Provider value="secondary"><CanvasPane /></CanvasPaneContext.Provider>
      </section>
    </>}
    {dragging && <div className="jx-canvasSplit-mask" />}
  </div>;
}

function CanvasPane() {
  const pane = useCanvasPaneId();
  const explicitLauncher = useCanvasLauncherStore(s => s.open && s.pane === pane);
  const dismiss = useCanvasLauncherStore(s => s.dismiss);
  const activeTabId = useCanvasPane(s => s.activeTabId);
  const launcherOpen = explicitLauncher || activeTabId === null;
  useEffect(() => {
    const launcher = useCanvasLauncherStore.getState();
    if (launcher.pane === pane) dismiss();
  }, [activeTabId, dismiss, pane]);

  return <div className={'jx-canvasWorkspace' + (launcherOpen ? ' is-launching' : '')}>
    <div className="jx-canvasWorkspace-current" inert={launcherOpen || undefined}><SidebarContent /></div>
    {launcherOpen && <CanvasLauncher />}
  </div>;
}

function SidebarContent() {
  const activeView = useCanvasPane((state) => state.activeView);
  const artifact = useCanvasPane((state) => state.artifact);
  const activeTabId = useCanvasPane((state) => state.activeTabId);
  const ontologyTarget = useCanvasPane((state) => state.ontologyTarget);

  // key=页签 id：切换页签必须换一份面板实例，否则上一份的预览状态（xlsx 编辑缓冲、
  // 画布视口、加载中的 blob）会漏到新页签上。
  if (activeView === 'file' && artifact) return <CanvasPanel key={activeTabId} />;
  if (activeView === 'subagent') {
    return (
      <ContentErrorBoundary
        resetKey={activeTabId || ''}
        fallback={(
          <aside className="jx-rightSidebar">
            <CanvasTabBar />
            <div className="jx-rightSidebar-empty">{t('暂无智能体执行记录')}</div>
          </aside>
        )}
      >
        <SubagentSidebarPanel key={activeTabId} />
      </ContentErrorBoundary>
    );
  }
  if (activeView === 'plugin') return <PluginCanvasPanel key={activeTabId} />;
  if (activeView === 'custom') return <CustomCanvasPanel key={activeTabId} />;
  // No evolution view: what a turn learned is now shown, and edited, inline on
  // the card itself. A side panel could only restate it one click further away.
  if (activeView === 'ontology') {
    return (
      <ContentErrorBoundary
        resetKey={`ontology:${ontologyTarget?.chatId ?? ''}:${ontologyTarget?.messageUid ?? ''}`}
        fallback={(
          <aside className="jx-rightSidebar jx-rightSidebar--ontology" role="alert">
            <CanvasTabBar />
            <div className="jx-rightSidebar-empty">
              <strong>{t('本体校验结果暂时无法显示')}</strong>
              <span>{t('当前会话仍可继续使用，请稍后重新打开结果。')}</span>
            </div>
          </aside>
        )}
      >
        <OntologySidebarPanel />
      </ContentErrorBoundary>
    );
  }

  return (
    <aside className="jx-rightSidebar jx-rightSidebar--blank" aria-label={t('右侧面板')}>
      <CanvasTabBar />
      <div className="jx-rightSidebar-body">
        <div className="jx-rightSidebar-empty">
          <InsertRowRightOutlined />
          <strong>{t('暂无可展示内容')}</strong>
          <span>{t('预览文件或运行本体校验后，内容会显示在这里。')}</span>
        </div>
      </div>
    </aside>
  );
}

function CustomCanvasPanel() {
  const target = useCanvasPane((state) => state.customTarget);
  const View = target ? canvasRenderer(target.renderer)?.component : undefined;
  return (
    <aside className="jx-rightSidebar jx-rightSidebar--custom" aria-label={target?.title}>
      <CanvasTabBar />
      <div className="jx-rightSidebar-body">
        {View ? <ContentErrorBoundary resetKey={target?.renderer || ''} fallback={<div className="jx-rightSidebar-empty">{t('内容暂时无法显示')}</div>}><View /></ContentErrorBoundary>
          : <div className="jx-rightSidebar-empty">{t('内容暂时无法显示')}</div>}
      </div>
    </aside>
  );
}
