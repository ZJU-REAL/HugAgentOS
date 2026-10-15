import { StandaloneLinkCard } from '../common/StandaloneLinkCard';
import { Modal, Tabs, Tag, Typography } from 'antd';
import { ApiOutlined } from '@ant-design/icons';
import { listProjectApiKeys, createProjectApiKey, toggleProjectApiKey, revokeProjectApiKey, revealProjectApiKey } from '../../api/projectApi';
import { useAuthStore } from '../../stores/authStore';
import { useProjectStore } from '../../stores/projectStore';
import ProjectApiGuide from './ProjectApiGuide';
import { ApiKeyManager } from '../common/ApiKeyManager';
import { CopyButton } from '../common/CopyButton';
import { t } from '../../i18n';
import '../../styles/agent-api.css';
import '../../styles/project-api.css';

const projectKeys = { list: listProjectApiKeys, create: createProjectApiKey, toggle: toggleProjectApiKey, revoke: revokeProjectApiKey, reveal: revealProjectApiKey };

export default function ProjectApiModal({ projectId, onClose }: {
  projectId: string;
  onClose: () => void;
}) {
  const project = useProjectStore(s => s.currentProject);
  const canUseApiKey = useAuthStore(s => s.authUser?.can_use_api_key === true);
  const canEdit = project?.permission === 'admin' || project?.permission === 'edit';
  return <Modal centered open title={<div className="jx-projectApi-title">
      <span className="jx-projectApi-icon"><ApiOutlined /></span>
      <div><div>{t('项目 API')}</div><Typography.Text type="secondary">{project?.name}</Typography.Text></div>
    </div>} onCancel={onClose} footer={null} width={900} className="jx-agentApi-modal jx-projectApi-modal">
    <div className="jx-agentApi-body">
      <div className="jx-projectApi-identity">
        <Typography.Text type="secondary">{t('项目 ID')}</Typography.Text>
        <Typography.Text code>{projectId}</Typography.Text>
        <CopyButton text={projectId} aria-label={t('复制项目 ID')} />
        <Tag>{t('仅限此项目')}</Tag>
      </div>
      <StandaloneLinkCard kind="projects" resourceId={projectId} canManage={canEdit && canUseApiKey} />
      <Tabs destroyOnHidden items={[
        ...(canEdit && canUseApiKey ? [{ key: 'keys', label: 'API-Key', children: <ApiKeyManager resourceId={projectId} api={projectKeys} /> }] : []),
        { key: 'guide', label: t('调用示例'), children: <ProjectApiGuide projectId={projectId} /> },
      ]} />
    </div>
  </Modal>;
}
