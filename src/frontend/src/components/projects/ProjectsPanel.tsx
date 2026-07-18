import { useEffect, useMemo } from 'react';
import { Button, Empty, Input, Select, Spin } from 'antd';
import { SearchOutlined, PlusOutlined } from '@ant-design/icons';
import { useProjectStore } from '../../stores/projectStore';
import { usePanelHeader } from '../../hooks/usePageConfig';
import ProjectCard from './ProjectCard';
import CreateProjectModal from './CreateProjectModal';
import { t } from '../../i18n';

interface Props {
  onOpenProject: (projectId: string) => void;
}

export default function ProjectsPanel({ onOpenProject }: Props) {
  const list = useProjectStore((s) => s.list);
  const loading = useProjectStore((s) => s.listLoading);
  const search = useProjectStore((s) => s.searchKeyword);
  const setSearch = useProjectStore((s) => s.setSearchKeyword);
  const sort = useProjectStore((s) => s.sort);
  const setSort = useProjectStore((s) => s.setSort);
  const fetchProjects = useProjectStore((s) => s.fetchProjects);
  const setCreateOpen = useProjectStore((s) => s.setCreateModalOpen);
  const toggleFavoriteById = useProjectStore((s) => s.toggleFavoriteById);
  const { title, subtitle } = usePanelHeader('projects', {
    title: '项目',
    subtitle: '把对话、文件和指令打包成专属工作空间',
  });

  useEffect(() => {
    void fetchProjects();
  }, [fetchProjects, sort]);

  // Search debounce
  useEffect(() => {
    const id = setTimeout(() => void fetchProjects(), 300);
    return () => clearTimeout(id);
  }, [search, fetchProjects]);

  const grouped = useMemo(() => {
    const personal = list.filter((p) => p.kind === 'personal');
    const team = list.filter((p) => p.kind === 'team');
    return { personal, team };
  }, [list]);

  // Optimistic star update: the store flips the list first and rolls back on failure —
  // no longer calls openProject + a full refetch
  const handleStar = (projectId: string, on: boolean) => {
    void toggleFavoriteById(projectId, on);
  };

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
              size="middle"
              value={sort}
              onChange={setSort}
              style={{ width: 120 }}
              options={[
                { value: 'activity', label: t('活跃度') },
                { value: 'name', label: t('名称') },
                { value: 'created', label: t('创建时间') },
              ]}
            />
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setCreateOpen(true)}
            >
              {t('新建项目')}
            </Button>
          </div>
        </div>

        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder={t('搜索项目...')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="jx-projects-searchInput"
        />

        {loading && list.length === 0 ? (
          <div className="jx-projects-loading"><Spin /></div>
        ) : list.length === 0 ? (
          <Empty description={t('还没有项目，创建一个开始吧')} style={{ marginTop: 60 }} />
        ) : (
          <>
            {grouped.personal.length > 0 && (
              <section className="jx-projects-section">
                <div className="jx-projects-sectionTitle">{t('个人项目')}</div>
                {/* CSS stagger: plays only once when elements are first inserted into the DOM; a refetch (key unchanged) does not replay it */}
                <div className="jx-projects-grid jx-anim-stagger">
                  {grouped.personal.map((p, i) => (
                    <ProjectCard
                      key={p.project_id}
                      project={p}
                      staggerIndex={i}
                      onOpen={() => onOpenProject(p.project_id)}
                      onToggleFavorite={() => handleStar(p.project_id, !p.favorite)}
                    />
                  ))}
                </div>
              </section>
            )}
            {grouped.team.length > 0 && (
              <section className="jx-projects-section">
                <div className="jx-projects-sectionTitle">{t('团队项目')}</div>
                <div className="jx-projects-grid jx-anim-stagger">
                  {grouped.team.map((p, i) => (
                    <ProjectCard
                      key={p.project_id}
                      project={p}
                      staggerIndex={i}
                      onOpen={() => onOpenProject(p.project_id)}
                      onToggleFavorite={() => handleStar(p.project_id, !p.favorite)}
                    />
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </div>

      <CreateProjectModal onCreated={(id) => onOpenProject(id)} />
    </div>
  );
}
