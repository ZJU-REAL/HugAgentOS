import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Empty, Spin } from 'antd';
import { MessageOutlined, ReloadOutlined } from '@ant-design/icons';
import { listProjectChats } from '../../api';
import { mergeProjectHistory } from '../../stores/projectHistory';
import { isAutomationHistoryChat } from '../../utils/history';
import { useChatStore, isLocalDraftChat } from '../../stores/chatStore';
import { useAutomationChatStore } from '../../stores/automationChatStore';
import { useAuthStore } from '../../stores/authStore';
import { formatShortDateTime } from '../../utils/date';
import type { ProjectDetail } from '../../types';
import { t } from '../../i18n';

/** Discovery list only; every record opens the shared main conversation. */
export default function ProjectConversationHistory({ project }: { project: ProjectDetail }) {
  const projectId = project.project_id;
  const userId = useAuthStore(s => s.authUser?.user_id);
  const store = useChatStore(s => s.store);
  const backendIds = useChatStore(s => s.backendSessionIds);
  const [recordIds, setRecordIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [limit, setLimit] = useState(20);
  const generation = useRef(0);
  const reload = useCallback(async () => {
    const version = ++generation.current;
    setLoading(true);
    setError('');
    setRecordIds([]);
    try {
      const { items } = await listProjectChats(projectId, 1, 50);
      if (version !== generation.current) return;
      await mergeProjectHistory(items, projectId, project.name, () => version !== generation.current);
      if (version === generation.current) setRecordIds(items.map(item => item.chat_id));
    }
    catch (failure) {
      if (version === generation.current) setError(failure instanceof Error ? failure.message : t('加载失败'));
    } finally { if (version === generation.current) setLoading(false); }
  }, [projectId, project.name]);
  useEffect(() => {
    void reload();
    return () => { generation.current += 1; };
  }, [reload, userId]);
  const items = recordIds.map(id => store.chats[id])
    .filter(item => item && item.projectId === projectId && backendIds.has(item.id)
      && !isLocalDraftChat(item.id) && !isAutomationHistoryChat(item));
  const openChat = (id: string) => {
    useAutomationChatStore.getState().exitAutomationChat();
    useChatStore.getState().setCurrentChatId(id);
    useChatStore.getState().setToolResultPanel(null);
  };
  return <section className="jx-projectConversationHistory" aria-label={t('本项目对话历史')}>
    <div className="jx-projectConversationHistory-header">
      <strong>{t('本项目对话历史')}</strong>
      <Button type="text" size="small" icon={<ReloadOutlined />} loading={loading}
        aria-label={t('刷新')} onClick={() => void reload()} />
    </div>
    {error && <div role="alert">{error}<Button size="small" onClick={() => void reload()}>{t('重试')}</Button></div>}
    {loading && items.length === 0 ? <Spin size="small" /> : items.length === 0 && !error
      ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('还没有对话')} />
      : <div className="jx-projectConversationHistory-list">
        {items.slice(0, limit).map(item => <button type="button" key={item.id}
          className="jx-projectConversationHistory-item" onClick={() => openChat(item.id)}>
          <MessageOutlined />
          <span className="jx-projectConversationHistory-body">
            <span className="jx-projectConversationHistory-title">{item.title || t('未命名对话')}
            </span>
            <span className="jx-projectConversationHistory-meta">
              {t('最近更新：{time}', {time:formatShortDateTime(item.updatedAt, '—')})}
            </span>
          </span>
        </button>)}
        {items.length > limit && <Button type="text" onClick={() => setLimit(value => value + 20)}>{t('加载更多')}</Button>}
      </div>}
  </section>;
}
