import { useState } from 'react';
import { Popover } from 'antd';
import { BellOutlined, CloseOutlined, RightOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import { useChatStore } from '../../stores';
import { useAuthStore } from '../../stores/authStore';
import { useCatalogStore } from '../../stores/catalogStore';
import { useEvaluationStore } from '../../stores/evaluationStore';
import { formatMonthDayTime } from '../../utils/date';
import { userScopedKey } from '../../storage';
import type { PendingEvaluationItem } from '../../api/evaluation';

const DISMISS_KEY = 'jx_eval_reminder_dismissed';

/** Per account, so another user signing in on this tab still gets reminded. */
function readDismissed(key: string | null): boolean {
  if (!key) return false;
  try { return sessionStorage.getItem(key) === '1'; } catch { return false; }
}

/**
 * 温馨提醒: a floating chip in the bottom-right corner that lists the five most recent
 * conversations still waiting for an evaluation. It only appears when something is pending,
 * sits beside (not over) the composer, and can be hidden for this session.
 */
interface EvaluationReminderProps {
  /** `chat` floats just above the composer of an open conversation; `welcome` sits in the
   * bottom-right corner of the empty new-chat page. */
  placement?: 'chat' | 'welcome';
}

export function EvaluationReminder({ placement = 'chat' }: EvaluationReminderProps) {
  const enabled = useAuthStore((s) => s.authUser?.can_use_evaluation === true);
  const dismissKey = userScopedKey(DISMISS_KEY, useAuthStore((s) => s.authUser?.user_id));
  const items = useEvaluationStore((s) => s.pendingItems);
  const total = useEvaluationStore((s) => s.pendingTotal);
  const [open, setOpen] = useState(false);
  const [dismissedKey, setDismissedKey] = useState<string | null>(null);
  const dismissed = dismissedKey === dismissKey || readDismissed(dismissKey);

  if (!enabled || dismissed || total === 0) return null;

  const jump = (item: PendingEvaluationItem) => {
    setOpen(false);
    useEvaluationStore.getState().setFocus({ chatId: item.chat_id, messageId: item.message_id });
    useCatalogStore.getState().setPanel('chat');
    useChatStore.getState().setCurrentChatId(item.chat_id);
  };

  const dismiss = () => {
    if (dismissKey) {
      try { sessionStorage.setItem(dismissKey, '1'); } catch { /* private mode: hide for this view only */ }
    }
    setDismissedKey(dismissKey);
  };

  const content = (
    <div className="jx-evalReminder-panel">
      <ul className="jx-evalReminder-list">
        {items.map((item) => (
          <li key={item.chat_id}>
            <button type="button" className="jx-evalReminder-item" onClick={() => jump(item)}>
              <span className="jx-evalReminder-title">{item.title || t('新对话')}</span>
              <span className="jx-evalReminder-meta">
                {item.unrated > 1 ? t('{n} 条回答待评价', { n: item.unrated }) : t('1 条回答待评价')}
                {item.delivered_at ? ` · ${formatMonthDayTime(item.delivered_at)}` : ''}
              </span>
              <RightOutlined className="jx-evalReminder-chevron" aria-hidden />
            </button>
          </li>
        ))}
      </ul>
      <div className="jx-evalReminder-footer">
        <button type="button" className="jx-evalReminder-dismiss" onClick={dismiss}>
          {t('本次登录不再提醒')}
        </button>
      </div>
    </div>
  );

  return (
    <div className={`jx-evalReminder jx-evalReminder--${placement}`}>
      <Popover
        content={content}
        title={t('温馨提醒')}
        trigger="click"
        open={open}
        onOpenChange={setOpen}
        placement="topRight"
        classNames={{ root: 'jx-evalReminder-popover' }}
      >
        <button type="button" className="jx-evalReminder-chip" aria-haspopup="dialog" aria-expanded={open}>
          <BellOutlined aria-hidden />
          <span className="jx-evalReminder-label">
            <span className="jx-evalReminder-prefix">{t('温馨提醒')} · </span>
            {t('{n} 条待评价', { n: total })}
          </span>
        </button>
      </Popover>
      <button type="button" className="jx-evalReminder-close" aria-label={t('本次登录不再提醒')} title={t('本次登录不再提醒')} onClick={dismiss}>
        <CloseOutlined aria-hidden />
      </button>
    </div>
  );
}
