import { useEffect } from 'react';
import { FolderAddOutlined, LaptopOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import { Button, Dropdown, Empty, Input, Select, Spin, message } from 'antd';

import { createLocalProject } from '../../api';
import { usePanelHeader } from '../../hooks/usePageConfig';
import { t } from '../../i18n';
import { projectCreationTargets, useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { useProjectStore } from '../../stores/projectStore';
import CreateProjectModal from './CreateProjectModal';
import ProjectCard from './ProjectCard';

export default function ProjectsPanel({ onOpenProject }: { onOpenProject: (projectId: string) => void }) {
  const list = useProjectStore((state) => state.list);
  const loading = useProjectStore((state) => state.listLoading);
  const search = useProjectStore((state) => state.searchKeyword);
  const setSearch = useProjectStore((state) => state.setSearchKeyword);
  const sort = useProjectStore((state) => state.sort);
  const setSort = useProjectStore((state) => state.setSort);
  const fetchProjects = useProjectStore((state) => state.fetchProjects);
  const setCreateOpen = useProjectStore((state) => state.setCreateModalOpen);
  const toggleFavoriteById = useProjectStore((state) => state.toggleFavoriteById);
  const isDesktop = useDeploymentModeStore((state) => state.isDesktop);
  const provisionMode = useDeploymentModeStore((state) => state.provisionMode);
  const refreshDeploymentMode = useDeploymentModeStore((state) => state.refresh);
  const {
    cloud: canCreateCloudProject,
    local: canCreateLocalProject,
  } = projectCreationTargets(isDesktop, provisionMode);
  const { title, subtitle } = usePanelHeader('projects', {
    title: '项目',
    subtitle: '把对话、文件和指令打包成专属工作空间',
  });

  useEffect(() => { void fetchProjects(); }, [fetchProjects, sort]);
  useEffect(() => { refreshDeploymentMode(); }, [refreshDeploymentMode]);
  useEffect(() => {
    const timer = setTimeout(() => void fetchProjects(), 300);
    return () => clearTimeout(timer);
  }, [fetchProjects, search]);

  useEffect(() => {
    if (!isDesktop || !canCreateLocalProject) return;
    const onFolder = (event: Event) => {
      const path = (event as CustomEvent<string>).detail;
      if (!path) return;
      const name = path.split(/[/\\]/).filter(Boolean).pop() || '本地项目';
      void createLocalProject({ name, local_path: path })
        .then(async (project) => {
          await fetchProjects();
          onOpenProject(project.project_id);
        })
        .catch((error) => {
          message.error(`${t('新建本地项目')}：${error?.message || error}`);
        });
    };
    window.addEventListener('hugagent:local-folder', onFolder as EventListener);
    return () => window.removeEventListener('hugagent:local-folder', onFolder as EventListener);
  }, [isDesktop, canCreateLocalProject, fetchProjects, onOpenProject]);

  const pickLocalProjectFolder = () => {
    window.location.href = '/__desktop/pick-local-folder';
  };

  const createButton = (
    <Button
      type="primary"
      icon={<PlusOutlined />}
      onClick={
        canCreateLocalProject && !canCreateCloudProject
          ? pickLocalProjectFolder
          : canCreateCloudProject && !canCreateLocalProject
            ? () => setCreateOpen(true)
            : undefined
      }
    >
      {isDesktop && canCreateLocalProject && !canCreateCloudProject
        ? t('新建本地项目')
        : isDesktop && canCreateCloudProject && !canCreateLocalProject
          ? t('新建云端项目')
          : t('新建项目')}
    </Button>
  );

  return (
    <div className="jx-projects">
      <div className="jx-projects-shell">
        <div className="jx-projects-header">
          <div>
            <div className="jx-agentPage-title">{title}</div>
            {subtitle ? <div className="jx-agentPage-subtitle">{subtitle}</div> : null}
          </div>
          <div className="jx-projects-actions">
            <span className="jx-projects-sortLabel">Sort by</span>
            <Select
              value={sort}
              onChange={setSort}
              style={{ width: 120 }}
              options={[
                { value: 'activity', label: t('活跃度') },
                { value: 'name', label: t('名称') },
                { value: 'created', label: t('创建时间') },
              ]}
            />
            {isDesktop && canCreateCloudProject && canCreateLocalProject ? (
              <Dropdown
                trigger={['click']}
                menu={{
                  items: [
                    {
                      key: 'new-cloud-project',
                      icon: <FolderAddOutlined />,
                      label: t('新建云端项目'),
                      onClick: () => setCreateOpen(true),
                    },
                    {
                      key: 'new-local-project',
                      icon: <LaptopOutlined />,
                      label: t('新建本地项目'),
                      onClick: pickLocalProjectFolder,
                    },
                  ],
                }}
              >
                {createButton}
              </Dropdown>
            ) : createButton}
          </div>
        </div>
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder={t('搜索项目...')}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className="jx-projects-searchInput"
        />
        {loading && list.length === 0 ? (
          <div className="jx-projects-loading"><Spin /></div>
        ) : list.length === 0 ? (
          <Empty description={t('还没有项目，创建一个开始吧')} style={{ marginTop: 60 }} />
        ) : (
          <section className="jx-projects-section">
            <div className="jx-projects-sectionTitle">{t('个人项目')}</div>
            <div className="jx-projects-grid jx-anim-stagger">
              {list.map((project, index) => (
                <ProjectCard
                  key={project.project_id}
                  project={project}
                  staggerIndex={index}
                  onOpen={() => onOpenProject(project.project_id)}
                  onToggleFavorite={() => void toggleFavoriteById(project.project_id, !project.favorite)}
                />
              ))}
            </div>
          </section>
        )}
      </div>
      {canCreateCloudProject && <CreateProjectModal onCreated={onOpenProject} />}
    </div>
  );
}
