import { Modal } from 'antd';
import { t } from '../../i18n';
import { useCanvasStore } from '../../stores/canvasStore';
import type { ToolCall } from '../../types';

/** Open a sub-agent's session as a canvas tab, guarding an unsaved file tab. */
export function openSubagentTab(
  tool: ToolCall,
  agent: { agent_id: string; name: string; avatar: string | null },
  identity: { chatId: string; messageUid: string },
) {
  const state = useCanvasStore.getState();
  const action = () => state.openSubagent({
    chatId: identity.chatId, messageUid: identity.messageUid, toolId: tool.id!,
    agent: { agent_id: agent.agent_id, name: agent.name, avatar: agent.avatar },
  });
  const active = state.tabs.find((tab) => tab.id === state.activeTabId);
  if (active?.kind === 'file' && active.dirty) {
    Modal.confirm({ title: t('有未保存的修改'), content: t('离开后编辑内容将丢失，确定继续？'),
      okText: t('放弃修改'), cancelText: t('取消'), okButtonProps: { danger: true }, onOk: action });
  } else action();
}
