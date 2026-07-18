/** 英文字典（apidoc 域）：key 为中文原文，value 为英文译文。 */
export const APIDOC_DICT: Record<string, string> = {
  // ApiDocPanel — SchemaTree
  '…（已折叠，超过 4 层嵌套）': '… (collapsed, exceeds 4 levels of nesting)',
  '…（循环引用：{ref}）': '… (circular ref: {ref})',
  '未解析: {ref}': 'Unresolved: {ref}',
  '联合类型（满足任一即可）：': 'Union type (any of):',
  '选项 {n}：{type}': 'Option {n}: {type}',
  '数组，元素类型：': 'Array, element type:',
  '任意键值对（additionalProperties）': 'Any key-value pairs (additionalProperties)',
  '空对象': 'Empty object',
  '字段': 'Field',
  '枚举：': 'Enum:',
  '默认：': 'Default:',
  '展开嵌套结构': 'Expand nested structure',

  // ApiDocPanel — ParamGroup columns

  // ApiDocPanel — Tabs / detail
  '参数 ({n})': 'Parameters ({n})',
  '无参数': 'No parameters',
  'Path 参数': 'Path Parameters',
  'Query 参数': 'Query Parameters',
  'Header 参数': 'Header Parameters',
  '请求体': 'Request Body',
  '无请求体': 'No request body',
  '响应 ({n})': 'Responses ({n})',
  '无响应定义': 'No response defined',
  '无响应体': 'No response body',
  '示例': 'Examples',
  '需要鉴权': 'Auth Required',
  '请求体示例：': 'Request Body Example:',
  'cURL 示例：': 'cURL Example:',
  '在 Swagger 中调试此接口': 'Debug in Swagger',
  '想试调用此接口？': 'Want to try this endpoint?',

  // ApiDocPanel — AuthGuide
  '接入指南 · 认证与调用约定': 'Integration Guide · Auth & Calling Convention',
  '方式一 · API-Key（推荐，用于程序化 / 外部调用）': 'Method 1 · API-Key (recommended for programmatic/external calls)',
  '方式二 · 会话 Cookie': 'Method 2 · Session Cookie',
  '完整调用示例': 'Full Call Example',
  '响应信封': 'Response Envelope',

  // ApiDocPanel — loading / error states
  '加载接口文档…': 'Loading API docs…',
  '无法加载接口文档': 'Failed to load API docs',
  '请确认后端服务可访问，且 /api/openapi.json 路径可用。': 'Please ensure the backend service is reachable and /api/openapi.json is accessible.',
  '重试': 'Retry',

  // ApiDocPanel — filter bar
  '搜索路径、摘要、描述、operationId': 'Search path, summary, description, operationId',
  '筛选方法': 'Filter by method',
  '总计 {total} 接口 / {groups} 分组': 'Total {total} endpoints / {groups} groups',
  '当前筛选 {n}': 'Filtered: {n}',

  // ApiDocPanel — lists
  '无匹配分组': 'No matching groups',
  '无匹配接口': 'No matching endpoints',
  '请选择接口查看详情': 'Select an endpoint to view details',

  // ApiDocApp
  'HugAgentOS — 接口文档': 'HugAgentOS — API Docs',
  '返回 Config': 'Back to Config',
  '打开 Swagger': 'Open Swagger',
  '打开 ReDoc': 'Open ReDoc',

  // SharePreviewApp
  '链接已失效': 'Link Expired',
  '该分享链接已失效，或内容已不可用。': 'This share link has expired or the content is no longer available.',
  '打印': 'Print',
  '内容由AI生成，请注意甄别': 'Content is AI-generated. Please verify before use.',
  '会话分享': ' — Chat Share',
  '会话分享：': ' — Chat Share: ',
  '会话分享：链接已失效': ' — Chat Share: Link Expired',
  '会话分享页': ' Chat Share',
  '分享 ID：{id}': 'Share ID: {id}',
  '用户: {name}': 'User: {name}',

};
