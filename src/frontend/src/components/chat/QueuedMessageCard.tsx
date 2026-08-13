import { useState } from 'react';
import {
  CheckOutlined,
  CloseOutlined,
  DeleteOutlined,
  EditOutlined,
  MoreOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { Dropdown } from 'antd';
import type { QueuedChatMessage } from '../../stores/chatStore';
import { t } from '../../i18n';

interface QueuedMessageCardProps {
  queued: QueuedChatMessage;
  running: boolean;
  canSteer: boolean;
  onSteer: () => void;
  onDelete: () => void;
  onEdit: (content: string) => void;
}

export function QueuedMessageCard({
  queued,
  running,
  canSteer,
  onSteer,
  onDelete,
  onEdit,
}: QueuedMessageCardProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(queued.content);

  const saveEdit = () => {
    const next = draft.trim();
    if (!next) return;
    onEdit(next);
    setEditing(false);
  };

  const steerLabel = queued.status === 'steering'
    ? t('等待执行节点…')
    : queued.status === 'applied'
      ? t('已发送，正在调整…')
      : t('立即开始');
  const steerDisabled = queued.status !== 'queued' || (running && !canSteer);
  const steerTitle = running && !canSteer
    ? t('带附件、技能、插件或子智能体的消息将在当前任务结束后发送')
    : running
      ? t('在本轮工具完成后、下一轮推理前发送给模型')
      : t('立即开始');

  return (
    <div className={`jx-queuedMessage jx-queuedMessage--${queued.status}`}>
      <div className="jx-queuedMessage-main">
        <span className="jx-queuedMessage-indicator" aria-hidden="true">↳</span>
        {editing ? (
          <textarea
            className="jx-queuedMessage-editor"
            value={draft}
            autoFocus
            rows={2}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                saveEdit();
              }
              if (event.key === 'Escape') {
                event.preventDefault();
                setDraft(queued.content);
                setEditing(false);
              }
            }}
          />
        ) : (
          <div className="jx-queuedMessage-content" title={queued.content}>{queued.content}</div>
        )}
      </div>
      <div className="jx-queuedMessage-actions">
        {editing ? (
          <>
            <button
              type="button"
              className="jx-queuedMessage-iconBtn"
              onClick={saveEdit}
              disabled={!draft.trim()}
              aria-label={t('保存修改')}
            >
              <CheckOutlined />
            </button>
            <button
              type="button"
              className="jx-queuedMessage-iconBtn"
              onClick={() => {
                setDraft(queued.content);
                setEditing(false);
              }}
              aria-label={t('取消编辑')}
            >
              <CloseOutlined />
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="jx-queuedMessage-steer"
              onClick={onSteer}
              disabled={steerDisabled}
              title={steerTitle}
            >
              {queued.status === 'steering' && <SyncOutlined spin />}
              {queued.status === 'applied' && <CheckOutlined />}
              <span>{steerLabel}</span>
            </button>
            {queued.status !== 'applied' && (
              <button
                type="button"
                className="jx-queuedMessage-iconBtn"
                onClick={onDelete}
                aria-label={t('删除待发送消息')}
                title={t('删除')}
              >
                <DeleteOutlined />
              </button>
            )}
            <Dropdown
              trigger={['click']}
              placement="topRight"
              menu={{
                items: [
                  {
                    key: 'edit',
                    icon: <EditOutlined />,
                    label: t('编辑消息'),
                    disabled: queued.status !== 'queued',
                    onClick: () => {
                      setDraft(queued.content);
                      setEditing(true);
                    },
                  },
                ],
              }}
            >
              <button
                type="button"
                className="jx-queuedMessage-iconBtn"
                aria-label={t('更多操作')}
              >
                <MoreOutlined />
              </button>
            </Dropdown>
          </>
        )}
      </div>
    </div>
  );
}
