/**
 * 智能路由（mode_slug=auto）的「用户意图识别」结果。
 *
 * 后端在每轮模型输出之前合成一对工具事件：`tool_call`（tool_name=intent_recognition，
 * tool_args={question}）和 `tool_result`（result 即下面这份结构）。同一份结构也落在
 * 助手消息元数据 `intent_route` 上；历史记录里这张卡按普通工具记录返回。
 */
export const INTENT_RECOGNITION_TOOL = 'intent_recognition';

export type IntentRouteSource = 'llm' | 'kept' | 'single_candidate' | 'first_turn_sticky' | 'fallback';

export interface IntentRouteCandidate {
  slug: string;
  name: string;
}

export interface IntentRouteResult {
  mode_slug: string;
  mode_name: string;
  reason: string;
  confidence: number | null;
  source: IntentRouteSource | string;
  switched: boolean;
  previous_mode_slug: string | null;
  candidates: IntentRouteCandidate[];
  latency_ms: number | null;
  fallback_cause: string | null;
}

const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** 在边界上把工具结果解析成类型；不是这个形状（缺 mode_slug）就返回 null。 */
export function parseIntentRoute(raw: unknown): IntentRouteResult | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const o = raw as Record<string, unknown>;
  const modeSlug = str(o.mode_slug);
  if (!modeSlug) return null;
  const candidates = Array.isArray(o.candidates)
    ? o.candidates.flatMap((c): IntentRouteCandidate[] => {
      if (!c || typeof c !== 'object') return [];
      const slug = str((c as Record<string, unknown>).slug);
      if (!slug) return [];
      return [{ slug, name: str((c as Record<string, unknown>).name) || slug }];
    })
    : [];
  return {
    mode_slug: modeSlug,
    mode_name: str(o.mode_name) || candidates.find((c) => c.slug === modeSlug)?.name || modeSlug,
    reason: str(o.reason),
    confidence: num(o.confidence),
    source: str(o.source) || 'llm',
    switched: o.switched === true,
    previous_mode_slug: str(o.previous_mode_slug) || null,
    candidates,
    latency_ms: num(o.latency_ms),
    fallback_cause: str(o.fallback_cause) || null,
  };
}

/** 识别前所在模式的名字（只在 switched 时有意义）。 */
export function previousModeName(route: IntentRouteResult): string {
  const prev = route.previous_mode_slug;
  if (!prev) return '';
  return route.candidates.find((c) => c.slug === prev)?.name || prev;
}

/** 「换成」可选的模式：候选里除本次结果外的其余模式。 */
export function alternativeModes(route: IntentRouteResult): IntentRouteCandidate[] {
  return route.candidates.filter((c) => c.slug !== route.mode_slug);
}

/** 这一轮的问题：优先取工具入参，入参缺失时为空。 */
export function intentQuestion(input: unknown): string {
  if (!input || typeof input !== 'object') return '';
  return str((input as Record<string, unknown>).question).trim();
}
