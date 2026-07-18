import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Dropdown, Tag, Tooltip, message } from 'antd';
import { CollapseHeight } from '../common/CollapseHeight';
import {
  EyeOutlined,
  LockOutlined,
  ShareAltOutlined,
  TeamOutlined,
  UserOutlined,
} from '@ant-design/icons';
import type { ChatDetail } from '../../api';
import { getChatDetail, updateChatShareScope } from '../../api';
import { t } from '../../i18n';

interface Props {
  chatId: string;
  /** Optional: pass a new value to trigger a refresh whenever the upper-level share state changes (e.g. turning off the project-level switch). */
  refreshKey?: number;
  /** Callback after the share state finishes loading, used by ChatArea to coordinate disabling the input box, etc. */
  onLevelChange?: (level: 'admin' | 'edit' | 'read' | null, scope: 'private' | 'team_read' | 'team_edit' | null) => void;
}

/**
 * Team project session sharing UI:
 * - owner within a team project → sees the "Share" button; opening it pops up a private/read-only/editable three-way choice
 * - non-owner entering a shared session → top banner notice + creator + read-only/editable chip
 * - non-team project / personal session → renders nothing
 */
export function ChatShareBanner({ chatId, refreshKey, onLevelChange }: Props) {
  const [detail, setDetail] = useState<ChatDetail | null>(null);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    try {
      const d = await getChatDetail(chatId);
      setDetail(d);
      onLevelChange?.(d.access_level, d.share_scope);
    } catch {
      setDetail(null);
      onLevelChange?.(null, null);
    }
  }, [chatId, onLevelChange]);

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const isOwner = !!detail?.is_owner;
  const scope = detail?.share_scope || 'private';
  const isShared = scope === 'team_read' || scope === 'team_edit';
  // Only render within team project sessions (personal projects and no-project sessions are never shared); a non-owner additionally needs it to be already shared
  const visible = !!detail && !!detail.project_id && !!detail.is_team_project && (isOwner || isShared);

  // Zero construction when not visible: the menu / banner element trees are all gated behind the visible check
  let banner: ReactNode = null;
  if (visible) {
    const setScope = async (next: 'private' | 'team_read' | 'team_edit') => {
      setSaving(true);
      try {
        await updateChatShareScope(chatId, next);
        await reload();
        message.success(
          next === 'private' ? t('已取消共享') : next === 'team_read' ? t('已设为只读共享') : t('已设为可编辑共享')
        );
      } catch (err) {
        message.error((err as Error)?.message || t('操作失败'));
      } finally {
        setSaving(false);
      }
    };

    if (isOwner) {
      const items = [
        {
          key: 'private',
          label: t('私密（仅自己）'),
          icon: <LockOutlined />,
          onClick: () => void setScope('private'),
          disabled: scope === 'private',
        },
        {
          key: 'team_read',
          label: t('只读共享给项目成员'),
          icon: <EyeOutlined />,
          onClick: () => void setScope('team_read'),
          disabled: scope === 'team_read',
        },
        {
          key: 'team_edit',
          label: t('可编辑共享给项目成员'),
          icon: <TeamOutlined />,
          onClick: () => void setScope('team_edit'),
          disabled: scope === 'team_edit',
        },
      ];
      banner = (
        <div className="jx-chatShareBanner jx-chatShareBanner--owner">
          {scope === 'team_read' && (
            <Tag icon={<EyeOutlined />} color="default">{t('只读共享中')}</Tag>
          )}
          {scope === 'team_edit' && (
            <Tag icon={<TeamOutlined />} color="blue">{t('可编辑共享中')}</Tag>
          )}
          {scope === 'private' && (
            <Tag icon={<LockOutlined />} color="default">{t('私密')}</Tag>
          )}
          <Tooltip title={t('设置该会话对项目成员的可见性 / 可编辑性')}>
            <Dropdown menu={{ items }} trigger={['click']} disabled={saving}>
              <Button
                type="text"
                size="small"
                icon={<ShareAltOutlined />}
                loading={saving}
              >
                {t('共享')}
              </Button>
            </Dropdown>
          </Tooltip>
        </div>
      );
    } else {
      // Non-owner perspective
      banner = (
        <div className="jx-chatShareBanner jx-chatShareBanner--member">
          {scope === 'team_read' ? (
            <>
              <Tag icon={<EyeOutlined />} color="default">{t('只读共享')}</Tag>
              <span className="jx-chatShareBanner-text">{t('该会话由创建者设为只读，无法发送新消息')}</span>
            </>
          ) : (
            <>
              <Tag icon={<TeamOutlined />} color="blue">{t('可编辑共享')}</Tag>
              <span className="jx-chatShareBanner-text">{t('你可以继续与会话创建者协作')}</span>
            </>
          )}
          <span className="jx-chatShareBanner-owner">
            <UserOutlined style={{ marginRight: 2 }} />{t('创建人：{id}', { id: detail?.owner_user_id ?? '' })}
          </span>
        </div>
      );
    }
  }

  // Enter/exit: height + opacity (initial={false} — a banner already shared when entering the session
  // is a replay of historical state, so it does not play the enter animation; only subsequent share on/off animates)
  return (
    <CollapseHeight show={visible} motionKey="chatShareBanner" initial={false}>
      {banner}
    </CollapseHeight>
  );
}
