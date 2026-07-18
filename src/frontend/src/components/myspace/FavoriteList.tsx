import { ExportOutlined, StarOutlined } from '@ant-design/icons';
import { AnimatePresence, motion } from 'motion/react';
import { Tooltip } from 'antd';
import type { ResourceItem } from '../../types';
import { formatDateTime } from '../../utils/date';
import { LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import { LIST_ITEM_EXIT } from '../../utils/motionVariants';
import { t } from '../../i18n';

interface FavoriteListProps {
  items: ResourceItem[];
  onNavigate: (item: ResourceItem) => void;
  onRequestUnfavorite: (item: ResourceItem) => Promise<boolean | void>;
  onFinalizeUnfavorite: (item: ResourceItem) => void;
}

export function FavoriteList({
  items,
  onNavigate,
  onRequestUnfavorite,
  onFinalizeUnfavorite,
}: FavoriteListProps) {
  // Exit animation is handled by AnimatePresence: remove from the store immediately after confirmation,
  // motion handles the exit + popLayout repositioning (the old 500ms timer hack has been removed).
  async function handleUnfavorite(item: ResourceItem) {
    const confirmed = await onRequestUnfavorite(item);
    if (!confirmed) return;
    onFinalizeUnfavorite(item);
  }

  if (items.length === 0) return null;

  return (
    <div className="jx-mySpace-favList">
      <AnimatePresence mode="popLayout" initial={false}>
        {items.map((item) => {
          const isAutomationFavorite = item.source_chat_id?.startsWith('automation:');
          return (
            <motion.div
              key={item.id}
              layout={items.length <= LAYOUT_ANIM_MAX_ITEMS ? 'position' : false}
              whileHover={{ y: -1 }}
              exit={LIST_ITEM_EXIT}
              className="jx-mySpace-favCard"
            >
              <div className="jx-mySpace-favHeader">
                <span className="jx-mySpace-favSource">
                  {t('来自「{title}」', { title: item.source_chat_title || t('对话') })}
                </span>
                <span className="jx-mySpace-favTime">{formatDateTime(item.created_at, '')}</span>
              </div>
              {item.content_preview && (
                <div className="jx-mySpace-favPreview">
                  {item.content_preview}
                </div>
              )}
              <div className="jx-mySpace-favActions">
                <Tooltip title={t('取消收藏')}>
                  <button className="jx-mySpace-actionBtn jx-mySpace-actionBtn--danger" onClick={() => void handleUnfavorite(item)}>
                    <StarOutlined /> {t('取消收藏')}
                  </button>
                </Tooltip>
                <Tooltip title={isAutomationFavorite ? t('查看自动化记录') : t('跳转到对话')}>
                  <button className="jx-mySpace-actionBtn" onClick={() => onNavigate(item)}>
                    <ExportOutlined /> {isAutomationFavorite ? t('查看记录') : t('查看对话')}
                  </button>
                </Tooltip>
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
