import { DeleteOutlined, EditOutlined, EllipsisOutlined, FolderOpenOutlined, FolderOutlined, LaptopOutlined, PushpinFilled, PushpinOutlined } from '@ant-design/icons';
import { Dropdown, Modal, Tooltip, message } from 'antd';
import { AnimatePresence } from 'motion/react';
import { useRef, useState } from 'react';
import { t } from '../../i18n';
import { usePanel } from '../../routing/usePanel';
import { projectOverviewId, useChatStore } from '../../stores/chatStore';
import { useProjectStore } from '../../stores/projectStore';
import { SharedFolderOutlined } from './SharedFolderOutlined';
import { ChatSidebarItem } from './ChatSidebarItem';
import type { SidebarProps } from './sidebarTypes';
import type { SidebarProjectGroup } from './useSidebarHistory';
export function ProjectSidebarGroup({ pg, actions, dragScopeRef }: { pg: SidebarProjectGroup; actions: SidebarProps; dragScopeRef: ReturnType<typeof useRef<string | null>> }) {
  const [projCollapsed, setCollapsed] = useState(false);
  const currentProjectId = useProjectStore(s => s.currentProjectId);
  const panel = usePanel();
  const selectedProjectId = useChatStore(s => projectOverviewId(s.store.chats[s.currentChatId]));
  const ProjectIcon = pg.isLocal ? LaptopOutlined : pg.isTeam ? SharedFolderOutlined : FolderOutlined;
  const projActive = (panel === 'chat' && selectedProjectId === pg.projectId);
  const openProjectPanel = (projectId: string) => {
    void useProjectStore.getState().openProject(projectId);
  };

  const toggleProjectPinned = async (group: SidebarProjectGroup) => {
    try {
      await useProjectStore.getState().togglePinnedById(group.projectId, !group.pinned);
    } catch (err) {
      message.error((err as Error)?.message || t('操作失败'));
    }
  };

  const removeProject = (group: SidebarProjectGroup) => {
    Modal.confirm({
      title: t('移除项目「{name}」？', { name: group.name }),
      content: t('项目对应的直传文件会一同软删除；引用文件不动。该操作可由数据库恢复。'),
      okType: 'danger',
      okText: t('移除'),
      cancelText: t('取消'),
      onOk: async () => {
        try {
          const wasOpen = currentProjectId === group.projectId;
          // 解绑会话上的 projectId/projectName 由 projectStore.deleteProject 统一做
          // （同时登记跨窗口的"已删除项目"黑名单），这里不再重复一遍。
          await useProjectStore.getState().deleteProject(group.projectId);
          if (wasOpen) actions.onNewChat();
          message.success(t('项目已移除'));
        } catch (err) {
          message.error((err as Error)?.message || t('移除失败'));
        }
      },
    });
  };

  return (
    <div key={pg.projectId} className="jx-projectGroup">
      <div
        className={`jx-projectRow${projActive ? ' active' : ''}`}
      >
        <button
          type="button"
          className="jx-projectRowToggle"
          title={pg.name}
          onClick={() => setCollapsed(v => !v)}
          aria-label={projCollapsed ? t('展开项目会话') : t('收起项目会话')}
          aria-expanded={!projCollapsed}
        >
          <ProjectIcon className="jx-projectRowIcon" />
          <span className="jx-projectRowName">{pg.name}</span>
          {pg.pinned && (
            <Tooltip title={t('已置顶')}>
              <span className="jx-historyPinIcon">
                <PushpinFilled />
              </span>
            </Tooltip>
          )}
        </button>
        {pg.known && (
          <div className="jx-projectRowActions">
            <Dropdown
              menu={{
                items: [
                  {
                    key: 'pin-project',
                    label: pg.pinned ? t('取消置顶') : t('项目置顶'),
                    icon: pg.pinned ? <PushpinFilled /> : <PushpinOutlined />,
                    disabled: !pg.canAdmin,
                    onClick: ({ domEvent }) => {
                      domEvent.stopPropagation();
                      void toggleProjectPinned(pg);
                    },
                  },
                  {
                    key: 'open-project',
                    label: t('在项目面板中打开'),
                    icon: <FolderOpenOutlined />,
                    onClick: ({ domEvent }) => {
                      domEvent.stopPropagation();
                      openProjectPanel(pg.projectId);
                    },
                  },
                  {
                    key: 'remove-project',
                    label: t('移除项目'),
                    icon: <DeleteOutlined />,
                    danger: true,
                    disabled: !pg.canDelete,
                    onClick: ({ domEvent }) => {
                      domEvent.stopPropagation();
                      removeProject(pg);
                    },
                  },
                ],
              }}
              trigger={['click']}
              placement="bottomRight"
              overlayClassName="jx-chatItemMenu"
            >
              <button
                type="button"
                className="jx-projectRowActionBtn"
                aria-label={t('项目更多操作')}
                onClick={(e) => e.stopPropagation()}
              >
                <EllipsisOutlined />
              </button>
            </Dropdown>
            <Tooltip title={t('新建项目对话')}>
              <button
                type="button"
                className="jx-projectRowActionBtn"
                aria-label={t('新建项目对话')}
                onClick={(e) => {
                  e.stopPropagation();
                  setCollapsed(false);
                  actions.onNewProjectChat(pg.projectId, pg.name);
                }}
              >
                <EditOutlined />
              </button>
            </Tooltip>
          </div>
        )}
      </div>
      {pg.items.length > 0 && (
        <div className={`jx-expandWrap jx-historyGroupExpand${projCollapsed ? '' : ' jx-expandWrap--open'}`}>
          <div className={`jx-historyGroupList jx-projectChatList${projCollapsed ? '' : ' jx-projectChatList--open'}`}>
            <AnimatePresence initial={false} mode="popLayout">
              {pg.items.map(item => <ChatSidebarItem key={item.id} item={item} groupItems={pg.items} scopeKey={`project:${pg.projectId}`} dragScopeRef={dragScopeRef} actions={actions} />)}
            </AnimatePresence>
          </div>
        </div>
      )}
    </div>
  );
}
