import { CaretDownOutlined, CheckOutlined, EditOutlined, EllipsisOutlined, FolderAddOutlined, LaptopOutlined, PlusOutlined } from '@ant-design/icons';
import { Dropdown, Tooltip, type MenuProps } from 'antd';
import { AnimatePresence } from 'motion/react';
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { t } from '../../i18n';
import { loadJsonPref, saveJsonPref } from '../../storage';
import { useChatStore, useSidebarOrderStore } from '../../stores';
import { projectCreationTargets, useDeploymentModeStore } from '../../stores/deploymentModeStore';
import { PersonalAgentSidebarEntry } from '../../personalAgentEdition';
import CreateProjectModal from '../projects/CreateProjectModal';
import { ChatSidebarItem } from './ChatSidebarItem';
import { ProjectSidebarGroup } from './ProjectSidebarGroup';
import type { SidebarProps } from './sidebarTypes';
import { useSidebarHistory } from './useSidebarHistory';
const SHOW_PROJECTS_PREF_KEY = 'hugagent_ui_sidebar_show_projects_v1';
const PROJECT_SORT_PREF_KEY = 'hugagent_ui_sidebar_project_sort_v1';
type ProjectSort = 'activity' | 'name';
function pickLocalProjectFolder() { window.location.href = '/__desktop/pick-local-folder'; }
const check = (on: boolean) => on ? <CheckOutlined /> : null;
export function ChatSidebar({ actions, children, navigation }: { actions: SidebarProps; children?: ReactNode; navigation?: ReactNode }) {
  const { projectGroups, groupedHistoryList } = useSidebarHistory();
  const loading = useChatStore(s => s.chatsLoading);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const dragScopeRef = useRef<string | null>(null);
  const [showProjects, setShowProjects] = useState(() => loadJsonPref(SHOW_PROJECTS_PREF_KEY, { show: true }).show !== false);
  const [projectSort, setProjectSort] = useState<ProjectSort>(() => loadJsonPref(PROJECT_SORT_PREF_KEY, { sort: 'activity' }).sort === 'name' ? 'name' : 'activity');
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const manualOrder = useSidebarOrderStore(s => s.order.length > 0);
  const isDesktop = useDeploymentModeStore(s => s.isDesktop);
  const provisionMode = useDeploymentModeStore(s => s.provisionMode);
  const { cloud: canCreateCloudProject, local: canCreateLocalProject } = projectCreationTargets(isDesktop, provisionMode);
  const sortedProjectGroups = useMemo(() => projectSort === 'name'
    ? [...projectGroups].sort((a, b) => (Number(b.pinned) - Number(a.pinned)) || a.name.localeCompare(b.name))
    : projectGroups, [projectGroups, projectSort]);
  const groups = [...(showProjects ? [{ key: 'projects', label: t('项目') }] : []), ...groupedHistoryList];
  const setAllCollapsed = (value: boolean) => setCollapsed(Object.fromEntries(groups.map(g => [g.key, value])));
  const toggleShowProjects = () => {
    const show = !showProjects;
    setShowProjects(show);
    saveJsonPref(SHOW_PROJECTS_PREF_KEY, { show });
  };
  const changeProjectSort = (sort: ProjectSort) => {
    setProjectSort(sort);
    saveJsonPref(PROJECT_SORT_PREF_KEY, { sort });
  };
  const organizeItem = { key: 'organize', label: t('整理侧栏'), children: [
    { key: 'expand-all', label: t('全部展开'), onClick: () => setAllCollapsed(false) },
    { key: 'collapse-all', label: t('全部收起'), onClick: () => setAllCollapsed(true) },
  ] };
  const showItems: MenuProps['items'] = [
    { type: 'divider' },
    { key: 'show', type: 'group', label: t('显示'), children: [
      { key: 'show-projects', label: t('项目'), extra: check(showProjects), onClick: toggleShowProjects },
    ] },
  ];
  const historyMenu: MenuProps['items'] = [
    organizeItem,
    { key: 'sort', label: t('排序方式'), children: [
      { key: 'sort-recent', label: t('最近更新'), extra: check(!manualOrder), onClick: () => { if (manualOrder) useSidebarOrderStore.getState().resetOrder(); } },
      { key: 'sort-manual', label: t('手动拖拽'), extra: check(manualOrder), disabled: !manualOrder, title: manualOrder ? undefined : t('拖拽对话即可调整顺序') },
    ] },
    ...showItems,
  ];
  const projectMenu: MenuProps['items'] = [
    organizeItem,
    { key: 'sort', label: t('排序方式'), children: [
      { key: 'sort-activity', label: t('最近活跃'), extra: check(projectSort === 'activity'), onClick: () => changeProjectSort('activity') },
      { key: 'sort-name', label: t('名称'), extra: check(projectSort === 'name'), onClick: () => changeProjectSort('name') },
    ] },
    { key: 'all-projects', label: t('查看全部项目'), onClick: () => actions.onSetPanel('projects') },
    ...showItems,
  ];
  // 新建入口跟随运行形态（同项目面板）：双模式给云端/本地二选一，单一形态直接进对应流程。
  const newProjectButton = (onClick?: () => void) => (
    <button type="button" className="jx-projectRowActionBtn" aria-label={t('新建项目')} onClick={onClick}><PlusOutlined /></button>
  );
  const renderHeaderActions = (key: string) => key === 'projects'
    ? <div className="jx-historyGroupActions">
        <Dropdown menu={{ items: projectMenu }} trigger={['click']} placement="bottomRight" overlayClassName="jx-chatItemMenu jx-historyGroupMenu">
          <button type="button" className="jx-projectRowActionBtn" aria-label={t('项目列表更多操作')}><EllipsisOutlined /></button>
        </Dropdown>
        {isDesktop && canCreateCloudProject && canCreateLocalProject
          ? <Dropdown trigger={['click']} placement="bottomRight" overlayClassName="jx-chatItemMenu" menu={{ items: [
              { key: 'new-cloud-project', icon: <FolderAddOutlined />, label: t('新建云端项目'), onClick: () => setCreateProjectOpen(true) },
              { key: 'new-local-project', icon: <LaptopOutlined />, label: t('新建本地项目'), onClick: pickLocalProjectFolder },
            ] }}>{newProjectButton()}</Dropdown>
          : (canCreateCloudProject || canCreateLocalProject) && <Tooltip title={t('新建项目')}>
              {newProjectButton(canCreateCloudProject ? () => setCreateProjectOpen(true) : pickLocalProjectFolder)}
            </Tooltip>}
      </div>
    : <div className="jx-historyGroupActions">
        <Dropdown menu={{ items: historyMenu }} trigger={['click']} placement="bottomRight" overlayClassName="jx-chatItemMenu jx-historyGroupMenu">
          <button type="button" className="jx-projectRowActionBtn" aria-label={t('历史对话更多操作')}><EllipsisOutlined /></button>
        </Dropdown>
        <Tooltip title={t('新建对话')}>
          <button type="button" className="jx-projectRowActionBtn" aria-label={t('新建对话')} onClick={actions.onNewChat}><EditOutlined /></button>
        </Tooltip>
      </div>;
  return <>
    <button className="jx-newChatBtn" onClick={actions.onNewChat}><img src="/home/new-chat.svg" alt="" className="jx-newChatIcon" /><span>{t('新建对话')}</span></button>
    {navigation}
    <PersonalAgentSidebarEntry onOpen={() => actions.onSetPanel('personal_agent')} />
    <div className="jx-sidebarScrollArea">
      {children}
      {loading && <div className="jx-historySkeletonList" aria-busy="true" aria-label={t('加载中')}>
        {Array.from({ length: 8 }, (_, i) => <div key={i} className="jx-historyItem jx-historyItemSkeleton"><div className="jx-skeletonBlock jx-historySkLine" /></div>)}
      </div>}
      {!loading && groups.map(group => <section key={group.key} className={`jx-historyGroup${collapsed[group.key] ? ' jx-historyGroup--collapsed' : ''}`} aria-label={group.label}>
        <div className="jx-historyGroupHeaderRow">
          <button className="jx-historyGroupHeader" aria-expanded={!collapsed[group.key]} onClick={() => setCollapsed(v => ({ ...v, [group.key]: !v[group.key] }))}><span className="jx-historyGroupTitle">{group.label}</span><CaretDownOutlined className="jx-historyGroupCaret" /></button>
          {renderHeaderActions(group.key)}
        </div>
        <div className={`jx-expandWrap jx-historyGroupExpand${collapsed[group.key] ? '' : ' jx-expandWrap--open'}`}><div className="jx-historyGroupList">
          {group.key === 'projects' ? sortedProjectGroups.map(pg => <ProjectSidebarGroup key={pg.projectId} pg={pg} actions={actions} dragScopeRef={dragScopeRef} />) : <AnimatePresence initial={false} mode="popLayout">{groupedHistoryList[0].items.map(item => <ChatSidebarItem key={item.id} item={item} groupItems={groupedHistoryList[0].items} scopeKey="history" dragScopeRef={dragScopeRef} actions={actions} />)}</AnimatePresence>}
        </div></div>
      </section>)}
    </div>
    {createProjectOpen && <CreateProjectModal open onOpenChange={setCreateProjectOpen} onCreated={pid => {
      const created = projectGroups.find(pg => pg.projectId === pid);
      actions.onNewProjectChat(pid, created?.name || t('项目'));
    }} />}
  </>;
}
