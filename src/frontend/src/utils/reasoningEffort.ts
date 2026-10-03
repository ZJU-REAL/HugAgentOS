import type { ThinkingEffort } from '../stores/chatStore';

export type ReasoningKey = 'low' | 'medium' | 'high' | 'xhigh' | 'max';
export interface ReasoningProbeResult {
  levels: ReasoningLevel[];
  default: ReasoningKey | null;
  source: 'model_metadata' | 'validation_error' | 'accepted_probe' | 'unknown';
  notes: string[];
  complete?: boolean;
  numeric_range?: [number, number] | null;
}
export interface ReasoningLevel { key: ReasoningKey; value: string | number }
export interface ReasoningPolicy {
  supports_reasoning_effort: boolean;
  reasoning_effort_levels?: ReasoningLevel[];
  default_reasoning_effort?: ReasoningKey;
  reasoning_effort_configured?: boolean;
}
export const effortLabels: Record<ReasoningKey, string> = {
  low: '思考·低', medium: '思考·中', high: '思考·高', xhigh: '思考·超高', max: '思考·最高',
};

export const REASONING_KEYS: ReasoningKey[] = ['low', 'medium', 'high', 'xhigh', 'max'];
export const DEFAULT_REASONING_LEVELS: ReasoningLevel[] = [
  { key: 'medium', value: 'medium' }, { key: 'high', value: 'high' }, { key: 'xhigh', value: 'xhigh' },
];
const LEGACY_LEVELS: ReasoningLevel[] = [
  { key: 'medium', value: 'medium' }, { key: 'high', value: 'high' }, { key: 'max', value: 'max' },
];

export function thinkingLevels(policy: ReasoningPolicy): ReasoningLevel[] {
  return policy.supports_reasoning_effort ? (policy.reasoning_effort_levels ?? LEGACY_LEVELS) : [];
}

export function normalizeThinkingEffort(effort: ThinkingEffort | 'turbo', policy: ReasoningPolicy): ThinkingEffort | 'turbo' {
  if (effort === 'fast' || effort === 'turbo') return effort;
  if (!policy.supports_reasoning_effort) return 'medium';
  const levels = thinkingLevels(policy);
  if (levels.some((level) => level.key === effort)) return effort;
  return levels.find((level) => level.key === policy.default_reasoning_effort)?.key ?? levels[0]?.key ?? 'medium';
}

export function selectedReasoningPolicy(
  caps: ReasoningPolicy & { user_model_switch_enabled: boolean; user_selectable_models: (ReasoningPolicy & { provider_id: string; is_default: boolean })[] },
  providerId: string | null,
): ReasoningPolicy {
  if (!caps.user_model_switch_enabled) return caps;
  return caps.user_selectable_models.find((m) => m.provider_id === providerId)
    ?? caps.user_selectable_models.find((m) => m.is_default)
    ?? caps.user_selectable_models[0] ?? caps;
}

export function reasoningFormValues(extra: Record<string, unknown> = {}) {
  const levels = (extra.reasoning_effort_levels as ReasoningLevel[] | undefined) ?? DEFAULT_REASONING_LEVELS;
  return {
    reasoning_effort_detected: false,
    reasoning_effort_keys: levels.map((level) => level.key),
    reasoning_effort_values: Object.fromEntries(levels.map((level) => [level.key, String(level.value)])),
    default_reasoning_effort: extra.default_reasoning_effort ?? 'medium',
  };
}

export function collectReasoningFields(values: Record<string, unknown>, extra: Record<string, unknown>, original?: Record<string, unknown>) {
  delete extra.reasoning_effort_levels;
  delete extra.default_reasoning_effort;
  if (values.provider_type !== 'chat' || !values.supports_reasoning_effort) {
    delete extra.supports_reasoning_effort;
    return;
  }
  extra.supports_reasoning_effort = true;
  // Ordinary edits must not migrate a legacy implicit policy.
  if (original?.supports_reasoning_effort && !original.reasoning_effort_levels && !values.reasoning_effort_detected) {
    const initial = reasoningFormValues(original);
    if (JSON.stringify(values.reasoning_effort_keys) === JSON.stringify(initial.reasoning_effort_keys)
      && JSON.stringify(values.reasoning_effort_values) === JSON.stringify(initial.reasoning_effort_values)
      && values.default_reasoning_effort === initial.default_reasoning_effort) return;
  }
  const keys = values.reasoning_effort_keys as ReasoningKey[];
  const mapping = values.reasoning_effort_values as Record<string, string>;
  extra.reasoning_effort_levels = keys.map((key) => {
    const raw = String(mapping[key]).trim();
    return { key, value: /^\d+$/.test(raw) ? Number(raw) : raw };
  });
  extra.default_reasoning_effort = values.default_reasoning_effort;
}
