import { useState } from 'react';
import { Button, Dropdown, Modal, Tag, Tooltip, message } from 'antd';
import { ArrowLeftOutlined, EditOutlined, MoreOutlined, StarFilled, StarOutlined } from '@ant-design/icons';
import { useProjectStore } from '../../stores/projectStore';
import { useCatalogStore } from '../../stores/catalogStore';
import { t } from '../../i18n';
import EditProjectModal from './EditProjectModal';

export default function ProjectHeader({ projectId }: { projectId: string }) {
  const project = useProjectStore(s => s.currentProject);
  const [editOpen, setEditOpen] = useState(false);
  if (!project || project.project_id !== projectId) return null;
  const canAdmin = project.permission === 'admin';
  const canEdit = canAdmin || project.permission === 'edit';
  const canDelete = !!project.is_owner;
  const onBack = () => useCatalogStore.getState().setPanel('projects');
  const toggleFav = (on: boolean) => {
    void useProjectStore.getState().toggleFavorite(on).catch(error => message.error(error.message || t('操作失败')));
  };
  const handleDelete = () => Modal.confirm({
    title: t('删除项目「{name}」？', { name: project.name }),
    content: t('项目对应的直传文件会一同软删除；引用文件不动。该操作可由数据库恢复。'),
    okType: 'danger', okText: t('删除'), cancelText: t('取消'),
    onOk: async () => {
      await useProjectStore.getState().deleteProject(project.project_id);
      onBack();
    },
  });
  return <section className="jx-projectHeader">
    <div className="jx-projectHeader-back">
      <Button type="link" icon={<ArrowLeftOutlined />} onClick={onBack}>
        {t('所有项目')}
      </Button>
    </div>
    <div className="jx-projectHeader-heading">
      <h2 className="jx-projectHeader-title">{project.name}</h2>
      {(project as { kind?: string }).kind === 'local' && (
        <Tag color="green">{t('本机')}</Tag>
      )}
      <div className="jx-projectHeader-actions">
        <Button
          type="text"
          icon={project.favorite ? <StarFilled style={{ color: 'var(--color-warning)' }} /> : <StarOutlined />}
          aria-label={t('收藏项目')}
          onClick={() => toggleFav(!project.favorite)}
        />
        {canEdit && (
          <Tooltip title={canAdmin ? t('编辑项目信息') : t('编辑项目目标')}>
            <Button type="text" icon={<EditOutlined />} aria-label={t('编辑项目信息')} onClick={() => setEditOpen(true)} />
          </Tooltip>
        )}
        {canDelete && (
          <Dropdown
            menu={{
              items: [
                { key: 'delete', label: t('删除项目'), danger: true, onClick: handleDelete },
              ],
            }}
            trigger={['click']}
          >
            <Button type="text" icon={<MoreOutlined />} aria-label={t('项目更多操作')} />
          </Dropdown>
        )}
      </div>
    </div>
    {project.description && (
      <div className="jx-projectHeader-description">{project.description}</div>
    )}


    <EditProjectModal open={editOpen} onClose={() => setEditOpen(false)} />
  </section>;
}
