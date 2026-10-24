import { useContext } from 'react';
import { UNSAFE_LocationContext } from 'react-router';
import { message } from 'antd';
import { t } from '../../../i18n';
import { isConversationPath } from '../../../routing/navigation';
import { useChatStore } from '../../../stores';
import { useCanvasStore, type CanvasArtifact } from '../../../stores/canvasStore';
import { chatDraftKey, readComposer } from '../../../stores/composerStore';
import type { QuotedFollowUp } from '../../../types';

/**
 * The chat this canvas sits beside, i.e. the composer a quote should land in. The
 * canvas is one global side panel: it belongs to whichever conversation is on screen,
 * and to none when it was opened from My Space, a project overview or an app page.
 */
export function useCanvasChatId(): string | null {
  // Read the router's location without useLocation(): the canvas is also mounted
  // outside a router (previews, fixtures), and there it simply belongs to no chat.
  const pathname = useContext(UNSAFE_LocationContext)?.location.pathname;
  const currentChatId = useChatStore((s) => s.currentChatId);
  return pathname !== undefined && isConversationPath(pathname) && currentChatId ? currentChatId : null;
}

/** A quote taken from a file open in the canvas. */
export function fileQuote(artifact: Pick<CanvasArtifact, 'file_id' | 'name'>, text: string, locator?: string | null): QuotedFollowUp {
  return {
    text,
    source: { kind: 'file', fileId: artifact.file_id, fileName: artifact.name, ...(locator ? { locator } : {}) },
  };
}

/** The chat API's limit on `quoted_follow_up.text`. */
export const QUOTE_MAX_CHARS = 8000;

/** Put a quote (and optionally a ready-to-edit prompt) into that chat's composer. Never sends. */
export function quoteIntoChat(chatId: string, quote: QuotedFollowUp, prompt?: string): boolean {
  if (quote.text.length > QUOTE_MAX_CHARS) {
    message.warning(t('选中的内容超过 {n} 字，请缩小范围后再引用', { n: QUOTE_MAX_CHARS }));
    return false;
  }
  const composer = readComposer(chatDraftKey(chatId));
  composer.setQuotedFollowUp(quote);
  if (prompt) composer.patch({ input: prompt, document: null });
  // A fullscreen canvas hides the composer the quote just went into.
  const canvas = useCanvasStore.getState();
  if (canvas.isFullscreen) canvas.setCanvasFullscreen(false);
  return true;
}
