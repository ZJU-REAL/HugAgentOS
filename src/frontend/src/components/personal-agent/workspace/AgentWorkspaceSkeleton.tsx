import { t } from '../../../i18n';
import { SkeletonLines } from '../../common/LoadState';
import './agent-workspace.css';

/** Code-loading placeholder with the workspace's toolbar, file tree and viewer geometry. */
export function AgentWorkspaceSkeleton() {
  return (
    <div className="pa-ws" role="status" aria-label={t('加载中…')} aria-busy="true">
      <div className="pa-ws-toolbar" aria-hidden="true">
        <div className="jx-skeletonBlock pa-ws-skHint" />
        <div className="jx-skeletonBlock pa-ws-skButton" />
      </div>
      <div className="pa-ws-split" aria-hidden="true">
        <aside className="pa-ws-tree">
          <div className="pa-ws-treeHead">/workspace</div>
          <div className="pa-ws-treeBody"><SkeletonLines rows={6} className="pa-ws-skTree" /></div>
        </aside>
        <section className="pa-ws-viewer">
          <div className="pa-ws-viewerHead"><div className="jx-skeletonBlock pa-ws-skPath" /></div>
          <div className="pa-ws-viewerBody"><SkeletonLines rows={8} /></div>
        </section>
      </div>
    </div>
  );
}
