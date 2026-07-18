import { useCallback, useEffect, useMemo, useState, type ReactElement } from 'react';
import {
  Avatar, AutoComplete, Button, Collapse, Empty, Form, Input, Modal,
  Popconfirm, Select, Skeleton, Space, Tag, Tooltip, Typography, message,
} from 'antd';
import {
  CrownOutlined, LinkOutlined, LogoutOutlined, SafetyOutlined,
  TeamOutlined, UserAddOutlined, UserDeleteOutlined, UserOutlined,
} from '@ant-design/icons';
import { AnimatePresence, motion } from 'motion/react';
import { DUR, EASE } from '../../utils/motionTokens';
import {
  getMyTeams, getTeamMembers, inviteTeamMember, removeTeamMember, searchUsers,
  type TeamMembershipBrief, type TeamMemberBrief, type UserSearchResult,
} from '../../api';
import { useAuthStore } from '../../stores';
import { resolveAvatarUrl } from '../../utils/avatar';
import { formatDate } from '../../utils/date';
import { roleAtLeast, roleLabel, roleRank } from '../../utils/roles';
import { t, tCtx } from '../../i18n';

const { Text } = Typography;

const ROLE_META: Record<string, { color: string; icon: ReactElement }> = {
  owner: { color: 'gold', icon: <CrownOutlined /> },
  admin: { color: 'blue', icon: <SafetyOutlined /> },
  member: { color: 'default', icon: <UserOutlined /> },
};

interface MembersCacheEntry {
  items: TeamMemberBrief[];
  my_role: string;
  loading: boolean;
}

export function TeamsSection() {
  const { authUser } = useAuthStore();
  const [teams, setTeams] = useState<TeamMembershipBrief[]>([]);
  const [loading, setLoading] = useState(false);
  const [membersCache, setMembersCache] = useState<Record<string, MembersCacheEntry>>({});
  const [inviteTarget, setInviteTarget] = useState<TeamMembershipBrief | null>(null);
  const [inviteForm] = Form.useForm();
  const [searchOptions, setSearchOptions] = useState<{ value: string; label: ReactElement; user: UserSearchResult }[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [pickedUser, setPickedUser] = useState<UserSearchResult | null>(null);

  const loadTeams = useCallback(async () => {
    setLoading(true);
    try {
      const items = await getMyTeams();
      setTeams(items);
    } catch (e: any) {
      message.error(e.message || t('加载团队失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadTeams(); }, [loadTeams]);

  const loadMembers = useCallback(async (teamId: string) => {
    setMembersCache((prev) => ({
      ...prev,
      [teamId]: { items: prev[teamId]?.items || [], my_role: prev[teamId]?.my_role || 'member', loading: true },
    }));
    try {
      const res = await getTeamMembers(teamId);
      setMembersCache((prev) => ({
        ...prev,
        [teamId]: { items: res.items, my_role: res.my_role, loading: false },
      }));
    } catch (e: any) {
      message.error(e.message || t('加载成员失败'));
      setMembersCache((prev) => ({
        ...prev,
        [teamId]: { ...(prev[teamId] || { items: [], my_role: 'member' }), loading: false },
      }));
    }
  }, []);

  const handleInvite = async () => {
    if (!inviteTarget) return;
    const values = await inviteForm.validateFields();
    const body: { user_id?: string; username?: string; role: 'member' | 'admin' } = {
      role: values.role,
    };
    if (pickedUser?.user_id) {
      body.user_id = pickedUser.user_id;
    } else if (values.username_or_id?.trim()) {
      body.username = values.username_or_id.trim();
    } else {
      message.warning(t('请选择或输入要邀请的用户'));
      return;
    }
    try {
      await inviteTeamMember(inviteTarget.team_id, body);
      message.success(t('邀请成功'));
      setInviteTarget(null);
      inviteForm.resetFields();
      setPickedUser(null);
      await loadMembers(inviteTarget.team_id);
      void loadTeams();
    } catch (e: any) {
      message.error(e.message || t('邀请失败'));
    }
  };

  const handleSearch = async (value: string) => {
    const q = value.trim();
    setPickedUser(null);
    if (q.length < 2) {
      setSearchOptions([]);
      return;
    }
    setSearchLoading(true);
    try {
      const users = await searchUsers(q);
      setSearchOptions(
        users.map((u) => ({
          value: u.username,
          user: u,
          label: (
            <Space>
              <Avatar size="small" src={resolveAvatarUrl(u.avatar_url)} icon={<UserOutlined />} />
              <span>{u.username}</span>
              {u.real_name && <Text type="secondary">{u.real_name}</Text>}
            </Space>
          ),
        })),
      );
    } catch {
      setSearchOptions([]);
    } finally {
      setSearchLoading(false);
    }
  };

  const handleRemove = async (teamId: string, userId: string, isSelf: boolean) => {
    try {
      await removeTeamMember(teamId, userId);
      message.success(isSelf ? t('已退出团队') : t('已移出成员'));
      if (isSelf) {
        await loadTeams();
      } else {
        // On success, filter locally first so the row immediately plays the exit animation; then fetch to reconcile.
        setMembersCache((prev) => {
          const entry = prev[teamId];
          if (!entry) return prev;
          return {
            ...prev,
            [teamId]: { ...entry, items: entry.items.filter((m) => m.user_id !== userId) },
          };
        });
        void loadMembers(teamId);
        void loadTeams();
      }
    } catch (e: any) {
      message.error(e.message || t('操作失败'));
    }
  };

  const onCollapseChange = (keys: string | string[]) => {
    const arr = Array.isArray(keys) ? keys : [keys];
    arr.forEach((tid) => {
      if (tid && !membersCache[tid]) {
        void loadMembers(tid);
      }
    });
  };

  const collapseItems = useMemo(
    () =>
      teams.map((team) => {
        const cacheEntry = membersCache[team.team_id];
        const members = cacheEntry?.items || [];
        const myRole = cacheEntry?.my_role || team.role;
        const isAdmin = roleAtLeast(myRole, 'admin');
        const meta = ROLE_META[team.role] || ROLE_META.member;

        return {
          key: team.team_id,
          label: (
            <div className="jx-settings-teamHeader">
              <Space size={8}>
                <TeamOutlined style={{ color: '#3d7bff' }} />
                <Text strong>{team.name}</Text>
                {team.source === 'sso_auto' && (
                  <Tooltip title={t('由外部 SSO 部门自动建立')}>
                    <Tag color="cyan" icon={<LinkOutlined />}>{t('部门')}</Tag>
                  </Tooltip>
                )}
                <Tag color={meta.color} icon={meta.icon}>{roleLabel(team.role)}</Tag>
              </Space>
              {typeof team.member_count === 'number' && (
                <Text type="secondary" className="jx-settings-teamCount">{t('{n} 人', { n: team.member_count })}</Text>
              )}
            </div>
          ),
          children: (
            <div className="jx-settings-teamBody">
              {team.description && <p className="jx-settings-teamDesc">{team.description}</p>}

              {cacheEntry?.loading && members.length === 0 && (
                <Skeleton active title={false} paragraph={{ rows: 2 }} style={{ padding: '8px 0' }} />
              )}

              {!cacheEntry?.loading && members.length === 0 && (
                <Empty className="jx-anim-fadeIn" image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('暂无成员')} />
              )}

              {members.length > 0 && (
                <ul className="jx-settings-memberList">
                  <AnimatePresence initial={false}>
                  {members.map((m, i) => {
                    const canKick =
                      isAdmin &&
                      !m.is_self &&
                      roleRank(myRole) > roleRank(m.role);
                    const avatarSrc = m.is_self
                      ? resolveAvatarUrl(authUser?.avatar_url || m.avatar_url)
                      : resolveAvatarUrl(m.avatar_url);
                    return (
                      <motion.li
                        key={m.user_id}
                        className="jx-settings-memberRow"
                        layout
                        initial={{ opacity: 0, y: 8 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{
                          opacity: 0,
                          x: -16,
                          height: 0,
                          marginTop: 0,
                          marginBottom: 0,
                          paddingTop: 0,
                          paddingBottom: 0,
                          borderTopWidth: 0,
                          borderBottomWidth: 0,
                          transition: { duration: 0.2, ease: EASE.exit },
                        }}
                        transition={{
                          duration: DUR.normal,
                          ease: EASE.brandOut,
                          delay: Math.min(i, 8) * 0.03,
                          layout: { duration: 0.2, ease: EASE.standard, delay: 0 },
                        }}
                        style={{ overflow: 'hidden' }}
                      >
                        <Avatar size={32} src={avatarSrc} icon={<UserOutlined />} />
                        <div className="jx-settings-memberInfo">
                          <div className="jx-settings-memberName">
                            {m.username}
                            {m.is_self && <Tag style={{ marginLeft: 6 }}>{t('你')}</Tag>}
                          </div>
                          <div className="jx-settings-memberMeta">
                            <Tag color={ROLE_META[m.role]?.color} icon={ROLE_META[m.role]?.icon}>
                              {roleLabel(m.role)}
                            </Tag>
                            {m.joined_at && (
                              <span className="jx-settings-memberJoined">
                                {t('加入于 {date}', { date: formatDate(m.joined_at) })}
                              </span>
                            )}
                          </div>
                        </div>
                        {canKick && (
                          <Popconfirm
                            title={t('将 {name} 移出团队？', { name: m.username })}
                            onConfirm={() => void handleRemove(team.team_id, m.user_id, false)}
                            okText={t('移出')}
                            cancelText={t('取消')}
                            okButtonProps={{ danger: true }}
                          >
                            <Button
                              size="small"
                              danger
                              type="text"
                              icon={<UserDeleteOutlined />}
                            >
                              {t('移出')}
                            </Button>
                          </Popconfirm>
                        )}
                      </motion.li>
                    );
                  })}
                  </AnimatePresence>
                </ul>
              )}

              <div className="jx-settings-teamActions">
                {isAdmin && (
                  <Button
                    type="primary"
                    icon={<UserAddOutlined />}
                    onClick={() => {
                      setInviteTarget(team);
                      inviteForm.resetFields();
                      setPickedUser(null);
                      setSearchOptions([]);
                    }}
                  >
                    {t('邀请成员')}
                  </Button>
                )}
                <Popconfirm
                  title={t('确认退出该团队？')}
                  description={myRole === 'owner' ? t('你是团队所有者，需先转让所有权。') : undefined}
                  onConfirm={() => void handleRemove(team.team_id, authUser?.user_id || '', true)}
                  okText={tCtx('leave', '退出')}
                  cancelText={t('取消')}
                  okButtonProps={{ danger: true }}
                  disabled={myRole === 'owner'}
                >
                  <Button
                    icon={<LogoutOutlined />}
                    disabled={myRole === 'owner'}
                  >
                    {t('退出团队')}
                  </Button>
                </Popconfirm>
              </div>
            </div>
          ),
        };
      }),
    [teams, membersCache, authUser?.user_id],
  );

  if (loading && teams.length === 0) {
    return (
      <div className="jx-settings-emptyHint">
        <Skeleton active title={false} paragraph={{ rows: 2 }} />
      </div>
    );
  }

  if (!loading && teams.length === 0) {
    return (
      <Empty
        className="jx-anim-fadeIn"
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={t('暂未加入任何团队')}
        style={{ padding: '12px 0' }}
      />
    );
  }

  return (
    <>
      <Collapse
        bordered={false}
        items={collapseItems}
        onChange={onCollapseChange}
        className="jx-settings-teamCollapse"
      />

      <Modal
        title={inviteTarget ? t('邀请成员 · {name}', { name: inviteTarget.name }) : t('邀请成员')}
        open={inviteTarget !== null}
        onCancel={() => {
          setInviteTarget(null);
          inviteForm.resetFields();
          setPickedUser(null);
          setSearchOptions([]);
        }}
        onOk={handleInvite}
        okText={t('邀请')}
        destroyOnClose
      >
        <Form form={inviteForm} layout="vertical" initialValues={{ role: 'member' }}>
          <Form.Item
            label={t('用户')}
            name="username_or_id"
            rules={[{ required: true, message: t('请输入或选择用户') }]}
            tooltip={t('至少 2 字，按用户名或真实姓名模糊搜索')}
          >
            <AutoComplete
              options={searchOptions}
              onSearch={handleSearch}
              onSelect={(_val, option: any) => setPickedUser(option?.user || null)}
              notFoundContent={searchLoading ? t('搜索中…') : null}
              placeholder={t('输入用户名 / 姓名')}
              allowClear
            >
              <Input />
            </AutoComplete>
          </Form.Item>
          <Form.Item label={t('角色')} name="role">
            <Select>
              <Select.Option value="member">{roleLabel('member')}</Select.Option>
              {inviteTarget && roleAtLeast(membersCache[inviteTarget.team_id]?.my_role || inviteTarget.role, 'owner') && (
                <Select.Option value="admin">{roleLabel('admin')}</Select.Option>
              )}
            </Select>
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
