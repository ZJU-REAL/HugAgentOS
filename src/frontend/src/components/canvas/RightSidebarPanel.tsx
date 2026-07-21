import { InsertRowRightOutlined } from '@ant-design/icons';

import { t } from '../../i18n';
import { useCanvasStore } from '../../stores';
import { ContentErrorBoundary } from '../common';
import { CanvasPanel } from './CanvasPanel';
import { OntologySidebarPanel } from './OntologySidebarPanel';

export function RightSidebarPanel() {
  const activeView = useCanvasStore((state) => state.activeView);
  const artifact = useCanvasStore((state) => state.artifact);
  const ontologyTarget = useCanvasStore((state) => state.ontologyTarget);

  if (activeView === 'file' && artifact) return <CanvasPanel />;
  if (activeView === 'ontology') {
    return (
      <ContentErrorBoundary
        resetKey={`ontology:${ontologyTarget?.chatId ?? ''}:${ontologyTarget?.messageTs ?? ''}`}
        fallback={(
          <aside className="jx-rightSidebar jx-rightSidebar--ontology" role="alert">
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
