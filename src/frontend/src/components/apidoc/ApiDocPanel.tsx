import { useCallback, useEffect, useMemo, useState } from 'react';
import { motion } from 'motion/react';
import {
  Button, Collapse, Empty, Input, Select, Space, Spin, Table, Tabs, Tag, Tooltip,
  Typography,
} from 'antd';
import {
  FileSearchOutlined, KeyOutlined, LinkOutlined, LockOutlined, ReloadOutlined,
} from '@ant-design/icons';
import { CopyButton } from '../common/CopyButton';
import { EASE } from '../../utils/motionTokens';
import { t } from '../../i18n';
import { EDITION_API_CATEGORY_RULES } from '../../toolEdition';

const { Text, Paragraph } = Typography;

/* Right-column detail-switch keyed enter params (x offset is not a CSS primitive, keep motion) */
const DETAIL_ENTER = {
  initial: { opacity: 0, x: 6 },
  animate: { opacity: 1, x: 0 },
  transition: { duration: 0.15, ease: EASE.standard },
} as const;

// ===== Types =====

type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

interface OpenApiSchema {
  openapi: string;
  info: { title: string; version: string };
  paths: Record<string, Record<string, OperationObject>>;
  components?: { schemas?: Record<string, unknown> };
}

interface OperationObject {
  tags?: string[];
  summary?: string;
  description?: string;
  operationId?: string;
  parameters?: ParameterObject[];
  requestBody?: { content?: Record<string, { schema?: unknown }> };
  responses?: Record<string, { description?: string; content?: Record<string, { schema?: unknown }> }>;
  security?: unknown[];
}

interface ParameterObject {
  name: string;
  in: 'query' | 'path' | 'header' | 'cookie';
  required?: boolean;
  schema?: any;
  description?: string;
}

interface Endpoint {
  id: string;
  method: HttpMethod;
  path: string;
  summary: string;
  description: string;
  tags: string[];
  operationId?: string;
  parameters: ParameterObject[];
  requestBody: any;
  responses: Record<string, any>;
  requiresAuth: boolean;
  group: string;
  groupOrder: number;
}

interface GroupBucket {
  name: string;
  order: number;
  endpoints: Endpoint[];
}

// ===== Category rules: path prefix → group =====

// Order-sensitive: the first matching rule wins, so more specific prefixes (e.g. /v1/catalog/kb)
// must come before broader prefixes (/v1/catalog). Path shapes follow the backend's real routes.
// Group display order is derived from each group's first appearance in this array (see GROUP_ORDER); no manual ordinals needed.
const CATEGORY_RULES: Array<{ test: RegExp; group: string }> = [
  { test: /^\/(login|register|logout)\b/,                    group: '认证与会话' },
  { test: /^\/mock-sso(\/|$)/,                               group: '认证与会话' },
  { test: /^\/v1\/auth(\/|$)/,                               group: '认证与会话' },
  { test: /^\/v1\/me(\/|$)/,                                 group: '用户与个人中心' },
  { test: /^\/v1\/users(\/|$)/,                              group: '用户与个人中心' },
  { test: /^\/v1\/chats(\/|$)/,                              group: '聊天对话' },
  { test: /^\/v1\/chat-runs(\/|$)/,                          group: '聊天运行' },
  { test: /^\/v1\/chat-shares(\/|$)/,                        group: '会话分享' },
  { test: /^\/v1\/agents(\/|$)/,                             group: '智能体' },
  { test: /^\/v1\/catalog\/kb(\/|$)/,                        group: '知识库' },
  { test: /^\/v1\/catalog(\/|$)/,                            group: '能力目录' },
  { test: /^\/v1\/memories(\/|$)/,                           group: '记忆系统' },
  { test: /^\/v1\/file(\/|$)/,                               group: '文件管理' },
  { test: /^\/files(\/|$)/,                                  group: '文件管理' },
  { test: /^\/v1\/content(\/|$)/,                            group: '内容管理' },
  { test: /^\/v1\/automations(\/|$)/,                        group: '自动化' },
  { test: /^\/v1\/code(\/|$)/,                               group: '代码执行' },
  { test: /^\/v1\/(artifacts|plans)(\/|$)/,                  group: '工件与计划' },
  { test: /^\/v1\/myspace(\/|$)/,                            group: '个人空间' },
  ...EDITION_API_CATEGORY_RULES,
  { test: /^\/v1\/(batch|internal\/batch)(\/|$)/,            group: '批量处理' },
  { test: /^\/v1\/projects(\/|$)/,                           group: '项目' },
  { test: /^\/v1\/(audit|summary|classify)(\/|$)/,           group: '辅助处理' },
  { test: /^\/v1\/(service-configs|models)(\/|$)/,           group: '系统配置' },
  { test: /^\/v1\/config(\/|$)/,                             group: '配置管理' },
  { test: /^\/v1\/admin(\/|$)/,                              group: '管理后台' },
  { test: /^\/(health|ready|live|metrics)\b/,                group: '运维监控' },
  { test: /^\/$/,                                            group: '运维监控' },
];

// Group → display ordinal: assigned by each group's first appearance order in CATEGORY_RULES; auto-shifts when new rules are inserted.
const GROUP_ORDER: Map<string, number> = (() => {
  const m = new Map<string, number>();
  for (const rule of CATEGORY_RULES) {
    if (!m.has(rule.group)) m.set(rule.group, m.size);
  }
  return m;
})();

const METHOD_OPTIONS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];

const METHOD_COLORS: Record<HttpMethod, string> = {
  GET: '#1890ff',
  POST: '#52c41a',
  PUT: '#fa8c16',
  PATCH: '#13c2c2',
  DELETE: '#f5222d',
};

// ===== Helpers =====

function classifyPath(path: string): { group: string; order: number } {
  for (const rule of CATEGORY_RULES) {
    if (rule.test.test(path)) return { group: rule.group, order: GROUP_ORDER.get(rule.group)! };
  }
  return { group: '其他', order: 999 };
}

function detectAuth(op: OperationObject, path: string): boolean {
  if (op.security && op.security.length > 0) return true;
  // FastAPI's Depends-based auth mostly does not land in openapi.security, so fall back to path-prefix inference:
  // first exclude explicitly public endpoints, then mark all other /v1/* as "requires session / Token".
  if (/^\/$/.test(path)) return false;
  if (/^\/(health|ready|live|metrics)\b/.test(path)) return false;
  if (/^\/(login|register)\b/.test(path)) return false;
  if (/^\/mock-sso(\/|$)/.test(path)) return false;
  if (/^\/v1\/auth\/(login|register|sso|callback|mock|providers)/.test(path)) return false;
  if (/^\/v1\//.test(path)) return true;
  return false;
}

function normalizeOpenApi(spec: OpenApiSchema): Endpoint[] {
  const endpoints: Endpoint[] = [];
  for (const [path, methods] of Object.entries(spec.paths || {})) {
    for (const [methodRaw, op] of Object.entries(methods)) {
      const method = methodRaw.toUpperCase() as HttpMethod;
      if (!METHOD_OPTIONS.includes(method)) continue;
      const cls = classifyPath(path);
      endpoints.push({
        id: `${method} ${path}`,
        method,
        path,
        summary: op.summary || '',
        description: op.description || '',
        tags: op.tags || [],
        operationId: op.operationId,
        parameters: op.parameters || [],
        requestBody: op.requestBody,
        responses: op.responses || {},
        requiresAuth: detectAuth(op, path),
        group: cls.group,
        groupOrder: cls.order,
      });
    }
  }
  endpoints.sort((a, b) => {
    if (a.groupOrder !== b.groupOrder) return a.groupOrder - b.groupOrder;
    if (a.path !== b.path) return a.path.localeCompare(b.path);
    return a.method.localeCompare(b.method);
  });
  return endpoints;
}

function MethodTag({ method }: { method: HttpMethod }) {
  return (
    <Tag style={{
      width: 56,
      textAlign: 'center',
      fontFamily: 'monospace',
      fontWeight: 600,
      margin: 0,
      color: '#fff',
      background: METHOD_COLORS[method],
      border: 'none',
      lineHeight: '20px',
    }}>
      {method}
    </Tag>
  );
}

// ===== Schema rendering =====

function resolveRef(ref: string, components: any): any {
  const parts = ref.replace(/^#\//, '').split('/');
  let cur: any = { components };
  for (const p of parts) {
    if (cur && typeof cur === 'object') cur = cur[p];
    else return null;
  }
  return cur;
}

function typeLabel(def: any): string {
  if (!def) return '?';
  if (def.$ref) return def.$ref.split('/').pop() || 'ref';
  if (def.type === 'array') {
    const inner = def.items?.type || (def.items?.$ref ? def.items.$ref.split('/').pop() : '?');
    return `array<${inner}>`;
  }
  if (Array.isArray(def.anyOf)) return 'anyOf';
  if (Array.isArray(def.oneOf)) return 'oneOf';
  if (Array.isArray(def.allOf)) return 'allOf';
  return def.type || (def.format ? def.format : '?');
}

interface SchemaTreeProps {
  schema: any;
  components: any;
  depth?: number;
  visited?: Set<string>;
}

function SchemaTree({ schema, components, depth = 0, visited }: SchemaTreeProps) {
  const v = visited ?? new Set<string>();

  if (!schema) return <Text type="secondary">{t('无')}</Text>;

  if (depth > 4) return <Text type="secondary">{t('…（已折叠，超过 4 层嵌套）')}</Text>;

  // Empty schema {} —— common when FastAPI declares no response_model
  if (typeof schema === 'object' && !schema.$ref && Object.keys(schema).length === 0) {
    return (
      <Text type="secondary">
        任意类型（schema 未限定，通常意味着后端返回标准响应包络 <Text code>{'{ code, message, data, trace_id, timestamp }'}</Text>）
      </Text>
    );
  }

  if (schema.$ref) {
    if (v.has(schema.$ref)) {
      return <Text type="secondary">{t('…（循环引用：{ref}）', { ref: schema.$ref })}</Text>;
    }
    const next = new Set(v);
    next.add(schema.$ref);
    const resolved = resolveRef(schema.$ref, components);
    if (!resolved) return <Text type="secondary">{t('未解析: {ref}', { ref: schema.$ref })}</Text>;
    return <SchemaTree schema={resolved} components={components} depth={depth} visited={next} />;
  }

  if (Array.isArray(schema.allOf)) {
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        {schema.allOf.map((s: any, i: number) => (
          <SchemaTree key={i} schema={s} components={components} depth={depth} visited={v} />
        ))}
      </Space>
    );
  }

  if (Array.isArray(schema.anyOf) || Array.isArray(schema.oneOf)) {
    const list = schema.anyOf || schema.oneOf;
    return (
      <div>
        <Text type="secondary">{t('联合类型（满足任一即可）：')}</Text>
        {list.map((s: any, i: number) => (
          <Collapse size="small" style={{ marginTop: 4 }} key={i}
            items={[{
              key: String(i),
              label: t('选项 {n}：{type}', { n: i + 1, type: typeLabel(s) }),
              children: <SchemaTree schema={s} components={components} depth={depth + 1} visited={v} />,
            }]}
          />
        ))}
      </div>
    );
  }

  if (schema.type === 'array') {
    return (
      <div>
        <Text type="secondary">{t('数组，元素类型：')}</Text>
        <div style={{ marginLeft: 12, marginTop: 4 }}>
          <SchemaTree schema={schema.items} components={components} depth={depth + 1} visited={v} />
        </div>
      </div>
    );
  }

  if (schema.type === 'object' || schema.properties) {
    const props = schema.properties || {};
    const required: string[] = schema.required || [];
    const rows = Object.entries(props).map(([name, def]: [string, any]) => ({
      name,
      def,
      isRequired: required.includes(name),
      typeStr: typeLabel(def),
      isComplex: def.type === 'object' || def.properties || def.$ref || def.type === 'array' || def.anyOf || def.oneOf || def.allOf,
    }));

    if (rows.length === 0) {
      return <Text type="secondary">{schema.additionalProperties ? t('任意键值对（additionalProperties）') : t('空对象')}</Text>;
    }

    return (
      <Table
        dataSource={rows}
        rowKey="name"
        pagination={false}
        size="small"
        columns={[
          {
            title: t('字段'),
            dataIndex: 'name',
            width: 200,
            render: (v: string) => <Text code style={{ fontSize: 12 }}>{v}</Text>,
          },
          {
            title: t('类型'),
            dataIndex: 'typeStr',
            width: 140,
            render: (v: string) => <Tag color="default" style={{ fontFamily: 'monospace' }}>{v}</Tag>,
          },
          {
            title: t('必填'),
            dataIndex: 'isRequired',
            width: 70,
            render: (v: boolean) => v ? <Tag color="red">{t('必填')}</Tag> : <Tag>{t('可选')}</Tag>,
          },
          {
            title: t('说明'),
            render: (_, r: any) => (
              <div>
                {r.def.description && <div style={{ marginBottom: 4 }}>{r.def.description}</div>}
                {Array.isArray(r.def.enum) && (
                  <div style={{ marginBottom: 4 }}>
                    <Text type="secondary">{t('枚举：')}</Text>
                    {r.def.enum.map((e: any, i: number) => (
                      <Tag key={i} style={{ marginRight: 4 }}>{String(e)}</Tag>
                    ))}
                  </div>
                )}
                {r.def.default !== undefined && (
                  <div style={{ marginBottom: 4 }}>
                    <Text type="secondary">{t('默认：')}</Text>
                    <Text code>{JSON.stringify(r.def.default)}</Text>
                  </div>
                )}
                {r.isComplex && (
                  <Collapse size="small" ghost
                    items={[{
                      key: 'nested',
                      label: <Text type="secondary">{t('展开嵌套结构')}</Text>,
                      children: <SchemaTree schema={r.def} components={components} depth={depth + 1} visited={v} />,
                    }]}
                  />
                )}
              </div>
            ),
          },
        ]}
      />
    );
  }

  return (
    <Space size={4} wrap>
      <Tag style={{ fontFamily: 'monospace' }}>{typeLabel(schema)}</Tag>
      {schema.format && <Text type="secondary">format: {schema.format}</Text>}
      {Array.isArray(schema.enum) && (
        <span>{t('枚举：')}{schema.enum.map((e: any, i: number) => <Tag key={i}>{String(e)}</Tag>)}</span>
      )}
      {schema.description && <Text type="secondary">{schema.description}</Text>}
    </Space>
  );
}

// ===== Sample JSON generation =====

function generateSample(schema: any, components: any, visited?: Set<string>): any {
  const v = visited ?? new Set<string>();
  if (!schema) return null;

  if (schema.$ref) {
    if (v.has(schema.$ref)) return null;
    const next = new Set(v);
    next.add(schema.$ref);
    return generateSample(resolveRef(schema.$ref, components), components, next);
  }
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (Array.isArray(schema.enum) && schema.enum.length > 0) return schema.enum[0];
  if (Array.isArray(schema.allOf)) {
    const merged: any = {};
    for (const s of schema.allOf) Object.assign(merged, generateSample(s, components, v) || {});
    return merged;
  }
  if (Array.isArray(schema.anyOf) && schema.anyOf.length > 0) return generateSample(schema.anyOf[0], components, v);
  if (Array.isArray(schema.oneOf) && schema.oneOf.length > 0) return generateSample(schema.oneOf[0], components, v);
  if (schema.type === 'array') return [generateSample(schema.items, components, v)];
  if (schema.type === 'object' || schema.properties) {
    const out: Record<string, any> = {};
    for (const [k, def] of Object.entries(schema.properties || {})) {
      out[k] = generateSample(def, components, v);
    }
    return out;
  }
  switch (schema.type) {
    case 'string':
      if (schema.format === 'date-time') return '2025-01-01T00:00:00Z';
      if (schema.format === 'date') return '2025-01-01';
      if (schema.format === 'uuid') return '00000000-0000-0000-0000-000000000000';
      return 'string';
    case 'integer': return 0;
    case 'number': return 0;
    case 'boolean': return false;
    default: return null;
  }
}

function buildCurl(ep: Endpoint, sample: any): string {
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

function ApiDocDetail({ endpoint, components }: { endpoint: Endpoint; components: any }) {
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
        <Text style={{ fontSize: 14, color: '#262626' }}>{endpoint.summary}</Text>
      )}
      {endpoint.description && endpoint.description !== endpoint.summary && (
        <Paragraph style={{ marginTop: 4, color: '#4D4D4D', whiteSpace: 'pre-wrap' }}>
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
                      background: '#F5F6F7',
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
                    background: '#F5F6F7',
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
        background: '#F5F8FF',
        border: '1px solid #D6E4FF',
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

// ===== Authentication / onboarding guide =====

function CodeLine({ children }: { children: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6 }}>
      <pre style={{
        flex: 1,
        background: '#0F1115',
        color: '#E6E6E6',
        padding: '8px 12px',
        borderRadius: 4,
        fontSize: 12,
        margin: 0,
        overflow: 'auto',
        whiteSpace: 'pre',
      }}>{children}</pre>
      <CopyButton size="small" text={children} />
    </div>
  );
}

/** Top access guide: explains API-Key auth, 401 behavior and the unified response envelope — the OpenAPI schema does not carry these, so they are hand-written. */
function AuthGuide() {
  return (
    <Collapse
      defaultActiveKey={['guide']}
      style={{ margin: '12px 16px 0', background: '#F5F8FF', border: '1px solid #D6E4FF' }}
      items={[{
        key: 'guide',
        label: (
          <Space>
            <KeyOutlined style={{ color: '#126DFF' }} />
            <Text strong>{t('接入指南 · 认证与调用约定')}</Text>
          </Space>
        ),
        children: (
          <div style={{ fontSize: 13, color: '#4D4D4D' }}>
            <Paragraph style={{ marginBottom: 8 }}>
              所有 <Text code>/v1/*</Text> 接口都需要身份认证。支持两种方式，二者皆无（或无效）时返回 <Tag color="orange">401</Tag>，
              <b>绝不会匿名放行</b>。
            </Paragraph>

            <Text strong>{t('方式一 · API-Key（推荐，用于程序化 / 外部调用）')}</Text>
            <Paragraph style={{ marginTop: 4, marginBottom: 4 }}>
              在 <Text strong>「设置 → API-Key」</Text> 创建。明文形如 <Text code>sk-jx-xxxxxxxx</Text>，
              <b>仅创建时显示一次</b>，请妥善保存。调用时放入请求头：
            </Paragraph>
            <CodeLine>{`Authorization: Bearer sk-jx-<YOUR_API_KEY>`}</CodeLine>
            <Paragraph type="secondary" style={{ marginTop: 6, marginBottom: 12, fontSize: 12 }}>
              Key 以调用者的用户身份执行，继承其全部能力（技能 / MCP / 知识库 / 项目权限等）。
              撤销、禁用或过期的 Key 会被拒绝（<Text code>code: 30002</Text>）。需管理员开通 <Text code>can_use_api_key</Text> 权限位。
            </Paragraph>

            <Text strong>{t('方式二 · 会话 Cookie')}</Text>
            <Paragraph style={{ marginTop: 4, marginBottom: 12 }}>
              浏览器 SSO 登录后自动携带 <Text code>jx_session</Text>，前端页面走此方式，无需手动设置。
            </Paragraph>

            <Text strong>{t('完整调用示例')}</Text>
            <CodeLine>{`curl -X POST 'https://<HOST>/api/v1/chats/stream' \\
  -H 'Authorization: Bearer sk-jx-<YOUR_API_KEY>' \\
  -H 'Content-Type: application/json' \\
  -d '{"chat_id":"demo","message":"你好","model_name":"qwen"}'`}</CodeLine>

            <Paragraph style={{ marginTop: 12, marginBottom: 4 }}>
              <Text strong>{t('响应信封')}</Text>：所有 <Text code>/v1</Text> 接口返回统一结构，成功 <Text code>code=10000</Text>：
            </Paragraph>
            <CodeLine>{`{ "code": 10000, "message": "Success", "data": { ... }, "trace_id": "...", "timestamp": 0 }`}</CodeLine>

            <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0, fontSize: 12 }}>
              说明：浏览器实际请求带 <Text code>/api</Text> 前缀（经 nginx 反代到后端）。下方接口列表中标注
              <Tag color="orange" icon={<LockOutlined />} style={{ margin: '0 4px' }}>{t('需要鉴权')}</Tag>
              的均需携带上述任一凭证。
            </Paragraph>
          </div>
        ),
      }]}
    />
  );
}

// ===== Main panel =====

interface ApiDocPanelProps {
  /** A ref passed in by the parent component, filled with a reload function; lets the Header's "refresh" button trigger a re-fetch of the schema. */
  onReloadRef?: { current: (() => void) | null };
}

export function ApiDocPanel({ onReloadRef }: ApiDocPanelProps = {}) {
  const [spec, setSpec] = useState<OpenApiSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [methods, setMethods] = useState<HttpMethod[]>([]);
  const [activeGroup, setActiveGroup] = useState<string>('');
  const [activeEndpointId, setActiveEndpointId] = useState<string>('');

  const loadSpec = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // /api/openapi.json: reverse-proxied to the backend's /openapi.json via nginx
      // /openapi.json: used when connecting directly to the backend locally
      const candidates = ['/api/openapi.json', '/openapi.json'];
      let data: OpenApiSchema | null = null;
      let lastErr: string | null = null;
      for (const url of candidates) {
        try {
          const res = await fetch(url);
          if (!res.ok) {
            lastErr = `${url}: HTTP ${res.status}`;
            continue;
          }
          data = await res.json();
          break;
        } catch (e: any) {
          lastErr = `${url}: ${e?.message || e}`;
        }
      }
      if (!data) throw new Error(lastErr || '无法获取 openapi.json');
      setSpec(data);
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSpec();
    if (onReloadRef) onReloadRef.current = loadSpec;
    return () => {
      if (onReloadRef) onReloadRef.current = null;
    };
  }, [loadSpec, onReloadRef]);

  const allEndpoints = useMemo(() => spec ? normalizeOpenApi(spec) : [], [spec]);

  const filteredEndpoints = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allEndpoints.filter(ep => {
      if (methods.length > 0 && !methods.includes(ep.method)) return false;
      if (q) {
        const blob = `${ep.path} ${ep.summary} ${ep.description} ${ep.operationId || ''}`.toLowerCase();
        if (!blob.includes(q)) return false;
      }
      return true;
    });
  }, [allEndpoints, methods, search]);

  const groups = useMemo<GroupBucket[]>(() => {
    const map = new Map<string, GroupBucket>();
    for (const ep of filteredEndpoints) {
      let bucket = map.get(ep.group);
      if (!bucket) {
        bucket = { name: ep.group, order: ep.groupOrder, endpoints: [] };
        map.set(ep.group, bucket);
      }
      bucket.endpoints.push(ep);
    }
    return Array.from(map.values()).sort((a, b) => a.order - b.order);
  }, [filteredEndpoints]);

  useEffect(() => {
    setActiveGroup(prev => {
      if (groups.length === 0) return '';
      if (groups.some(g => g.name === prev)) return prev;
      return groups[0].name;
    });
  }, [groups]);

  const groupEndpoints = useMemo(
    () => groups.find(g => g.name === activeGroup)?.endpoints || [],
    [groups, activeGroup],
  );

  useEffect(() => {
    setActiveEndpointId(prev => {
      if (groupEndpoints.length === 0) return '';
      if (prev && groupEndpoints.some(e => e.id === prev)) return prev;
      return groupEndpoints[0].id;
    });
  }, [groupEndpoints]);

  const activeEndpoint = groupEndpoints.find(e => e.id === activeEndpointId) || null;

  const totalCount = allEndpoints.length;
  const groupCount = useMemo(() => {
    const set = new Set<string>();
    for (const ep of allEndpoints) set.add(ep.group);
    return set.size;
  }, [allEndpoints]);

  if (loading && !spec) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', minHeight: 400 }}>
        <Spin tip={t('加载接口文档…')} size="large" />
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Empty
          description={
            <Space direction="vertical">
              <Text type="danger">{t('无法加载接口文档')}</Text>
              <Text type="secondary">{error}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {t('请确认后端服务可访问，且 /api/openapi.json 路径可用。')}
              </Text>
              <Button icon={<ReloadOutlined />} onClick={loadSpec}>{t('重试')}</Button>
            </Space>
          }
        />
      </div>
    );
  }

  const components = spec?.components || {};

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', background: '#fff' }}>
      {/* Access guide: authentication and call conventions */}
      <AuthGuide />

      {/* Filter bar */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '12px 16px',
        borderBottom: '1px solid #E3E6EA',
        background: '#fff',
        flexShrink: 0,
      }}>
        <Input
          allowClear
          prefix={<FileSearchOutlined />}
          placeholder={t('搜索路径、摘要、描述、operationId')}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ maxWidth: 360 }}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder={t('筛选方法')}
          value={methods}
          onChange={(v) => setMethods(v as HttpMethod[])}
          style={{ minWidth: 240 }}
          options={METHOD_OPTIONS.map(m => ({ value: m, label: m }))}
        />
        <div style={{ marginLeft: 'auto', color: '#808080', fontSize: 13 }}>
          {/* Keyed fade transition when the count changes */}
          <span key={`${totalCount}-${filteredEndpoints.length}`} className="jx-apidoc-countFade">
            {t('总计 {total} 接口 / {groups} 分组', { total: totalCount, groups: groupCount })}
            {filteredEndpoints.length !== totalCount && (
              <> · {t('当前筛选 {n}', { n: filteredEndpoints.length })}</>
            )}
          </span>
        </div>
      </div>

      {/* Three-column body */}
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        {/* Left: groups */}
        <div style={{
          width: 240,
          borderRight: '1px solid #E3E6EA',
          overflow: 'auto',
          background: '#FAFAFA',
          flexShrink: 0,
        }}>
          {groups.map(g => (
            <div
              key={g.name}
              onClick={() => setActiveGroup(g.name)}
              className={`jx-apidoc-navItem jx-apidoc-groupItem${activeGroup === g.name ? ' active' : ''}`}
            >
              <span>{g.name}</span>
              <Tag color={activeGroup === g.name ? 'blue' : 'default'} style={{ margin: 0 }}>
                {g.endpoints.length}
              </Tag>
            </div>
          ))}
          {groups.length === 0 && (
            <div style={{ padding: 24 }}>
              <Empty description={t('无匹配分组')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
          )}
        </div>

        {/* Middle: endpoints */}
        <div style={{
          width: 420,
          borderRight: '1px solid #E3E6EA',
          overflow: 'auto',
          background: '#fff',
          flexShrink: 0,
        }}>
          {groupEndpoints.map(ep => (
            <div
              key={ep.id}
              onClick={() => setActiveEndpointId(ep.id)}
              className={`jx-apidoc-navItem jx-apidoc-epItem${activeEndpointId === ep.id ? ' active' : ''}`}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <MethodTag method={ep.method} />
                <Text style={{
                  fontFamily: 'monospace',
                  fontSize: 12,
                  flex: 1,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {ep.path}
                </Text>
                {ep.requiresAuth && (
                  <Tooltip title={t('需要鉴权')}>
                    <LockOutlined style={{ color: '#fa8c16', fontSize: 12 }} />
                  </Tooltip>
                )}
              </div>
              {ep.summary && (
                <div style={{
                  marginTop: 4,
                  marginLeft: 64,
                  color: '#4D4D4D',
                  fontSize: 12,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}>
                  {ep.summary}
                </div>
              )}
            </div>
          ))}
          {groupEndpoints.length === 0 && (
            <div style={{ padding: 24 }}>
              <Empty description={t('无匹配接口')} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </div>
          )}
        </div>

        {/* Right: detail */}
        <div style={{ flex: 1, overflow: 'auto', padding: 24, background: '#fff', minWidth: 0 }}>
          {activeEndpoint ? (
            /* Detail-switch keyed enter: animation plays only once on the outer container */
            <motion.div key={activeEndpoint.id} {...DETAIL_ENTER}>
              <ApiDocDetail endpoint={activeEndpoint} components={components} />
            </motion.div>
          ) : (
            <Empty description={t('请选择接口查看详情')} />
          )}
        </div>
      </div>
    </div>
  );
}
