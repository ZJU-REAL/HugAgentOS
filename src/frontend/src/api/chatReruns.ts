import { authFetch, chatTargetHeaders, getApiUrl } from '../api';
import { useChatStore, useModelCapabilitiesStore } from '../stores';
import { useChatModeStore } from '../stores/chatModeStore';

/** Reruns use the same current model and locked mode effort as a normal send. */
export function requestChatRerun(
  chatId: string,
  messageIndex: number,
  signal: AbortSignal,
  newContent?: string,
): Promise<Response> {
  const { chatMode, modeSlug } = useChatStore.getState();
  const caps = useModelCapabilitiesStore.getState();
  const mode = useChatModeStore.getState().modeOf(modeSlug);
  return authFetch(`${getApiUrl()}/v1/chats/${encodeURIComponent(chatId)}/${newContent === undefined ? 'regenerate' : 'edit'}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...chatTargetHeaders(chatId) },
    body: JSON.stringify({
      message_index: messageIndex,
      ...(newContent === undefined ? {} : { new_content: newContent }),
      ...(caps.capabilities.user_model_switch_enabled && caps.selectedModelProviderId
        ? { model_provider_id: caps.selectedModelProviderId } : {}),
      mode_slug: modeSlug,
      chat_mode: mode.effort_locked ? mode.default_effort : chatMode,
    }),
    signal,
  });
}
