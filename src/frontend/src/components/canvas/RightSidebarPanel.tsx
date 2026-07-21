import { InsertRowRightOutlined } from '@ant-design/icons';

import { t } from '../../i18n';
import { useCanvasStore } from '../../stores';
import { CanvasPanel } from './CanvasPanel';
import { OntologySidebarPanel } from './OntologySidebarPanel';

export function RightSidebarPanel() {
  const activeView = useCanvasStore((state) => state.activeView);
  const artifact = useCanvasStore((state) => state.artifact);

  if (activeView === 'file' && artifact) return <CanvasPanel />;
  if (activeView === 'ontology') return <OntologySidebarPanel />;

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
