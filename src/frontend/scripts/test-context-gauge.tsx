import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import { t } from '../src/i18n';
import { ContextGauge } from '../src/components/chat/ContextGauge';
import { useChatStore, useModelCapabilitiesStore } from '../src/stores';
import { parseContextUsageSnapshot } from '../src/utils/contextUsage';

const snapshot = parseContextUsageSnapshot({
  schema_version: 'context-usage.v2', source: 'provider', exact: true,
  prompt_tokens: 1100, completion_tokens: 100, used_tokens: 1200,
  context_window: 1000, model_provider_id: 'p1',
  breakdown: { messages: 1200, tools: 0, thinking: 0, files: 0, system: 0, input: 0 },
})!;
useChatStore.setState({
  currentChatId: 'gauge-audit', contextUsages: { 'gauge-audit': snapshot },
  store: { chats: { 'gauge-audit': { id: 'gauge-audit', messages: [
    { uid: 'a1', role: 'assistant', content: 'answer', ts: 1 },
  ] } } } as never,
});
useModelCapabilitiesStore.setState({
  selectedModelProviderId: 'p1',
  capabilities: { ...useModelCapabilitiesStore.getState().capabilities,
    user_selectable_models: [{ provider_id: 'p1', model_name: 'demo', context_length: 1000 }],
  } as never,
});
// SSR reads the initial store snapshot. Pin it to this fixture for both stores.
Object.assign(useChatStore.getInitialState(), useChatStore.getState());
Object.assign(useModelCapabilitiesStore.getInitialState(), useModelCapabilitiesStore.getState());
assert.ok(renderToStaticMarkup(<ContextGauge />).includes(t('上下文占用 {pct}%', { pct: 120 })));
useModelCapabilitiesStore.setState({
  selectedModelProviderId: null,
  capabilities: { ...useModelCapabilitiesStore.getState().capabilities,
    user_model_switch_enabled: false, user_selectable_models: [], main_context_length: 10000,
  },
});
Object.assign(useModelCapabilitiesStore.getInitialState(), useModelCapabilitiesStore.getState());
assert.ok(renderToStaticMarkup(<ContextGauge />).includes(t('上下文占用 {pct}%', { pct: 12 })));
// An admin can resize the same provider; its old snapshot must not pin the window.
useModelCapabilitiesStore.setState({
  selectedModelProviderId: 'p1',
  capabilities: { ...useModelCapabilitiesStore.getState().capabilities,
    user_model_switch_enabled: true,
    user_selectable_models: [{ provider_id: 'p1', model_name: 'demo', context_length: 2000 }],
  } as never,
});
Object.assign(useModelCapabilitiesStore.getInitialState(), useModelCapabilitiesStore.getState());
assert.ok(renderToStaticMarkup(<ContextGauge />).includes(t('上下文占用 {pct}%', { pct: 60 })));
console.log('context gauge tests passed');
