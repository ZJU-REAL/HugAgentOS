import { InsertRowRightOutlined } from '@ant-design/icons';

import { t } from '../../i18n';
import { useCanvasStore } from '../../stores';
import { ContentErrorBoundary } from '../common';
import { CanvasPanel } from './CanvasPanel';
import { CanvasTabBar } from './CanvasTabBar';
import { OntologySidebarPanel } from './OntologySidebarPanel';
import { PluginCanvasPanel } from './PluginCanvasPanel';

export function RightSidebarPanel() {
  const activeView = useCanvasStore((state) => state.activeView);
  const artifact = useCanvasStore((state) => state.artifact);
  const activeTabId = useCanvasStore((state) => state.activeTabId);
  const ontologyTarget = useCanvasStore((state) => state.ontologyTarget);

  // key=页签 id：切换页签必须换一份面板实例，否则上一份的预览状态（xlsx 编辑缓冲、
  // 画布视口、加载中的 blob）会漏到新页签上。
  if (activeView === 'file' && artifact) return <CanvasPanel key={activeTabId} />;
  if (activeView === 'plugin') return <PluginCanvasPanel key={activeTabId} />;
  // No evolution view: what a turn learned is now shown, and edited, inline on
  // the card itself. A side panel could only restate it one click further away.
  if (activeView === 'ontology') {
    return (
      <ContentErrorBoundary
        resetKey={`ontology:${ontologyTarget?.chatId ?? ''}:${ontologyTarget?.messageTs ?? ''}`}
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
