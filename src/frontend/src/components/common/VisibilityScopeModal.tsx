import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Modal, Radio, Select, Spin, Typography, message } from 'antd';
import { TeamOutlined, UserOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { t } from '../../i18n';
import type {
  MarketVisibilityGrant,
  MarketVisibilityValue,
  VisibilityPrincipals,
  VisibilityScopeFetchers,
} from '../../types';

// "Visibility scope" configuration modal for marketplace items (shared by the skills / plugins / sub-agents marketplaces).
// Visible to everyone by default; after switching to "visible to a specified scope", pick a whitelist across three subject types: roles / teams / people,
// matching any subject makes it visible (union). Saving is a full replacement. Transport is injected by fetchers (admin token domain).
interface VisibilityScopeModalProps {
  open: boolean;
  /** Target item slug (does not load when null) */
  slug: string | null;
  /** Item display name (used as the modal title) */
  itemName?: string;
  fetchers: VisibilityScopeFetchers;
  onClose: () => void;
  /** Save-success callback (parent updates the list annotation in place, avoiding a refetch) */
  onSaved?: (slug: string, visibility: MarketVisibilityValue) => void;
}

export function VisibilityScopeModal({
  open,
  slug,
  itemName,
  fetchers,
  onClose,
  onSaved,
}: VisibilityScopeModalProps) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [visibility, setVisibility] = useState<MarketVisibilityValue>('public');
  const [userIds, setUserIds] = useState<string[]>([]);
  const [teamIds, setTeamIds] = useState<string[]>([]);
  const [roleIds, setRoleIds] = useState<string[]>([]);
  const [principals, setPrincipals] = useState<VisibilityPrincipals | null>(null);

  const load = useCallback(async () => {
    if (!slug) return;
    setLoading(true);
    try {
      const [cfg, prin] = await Promise.all([
        fetchers.getVisibility(slug),
        fetchers.loadPrincipals(),
      ]);
      setPrincipals(prin);
      setVisibility(cfg.visibility === 'scoped' ? 'scoped' : 'public');
      setUserIds(cfg.grants.filter((g) => g.principal_type === 'user').map((g) => g.principal_id));
      setTeamIds(cfg.grants.filter((g) => g.principal_type === 'team').map((g) => g.principal_id));
      setRoleIds(cfg.grants.filter((g) => g.principal_type === 'role').map((g) => g.principal_id));
    } catch (e) {
      message.error((e as Error).message || t('加载可见范围失败'));
    } finally {
      setLoading(false);
    }
  }, [slug, fetchers]);

  useEffect(() => {
    if (open && slug) void load();
  }, [open, slug, load]);

  const userOptions = useMemo(
    () =>
      (principals?.users || []).map((u) => ({
        value: u.user_id,
        label: u.real_name ? `${u.real_name}（${u.username}）` : u.username,
      })),
    [principals],
  );
  const teamOptions = useMemo(
    () => (principals?.teams || []).map((tm) => ({ value: tm.team_id, label: tm.name })),
    [principals],
  );
  const roleOptions = useMemo(
    () => (principals?.roles || []).map((r) => ({ value: r.role_id, label: r.name })),
    [principals],
  );

  const grantCount = userIds.length + teamIds.length + roleIds.length;

  const handleSave = useCallback(async () => {
    if (!slug) return;
    if (visibility === 'scoped' && grantCount === 0) {
      message.warning(t('指定范围可见时，请至少选择一个角色、团队或人员'));
      return;
    }
    const grants: MarketVisibilityGrant[] =
      visibility === 'scoped'
        ? [
            ...roleIds.map((id) => ({ principal_type: 'role' as const, principal_id: id })),
            ...teamIds.map((id) => ({ principal_type: 'team' as const, principal_id: id })),
            ...userIds.map((id) => ({ principal_type: 'user' as const, principal_id: id })),
          ]
        : [];
    setSaving(true);
    try {
      await fetchers.setVisibility(slug, { visibility, grants });
      message.success(
        visibility === 'public'
          ? t('已设为所有人可见')
          : t('已设为指定范围可见（{n} 条授权）', { n: grantCount }),
      );
      onSaved?.(slug, visibility);
      onClose();
    } catch (e) {
      message.error((e as Error).message || t('保存可见范围失败'));
    } finally {
      setSaving(false);
    }
  }, [slug, visibility, grantCount, roleIds, teamIds, userIds, fetchers, onSaved, onClose]);

  const selectCommon = {
    mode: 'multiple' as const,
    allowClear: true,
    showSearch: true,
    optionFilterProp: 'label' as const,
    style: { width: '100%' },
    maxTagCount: 'responsive' as const,
  };

  return (
    <Modal
      title={itemName ? t('可见范围 — {name}', { name: itemName }) : t('可见范围')}
      open={open}
      onCancel={onClose}
      onOk={() => void handleSave()}
      okText={t('保存')}
      cancelText={t('取消')}
      confirmLoading={saving}
      width={520}
      destroyOnHidden
    >
      <Spin spinning={loading}>
        <Radio.Group
          value={visibility}
          onChange={(e) => setVisibility(e.target.value)}
          style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 16 }}
        >
          <Radio value="public">
            {t('所有人可见')}
            <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
              {t('（默认）市场中全员可浏览、可安装')}
            </Typography.Text>
          </Radio>
          <Radio value="scoped">
            {t('指定范围可见')}
            <Typography.Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
              {t('仅所选角色 / 团队 / 人员可见，命中任意一项即可')}
            </Typography.Text>
          </Radio>
        </Radio.Group>

        {visibility === 'scoped' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div>
              <div style={{ marginBottom: 4, fontSize: 13 }}>
                <SafetyCertificateOutlined style={{ marginRight: 4, color: 'var(--color-primary)' }} />
                {t('按角色')}
              </div>
              <Select
                {...selectCommon}
                placeholder={t('选择角色（可多选）')}
                value={roleIds}
                onChange={setRoleIds}
                options={roleOptions}
              />
            </div>
            <div>
              <div style={{ marginBottom: 4, fontSize: 13 }}>
                <TeamOutlined style={{ marginRight: 4, color: 'var(--color-primary)' }} />
                {t('按团队')}
              </div>
              <Select
                {...selectCommon}
                placeholder={t('选择团队（可多选）')}
                value={teamIds}
                onChange={setTeamIds}
                options={teamOptions}
              />
            </div>
            <div>
              <div style={{ marginBottom: 4, fontSize: 13 }}>
                <UserOutlined style={{ marginRight: 4, color: 'var(--color-primary)' }} />
                {t('按人员')}
              </div>
              <Select
                {...selectCommon}
                placeholder={t('搜索并选择人员（可多选）')}
                value={userIds}
                onChange={setUserIds}
                options={userOptions}
              />
            </div>
            <Alert
              type="info"
              showIcon
              message={t('可见范围只影响市场浏览与安装；此前已安装的用户不受影响。管理员始终可见。')}
              style={{ fontSize: 12 }}
            />
          </div>
        )}
      </Spin>
    </Modal>
  );
}
