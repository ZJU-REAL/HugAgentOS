import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Pagination,
  Select,
  Spin,
  Tag,
  Typography,
  message,
} from 'antd';
import {
  ApiOutlined,
  KeyOutlined,
  LeftOutlined,
  LoginOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { t } from '../../i18n';
import {
  getMcpMarketItem,
  getMcpMarketItems,
  getMcpMarketOAuthStatus,
  cancelMcpMarketOAuth,
  installMcpMarketItem,
  startMcpMarketOAuth,
} from '../../api';
import type { McpMarketItem } from '../../types';
import { normalizeMcpIconUrl } from '../../utils/iconLibrary';

const PAGE_SIZE = 10;

interface McpMarketplaceModalProps {
  open: boolean;
  canInstall: boolean;
  onClose: () => void;
  onInstalled?: () => void;
}

function riskTag(level: McpMarketItem['risk_level']) {
  const config = {
    low: { color: 'green', label: t('低风险') },
    medium: { color: 'gold', label: t('中风险') },
    high: { color: 'red', label: t('高风险') },
  }[level];
  return <Tag color={config.color}>{config.label}</Tag>;
}

function MarketIcon({ item, size = 40 }: { item: McpMarketItem; size?: number }) {
  const icon = normalizeMcpIconUrl(item.icon);
  const [failedIcon, setFailedIcon] = useState('');
  return (
    <div className="jx-mcp-marketIcon" style={{ width: size, height: size }}>
      {icon && failedIcon !== icon
        ? <img src={icon} alt="" onError={() => setFailedIcon(icon)} />
        : <ApiOutlined />}
    </div>
  );
}

export function McpMarketplaceModal({ open, canInstall, onClose, onInstalled }: McpMarketplaceModalProps) {
  const [items, setItems] = useState<McpMarketItem[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('全部');
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<McpMarketItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [installTarget, setInstallTarget] = useState<McpMarketItem | null>(null);
  const [installing, setInstalling] = useState(false);
  const [installForm] = Form.useForm();
  const selectedAuthMethod = Form.useWatch('auth_method', installForm);
  const oauthAttemptRef = useRef<{
    flowId: string;
    popup: Window | null;
    cancelled: boolean;
  } | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const result = await getMcpMarketItems();
      setItems(result.items || []);
      setCategories(result.categories || []);
    } catch (error) {
      message.error((error as Error).message || t('加载 MCP 市场失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setQuery('');
    setCategory('全部');
    setPage(1);
    setDetail(null);
    void reload();
  }, [open, reload]);

  const openDetail = useCallback(async (slug: string) => {
    setDetailLoading(true);
    try {
      setDetail(await getMcpMarketItem(slug));
    } catch (error) {
      message.error((error as Error).message || t('加载 MCP 详情失败'));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return items.filter((item) => {
      if (category !== '全部' && item.category !== category) return false;
      if (!keyword) return true;
      return `${item.display_name} ${item.description} ${item.tags.join(' ')} ${item.publisher_name}`
        .toLowerCase()
        .includes(keyword);
    });
  }, [items, query, category]);

  const paged = useMemo(
    () => filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [filtered, page],
  );

  useEffect(() => setPage(1), [query, category]);

  const startInstall = useCallback((item: McpMarketItem) => {
    if (!canInstall) return;
    installForm.resetFields();
    installForm.setFieldsValue({ auth_method: item.auth_config?.default_method });
    setInstallTarget(item);
  }, [canInstall, installForm]);

  const cancelInstall = useCallback(() => {
    const attempt = oauthAttemptRef.current;
    if (attempt) {
      attempt.cancelled = true;
      attempt.popup?.close();
      if (attempt.flowId) void cancelMcpMarketOAuth(attempt.flowId).catch(() => undefined);
    }
    setInstalling(false);
    setInstallTarget(null);
  }, []);

  const submitInstall = useCallback(async () => {
    if (!installTarget) return;
    const credentialsManaged = installTarget.credentials_managed_by_admin === true;
    const methodId = selectedAuthMethod || installTarget.auth_config?.default_method;
    const method = installTarget.auth_config?.methods?.find((row) => row.id === methodId);
    const popup = method?.type === 'oauth2' && !credentialsManaged
      ? window.open('about:blank', '_blank', 'width=720,height=760')
      : null;
    let values;
    try {
      values = await installForm.validateFields();
    } catch {
      popup?.close();
      return;
    }
    setInstalling(true);
    const oauthAttempt = method?.type === 'oauth2'
      ? { flowId: '', popup, cancelled: false }
      : null;
    if (oauthAttempt) oauthAttemptRef.current = oauthAttempt;
    try {
      let result: { server_id?: string; action?: string };
      if (method?.type === 'oauth2' && !credentialsManaged) {
        const started = await startMcpMarketOAuth({
          slug: installTarget.slug,
          auth_method: method.id,
          credentials: values.credentials || {},
          client_id: values.oauth_client_id || '',
          client_secret: values.oauth_client_secret || '',
          confirm_high_risk: values.confirm_high_risk === true,
        });
        if (!popup) throw new Error(t('浏览器阻止了 OAuth 登录窗口，请允许弹窗后重试'));
        oauthAttempt!.flowId = started.flow_id;
        if (oauthAttempt!.cancelled) {
          await cancelMcpMarketOAuth(started.flow_id);
          throw new Error(t('OAuth 登录已取消'));
        }
        popup.opener = null;
        popup.location.href = started.authorization_url;
        let status = await getMcpMarketOAuthStatus(started.flow_id);
        for (let attempt = 0; attempt < 300 && !['completed', 'failed'].includes(status.status); attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
          if (oauthAttempt!.cancelled) throw new Error(t('OAuth 登录已取消'));
          status = await getMcpMarketOAuthStatus(started.flow_id);
          if (popup.closed && status.status === 'waiting_for_user') {
            oauthAttempt!.cancelled = true;
            await cancelMcpMarketOAuth(started.flow_id);
            throw new Error(t('OAuth 登录已取消'));
          }
        }
        popup.close();
        if (status.status !== 'completed') throw new Error(status.error || t('OAuth 登录超时或失败'));
        result = status.result || {};
      } else {
        result = await installMcpMarketItem(
          installTarget.slug,
          values.credentials || {},
          values.confirm_high_risk === true,
          methodId,
        );
      }
      message.success(result.action === 'installed'
        ? t('「{name}」已安装到连接器', { name: installTarget.display_name })
        : credentialsManaged
          ? t('「{name}」已重新安装', { name: installTarget.display_name })
          : t('「{name}」的凭据已更新', { name: installTarget.display_name }));
      setItems((current) => current.map((item) => (
        item.slug === installTarget.slug ? { ...item, installed: true } : item
      )));
      setDetail((current) => current?.slug === installTarget.slug
        ? { ...current, installed: true }
        : current);
      setInstallTarget(null);
      onInstalled?.();
    } catch (error) {
      popup?.close();
      const errorMessage = (error as Error).message || t('安装失败');
      if (oauthAttempt?.cancelled || errorMessage === t('OAuth 登录已取消')) {
        message.info(t('OAuth 登录已取消'));
      } else {
        message.error(errorMessage);
      }
    } finally {
      if (oauthAttemptRef.current === oauthAttempt) oauthAttemptRef.current = null;
      setInstalling(false);
    }
  }, [installForm, installTarget, onInstalled, selectedAuthMethod]);

  const installButton = (item: McpMarketItem) => (
    <Button
      type={item.installed ? 'default' : 'primary'}
      size="small"
      disabled={!canInstall || item.status !== 'active'}
      icon={item.installed ? <KeyOutlined /> : undefined}
      onClick={(event) => {
        event.stopPropagation();
        startInstall(item);
      }}
    >
      {item.installed
        ? item.credentials_managed_by_admin ? t('重新安装') : t('更新凭据')
        : canInstall ? t('安装') : t('无安装权限')}
    </Button>
  );

  return (
    <>
      <Modal
        open={open}
        onCancel={onClose}
        footer={null}
        width={920}
        destroyOnHidden
        className="jx-mcp-marketModal"
        title={detail ? (
          <span>
            <Button type="text" size="small" icon={<LeftOutlined />} onClick={() => setDetail(null)} />
            {t('MCP 详情')}
          </span>
        ) : t('MCP 市场')}
      >
        {detailLoading ? (
          <div className="jx-mcp-marketLoading"><Spin /></div>
        ) : detail ? (
          <div className="jx-mcp-marketDetail">
            <div className="jx-mcp-marketDetailHead">
              <MarketIcon item={detail} size={56} />
              <div className="jx-mcp-marketDetailInfo">
                <Typography.Title level={4}>{detail.display_name}</Typography.Title>
                <div>
                  <Tag>{detail.category}</Tag>
                  {riskTag(detail.risk_level)}
                  <Tag color={detail.source === 'admin' ? 'blue' : 'purple'}>
                    {detail.source === 'admin' ? t('平台精选') : t('社区共享')}
                  </Tag>
                  <Typography.Text type="secondary">v{detail.version}</Typography.Text>
                </div>
              </div>
              {installButton(detail)}
            </div>
            <Typography.Paragraph>{detail.description}</Typography.Paragraph>
            <div className="jx-mcp-marketFacts">
              <span><ApiOutlined /> {detail.transport === 'streamable_http' ? 'Streamable HTTP' : 'SSE'}</span>
              <span><SafetyCertificateOutlined /> {t('最近验证：{time}', { time: detail.last_verified_at ? new Date(detail.last_verified_at).toLocaleString() : '-' })}</span>
              {detail.credentials_managed_by_admin
                ? <span><KeyOutlined /> {t('管理员已配置凭据，安装时无需填写')}</span>
                : detail.requires_user_credentials && <span><KeyOutlined /> {t('安装时需配置凭据')}</span>}
            </div>
            {detail.risk_level === 'high' && (
              <Alert
                type="warning"
                showIcon
                message={t('该 MCP 包含高风险操作，安装时需要明确确认')}
                description={(detail.risk_report.high_risk_tools || []).join('、')}
              />
            )}
            <Typography.Title level={5}>{t('工具清单（{n}）', { n: detail.tool_count })}</Typography.Title>
            {detail.risk_report.discovery_mode === 'per_install' && (
              <Typography.Paragraph type="secondary">
                {t('这里展示能力示例；完整工具清单将在使用你的凭据安装时动态发现。')}
              </Typography.Paragraph>
            )}
            <div className="jx-mcp-marketTools">
              {detail.tools.map((tool) => (
                <div key={tool.name} className="jx-mcp-marketTool">
                  <Typography.Text code>{tool.name}</Typography.Text>
                  <Typography.Paragraph type="secondary">{tool.description || t('暂无说明')}</Typography.Paragraph>
                  <details className="jx-mcp-marketSchema">
                    <summary>{t('查看输入参数')}</summary>
                    <pre>{JSON.stringify(tool.inputSchema || {}, null, 2)}</pre>
                  </details>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <>
            <div className="jx-mcp-marketFilters">
              <Input
                allowClear
                prefix={<SearchOutlined />}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t('搜索 MCP 服务或工具')}
              />
              <Select
                value={category}
                onChange={setCategory}
                options={['全部', ...categories].map((value) => ({ value, label: value }))}
              />
            </div>
            {loading ? (
              <div className="jx-mcp-marketLoading"><Spin /></div>
            ) : paged.length === 0 ? (
              <Empty description={t('MCP 市场暂无匹配条目')} />
            ) : (
              <div className="jx-mcp-marketGrid">
                {paged.map((item) => (
                  <div key={item.slug} className="jx-mcp-marketCard" onClick={() => void openDetail(item.slug)}>
                    <MarketIcon item={item} />
                    <div className="jx-mcp-marketCardBody">
                      <div className="jx-mcp-marketCardTitle">
                        <Typography.Text strong>{item.display_name}</Typography.Text>
                        {riskTag(item.risk_level)}
                      </div>
                      <Typography.Paragraph ellipsis={{ rows: 2 }}>{item.description}</Typography.Paragraph>
                      <div className="jx-mcp-marketCardMeta">
                        <span>{item.risk_report.discovery_mode === 'per_install'
                          ? t('安装时发现工具')
                          : `${item.tool_count} ${t('个工具')}`}</span>
                        <span>{item.publisher_name || t('平台')}</span>
                        {item.requires_user_credentials && <KeyOutlined />}
                      </div>
                    </div>
                    {installButton(item)}
                  </div>
                ))}
              </div>
            )}
            {filtered.length > PAGE_SIZE && (
              <Pagination
                current={page}
                pageSize={PAGE_SIZE}
                total={filtered.length}
                showSizeChanger={false}
                onChange={setPage}
              />
            )}
          </>
        )}
      </Modal>

      <Modal
        open={!!installTarget}
        title={installTarget ? t('安装「{name}」', { name: installTarget.display_name }) : ''}
        onCancel={cancelInstall}
        onOk={() => void submitInstall()}
        confirmLoading={installing}
        okText={installTarget?.installed
          ? installTarget.credentials_managed_by_admin ? t('重新安装') : t('更新凭据')
          : t('安装')}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary">
          {installTarget?.credentials_managed_by_admin
            ? t('该 MCP 使用管理员托管凭据，安装时无需填写 Token；凭据仅由后端使用，不会向用户展示。')
            : t('市场不会共享发布者凭据。请填写你自己的认证信息，凭据将加密保存到个人安装实例。')}
        </Typography.Paragraph>
        {installTarget?.risk_report.install_notice && (
          <Alert
            style={{ marginBottom: 16 }}
            type={installTarget.risk_level === 'high' ? 'warning' : 'info'}
            showIcon
            message={installTarget.risk_report.install_notice}
          />
        )}
        <Form form={installForm} layout="vertical">
          {!installTarget?.credentials_managed_by_admin && (installTarget?.auth_config?.methods?.length || 0) > 1 && (
            <Form.Item name="auth_method" label={t('认证方式')} rules={[{ required: true }]}>
              <Select options={installTarget?.auth_config.methods.map((method) => ({
                value: method.id,
                label: method.label,
              }))} />
            </Form.Item>
          )}
          {!installTarget?.credentials_managed_by_admin && installTarget?.auth_config?.methods?.find((method) => method.id === selectedAuthMethod)?.type === 'oauth2' && (
            <Alert
              type="info"
              showIcon
              icon={<LoginOutlined />}
              style={{ marginBottom: 16 }}
              message={installTarget.auth_config.methods.find((method) => method.id === selectedAuthMethod)?.help_text || t('点击安装后将在新窗口完成 OAuth 登录。')}
            />
          )}
          {!installTarget?.credentials_managed_by_admin && installTarget?.auth_config?.methods?.find((method) => method.id === selectedAuthMethod)?.client_id_required && (
            <Form.Item name="oauth_client_id" label="OAuth Client ID" rules={[{ required: true }]}>
              <Input autoComplete="off" />
            </Form.Item>
          )}
          {!installTarget?.credentials_managed_by_admin && installTarget?.auth_config?.methods?.find((method) => method.id === selectedAuthMethod)?.client_secret_required && (
            <Form.Item name="oauth_client_secret" label="OAuth Client Secret" rules={[{ required: true }]}>
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          )}
          {!installTarget?.credentials_managed_by_admin && (installTarget?.auth_schema || []).filter((field) => (
            !field.methods?.length || field.methods.includes(selectedAuthMethod || installTarget?.auth_config?.default_method)
          )).map((field) => (
            <Form.Item
              key={field.key}
              name={['credentials', field.key]}
              label={field.label || field.key}
              extra={(field.help_text || field.doc_url) ? (
                <span>
                  {field.help_text || ''}
                  {field.doc_url && <a href={field.doc_url} target="_blank" rel="noreferrer"> {t('查看获取说明')}</a>}
                </span>
              ) : undefined}
              rules={field.required ? [{ required: true, message: t('请填写该凭据') }] : []}
            >
              {field.secret
                ? <Input.Password autoComplete="new-password" placeholder={field.placeholder} />
                : <Input placeholder={field.placeholder} />}
            </Form.Item>
          ))}
          {installTarget?.risk_level === 'high' && (
            <Form.Item
              name="confirm_high_risk"
              valuePropName="checked"
              rules={[{
                validator: (_, checked) => checked
                  ? Promise.resolve()
                  : Promise.reject(new Error(t('请确认你了解高风险工具的影响'))),
              }]}
            >
              <Checkbox>{t('我了解该 MCP 可能执行删除、发送、发布或其他高风险操作')}</Checkbox>
            </Form.Item>
          )}
        </Form>
      </Modal>
    </>
  );
}
