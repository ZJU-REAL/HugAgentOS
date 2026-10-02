import assert from 'node:assert/strict';
import { collectReasoningFields, normalizeThinkingEffort, selectedReasoningPolicy, thinkingLevels, reasoningFormValues } from '../src/utils/reasoningEffort';

const main = { supports_reasoning_effort: true, reasoning_effort_configured: true,
  reasoning_effort_levels: [{ key: 'medium' as const, value: 'medium' }],
  default_reasoning_effort: 'medium' as const };
const vision = { provider_id: 'vision', is_default: false, supports_reasoning_effort: true,
  reasoning_effort_configured: true,
  reasoning_effort_levels: [{ key: 'low' as const, value: 'low' }, { key: 'high' as const, value: 'high' }, { key: 'max' as const, value: 100 }],
  default_reasoning_effort: 'high' as const };
const caps = { ...main, user_model_switch_enabled: true, user_selectable_models: [vision] };
const policy = selectedReasoningPolicy(caps, 'vision');
assert.deepEqual(thinkingLevels(policy).map((l) => l.key), ['low', 'high', 'max']);
assert.equal(normalizeThinkingEffort('medium', policy), 'high');
assert.equal(normalizeThinkingEffort('max', policy), 'max');
assert.equal(normalizeThinkingEffort('fast', policy), 'fast');
assert.equal(normalizeThinkingEffort('turbo', policy), 'turbo');
assert.equal(normalizeThinkingEffort('max', { supports_reasoning_effort: false }), 'medium');
const extra: Record<string, unknown> = { api_protocol: 'responses' };
collectReasoningFields({ provider_type: 'chat', supports_reasoning_effort: true,
  reasoning_effort_keys: ['high', 'max'], reasoning_effort_values: { high: 'high', max: '100' },
  default_reasoning_effort: 'high' }, extra);
assert.deepEqual(extra.reasoning_effort_levels, [{ key: 'high', value: 'high' }, { key: 'max', value: 100 }]);
collectReasoningFields({ provider_type: 'chat', supports_reasoning_effort: false }, extra);
assert.deepEqual(extra, { api_protocol: 'responses' });
console.log('Model-specific thinking selection and form serialization: OK');

const legacy = { supports_reasoning_effort: true, api_protocol: 'responses' };
const retained: Record<string, unknown> = { ...legacy };
collectReasoningFields({ provider_type: 'chat', supports_reasoning_effort: true,
  ...reasoningFormValues(legacy) }, retained, legacy);
assert.deepEqual(retained, legacy, 'Ordinary edits preserve legacy implicit mapping');
collectReasoningFields({ provider_type: 'chat', supports_reasoning_effort: true,
  ...reasoningFormValues(legacy), reasoning_effort_detected: true }, retained, legacy);
assert.ok(retained.reasoning_effort_levels, 'Explicit discovery creates mapping');
