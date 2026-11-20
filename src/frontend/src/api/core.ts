import type { ChatItem, PlanProgressState } from '../types';
import { API_BASE } from '../utils/apiBase';
import { t } from '../i18n';
import { readErrorMessage, createApiResponseError } from '../utils/apiError';
import { conversationTitle } from '../utils/conversationTitle';
import { publicChatHeaders } from '../publicChatTransport';
import { createEditionAccessError } from '../editionAccessError';
import type { KBDocumentItem } from './knowledge';

/**
 * API client infrastructure shared by every domain module in this folder: response envelope types,
 * base URL, desktop dual-mode (hybrid) request routing, upload limits, session mapping, the global
 * 401 handler, `apiRequest` and `authFetch`. Callers import through `src/api.ts`.
 */

export type JsonObject = Record<string, unknown>;

interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
  trace_id?: string;
  timestamp?: number;
}

export interface Pagination {
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
  has_previous: boolean;
  has_next: boolean;
}

export interface PaginatedData<T> {
  items: T[];
  pagination: Pagination;
}

export interface CatalogItem {
  id: string;
  name: string;
  desc: string;
  enabled: boolean;
  tags?: string[];
  detail?: string;
  [key: string]: unknown;
}

export interface CatalogResponse {
  skills: CatalogItem[];
  agents: CatalogItem[];
  mcp: CatalogItem[];
  kb: CatalogItem[];
}

export interface KBDocumentsResponse {
  items: KBDocumentItem[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  status_counts?: KBDocumentStatusCounts;
}

export interface KBDocumentStatusCounts {
  indexed: number;
  processing: number;
  failed: number;
}

export interface SessionListResponse {
  items: ChatItem[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface CreateSessionRequest {
  metadata?: Record<string, unknown>;
  title?: string;
  business_topic?: string;
}

export interface UpdateSessionRequest {
  title?: string;
  pinned?: boolean;
  favorite?: boolean;
  business_topic?: string;
}

export interface UserPreferences {
  default_model?: string;
  language?: string;
  theme?: string;
  enabled_skills?: string[];
  enabled_mcps?: string[];
}

export interface AddArtifactToKBResult {
  document_id: string;
  kb_id: string;
  title: string;
  filename: string;
  size_bytes: number;
  uploaded_at: string;
  already_exists?: boolean;
}

export const getApiUrl = (): string => API_BASE;
/** Absolute ws:// or wss:// URL for an API path, matching the page's protocol. */
export function webSocketUrl(path: string): string {
  const url = new URL(getApiUrl() + path, window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

// ── Hybrid routing（桌面双模式：云端为主 + 本机执行本地项目）─────────────────
// 桌面壳的 Rust 反代按请求头分流：带 `x-hugagent-target: local` → 本机执行面
// （127.0.0.1:32101），否则 → 云端。前端唯一的「本地」判定真源是项目
// kind==='local'；聊天则继承其绑定项目。此处维护两个注册表（api.ts 内自洽，
// 不引 store，避免模块环）。仅 provision_mode==='dual' 时生效；web 上恒为空。

// 头名必须用全小写技术标识：大写 HugAgentOS 会被 CE 品牌变换改写成 HugAgentOS，
// 与桌面壳 Rust 反代匹配的小写常量（proxy.rs TARGET_HEADER）对不上，路由整体失效。
export const LOCAL_TARGET_HEADER = 'x-hugagent-target';

let _hybridDual = false;
/** 由 deploymentModeStore.refresh() 在探测到桌面双模式后开启。 */
export function setHybridDual(on: boolean) {
  _hybridDual = on;
}
export function isHybridDual(): boolean {
  return _hybridDual;
}

let _capabilityPlaneReady = false;
/** 由 deploymentModeStore 在本机首轮能力同步完成后开启（壳的 /__desktop/events）。
 *
 *  闸门是「能力已同步」而不是「身份已认出」：身份一就绪就切到本机读，首轮同步
 *  还没落完，能力中心会先渲染一版残缺清单，用户看到的是自己的能力凭空少了一半。 */
export function setCapabilityPlaneReady(ready: boolean) {
  _capabilityPlaneReady = ready;
}

/** 四类能力此刻是不是从本机读写（见 capabilityTargetHeaders）。 */
export function capabilitiesRouteLocal(): boolean {
  return _hybridDual && _capabilityPlaneReady;
}

const _localProjects = new Set<string>();
const _localChats = new Set<string>();
const _localLoops = new Set<string>();
let _chatContext: (id: string) => { runTarget?: string; projectId?: string } | undefined = () => undefined;

/** Read the current user's persisted chat choice without importing stores into the API layer. */
export function setChatRoutingContext(reader: typeof _chatContext) {
  _chatContext = reader;
}

export function registerLocalProject(projectId: string) {
  if (projectId) _localProjects.add(projectId);
}
export function registerLocalLoop(loopId: string) {
  _localLoops.add(loopId);
}
export function registerLocalChat(chatId: string) {
  if (chatId) _localChats.add(chatId);
}
export function isLocalProject(projectId?: string | null): boolean {
  return !!projectId && _localProjects.has(projectId);
}
/** Unlike a draft choice, this means the session has been observed on the local backend. */
export function isRegisteredLocalChat(chatId: string): boolean {
  return _localChats.has(chatId);
}
export function isLocalChat(chatId?: string | null): boolean {
  if (!chatId) return false;
  const chat = _chatContext(chatId);
  return _localChats.has(chatId) || chat?.runTarget === 'local' || isLocalProject(chat?.projectId);
}

function localHeader(): Record<string, string> {
  return { [LOCAL_TARGET_HEADER]: 'local' };
}

/** 路由目标：`local` / `cloud` 是调用方的显式声明，不给就按下面的前缀表自动判定。 */
export type RequestTarget = 'local' | 'cloud';

/** 「这台机器上装了什么」——四类能力落在本机的接口。整条前缀归本机的写成字符串，
 *  只有部分子路径归本机的写成正则；新增一个能力入口在这里加一行，判定逻辑不用动。 */
const LOCAL_CAPABILITY_PATHS: (string | RegExp)[] = [
  'catalog', //          四类能力的启停目录（catalog.py）
  'agents', //           智能体（agents.py）
  'me/skills', //        自建技能（me_capabilities.py，prefix=/v1/me）
  'me/mcp-servers', //   自建连接器（同上）
  // 插件只有「本机这份清单长什么样、开还是关」归本机：装什么由云端账号定，市场
  // 列表与详情、安装、导入、卸载、展示信息、共享应用授权都只存在于云端，打到本机
  // 会在本机装出第二份——那份的连接器指向本机没人监听的端口，跑不通。界面贡献跟着
  // 启停走，同样归本机，否则本机关掉的插件面板还留在界面上。
  /^\/v1\/plugins\/(installed|ui-contributions)$/,
  /^\/v1\/plugins\/installed\/[^/]+\/(detail|enable)$/,
  /^\/v1\/plugins\/installed\/plugin(?:%3A|:)local(?:%3A|:)[^/]+(?:\/meta)?$/i,
  'plugins/import', // Uploaded packages are installed on this device.
];

/** 前缀命中时仍归云端的例外。
 *  - 知识库挂在 catalog 下，但它不是装在这台机器上的东西，只存在于云端。 */
const CLOUD_ONLY_PREFIXES = ['catalog/kb'];

function underPrefix(path: string, entries: (string | RegExp)[]): boolean {
  return entries.some((entry) =>
    typeof entry === 'string'
      ? path === `/v1/${entry}` || path.startsWith(`/v1/${entry}/`)
      : entry.test(path),
  );
}

/** 混合模式下四类能力在本机的**读和写都打本机**：本机自成一套能力体系，启停与增删就地
 *  生效，不经云端往返。
 *
 *  读必须和写同源。读云端、写本机会让能力中心显示的是云端那份、真正生效的是本机那份：
 *  用户在本机关掉的连接器界面上仍是开着的，插件开关点完当场弹回原位。
 *
 *  本机执行面还没认出云端身份时唯一存在的来源就是云端，这段时间照旧读云端——那不是第二
 *  个真源，是本机那份还不存在。
 *
 *  与云端市场打交道的请求（安装、申请上架、OAuth 授权）不在表里，打到云端。装完之后云端
 *  能力变更号会变，左侧边栏出现同步入口，用户点一下才把它落到本机。
 *
 *  纯本机模式的壳本来就把所有请求指向本机后端，不必额外打头。 */
function capabilityTargetHeaders(url: string): Record<string, string> {
  if (!capabilitiesRouteLocal()) return {};
  // 调用方给的可能是裸路径，也可能是带 /api 前缀的整地址，一律从 /v1/ 起算。
  const pathname = new URL(url, 'http://request.invalid').pathname;
  const start = pathname.indexOf('/v1/');
  if (start < 0) return {};
  const path = pathname.slice(start);
  // Responses execute on the caller's chat/project target. The neighboring
  // agent catalog (including key management) still follows the capability plane.
  if (path === '/v1/agents/responses' || path === '/v1/agents/responses/') return {};
  if (underPrefix(path, CLOUD_ONLY_PREFIXES) || !underPrefix(path, LOCAL_CAPABILITY_PATHS)) {
    return {};
  }
  return localHeader();
}

/** 项目作用域请求的路由头：本地项目 → 本机；否则空对象（云端默认）。 */
export function projectTargetHeaders(projectId?: string | null): Record<string, string> {
  return _hybridDual && isLocalProject(projectId) ? localHeader() : {};
}
/** 聊天作用域请求的路由头：本地项目的聊天 → 本机。 */
export function chatTargetHeaders(chatId?: string | null): Record<string, string> {
  return _hybridDual && isLocalChat(chatId) ? localHeader() : {};
}

/** Capture the upload destination before awaiting any file I/O. Project ownership wins. */
export function chatUploadTarget(chatId?: string, projectId?: string): 'local' | 'cloud' | undefined {
  if (!_hybridDual) return undefined;
  return (projectId ? isLocalProject(projectId) : isLocalChat(chatId)) ? 'local' : 'cloud';
}

/** 从 API URL 推断混合路由头（authFetch 自动兜底：预览/下载等散点直连调用）。 */
function inferTargetHeadersFromUrl(url: string): Record<string, string> {
  if (!_hybridDual) return {};
  const pm = url.match(/\/v1\/projects\/([^/?#]+)/);
  if (pm && isLocalProject(decodeURIComponent(pm[1]))) return localHeader();
  const lm = url.match(/\/v1\/loops\/([^/?#]+)/);
  if (lm && _localLoops.has(decodeURIComponent(lm[1]))) return localHeader();
  const cm = url.match(/\/v1\/chats\/([^/?#]+)/);
  if (cm && isLocalChat(decodeURIComponent(cm[1]))) return localHeader();
  return {};
}

/** <img>/<iframe>/<video> 等 src 场景无法带请求头：本地归属的 URL 追加
 *  ?hg_target=local（Rust 反代等价识别）。非本地/非双模式原样返回。 */
export function maybeLocalizeUrl(url: string): string {
  if (!url || Object.keys(inferTargetHeadersFromUrl(url)).length === 0) return url;
  if (url.includes('hg_target=local')) return url;
  return url.includes('?') ? `${url}&hg_target=local` : `${url}?hg_target=local`;
}

function isApiEnvelope<T>(payload: unknown): payload is ApiEnvelope<T> {
  return !!payload && typeof payload === 'object' && 'code' in payload && 'data' in payload;
}

export function unwrapData<T>(payload: unknown): T {
  if (isApiEnvelope<T>(payload)) {
    return payload.data;
  }
  return payload as T;
}

// User-friendly upload error messages.
// Recognizes nginx 413 first (HTML response, not parseable as JSON), then
// backend structured error codes.
// Baked in at build time from the same UPLOAD_MAX_MB env var that feeds the
// nginx client_max_body_size (see docker-compose.yml frontend build args).
export const UPLOAD_MAX_MB = Number(import.meta.env.VITE_UPLOAD_MAX_MB) || 50;
export const UPLOAD_MAX_BYTES = UPLOAD_MAX_MB * 1024 * 1024;
export function uploadErrorMessage(status: number, payload: unknown): string {
  if (status === 413) {
    return t('文件过大，单个文件不能超过 {n} MB', { n: UPLOAD_MAX_MB });
  }
  if (payload && typeof payload === 'object') {
    const record = payload as JsonObject;
    const code = typeof record.code === 'number' ? record.code : null;
    // 21001 FileTooLargeError, 21002 InvalidFileTypeError — see core/infra/exceptions.py
    if (code === 21001) {
      const data = (record.data ?? {}) as JsonObject;
      const max = typeof data.max_size === 'number' ? data.max_size : null;
      const mb = max ? Math.floor(max / 1024 / 1024) : UPLOAD_MAX_MB;
      return t('文件过大，单个文件不能超过 {n} MB', { n: mb });
    }
    if (code === 21002) {
      const data = (record.data ?? {}) as JsonObject;
      const allowed = Array.isArray(data.allowed_types) ? data.allowed_types.join('、') : '';
      return allowed ? t('不支持的文件格式，仅支持：{allowed}', { allowed }) : t('不支持的文件格式');
    }
  }
  return readErrorMessage(payload, t('上传失败 ({status})', { status }));
}

export function toTimestamp(value: unknown): number {
  if (typeof value !== 'string' || !value) return Date.now();
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Date.now() : parsed;
}

/** 会话 metadata 里的计划快照 → 计划栏状态。服务端是真源：`settled` 表示"不会再有哪一轮
 *  来更新它了"（不等于每一步都完成——停在 2/5 的计划收尾后仍显示 2/5，只是不再转圈）。 */
export function toPlanProgress(raw: unknown): PlanProgressState | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const o = raw as JsonObject;
  const steps = (Array.isArray(o.steps) ? o.steps : [])
    .map((s) => {
      const st = s as JsonObject;
      const title = typeof st?.title === 'string' ? st.title.trim() : '';
      const status = st?.status === 'in_progress' || st?.status === 'completed' || st?.status === 'failed'
        ? st.status : 'pending';
      return title ? { title, status: status as PlanProgressState['steps'][number]['status'] } : null;
    })
    .filter((s): s is PlanProgressState['steps'][number] => !!s);
  if (steps.length === 0) return undefined;
  const ts = typeof o.updated_at === 'string' ? Date.parse(o.updated_at) : NaN;
  return {
    source: 'agent',
    title: typeof o.title === 'string' ? o.title : '',
    steps,
    done: o.settled === true ? true : undefined,
    updatedAt: Number.isFinite(ts) ? ts : Date.now(),
  };
}

export function toChatItem(raw: JsonObject): ChatItem {
  const metadata = (raw.metadata ?? {}) as JsonObject;
  return {
    id: String(raw.chat_id ?? raw.id ?? ''),
    title: conversationTitle(String(raw.title ?? '新对话')),
    createdAt: toTimestamp(raw.created_at),
    updatedAt: toTimestamp(raw.updated_at),
    messages: [],
    favorite: Boolean(raw.favorite),
    pinned: Boolean(raw.pinned),
    businessTopic: typeof raw.business_topic === 'string' ? raw.business_topic : undefined,
    automationTaskId: typeof metadata.automation_task_id === 'string' ? metadata.automation_task_id : undefined,
    automationRun: metadata.automation_run === true,
    agentId: typeof metadata.agent_id === 'string' ? metadata.agent_id : undefined,
    agentName: typeof metadata.agent_name === 'string' ? metadata.agent_name : undefined,
    planChat: metadata.plan_chat === true ? true : undefined,
    personalAgentTask: typeof metadata.personal_agent_task_id === "string" ? true : undefined,
    workflowChat: metadata.workflow_chat === true ? true : undefined,
    planProgress: toPlanProgress(metadata.plan_progress),
    modeSlug: typeof metadata.mode_slug === 'string' ? metadata.mode_slug : undefined,
    thinkingEffort: typeof metadata.thinking_effort === 'string' && ['turbo', 'fast', 'low', 'medium', 'high', 'xhigh', 'max'].includes(metadata.thinking_effort) ? metadata.thinking_effort as ChatItem['thinkingEffort'] : undefined,
    planModeActive: typeof metadata.plan_mode_active === 'boolean' ? metadata.plan_mode_active : undefined,
    workflowModeActive: typeof metadata.workflow_mode_active === 'boolean' ? metadata.workflow_mode_active : undefined,
    projectId: typeof raw.project_id === 'string' && raw.project_id ? raw.project_id : undefined,
  };
}

// ── Global 401 handler ──────────────────────────────────────────────────
let _on401: ((loginUrl: string) => void) | null = null;

/** Register a callback invoked on any 401 with login_url. */
export function onUnauthorized(handler: (loginUrl: string) => void) {
  _on401 = handler;
}

/** Only 401 (session expired) triggers the login flow and throws Session expired;
 * 403 is insufficient permission (e.g. backend 31001 Access Denied) — let the caller
 * surface the backend message instead of misreporting it as session expiry and
 * masking the real cause. */
export function throwIfSessionExpired(status: number, payload: unknown, localTarget = false): void {
  if (status !== 401) return;
  // 本机执行面的 401 只说明云端身份还没同步到本机，与云端登录会话无关；
  // 不能触发"会话过期"弹框把用户踢出登录。
  if (localTarget) throw new Error('本机执行面尚未接受云端身份，请稍候重试');
  if (!_on401) return;
  const pickLoginUrl = (obj: unknown): string => {
    if (!obj || typeof obj !== 'object') return '';
    const data = (obj as Record<string, unknown>).data;
    if (!data || typeof data !== 'object') return '';
    const url = (data as Record<string, unknown>).login_url;
    return typeof url === 'string' ? url : '';
  };
  const record = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
  const loginUrl = pickLoginUrl(record) || pickLoginUrl(record.detail);
  _on401(loginUrl);
  throw new Error('Session expired');
}

/** Cloud activity offers a rate-limited account comparison, never a raw epoch prompt.
 * Content edits may leave the legacy global epoch unchanged, so observe successful
 * requests too. The store owns readiness, concurrency and the check interval. */
let _onCapabilityCheck: (() => void) | null = null;
export function setCapabilityCheckListener(fn: (() => void) | null) {
  _onCapabilityCheck = fn;
}

export async function apiRequest<T>(
  path: string,
  options?: RequestInit,
  target?: RequestTarget,
): Promise<T> {
  const url = `${getApiUrl()}${path}`;
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...publicChatHeaders(),
    // 混合路由：显式声明本地目标时打头（web / 非双模式下反代忽略该头）。
    ...(target === 'local' ? localHeader() : {}),
    ...(target === 'cloud' ? {} : capabilityTargetHeaders(path)),
    // 兜底：与 authFetch 同源推断——路径含本地项目/本地会话 id 时自动打头，
    // 否则 file-confirm / pending-confirm 等会话作用域请求会被误发云端，
    // 云端无此会话而报「会话不存在或无权访问」。
    ...inferTargetHeadersFromUrl(path),
    ...((options?.headers as Record<string, string> | undefined) ?? {}),
  };
  const localTarget = headers[LOCAL_TARGET_HEADER] === 'local';
  const response = await fetch(url, {
    ...options,
    credentials: 'include',
    headers,
  });
  if (!localTarget && response.ok && _hybridDual) _onCapabilityCheck?.();

  if (response.status === 204) {
    return undefined as T;
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    // 401 → session expired, show login; 403 → insufficient permission, fall through to the generic branch below to surface the backend message
    throwIfSessionExpired(response.status, payload, localTarget);
    const editionError = createEditionAccessError(response.status, payload, readErrorMessage);
    if (editionError) throw editionError;
    throw createApiResponseError(response.status, payload, `API Error: ${response.status}`, response.headers.get("Retry-After"));
  }
  return payload as T;
}

/**
 * The base fetch for every main-app request that needs the raw Response (streams, blobs, uploads);
 * `apiRequest` applies the same routing for JSON calls. Adds `credentials: 'include'`, the desktop local-target and capability
 * routing headers, public-chat credentials, and routes 401 to the global session-expired handler.
 */
export function authFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const request = input instanceof Request ? input : undefined;
  const url = request?.url ?? String(input);
  const headers = new Headers({ ...inferTargetHeadersFromUrl(url), ...publicChatHeaders() });
  new Headers(init?.headers ?? request?.headers).forEach((value, key) => headers.set(key, value));
  for (const [key, value] of Object.entries(capabilityTargetHeaders(url))) {
    headers.set(key, value);
  }
  const localTarget = headers.get(LOCAL_TARGET_HEADER) === 'local'
    || new URL(url, 'http://request.invalid').searchParams.get('hg_target') === 'local';
  return fetch(input, {
    ...init,
    headers,
    credentials: 'include',
  }).then(async (response) => {
    // Local file/service authentication cannot invalidate the cloud session.
    // Keep the response for the preview's own error state and retry action.
    if (response.status === 401 && !localTarget && _on401) {
      let loginUrl = '';
      try {
        const payload = await response.clone().json();
        loginUrl =
          payload?.data?.login_url ||
          payload?.detail?.data?.login_url ||
          '';
      } catch {
        // ignore parse errors
      }
      _on401(loginUrl);
    }
    return response;
  });
}
