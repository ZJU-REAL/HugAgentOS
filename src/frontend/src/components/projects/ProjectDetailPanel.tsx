import { useEffect, useRef, useState } from 'react';
import type { ChangeEvent, CSSProperties, RefObject } from 'react';
import { Button, Dropdown, Empty, Modal, Radio, Spin, Tag, message } from 'antd';
import {
  ArrowLeftOutlined,
  EyeOutlined,
  LockOutlined,
  MessageOutlined,
  MoreOutlined,
  StarFilled,
  StarOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import { useProjectStore } from '../../stores/projectStore';
import { useCatalogStore } from '../../stores/catalogStore';
import { useChatStore } from '../../stores/chatStore';
import { InputArea } from '../chat/InputArea';
import ProjectRightRail from './ProjectRightRail';
import { t } from '../../i18n';

interface Props {
  projectId: string;
  onBack: () => void;
  /** Reuse the main input box's attachment pipeline (useStreaming): picking a file uploads it and
   *  registers it into fileUploadMap, and it is picked up uniformly by the chat panel's send when
   *  the first message is sent (fileStore is globally shared). */
  handleFileSelect: (e: ChangeEvent<HTMLInputElement>, ref: RefObject<HTMLInputElement | null>) => void;
  removeFile: (index: number) => void;
}

function relativeTime(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  const min = 60 * 1000;
  const hr = 60 * min;
  const day = 24 * hr;
  if (diff < min) return t('刚刚');
  if (diff < hr) return t('{n} 分钟前', { n: Math.floor(diff / min) });
  if (diff < day) return t('{n} 小时前', { n: Math.floor(diff / hr) });
  return d.toLocaleDateString();
}

type ChatScope = 'all' | 'mine' | 'shared';

export default function ProjectDetailPanel({ projectId, onBack, handleFileSelect, removeFile }: Props) {
  const project = useProjectStore((s) => s.currentProject);
  const detailLoading = useProjectStore((s) => s.detailLoading);
  const chats = useProjectStore((s) => s.projectChats);
  const openProject = useProjectStore((s) => s.openProject);
  const closeProject = useProjectStore((s) => s.closeCurrentProject);
  const refreshChats = useProjectStore((s) => s.refreshChats);
  const toggleFav = useProjectStore((s) => s.toggleFavorite);
  const deleteProject = useProjectStore((s) => s.deleteProject);

  const [scope, setScope] = useState<ChatScope>('all');
  // Mode selected from the "+" menu: do not create a session / navigate immediately; defer it until
  // send, applying it to the newly created project session.
  const [pendingMode, setPendingMode] = useState<'plan' | 'batch' | null>(null);
  const isTeamProject = project?.kind === 'team';

  useEffect(() => {
    if (isTeamProject) {
      void refreshChats(scope);
    }
  }, [scope, isTeamProject, refreshChats]);

  const setCatalogPanel = useCatalogStore((s) => s.setPanel);
  const setCurrentChatId = useChatStore((s) => s.setCurrentChatId);
  const updateStore = useChatStore((s) => s.updateStore);
  const setPendingFirstMessage = useChatStore((s) => s.setPendingFirstMessage);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    void openProject(projectId);
    return () => closeProject();
  }, [projectId, openProject, closeProject]);

  const canAdmin = project?.permission === 'admin';

  const openChat = (chatId: string) => {
    setCurrentChatId(chatId);
    setCatalogPanel('chat');
  };

  /** InputArea's send callback: reuse the full UX of the home input box (@mention, /skill, mode
   *  switching, etc.); we only mint chat + write pending + jump to panel at the instant we confirm
   *  the send. The actual streaming is taken over by the chat panel's useStreaming (see App.tsx's
   *  pendingFirstMessage effect). */
  const submitFirstMessage = () => {
    if (!project) return;
    const content = useChatStore.getState().input.trim();
    if (!content) return;

    const newId = `chat_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
    const now = Date.now();
    updateStore((prev) => ({
      chats: {
        ...prev.chats,
        [newId]: {
          id: newId,
          title: content.slice(0, 18) || '新对话',
          createdAt: now,
          updatedAt: now,
          messages: [],
          favorite: false,
          pinned: false,
          businessTopic: '综合咨询',
          projectId: project.project_id,
          projectName: project.name,
          // When plan/batch mode is selected, create the session with the corresponding marker (mutually exclusive)
          ...(pendingMode === 'plan'
            ? { planChat: true }
            : pendingMode === 'batch'
              ? { batchChat: true }
              : {}),
        },
      },
      order: [newId, ...(prev.order || []).filter((x) => x !== newId)],
    }));
    // setCurrentChatId derives planMode from chat.planChat, so smartSend routes correctly
    setCurrentChatId(newId);
    setPendingFirstMessage({ chatId: newId, content });
    // Clear chatStore.input so the chat panel's InputArea does not still show this text
    useChatStore.getState().setInput('');
    setPendingMode(null);
    setCatalogPanel('chat');
  };

  /** Selecting plan / batch mode from the "+" menu: only toggle the pending mode, do **not** create
   *  a session or navigate — avoiding "the whole session jumping back to the home page". The real
   *  session is created with the marker by submitFirstMessage at send time. */
  const toggleProjectMode = (mode: 'plan' | 'batch') => {
    setPendingMode((prev) => (prev === mode ? null : mode));
  };

  const handleDelete = () => {
    if (!project) return;
    Modal.confirm({
      title: t('删除项目「{name}」？', { name: project.name }),
      content: t('项目对应的直传文件会一同软删除；引用文件不动。该操作可由数据库恢复。'),
      okType: 'danger',
      okText: t('删除'),
      cancelText: t('取消'),
      onOk: async () => {
        try {
          await deleteProject(project.project_id);
          message.success(t('项目已删除'));
          onBack();
        } catch (err) {
          message.error((err as Error)?.message || t('删除失败'));
        }
      },
    });
  };

  if (detailLoading && !project) {
    return (
      <div className="jx-projectDetail jx-projectDetail--loading">
        <Spin />
      </div>
    );
  }
  if (!project) {
    return (
      <div className="jx-projectDetail">
        <Empty description={t('项目不存在或你无权访问')} />
        <Button onClick={onBack}>{t('返回项目列表')}</Button>
      </div>
    );
  }

  return (
    <div className="jx-projectDetail">
      {/* Detail shell entrance: CSS primitives (backwards fill, no pinned transform on the final frame) */}
      <div
        key={projectId}
        className="jx-projectDetail-shell jx-anim-fadeInUp"
        style={{ '--fadeInUp-distance': '12px', animationDuration: '0.25s' } as CSSProperties}
      >
        <div className="jx-projectDetail-main">
          <div className="jx-projectDetail-backRow">
            <Button type="link" icon={<ArrowLeftOutlined />} onClick={onBack}>
              {t('所有项目')}
            </Button>
          </div>
          <div className="jx-projectDetail-titleRow">
            <h2 className="jx-projectDetail-title">{project.name}</h2>
            {project.kind === 'team' && project.team_name && (
              <Tag icon={<TeamOutlined />} color="blue">{project.team_name}</Tag>
            )}
            <div className="jx-projectDetail-titleActions">
              <Button
                type="text"
                icon={project.favorite ? <StarFilled style={{ color: '#F8AB42' }} /> : <StarOutlined />}
                onClick={() => toggleFav(!project.favorite)}
              />
              {canAdmin && (
                <Dropdown
                  menu={{
                    items: [
                      { key: 'delete', label: t('删除项目'), danger: true, onClick: handleDelete },
                    ],
                  }}
                  trigger={['click']}
                >
                  <Button type="text" icon={<MoreOutlined />} />
                </Dropdown>
              )}
            </div>
          </div>
          {project.description && (
            <div className="jx-projectDetail-desc">{project.description}</div>
          )}

          <div className="jx-projectDetail-inputWrap">
            <InputArea
              inputRef={inputRef}
              fileInputRef={fileInputRef}
              send={submitFirstMessage}
              handleFileSelect={handleFileSelect}
              removeFile={removeFile}
              placeholder={t('在「{name}」内开始新对话，Enter 发送，Shift+Enter 换行', { name: project.name })}
              projectComposer
              forceSendMode
              onEnterMode={toggleProjectMode}
              activeMode={pendingMode}
            />
          </div>

          <div className="jx-projectDetail-chatList">
            <div className="jx-projectDetail-chatListTitle">
              {t('本项目对话历史')}
              {isTeamProject && (
                <Radio.Group
                  size="small"
                  value={scope}
                  onChange={(e) => setScope(e.target.value as ChatScope)}
                  style={{ marginLeft: 12 }}
                  buttonStyle="solid"
                  optionType="button"
                  options={[
                    { label: t('全部'), value: 'all' },
                    { label: t('我创建的'), value: 'mine' },
                    { label: t('共享给我的'), value: 'shared' },
                  ]}
                />
              )}
            </div>
            {chats.length === 0 ? (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t('还没有对话')}
              />
            ) : (
              chats.map((c) => {
                const isShared =
                  isTeamProject && (c.share_scope === 'team_read' || c.share_scope === 'team_edit');
                const ownerLabel = c.is_owner === false ? (c.owner_name || t('其他成员')) : null;
                return (
                  <div
                    key={c.chat_id}
                    className="jx-projectDetail-chatItem"
                    onClick={() => openChat(c.chat_id)}
                  >
                    <MessageOutlined />
                    <div className="jx-projectDetail-chatItemBody">
                      <div className="jx-projectDetail-chatItemTitle">
                        {c.title || t('未命名对话')}
                        {isShared && c.share_scope === 'team_read' && (
                          <Tag icon={<EyeOutlined />} color="default" style={{ marginLeft: 8 }}>
                            {t('只读')}
                          </Tag>
                        )}
                        {isShared && c.share_scope === 'team_edit' && (
                          <Tag color="blue" style={{ marginLeft: 8 }}>{t('可编辑')}</Tag>
                        )}
                        {c.is_owner && isShared && (
                          <Tag color="gold" style={{ marginLeft: 8 }}>{t('已共享')}</Tag>
                        )}
                        {!isShared && isTeamProject && c.is_owner && (
                          <Tag icon={<LockOutlined />} color="default" style={{ marginLeft: 8 }}>
                            {t('私密')}
                          </Tag>
                        )}
                      </div>
                      <div className="jx-projectDetail-chatItemMeta">
                        {ownerLabel && (
                          <span style={{ marginRight: 8 }}>
                            <UserOutlined style={{ marginRight: 2 }} />{t('创建人：{name}', { name: ownerLabel })}
                          </span>
                        )}
                        Last message {relativeTime(c.last_message_at || c.updated_at)}
                      </div>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        <aside className="jx-projectDetail-aside">
          <ProjectRightRail />
        </aside>
      </div>
    </div>
  );
}
