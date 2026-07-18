import { useEffect, useRef } from 'react';
import { Button, Divider, Layout, Typography } from 'antd';
import {
  ArrowLeftOutlined, FileTextOutlined, LinkOutlined, ProfileOutlined, ReloadOutlined,
} from '@ant-design/icons';
import { ApiDocPanel } from './components/apidoc';
import { usePageConfig, usePageConfigPolling } from './hooks/usePageConfig';
import { t } from './i18n';

const { Title, Text } = Typography;
const { Header, Content } = Layout;

export default function ApiDocApp() {
  usePageConfigPolling();
  // Unified admin-platform branding + the "API Docs" tab name (configurable at /config → Page Config → Admin Platform).
  const platformName = usePageConfig('navigation.admin_platform.product_name', 'HugAgentOS');
  const apidocLabel = usePageConfig('navigation.admin_platform.apidoc_label', '接口文档');
  const docTitle = `${platformName} — ${apidocLabel}`;
  // The browser tab title follows the admin-platform branding (index.html's static <title> is only a first-frame fallback).
  useEffect(() => { if (typeof document !== 'undefined') document.title = docTitle; }, [docTitle]);
  const reloadRef = useRef<(() => void) | null>(null);

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{
        background: '#fff',
        borderBottom: '1px solid #E3E6EA',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 32px',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <Title level={5} style={{ margin: 0 }}>
            <ProfileOutlined style={{ marginRight: 8, color: '#126DFF' }} />
            {docTitle}
          </Title>
          <Divider type="vertical" />
          <Text type="secondary">{`${platformName} API`}</Text>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Button
            icon={<ArrowLeftOutlined />}
            size="small"
            onClick={() => { window.location.href = '/config'; }}
          >
            {t('返回 Config')}
          </Button>
          <Button
            icon={<LinkOutlined />}
            size="small"
            onClick={() => window.open('/docs', '_blank')}
          >
            {t('打开 Swagger')}
          </Button>
          <Button
            icon={<FileTextOutlined />}
            size="small"
            onClick={() => window.open('/redoc', '_blank')}
          >
            {t('打开 ReDoc')}
          </Button>
          <Button
            icon={<ReloadOutlined />}
            size="small"
            onClick={() => reloadRef.current?.()}
          >
            {t('刷新')}
          </Button>
        </div>
      </Header>
      <Content style={{ background: '#fff', height: 'calc(100vh - 64px)', overflow: 'hidden' }}>
        <ApiDocPanel onReloadRef={reloadRef} />
      </Content>
    </Layout>
  );
}
