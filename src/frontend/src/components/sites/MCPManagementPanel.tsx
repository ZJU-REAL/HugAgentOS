import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Input, Modal, Popconfirm, Space, Table, Tag, message } from 'antd';
import { applicationRequest, type Application, type MCPPublishReceipt, type MCPToolDefinition, type Target } from './applicationApi';
import { applicationMcpUrl } from './applicationFormatting';
import { MCPToolsEditor } from './MCPToolsEditor';
import { t } from '../../i18n';

export function MCPManagementPanel({ app, target, editOnOpen = false, onChanged, onBusyChange }: {
  app: Application; target: Target; editOnOpen?: boolean;
  onChanged: () => Promise<void>; onBusyChange?: (busy: boolean) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(editOnOpen);
  const [credential, setCredential] = useState<string | null>(null);
  const [partial, setPartial] = useState(false);
  const [sourcePartial, setSourcePartial] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const mutate = async (tools?: MCPToolDefinition[]) => {
    setBusy(true); onBusyChange?.(true);
    try {
      const result = await applicationRequest<MCPPublishReceipt>(`/v1/applications/${app.id}/mcp`,
        { method: tools ? 'POST' : 'DELETE', ...(tools ? { body: JSON.stringify({ tools }) } : {}) }, target);
      // Refresh the cards even if the user navigated away while the server saved.
      await onChanged();
      if (!mounted.current) return;
      setEditing(false);
      setCredential(result.token || null);
      setSourcePartial(result.project_synced === false);
      setPartial(result.installed === false || result.connection_verified === false);
      message.success(t('已保存'));
    } catch (failure) {
      if (mounted.current) message.error(failure instanceof Error ? failure.message : t('操作失败'));
    } finally {
      if (mounted.current) setBusy(false);
      onBusyChange?.(false);
    }
  };
  return <Space orientation="vertical" style={{ width: '100%' }}>
    <Tag color={app.mcp_enabled ? 'green' : 'default'}>{app.mcp_enabled ? t('已发布') : t('未发布')}</Tag>
    <Input readOnly aria-label={t('MCP 地址')} value={applicationMcpUrl(app.id)} />
    <Table size="small" rowKey="name" pagination={false} dataSource={app.tools} scroll={{ x: true }}
      columns={[{ title: t('工具'), dataIndex: 'name' }, { title: t('说明'), dataIndex: 'description' }]} />
    <Space wrap>
      <Button disabled={busy || !Object.keys(app.tables).length} onClick={() => setEditing(true)}>{t('发布或更新 MCP')}</Button>
      <Popconfirm title={t('撤销 MCP 访问')} description={t('撤销后所有客户端将无法访问此 MCP，确定撤销？')}
        okText={t('确认')} cancelText={t('取消')} okButtonProps={{ danger: true }}
        onConfirm={() => mutate()}>
        <Button danger loading={busy && !editing} disabled={busy || !app.mcp_enabled}>{t('撤销 MCP 访问')}</Button>
      </Popconfirm>
    </Space>
    {!Object.keys(app.tables).length && <Alert type="info" message={t('请先在数据页定义数据表，再创建 MCP 工具')} />}
    {sourcePartial && <Alert type="warning" showIcon message={t('MCP 已发布，但项目定义同步失败，请在项目中核对版本后继续编辑')} />}
    {partial && <Alert type="warning" showIcon message={t('MCP 已发布，但个人连接验证未完成，请重新发布以修复连接')} />}
    <Alert type="info" message={t('MCP 只提供已定义字段的查询；凭据更新后旧凭据立即失效')} />
    {editing && <MCPToolsEditor app={app} busy={busy} onClose={() => setEditing(false)} onSave={mutate} />}
    <Modal title={t('保存 MCP 访问凭据')} okText={t('确认')} cancelText={t('取消')} open={!!credential} onCancel={() => setCredential(null)}
      onOk={() => setCredential(null)}>
      <Alert type="warning" message={t('凭据仅显示本次；请保存到客户端，勿写入网页或分享给访客')} />
      <Input readOnly aria-label={t('MCP 地址')} value={applicationMcpUrl(app.id)} />
      <Input.Password readOnly aria-label={t('MCP 访问凭据')} value={credential || ''} />
    </Modal>
  </Space>;
}
