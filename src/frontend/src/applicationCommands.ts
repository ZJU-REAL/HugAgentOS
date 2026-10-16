import { useChatStore } from './stores/chatStore';
import { useCatalogStore } from './stores/catalogStore';
import { createWorkbenchRegistry } from './workbench/registry';

/** Keep the existing chat-mode transition, including its draft and project semantics. */
export const builtinCommands = createWorkbenchRegistry({
  components: {},
  commands: {
    enter_plan: () => {
      useChatStore.getState().enterChatMode('plan');
      useCatalogStore.getState().setPanel('chat');
    },
  },
  applications: [
    { id: 'plan_mode', target: { kind: 'command', commandId: 'enter_plan' } },
  ],
});
