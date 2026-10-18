import { useEffect } from 'react';
import { Button, Empty, Modal, message } from 'antd';
import { getAutomationRuns } from '../../api';
import { useCatalogStore, useChatStore } from '../../stores';
import { useAutomationChatStore } from '../../stores/automationChatStore';
import { useMySpaceStore } from '../../stores/mySpaceStore';
import type { ResourceItem } from '../../types';
import { t } from '../../i18n';
import { FavoriteList } from './FavoriteList';

export function SpaceFavorites() {
  const { favorites, loading, favHasMore, fetchFavorites, unfavoriteChat, removeFavorite } = useMySpaceStore();
  useEffect(() => { void fetchFavorites(true); }, [fetchFavorites]);
  function navigate(item: ResourceItem) {
    const chat = item.source_chat_id;
    if (!chat) return;
    const automation = useAutomationChatStore.getState();
    if (chat.startsWith('automation:')) {
      const id = chat.slice('automation:'.length);
      void getAutomationRuns(id, 50).then(runs => automation.enterAutomationChat(
        id, item.source_chat_title || item.name || t('定时任务'), runs,
      )).catch(error => message.error(String(error)));
    } else {
      if (automation.activeGroup) automation.exitAutomationChat();
      useChatStore.getState().setCurrentChatId(chat);
      useCatalogStore.getState().setPanel('chat');
    }
  }
  function confirm(item: ResourceItem): Promise<boolean> {
    return new Promise(resolve => Modal.confirm({
      title: t('取消收藏'), content: t('确定将这条会话从收藏列表中移除吗？'),
      okText: t('取消收藏'), cancelText: t('保留'),
      onOk: async () => {
        try {
          if (item.source_chat_id) await unfavoriteChat(item.source_chat_id);
          resolve(true);
        } catch (error) { void message.error(String(error)); resolve(false); }
      },
      onCancel: () => resolve(false),
    }));
  }
  return <div style={{padding: 24}} aria-busy={loading}>
    {!loading && !favorites.length && <Empty description={t('暂无内容')} />}
    <FavoriteList items={favorites} onNavigate={navigate} onRequestUnfavorite={confirm}
      onFinalizeUnfavorite={item => { if (item.source_chat_id) removeFavorite(item.source_chat_id); }} />
    {favHasMore && <Button loading={loading} onClick={() => void fetchFavorites()}>{t('加载更多')}</Button>}
  </div>;
}
