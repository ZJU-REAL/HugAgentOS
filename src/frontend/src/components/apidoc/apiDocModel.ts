import { EDITION_API_CATEGORY_RULES } from '../../toolEdition';

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
export interface SchemaDefinition {
  $ref?: string; type?: string; format?: string; description?: string;
  properties?: Record<string, SchemaDefinition>; required?: string[]; items?: SchemaDefinition;
  anyOf?: SchemaDefinition[]; oneOf?: SchemaDefinition[]; allOf?: SchemaDefinition[];
  example?: JsonValue; default?: JsonValue; enum?: JsonValue[];
  additionalProperties?: boolean | SchemaDefinition;
}
export interface ApiComponents { schemas?: Record<string, SchemaDefinition> }
export interface ApiResponse { description?: string; content?: Record<string, { schema?: SchemaDefinition }> }
export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

export interface OpenApiSchema {
  openapi: string;
  info: { title: string; version: string };
  paths: Record<string, Record<string, OperationObject>>;
  components?: ApiComponents;
}

export interface OperationObject {
  tags?: string[];
  summary?: string;
  description?: string;
  operationId?: string;
  parameters?: ParameterObject[];
  requestBody?: { content?: Record<string, { schema?: SchemaDefinition }> };
  responses?: Record<string, { description?: string; content?: Record<string, { schema?: SchemaDefinition }> }>;
  security?: unknown[];
}

export interface ParameterObject {
  name: string;
  in: 'query' | 'path' | 'header' | 'cookie';
  required?: boolean;
  schema?: SchemaDefinition;
  description?: string;
}

export interface Endpoint {
  id: string;
  method: HttpMethod;
  path: string;
  summary: string;
  description: string;
  tags: string[];
  operationId?: string;
  parameters: ParameterObject[];
  requestBody?: OperationObject['requestBody'];
  responses: Record<string, ApiResponse>;
  requiresAuth: boolean;
  group: string;
  groupOrder: number;
}

export interface GroupBucket {
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

export const METHOD_OPTIONS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];

export const METHOD_COLORS: Record<HttpMethod, string> = {
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

export function normalizeOpenApi(spec: OpenApiSchema): Endpoint[] {
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
