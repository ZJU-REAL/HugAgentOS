import { useMemo } from 'react';
import { CommentOutlined, EditOutlined, QuestionCircleOutlined, TranslationOutlined } from '@ant-design/icons';
import { t, tCtx } from '../../../i18n';
import type { CanvasArtifact } from '../../../stores/canvasStore';
import { useSelectionToolbar, type SelectionAction, type SelectionSnapshot } from '../../common/selection/useSelectionToolbar';
import { fileQuote, quoteIntoChat, useCanvasChatId } from './canvasChat';
import { describeSelectionLocation } from './canvasLocator';

/**
 * Selection toolbar for the DOM-rendered canvas previews (markdown, text, docx, pdf
 * text layer). Copy always works; quoting and the quick prompts need a chat beside
 * the canvas and are hidden otherwise. Iframe (HTML) and canvas-drawn (spreadsheet)
 * previews never produce a DOM selection here, so they simply get no toolbar.
 */
export function useCanvasSelection(artifact: CanvasArtifact | null) {
  const chatId = useCanvasChatId();
  const fileId = artifact?.file_id;
  const name = artifact?.name ?? '';
  const actions = useMemo<SelectionAction[]>(() => {
    if (!chatId || !fileId) return [];
    const quote = (prompt?: string) => ({ text, range, container }: SelectionSnapshot) => {
      const locator = describeSelectionLocation(container, range);
      quoteIntoChat(chatId, fileQuote({ file_id: fileId, name }, text, locator), prompt);
    };
    return [
      { key: 'quote', label: t('引用到对话'), icon: <CommentOutlined />, run: quote() },
      { key: 'explain', label: t('解释'), icon: <QuestionCircleOutlined />, run: quote(t('解释这段内容')) },
      { key: 'rewrite', label: t('改写'), icon: <EditOutlined />, run: quote(t('改写这段内容，让表达更清晰流畅')) },
      { key: 'translate', label: tCtx('selection', '翻译'), icon: <TranslationOutlined />, run: quote(t('翻译这段内容：中文译成英文，其他语言译成中文')) },
    ];
  }, [chatId, fileId, name]);
  return useSelectionToolbar({ actions });
}
