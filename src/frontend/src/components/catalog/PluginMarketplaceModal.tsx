import { useCallback, useEffect, useMemo, useState } from 'react';
import { AppstoreOutlined, KeyOutlined } from '@ant-design/icons';
import { Button, Empty, Form, Input, Modal, Spin, Tag, message } from 'antd';
import { installPlugin, listPlugins } from '../../api';
import type {
  PluginInstallResult,
  PluginListItem,
  PluginRequiredSecret,
} from '../../types';
import { t } from '../../i18n';

interface PluginMarketplaceModalProps {
  open: boolean;
  onClose: () => void;
  onInstalled?: (result: PluginInstallResult, plugin: PluginListItem) => void;
  scopeLabel?: string;
}

function normalizeSecret(field: string | PluginRequiredSecret): PluginRequiredSecret {
  return typeof field === 'string'
    ? { key: field, label: field, required: true }
    : field;
}

/**
 * Compact, reusable plugin-marketplace picker. Installing keeps the modal open
 * so callers such as the sub-agent editor can add several plugins in one pass.
 */
export function PluginMarketplaceModal({
  open,
  onClose,
  onInstalled,
  scopeLabel,
}: PluginMarketplaceModalProps) {
  const [items, setItems] = useState<PluginListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [secretPlugin, setSecretPlugin] = useState<PluginListItem | null>(null);
  const [secretForm] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await listPlugins());
    } catch (error) {
      message.error((error as Error).message || t('加载插件失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [load, open]);

  const install = useCallback(async (
    plugin: PluginListItem,
    secrets: Record<string, string>,
  ) => {
    setBusySlug(plugin.slug);
    try {
      const result = await installPlugin(plugin.slug, secrets);
      setItems((current) => current.map((item) => (
        item.slug === plugin.slug ? { ...item, installed: true } : item
      )));
      message.success(t('「{name}」已安装', { name: plugin.name }));
      onInstalled?.(result, plugin);
    } catch (error) {
      message.error((error as Error).message || t('安装失败'));
    } finally {
      setBusySlug(null);
    }
  }, [onInstalled]);

  const handleInstall = useCallback((plugin: PluginListItem) => {
    if ((plugin.required_secrets || []).length > 0) {
      secretForm.resetFields();
      setSecretPlugin(plugin);
      return;
    }
    void install(plugin, {});
  }, [install, secretForm]);

  const submitSecrets = useCallback(async () => {
    if (!secretPlugin) return;
    const values = await secretForm.validateFields();
    const secrets: Record<string, string> = {};
    for (const rawField of secretPlugin.required_secrets || []) {
      const field = normalizeSecret(rawField);
      const value = String(values[field.key] || '').trim();
      if (value) secrets[field.key] = value;
    }
    const plugin = secretPlugin;
    setSecretPlugin(null);
    await install(plugin, secrets);
  }, [install, secretForm, secretPlugin]);

  const availableItems = useMemo(
    () => items.filter((item) => !item.installed),
    [items],
  );

  return (
    <>
      <Modal
        open={open}
        title={t('插件市场')}
        onCancel={onClose}
        footer={null}
        width={760}
        styles={{ body: { maxHeight: '62vh', overflow: 'auto' } }}
        destroyOnHidden
      >
        {scopeLabel && (
          <div style={{ marginBottom: 12, color: 'var(--color-text-tertiary)', fontSize: 12 }}>
            {scopeLabel}
          </div>
        )}
        <Spin spinning={loading}>
          {availableItems.length === 0 ? (
            <Empty
              description={loading ? t('加载中…') : t('插件市场暂无可安装的插件')}
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            <div className="jx-mcp-grid">
              {availableItems.map((plugin) => (
                <div key={plugin.slug} className="jx-mcp-card">
                  <div className="jx-mcp-cardTop">
                    <div className="jx-mcp-iconWrap jx-mcp-iconFallback">
                      <AppstoreOutlined style={{ color: '#6366f1' }} />
                    </div>
                    <div className="jx-mcp-cardNameGroup">
                      <span className="jx-mcp-cardName">{plugin.name}</span>
                      {plugin.category && <Tag>{plugin.category}</Tag>}
                    </div>
                    <Button
                      type="primary"
                      size="small"
                      loading={busySlug === plugin.slug}
                      style={{ marginLeft: 'auto' }}
                      onClick={() => handleInstall(plugin)}
                    >
                      {busySlug === plugin.slug ? t('安装中') : t('安装并绑定')}
                    </Button>
                  </div>
                  <div className="jx-mcp-cardDesc">{plugin.description}</div>
                  <div style={{ marginTop: 6, display: 'flex', gap: 6 }}>
                    <Tag color="blue">{t('技能')} {plugin.skills_count}</Tag>
                    {(plugin.required_secrets || []).length > 0 && (
                      <Tag color="orange"><KeyOutlined /> {t('需凭据')}</Tag>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Spin>
      </Modal>

      <Modal
        open={!!secretPlugin}
        title={t('配置凭据：{name}', { name: secretPlugin?.name || '' })}
        onCancel={() => setSecretPlugin(null)}
        onOk={() => void submitSecrets()}
        confirmLoading={!!busySlug}
        okText={t('安装并绑定')}
        destroyOnHidden
      >
        <Form form={secretForm} layout="vertical">
          {(secretPlugin?.required_secrets || []).map((rawField) => {
            const field = normalizeSecret(rawField);
            return (
              <Form.Item
                key={field.key}
                name={field.key}
                label={field.label || field.key}
                rules={field.required ? [{
                  required: true,
                  message: t('请输入 {label}', { label: field.label || field.key }),
                }] : []}
              >
                <Input.Password
                  autoComplete="off"
                  placeholder={t('请输入 {label}', { label: field.label || field.key })}
                />
              </Form.Item>
            );
          })}
        </Form>
      </Modal>
    </>
  );
}
