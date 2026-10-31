import { useState } from 'react';
import { Button } from 'antd';
import { CloseOutlined, FolderOutlined } from '@ant-design/icons';
import { useProjectStore } from '../../stores/projectStore';
import { t } from '../../i18n';
import ProjectRightRail from './ProjectRightRail';
import { ProjectRailSkeleton } from './ProjectSkeletons';
import { LoadError } from '../common/LoadState';

export default function ProjectWorkspaceRail({ projectId }: { projectId: string }) {
  const loaded = useProjectStore(s => s.currentProject?.project_id === projectId);
  const error = useProjectStore(s => s.detailError);
  const [expanded, setExpanded] = useState(false);
  return <aside className={`jx-projectWorkspaceRail${expanded ? ' is-expanded' : ''}`} aria-label={t('项目资料')}>
    <Button className="jx-projectWorkspaceRail-toggle" icon={expanded ? <CloseOutlined /> : <FolderOutlined />}
      aria-expanded={expanded} aria-controls="project-resources" onClick={() => setExpanded(!expanded)}>
      {t('项目资料')}
    </Button>
    <div id="project-resources" className="jx-projectWorkspaceRail-body">
      {loaded ? <ProjectRightRail /> : error ? (
        <LoadError className="jx-projectRail-card" title={t('项目资料加载失败')} reason={error}
          onRetry={() => void useProjectStore.getState().reloadProject(projectId)} />
      ) : <ProjectRailSkeleton />}
    </div>
  </aside>;
}
