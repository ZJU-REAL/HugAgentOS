import { useEffect, useState } from 'react';
import { Modal, Table, Tag, Radio, Avatar, Spin, message } from 'antd';
import { t } from '../../../i18n';
import { UserOutlined } from '@ant-design/icons';
import {
  listTeamMemberPermissions,
  setTeamMemberPermission,
} from '../../../api';
import type { TeamMemberPermission, TeamFilePermission } from '../../../types/teamFiles';
import { roleLabel, filePermissionLabel } from '../../../utils/roles';
import { useAuthStore } from '../../../stores';
import { resolveAvatarUrl } from '../../../utils/avatar';

const ROLE_TAG_COLORS: Record<string, string> = {
  owner: 'gold',
  admin: 'blue',
  member: 'default',
};

interface Props {
  open: boolean;
  teamId: string | null;
  onClose: () => void;
}

export function TeamPermissionsModal({ open, teamId, onClose }: Props) {
  const [loading, setLoading] = useState(false);
  const [members, setMembers] = useState<TeamMemberPermission[]>([]);
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const authUser = useAuthStore((s) => s.authUser);

  useEffect(() => {
    if (!open || !teamId) return;
    setLoading(true);
    listTeamMemberPermissions(teamId)
      .then(setMembers)
      .catch((e) => message.error(e?.message || t('加载失败')))
      .finally(() => setLoading(false));
  }, [open, teamId]);

  const handleChange = async (userId: string, value: TeamFilePermission) => {
    if (!teamId) return;
    setPending((p) => ({ ...p, [userId]: true }));
    try {
      await setTeamMemberPermission(teamId, userId, value);
      setMembers((prev) => prev.map((m) => m.user_id === userId ? { ...m, file_permission: value } : m));
      message.success(t('权限已更新'));
    } catch (e: any) {
      message.error(e?.message || t('更新失败'));
    } finally {
      setPending((p) => ({ ...p, [userId]: false }));
    }
  };

  return (
    <Modal
      open={open}
      title={t('管理成员权限')}
      onCancel={onClose}
      footer={null}
      width={640}
      destroyOnHidden
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}><Spin /></div>
      ) : (
        <Table
          rowKey="user_id"
          dataSource={members}
          pagination={false}
          size="middle"
          columns={[
            {
              title: t('成员'),
              dataIndex: 'username',
              key: 'username',
              render: (_v, row) => {
                const isSelf = authUser?.user_id === row.user_id;
                const avatarSrc = resolveAvatarUrl(
                  (isSelf && authUser?.avatar_url) || row.avatar_url,
                );
                return (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <Avatar size={32} src={avatarSrc} icon={<UserOutlined />} />
                    <span style={{ fontWeight: 500 }}>{row.username}</span>
                  </div>
                );
              },
            },
            {
              title: t('角色'),
              dataIndex: 'role',
              key: 'role',
              width: 100,
              render: (role: string) => {
                const color = ROLE_TAG_COLORS[role];
                return color && color !== 'default'
                  ? <Tag color={color}>{roleLabel(role)}</Tag>
                  : <Tag>{roleLabel(role)}</Tag>;
              },
            },
            {
              title: t('文件权限'),
              dataIndex: 'file_permission',
              key: 'file_permission',
              width: 240,
              render: (perm: TeamFilePermission, row) => {
                if (row.role === 'owner' || row.role === 'admin') {
                  return <Tag color="gold">{t('默认：编辑（由角色决定）')}</Tag>;
                }
                return (
                  <Radio.Group
                    value={perm}
                    disabled={!!pending[row.user_id]}
                    onChange={(e) => void handleChange(row.user_id, e.target.value)}
                  >
                    <Radio.Button value="viewer">{filePermissionLabel('viewer')}</Radio.Button>
                    <Radio.Button value="editor">{filePermissionLabel('editor')}</Radio.Button>
                  </Radio.Group>
                );
              },
            },
          ]}
        />
      )}
    </Modal>
  );
}
