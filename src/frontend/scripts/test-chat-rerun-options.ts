import assert from 'node:assert/strict';
import { requestChatRerun } from '../src/api/chatReruns';
import { useChatStore, useModelCapabilitiesStore } from '../src/stores';
import { useChatModeStore } from '../src/stores/chatModeStore';

const originalFetch = globalThis.fetch;
const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
globalThis.fetch = async (input, init) => {
  requests.push({ url: String(input), body: JSON.parse(String(init?.body)) });
  return new Response('data: [DONE]\n\n', { status: 200 });
};
try {
  useChatStore.setState({ chatMode: 'high', modeSlug: 'turbo' });
  useModelCapabilitiesStore.setState((state) => ({
    selectedModelProviderId: 'selected-provider',
    capabilities: { ...state.capabilities, user_model_switch_enabled: true },
  }));
  const signal = new AbortController().signal;
  await requestChatRerun('audit-chat', 0, signal, 'edited short message');
  assert.deepEqual(requests[0].body, {
    message_index: 0, new_content: 'edited short message',
    model_provider_id: 'selected-provider', mode_slug: 'turbo', chat_mode: 'fast',
  });
  assert.ok(requests[0].url.endsWith('/v1/chats/audit-chat/edit'));
  useChatStore.setState({ modeSlug: 'standard' });
  await requestChatRerun('audit-chat', 1, signal);
  assert.equal(requests[1].body.chat_mode, 'high');
  assert.ok(requests[1].url.endsWith('/v1/chats/audit-chat/regenerate'));
  assert.equal(requests[1].body.new_content, undefined);
  useModelCapabilitiesStore.setState((state) => ({ capabilities: {
    ...state.capabilities, user_model_switch_enabled: false,
  } }));
  await requestChatRerun('audit-chat', 1, signal);
  assert.equal(requests[2].body.model_provider_id, undefined);
  assert.ok(useChatModeStore.getState().modeOf('turbo').effort_locked);
  console.log('chat rerun selection tests passed');
} finally { globalThis.fetch = originalFetch; }
