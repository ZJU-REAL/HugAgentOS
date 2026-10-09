import { Button, Tag, message } from 'antd';
import { ApiOutlined, DatabaseOutlined, EditOutlined, LinkOutlined, SettingOutlined } from '@ant-design/icons';
import { isMcpApplication, type Application } from './applicationApi';
import { HostedResourceCard } from './HostedResourceCard';
import { applicationMcpUrl } from './applicationFormatting';
import { copyToClipboard } from '../../utils/clipboard';
import { t } from '../../i18n';

export type ApplicationView = 'access' | 'edit' | 'manage';

export function ApplicationCard({ app, siteTitle, onOpen, editing = false }: {
  app: Application; siteTitle?: string; editing?: boolean; onOpen: (view: ApplicationView) => void;
}) {
  const isMcp = isMcpApplication(app);
  const url = applicationMcpUrl(app.id);
  const copy = async () => {
    if (await copyToClipboard(url)) message.success(t('链接已复制'));
    else message.error(t('复制失败，请手动复制'));
  };
  return <HostedResourceCard title={app.title}
    tags={<>
      <Tag icon={isMcp ? <ApiOutlined /> : <DatabaseOutlined />}>{isMcp ? t('MCP 服务') : t('应用数据库')}</Tag>
      {isMcp && <Tag color={app.mcp_enabled ? 'green' : 'default'}>{app.mcp_enabled ? t('已发布') : t('未发布')}</Tag>}
    </>}
    address={isMcp ? <button type="button" className="jx-sites-cardUrl jx-sites-addressButton"
      onClick={() => onOpen('access')}>{url}</button> : undefined}
    meta={<>
      {isMcp && <>{t('版本')} v{app.mcp_version ?? 0} · {app.tools.length} {t('个工具')} · </>}
      {Object.keys(app.tables).length} {t('个数据表')}
      {siteTitle ? ` · ${t('关联站点')}：${siteTitle}` : ''}
    </>}
    actions={<>
      <Button size="small" type="primary" ghost onClick={() => onOpen(isMcp ? 'access' : 'manage')}>{t('打开')}</Button>
      {isMcp && <Button size="small" icon={<LinkOutlined />} onClick={() => void copy()}>{t('复制链接')}</Button>}
      <Button size="small" icon={<EditOutlined />} loading={editing} onClick={() => onOpen('edit')}>{t('编辑')}</Button>
      <Button size="small" icon={<SettingOutlined />} onClick={() => onOpen('manage')}>{t('管理')}</Button>
    </>}
  />;
}
