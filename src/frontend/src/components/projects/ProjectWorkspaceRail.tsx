import { useState } from 'react';
import { Button } from 'antd';
import { CloseOutlined, FolderOutlined } from '@ant-design/icons';
import { useProjectStore } from '../../stores/projectStore';
import { t } from '../../i18n';
import ProjectRightRail from './ProjectRightRail';

export default function ProjectWorkspaceRail({ projectId }: { projectId: string }) {
  const project = useProjectStore(s => s.currentProject);
  const loading = useProjectStore(s => s.detailLoading);
  const [expanded, setExpanded] = useState(false);
  return <aside className={`jx-projectWorkspaceRail${expanded ? ' is-expanded' : ''}`} aria-label={t('项目资料')}>
    <Button className="jx-projectWorkspaceRail-toggle" icon={expanded ? <CloseOutlined /> : <FolderOutlined />}
      aria-expanded={expanded} aria-controls="project-resources" onClick={() => setExpanded(!expanded)}>
      {t('项目资料')}
    </Button>
    <div id="project-resources" className="jx-projectWorkspaceRail-body">
      {project?.project_id === projectId ? <ProjectRightRail /> : loading
        ? <div role="status">{t('加载中…')}</div>
        : <div role="alert">{t('项目不存在或你无权访问')}<Button onClick={() => void useProjectStore.getState().reloadProject(projectId)}>{t('重试')}</Button></div>}
    </div>
  </aside>;
}
