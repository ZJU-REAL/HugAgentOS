import { useMemo } from 'react';
import { RedoOutlined } from '@ant-design/icons';
import { useComposerDraft, chatDraftKey } from '../../stores/composerStore';
import type { ChatMessage } from '../../types';
import { t } from '../../i18n';
import { useSelectionToolbar, type SelectionSnapshot } from '../common/selection/useSelectionToolbar';

/** Selected text in a message bubble → copy / quote it as the next turn's follow-up. */
export function useMessageSelection(m: ChatMessage, chatId: string) {
  const { setQuotedFollowUp } = useComposerDraft(chatDraftKey(chatId));
  const actions = useMemo(() => [{
    key: 'quote',
    label: t('追问'),
    icon: <RedoOutlined />,
    run: ({ text }: SelectionSnapshot) => setQuotedFollowUp({ text, source: { kind: 'message', ts: m.ts } }),
  }], [setQuotedFollowUp, m.ts]);
  return useSelectionToolbar({ actions, enabled: !m.isStreaming });
}
