import { useEffect, useRef, useState } from 'react';
import type { ChangeEvent, CSSProperties, RefObject } from 'react';
import {
  ArrowLeftOutlined,
  MessageOutlined,
  MoreOutlined,
  StarFilled,
  StarOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Empty, Modal, Spin, message } from 'antd';

import { t } from '../../i18n';
import { useCatalogStore } from '../../stores/catalogStore';
import { useChatStore } from '../../stores/chatStore';
import { useProjectStore } from '../../stores/projectStore';
import { InputArea } from '../chat/InputArea';
import ProjectRightRail from './ProjectRightRail';

interface Props {
  projectId: string;
  onBack: () => void;
  handleFileSelect: (event: ChangeEvent<HTMLInputElement>, ref: RefObject<HTMLInputElement | null>) => void;
  removeFile: (index: number) => void;
}

function relativeTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  const diff = Date.now() - date.getTime();
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return t('刚刚');
  if (diff < hour) return t('{n} 分钟前', { n: Math.floor(diff / minute) });
  if (diff < day) return t('{n} 小时前', { n: Math.floor(diff / hour) });
  return date.toLocaleDateString();
}

export default function ProjectDetailPanel({ projectId, onBack, handleFileSelect, removeFile }: Props) {
  const project = useProjectStore((state) => state.currentProject);
  const detailLoading = useProjectStore((state) => state.detailLoading);
  const chats = useProjectStore((state) => state.projectChats);
  const openProject = useProjectStore((state) => state.openProject);
  const closeProject = useProjectStore((state) => state.closeCurrentProject);
  const toggleFavorite = useProjectStore((state) => state.toggleFavorite);
  const deleteProject = useProjectStore((state) => state.deleteProject);
  const setCatalogPanel = useCatalogStore((state) => state.setPanel);
  const setCurrentChatId = useChatStore((state) => state.setCurrentChatId);
  const updateStore = useChatStore((state) => state.updateStore);
  const setPendingFirstMessage = useChatStore((state) => state.setPendingFirstMessage);
  const [pendingMode, setPendingMode] = useState<'plan' | 'batch' | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    void openProject(projectId);
    return () => closeProject();
  }, [closeProject, openProject, projectId]);

  const openChat = (chatId: string) => {
    setCurrentChatId(chatId);
    setCatalogPanel('chat');
  };

  const submitFirstMessage = () => {
    if (!project) return;
    const content = useChatStore.getState().input.trim();
    if (!content) return;
    const newId = `chat_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
    const now = Date.now();
    updateStore((previous) => ({
      chats: {
        ...previous.chats,
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
          ...(pendingMode === 'plan' ? { planChat: true } : pendingMode === 'batch' ? { batchChat: true } : {}),
        },
      },
      order: [newId, ...(previous.order || []).filter((id) => id !== newId)],
    }));
    setCurrentChatId(newId);
    setPendingFirstMessage({ chatId: newId, content });
    useChatStore.getState().setInput('');
    setPendingMode(null);
    setCatalogPanel('chat');
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
        } catch (error) {
          message.error((error as Error)?.message || t('删除失败'));
        }
      },
    });
  };

  if (detailLoading && !project) return <div className="jx-projectDetail jx-projectDetail--loading"><Spin /></div>;
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
      <div
        key={projectId}
        className="jx-projectDetail-shell jx-anim-fadeInUp"
        style={{ '--fadeInUp-distance': '12px', animationDuration: '0.25s' } as CSSProperties}
      >
        <div className="jx-projectDetail-main">
          <div className="jx-projectDetail-backRow">
            <Button type="link" icon={<ArrowLeftOutlined />} onClick={onBack}>{t('所有项目')}</Button>
          </div>
          <div className="jx-projectDetail-titleRow">
            <h2 className="jx-projectDetail-title">{project.name}</h2>
            <div className="jx-projectDetail-titleActions">
              <Button
                type="text"
                icon={project.favorite ? <StarFilled style={{ color: '#F8AB42' }} /> : <StarOutlined />}
                onClick={() => void toggleFavorite(!project.favorite)}
              />
              {project.permission === 'admin' && (
                <Dropdown
                  menu={{ items: [{ key: 'delete', label: t('删除项目'), danger: true, onClick: handleDelete }] }}
                  trigger={['click']}
                >
                  <Button type="text" icon={<MoreOutlined />} />
                </Dropdown>
              )}
            </div>
          </div>
          {project.description && <div className="jx-projectDetail-desc">{project.description}</div>}
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
              onEnterMode={(mode) => setPendingMode((previous) => previous === mode ? null : mode)}
              activeMode={pendingMode}
            />
          </div>
          <div className="jx-projectDetail-chatList">
            <div className="jx-projectDetail-chatListTitle">{t('本项目对话历史')}</div>
            {chats.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('还没有对话')} />
            ) : chats.map((chat) => (
              <div key={chat.chat_id} className="jx-projectDetail-chatItem" onClick={() => openChat(chat.chat_id)}>
                <MessageOutlined />
                <div className="jx-projectDetail-chatItemBody">
                  <div className="jx-projectDetail-chatItemTitle">{chat.title || t('未命名对话')}</div>
                  <div className="jx-projectDetail-chatItemMeta">
                    Last message {relativeTime(chat.last_message_at || chat.updated_at)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
        <aside className="jx-projectDetail-aside"><ProjectRightRail /></aside>
      </div>
    </div>
  );
}
