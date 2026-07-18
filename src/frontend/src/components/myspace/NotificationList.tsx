import { AnimatePresence, motion } from 'motion/react';
import { Button, Checkbox, Tag, Empty, Modal } from 'antd';
import { CheckOutlined, DeleteOutlined, CloseOutlined, EyeOutlined } from '@ant-design/icons';
import { useChatStore, useCatalogStore, useMySpaceStore, useAutomationChatStore } from '../../stores';
import { formatDateTime } from '../../utils/date';
import { LAYOUT_ANIM_MAX_ITEMS } from '../../utils/motionTokens';
import { LIST_ITEM_EXIT } from '../../utils/motionVariants';
import { BulkActionBar } from './BulkActionBar';
import { t } from '../../i18n';

/** The unread-dot ripple is applied only to the first N cards, to avoid a page-wide persistent animation */
const BREATH_DOT_MAX = 3;

export function NotificationList() {
  const { setCurrentChatId } = useChatStore();
  const { setPanel } = useCatalogStore();

  const notifications = useMySpaceStore((s) => s.notifications);
  const notifSelectedIds = useMySpaceStore((s) => s.notifSelectedIds);
  const markNotificationRead = useMySpaceStore((s) => s.markNotificationRead);
  const markAllNotificationsRead = useMySpaceStore((s) => s.markAllNotificationsRead);
  const markSelectedNotificationsRead = useMySpaceStore((s) => s.markSelectedNotificationsRead);
  const deleteNotification = useMySpaceStore((s) => s.deleteNotification);
  const deleteSelectedNotifications = useMySpaceStore((s) => s.deleteSelectedNotifications);
  const toggleNotifSelected = useMySpaceStore((s) => s.toggleNotifSelected);
  const toggleNotifSelectAll = useMySpaceStore((s) => s.toggleNotifSelectAll);
  const clearNotifSelection = useMySpaceStore((s) => s.clearNotifSelection);

  if (notifications.length === 0) {
    return (
      <Empty
        description={t('暂无通知')}
        style={{ marginTop: 60 }}
      />
    );
  }

  const unreadCount = notifications.filter((n) => !n.read).length;
  const hasSelection = notifSelectedIds.size > 0;
  const allSelected = notifications.length > 0 && notifications.every((n) => notifSelectedIds.has(n.id));
  const selectedHasUnread = hasSelection && notifications.some((n) => notifSelectedIds.has(n.id) && !n.read);

  const handleClick = (n: typeof notifications[number]) => {
    if (hasSelection) {
      toggleNotifSelected(n.id);
      return;
    }
    if (!n.read) {
      markNotificationRead(n.id);
    }
    if (n.chat_id) {
      if (useAutomationChatStore.getState().activeGroup) {
        useAutomationChatStore.getState().exitAutomationChat();
      }
      setCurrentChatId(n.chat_id);
      setPanel('chat');
    }
  };

  const handleDelete = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    Modal.confirm({
      title: t('删除通知'),
      content: t('确定要删除这条通知吗？'),
      okText: t('删除'),
      cancelText: t('取消'),
      okButtonProps: { danger: true },
      onOk: () => deleteNotification(id),
    });
  };

  const handleDeleteSelected = () => {
    Modal.confirm({
      title: t('批量删除'),
      content: t('确定要删除选中的 {n} 条通知吗？', { n: notifSelectedIds.size }),
      okText: t('全部删除'),
      cancelText: t('取消'),
      okButtonProps: { danger: true },
      onOk: () => deleteSelectedNotifications(),
    });
  };

  return (
    <div className="jx-mySpace-notifList">
      <div className="jx-mySpace-notifActions">
        <Checkbox
          checked={allSelected}
          indeterminate={hasSelection && !allSelected}
          onChange={toggleNotifSelectAll}
        >
          <span className="jx-mySpace-notifActions-label">{t('全选')}</span>
        </Checkbox>
        <div className="jx-mySpace-notifActions-right">
          {unreadCount > 0 && (
            <Button
              type="link"
              size="small"
              icon={<CheckOutlined />}
              onClick={markAllNotificationsRead}
            >
              {t('全部标为已读')}
            </Button>
          )}
        </div>
      </div>

      <AnimatePresence mode="popLayout" initial={false}>
      {notifications.map((n, idx) => (
        <motion.div
          key={n.id}
          layout={notifications.length <= LAYOUT_ANIM_MAX_ITEMS ? 'position' : false}
          exit={LIST_ITEM_EXIT}
          className={
            `jx-mySpace-notifCard`
            + (n.read ? '' : ' jx-mySpace-notifCard--unread')
            + (notifSelectedIds.has(n.id) ? ' jx-mySpace-notifCard--selected' : '')
          }
          onClick={() => handleClick(n)}
          role="button"
          tabIndex={0}
        >
          <div className="jx-mySpace-notifCard-left">
            {hasSelection ? (
              <Checkbox
                checked={notifSelectedIds.has(n.id)}
                onClick={(e) => e.stopPropagation()}
                onChange={() => toggleNotifSelected(n.id)}
              />
            ) : (
              !n.read && (
                <span
                  className={`jx-mySpace-notifDot${idx < BREATH_DOT_MAX ? ' jx-anim-ripple' : ''}`}
                />
              )
            )}
          </div>
          <div className="jx-mySpace-notifCard-body">
            <div className="jx-mySpace-notifCard-header">
              <span className="jx-mySpace-notifCard-name">{n.task_name}</span>
              <Tag color={n.status === 'success' ? 'success' : 'error'}>
                {n.status === 'success' ? t('成功') : t('失败')}
              </Tag>
            </div>
            <div className="jx-mySpace-notifCard-summary">{n.summary}</div>
            <div className="jx-mySpace-notifCard-time">
              {formatDateTime(n.timestamp)}
            </div>
          </div>
          <button
            className="jx-mySpace-notifCard-deleteBtn"
            onClick={(e) => handleDelete(e, n.id)}
            title={t('删除通知')}
          >
            <DeleteOutlined />
          </button>
        </motion.div>
      ))}
      </AnimatePresence>

      {/* Batch-action floating bar (shared component: Portal + AnimatePresence + count spring) */}
      <BulkActionBar open={hasSelection} count={notifSelectedIds.size}>
          {selectedHasUnread && (
            <button
              className="jx-mySpace-bulkBar-btn"
              onClick={markSelectedNotificationsRead}
            >
              <EyeOutlined />
              <span>{t('标为已读')}</span>
            </button>
          )}
          <button
            className="jx-mySpace-bulkBar-btn jx-mySpace-bulkBar-btn--danger"
            onClick={handleDeleteSelected}
          >
            <DeleteOutlined />
            <span>{t('批量删除')}</span>
          </button>
          <button
            className="jx-mySpace-bulkBar-btn jx-mySpace-bulkBar-btn--cancel"
            onClick={clearNotifSelection}
          >
            <CloseOutlined />
            <span>{t('取消')}</span>
          </button>
      </BulkActionBar>
    </div>
  );
}
