import { useMemo } from 'react';
import { RedoOutlined } from '@ant-design/icons';
import { chatDraftKey, readComposer } from '../../stores/composerStore';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
import { useSelectionToolbar, type SelectionSnapshot } from '../common/selection/useSelectionToolbar';

/** Selected text in a message bubble → copy / quote it as the next turn's follow-up. */
export function useMessageSelection(m: ChatMessage, chatId: string) {
  // Resolve the draft at click time: subscribing every bubble to the draft would re-render them all on each keystroke.
  const actions = useMemo(() => [{
    key: 'quote',
    label: t('追问'),
    icon: <RedoOutlined />,
    run: ({ text }: SelectionSnapshot) =>
      readComposer(chatDraftKey(chatId)).setQuotedFollowUp({ text, source: { kind: 'message', ts: m.ts } }),
  }], [chatId, m.ts]);
  return useSelectionToolbar({ actions, enabled: !m.isStreaming });
}
