import { useCallback, useEffect, useState } from 'react';
import { Button, Input, Select, Space, Typography, message } from 'antd';
import { ThunderboltOutlined } from '@ant-design/icons';
import {
  getMyServiceConfigs,
  testMyServiceConfig,
  updateMyServiceConfigs,
  type ServiceConfigGroup,
  type ServiceConfigItem,
} from '../../api';
import { t } from '../../i18n';

const { Text } = Typography;

// Enum-type config items → dropdown options (everything else is a text/password input box)
const ENUM_OPTIONS: Record<string, Array<{ value: string; label: string }>> = {
  'internet_search.engine': [
    { value: 'tavily', label: 'Tavily' },
    { value: 'baidu', label: t('百度千帆') },
  ],
  'file_parser.parse_method': [
    { value: 'auto', label: 'auto' },
    { value: 'ocr', label: 'ocr' },
    { value: 'txt', label: 'txt' },
  ],
};

/**
 * "Settings → System Management → Service Configs" panel (whitelisted service configs pushed down in CE).
 *
 * Generic grouped editor: the backend /v1/me/system/service-configs only returns whitelisted groups
 * (internet search / file parsing / knowledge base / sandbox / context), with keys masked;
 * values kept at the **** mask are skipped by the backend on submit (the mask is never written back as the real key).
 */
export function SystemServicePanel() {
  const [groups, setGroups] = useState<ServiceConfigGroup[]>([]);
  const [loading, setLoading] = useState(false);
  // config_key → the value being edited (only records items the user changed; only these are submitted on save)
  const [edited, setEdited] = useState<Record<string, string>>({});
  const [savingGroup, setSavingGroup] = useState<string | null>(null);
  const [testingGroup, setTestingGroup] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setGroups(await getMyServiceConfigs());
      setEdited({});
    } catch (e) {
      message.error(t('加载服务配置失败：{msg}', { msg: (e as Error).message }));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const valueOf = (item: ServiceConfigItem): string =>
    edited[item.config_key] ?? (item.config_value ?? '');

  const handleSave = async (group: ServiceConfigGroup) => {
    const items = group.items
      .filter((i) => i.config_key in edited)
      .map((i) => ({ key: i.config_key, value: edited[i.config_key] }));
    if (!items.length) {
      message.info(t('没有改动需要保存'));
      return;
    }
    setSavingGroup(group.group_key);
    try {
      await updateMyServiceConfigs(items);
      message.success(t('{label} 配置已保存（约 30 秒内生效）', { label: group.label }));
      await reload();
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setSavingGroup(null);
    }
  };

  const handleTest = async (group: ServiceConfigGroup) => {
    setTestingGroup(group.group_key);
    try {
      const r = await testMyServiceConfig(group.group_key);
      if (r.success) {
        message.success(t('{label} 连通性正常（{ms}ms）', { label: group.label, ms: String(r.latency_ms) }));
      } else {
        message.error(t('{label} 连通性失败：{msg}', { label: group.label, msg: r.error || '' }));
      }
    } catch (e) {
      message.error((e as Error).message);
    } finally {
      setTestingGroup(null);
    }
  };

  return (
    <div className="jx-sysPanel">
      <div className="jx-sysPanel-toolbar">
        <Text type="secondary">
          {t('搜索引擎密钥、文件解析等个人使用相关的服务配置；保存后约 30 秒内生效，无需重启。')}
        </Text>
        <Button onClick={() => void reload()} loading={loading}>{t('刷新')}</Button>
      </div>
      {groups.map((group) => (
        <div key={group.group_key} className="jx-sysPanel-group">
          <div className="jx-sysPanel-groupHeader">
            <h4 className="jx-sysPanel-subtitle">{group.label}</h4>
            <Space size="small">
              {group.testable && (
                <Button
                  size="small"
                  icon={<ThunderboltOutlined />}
                  loading={testingGroup === group.group_key}
                  onClick={() => void handleTest(group)}
                >
                  {t('测试')}
                </Button>
              )}
              <Button
                size="small"
                type="primary"
                loading={savingGroup === group.group_key}
                onClick={() => void handleSave(group)}
              >
                {t('保存')}
              </Button>
            </Space>
          </div>
          {group.items.map((item) => (
            <div key={item.config_key} className="jx-sysPanel-row">
              <div className="jx-sysPanel-rowLabel">
                <Text>{item.display_name}</Text>
                {item.description && (
                  <Text type="secondary" style={{ fontSize: 12 }}>{item.description}</Text>
                )}
              </div>
              {ENUM_OPTIONS[item.config_key] ? (
                <Select
                  size="small"
                  style={{ width: 260 }}
                  value={valueOf(item) || undefined}
                  options={ENUM_OPTIONS[item.config_key]}
                  onChange={(v) => setEdited((prev) => ({ ...prev, [item.config_key]: v as string }))}
                />
              ) : item.is_secret ? (
                <Input.Password
                  size="small"
                  style={{ width: 260 }}
                  value={valueOf(item)}
                  placeholder={t('未配置')}
                  autoComplete="new-password"
                  onChange={(e) => setEdited((prev) => ({ ...prev, [item.config_key]: e.target.value }))}
                />
              ) : (
                <Input
                  size="small"
                  style={{ width: 260 }}
                  value={valueOf(item)}
                  placeholder={t('未配置')}
                  onChange={(e) => setEdited((prev) => ({ ...prev, [item.config_key]: e.target.value }))}
                />
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
