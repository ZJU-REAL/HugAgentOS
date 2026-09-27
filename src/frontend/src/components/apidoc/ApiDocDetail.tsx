import { useMemo } from 'react';
import { Button, Empty, Space, Table, Tabs, Tag, Typography } from 'antd';
import { LinkOutlined, LockOutlined } from '@ant-design/icons';
import { CopyButton } from '../common/CopyButton';
import { t } from '../../i18n';
import { type Endpoint, type ParameterObject, type ApiComponents, type JsonValue } from './apiDocModel';
import { SchemaTree } from './ApiDocSchema';
import { generateSample, typeLabel } from './apiDocSchemaHelpers';
import { MethodTag } from './MethodTag';
const { Text, Paragraph } = Typography;

function buildCurl(ep: Endpoint, sample: JsonValue): string {
  // Paths uniformly carry the /api prefix (reverse-proxied to the backend via nginx), matching the browser's actual calls.
  let cmd = `curl -X ${ep.method} 'https://<HOST>/api${ep.path}'`;
  if (ep.requiresAuth) cmd += ` \\\n  -H 'Authorization: Bearer sk-jx-<YOUR_API_KEY>'`;
  if (sample !== null && sample !== undefined && ['POST', 'PUT', 'PATCH'].includes(ep.method)) {
    cmd += ` \\\n  -H 'Content-Type: application/json'`;
    cmd += ` \\\n  -d '${JSON.stringify(sample)}'`;
  }
  return cmd;
}

// ===== Sub-components =====

function ParamGroup({ title, params }: { title: string; params: ParameterObject[] }) {
  return (
    <div>
      <Text strong style={{ display: 'block', marginBottom: 8 }}>{title}</Text>
      <Table
        size="small"
        rowKey="name"
        pagination={false}
        dataSource={params}
        columns={[
          { title: t('名称'), dataIndex: 'name', width: 200, render: (v: string) => <Text code>{v}</Text> },
          { title: t('类型'), width: 140, render: (_, r: ParameterObject) => <Tag style={{ fontFamily: 'monospace' }}>{typeLabel(r.schema)}</Tag> },
          { title: t('必填'), dataIndex: 'required', width: 70, render: (v: boolean) => v ? <Tag color="red">{t('必填')}</Tag> : <Tag>{t('可选')}</Tag> },
          { title: t('说明'), dataIndex: 'description', render: (v: string) => v || <Text type="secondary">—</Text> },
        ]}
      />
    </div>
  );
}

export function ApiDocDetail({ endpoint, components }: { endpoint: Endpoint; components: ApiComponents }) {
  const swaggerUrl = useMemo(() => {
    const tag = endpoint.tags[0] || 'default';
    if (endpoint.operationId) return `/docs#/${tag}/${endpoint.operationId}`;
    return '/docs';
  }, [endpoint]);

  const pathParams = endpoint.parameters.filter(p => p.in === 'path');
  const queryParams = endpoint.parameters.filter(p => p.in === 'query');
  const headerParams = endpoint.parameters.filter(p => p.in === 'header');

  const requestBodySchema = endpoint.requestBody?.content?.['application/json']?.schema;
  const responseEntries = Object.entries(endpoint.responses || {});

  const sample = useMemo(
    () => requestBodySchema ? generateSample(requestBodySchema, components) : null,
    [requestBodySchema, components],
  );

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <MethodTag method={endpoint.method} />
        <Text copyable style={{ fontFamily: 'monospace', fontSize: 16, fontWeight: 600 }}>
          {endpoint.path}
        </Text>
        {endpoint.requiresAuth && <Tag color="orange" icon={<LockOutlined />} style={{ marginLeft: 4 }}>{t('需要鉴权')}</Tag>}
      </div>

      {endpoint.summary && (
        <Text style={{ fontSize: 14, color: 'var(--color-text)' }}>{endpoint.summary}</Text>
      )}
      {endpoint.description && endpoint.description !== endpoint.summary && (
        <Paragraph style={{ marginTop: 4, color: 'var(--color-text-secondary)', whiteSpace: 'pre-wrap' }}>
          {endpoint.description}
        </Paragraph>
      )}

      <div style={{ marginTop: 8, marginBottom: 16 }}>
        <Space wrap size={[4, 4]}>
          {endpoint.tags.map(t => <Tag key={t}>{t}</Tag>)}
          {endpoint.operationId && (
            <Tag color="blue" style={{ fontFamily: 'monospace' }}>operationId: {endpoint.operationId}</Tag>
          )}
        </Space>
      </div>

      <Tabs
        items={[
          {
            key: 'params',
            label: t('参数 ({n})', { n: endpoint.parameters.length }),
            children: endpoint.parameters.length === 0 ? (
              <Empty description={t('无参数')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Space direction="vertical" style={{ width: '100%' }} size="middle">
                {pathParams.length > 0 && <ParamGroup title={t('Path 参数')} params={pathParams} />}
                {queryParams.length > 0 && <ParamGroup title={t('Query 参数')} params={queryParams} />}
                {headerParams.length > 0 && <ParamGroup title={t('Header 参数')} params={headerParams} />}
              </Space>
            ),
          },
          {
            key: 'body',
            label: t('请求体'),
            children: requestBodySchema ? (
              <SchemaTree schema={requestBodySchema} components={components} />
            ) : (
              <Empty description={t('无请求体')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ),
          },
          {
            key: 'response',
            label: t('响应 ({n})', { n: responseEntries.length }),
            children: responseEntries.length === 0 ? (
              <Empty description={t('无响应定义')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            ) : (
              <Tabs
                size="small"
                items={responseEntries.map(([code, resp]) => ({
                  key: code,
                  label: (
                    <span>
                      <Tag color={code.startsWith('2') ? 'green' : code.startsWith('4') ? 'orange' : code.startsWith('5') ? 'red' : 'default'} style={{ marginRight: 4 }}>
                        {code}
                      </Tag>
                      {resp.description && <Text type="secondary" style={{ fontSize: 12 }}>{resp.description}</Text>}
                    </span>
                  ),
                  children: (() => {
                    const schema = resp.content?.['application/json']?.schema;
                    return schema ? (
                      <SchemaTree schema={schema} components={components} />
                    ) : (
                      <Text type="secondary">{t('无响应体')}</Text>
                    );
                  })(),
                }))}
              />
            ),
          },
          {
            key: 'sample',
            label: t('示例'),
            children: (
              <Space direction="vertical" style={{ width: '100%' }} size="large">
                {sample !== null && sample !== undefined && (
                  <div>
                    <div style={{ marginBottom: 8 }}>
                      <Text strong>{t('请求体示例：')}</Text>
                      <CopyButton
                        size="small"
                        style={{ marginLeft: 8 }}
                        text={() => JSON.stringify(sample, null, 2)}
                      >
                        {t('复制')}
                      </CopyButton>
                    </div>
                    <pre style={{
                      background: 'var(--color-bg-layout)',
                      padding: 12,
                      borderRadius: 4,
                      fontSize: 12,
                      overflow: 'auto',
                      maxHeight: 320,
                      margin: 0,
                    }}>
                      {JSON.stringify(sample, null, 2)}
                    </pre>
                  </div>
                )}
                <div>
                  <div style={{ marginBottom: 8 }}>
                    <Text strong>{t('cURL 示例：')}</Text>
                    <CopyButton
                      size="small"
                      style={{ marginLeft: 8 }}
                      text={() => buildCurl(endpoint, sample)}
                    >
                      {t('复制')}
                    </CopyButton>
                  </div>
                  <pre style={{
                    background: 'var(--color-bg-layout)',
                    padding: 12,
                    borderRadius: 4,
                    fontSize: 12,
                    overflow: 'auto',
                    margin: 0,
                  }}>
                    {buildCurl(endpoint, sample)}
                  </pre>
                </div>
              </Space>
            ),
          },
        ]}
      />

      <div style={{
        marginTop: 24,
        padding: 12,
        background: 'var(--color-primary-light)',
        border: '1px solid var(--color-border)',
        borderRadius: 6,
      }}>
        <Space>
          <Text type="secondary">{t('想试调用此接口？')}</Text>
          <Button
            type="primary"
            size="small"
            icon={<LinkOutlined />}
            onClick={() => window.open(swaggerUrl, '_blank')}
          >
            {t('在 Swagger 中调试此接口')}
          </Button>
        </Space>
      </div>
    </div>
  );
}
