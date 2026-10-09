import { useState } from 'react';
import { Alert, Input, Modal, Table, Tag } from 'antd';
import { ApplicationDataPanel } from './ApplicationDataPanel';
import type { ApplicationView } from './ApplicationCard';
import { applicationMcpUrl } from './applicationFormatting';
import { isMcpApplication, type Application } from './applicationApi';
import { t } from '../../i18n';

export function ApplicationManageModal({ app, view, onClose, onChanged }: {
  app: Application; view: ApplicationView; onClose: () => void; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const isMcp = isMcpApplication(app);
  return <Modal open wrapClassName="jx-sites-manageModal" width={720} footer={null}
    title={`${isMcp ? t('MCP 服务') : t('应用数据库')} — ${app.title}`} onCancel={onClose} closable={!busy} maskClosable={!busy} keyboard={!busy} destroyOnHidden>
    {view === 'access' ? <div className="jx-sites-connection">
      <Tag color={app.mcp_enabled ? 'green' : 'default'}>{app.mcp_enabled ? t('已发布') : t('未发布')}</Tag>
      <label>{t('MCP 地址')}<Input readOnly value={applicationMcpUrl(app.id)} /></label>
      <Alert type="info" showIcon message={t('使用支持 Streamable HTTP 的 MCP 客户端连接此地址，并设置 Bearer 访问凭据')} />
      {!app.mcp_enabled && <Alert type="warning" showIcon message={t('该 MCP 尚未发布或已撤销，请在管理中重新发布')} />}
      <Table size="small" rowKey="name" pagination={false} dataSource={app.tools} scroll={{ x: true }}
        columns={[{ title: t('工具'), dataIndex: 'name' }, { title: t('说明'), dataIndex: 'description' }]} />
    </div> : <ApplicationDataPanel applicationId={app.id} initialTab={isMcp ? 'mcp' : 'data'}
      editOnOpen={view === 'edit' && isMcp} onChanged={onChanged} onBusyChange={setBusy} />}
  </Modal>;
}
