import type { FileDecision, QuestionAnswer } from '@hugagent/chat-sdk';
import { useMemo } from 'react';
import { useChatStore } from '../stores/chatStore';
import { useUIStore } from '../stores/uiStore';
import { chatDraftKey, readComposer, useComposerDraft, useComposerStore } from '../stores/composerStore';
import { useComposerFiles } from '../hooks/useComposerFiles';
import { usePageConfig } from '../hooks/usePageConfig';
import { answerUserQuestion, confirmFileWrite, getApiUrl } from '../api';
import type { useAppController } from '../hooks/useAppController';
import type { ChatActions, ChatRuntime } from '@hugagent/chat-sdk/react';
import { toMessageView } from './chatView';

export function useHostChatRuntime(state: ReturnType<typeof useAppController>): ChatRuntime {
  const currentId = useChatStore(s => s.currentChatId);
  const chats = useChatStore(s => s.store.chats);
  const sending = useChatStore(s => s.sendingChatIds.has(currentId));
  const productName = usePageConfig('branding.product_name', 'HugAgentOS');
  const confirmations = useUIStore(s => s.pendingConfirm[currentId]);
  const designPick = useUIStore(s => s.pendingDesignPick[currentId]);
  const questions = useUIStore(s => s.pendingUserQuestions[currentId]);
  const draft = useComposerDraft(chatDraftKey(currentId));
  const { attachFiles, removeFile, retryFile } = useComposerFiles(getApiUrl());
  const chat = chats[currentId];
  const view = useMemo(() => Object.freeze({
    productName, draft: draft.input,
    conversation: Object.freeze({
      id: currentId, title: chat?.title || '', sending,
      messages: Object.freeze((chat?.messages || []).map(toMessageView)),
    }),
    conversations: Object.freeze(Object.values(chats)
      .map(item => Object.freeze({ id: item.id, title: item.title }))),
    attachments: Object.freeze(draft.uploadedFiles.map((file, index) => Object.freeze({
      index, name: file.name, size: file.size, uploading: draft.uploadingFiles.has(file),
      error: draft.uploadErrors.get(file),
    }))),
    designPick: designPick ? Object.freeze({
      id: designPick.confirmId, question: designPick.question,
      options: Object.freeze(designPick.options.map(option => Object.freeze({
        id: option.id, title: option.title, brief: option.brief, imageFileId: option.imageFileId,
      }))),
    }) : null,
    confirmations: Object.freeze((confirmations || []).map(item => Object.freeze({
      id: item.confirmId, operation: item.op, path: item.logicalPath, message: item.message,
    }))),
    questions: Object.freeze((questions || []).map(item => Object.freeze({
      requestId: item.requestId, questions: Object.freeze(item.questions.map(question => Object.freeze({
        id: question.id, question: question.question, multiSelect: question.multiSelect,
        options: Object.freeze(question.options.map(option => Object.freeze({ id: option.id, label: option.label }))),
      }))),
    }))),
  }), [productName, currentId, chat, chats, sending, draft.input, draft.uploadedFiles,
    draft.uploadingFiles, draft.uploadErrors, confirmations, questions, designPick]);
  const actions = useMemo<ChatActions>(() => {
    const key = () => chatDraftKey(useChatStore.getState().currentChatId);
    const messageIndex = (id: string) => {
      const store = useChatStore.getState();
      const index = store.store.chats[store.currentChatId]?.messages.findIndex(m => m.uid === id) ?? -1;
      if (index < 0) throw new Error('Unknown message');
      return index;
    };
    return Object.freeze({
      send: (text?: string) => { void state.send(text); },
      cancel: () => state.abort(),
      newChat: () => state.handleNewChat(),
      selectChat: (id: string) => {
        if (!useChatStore.getState().store.chats[id]) throw new Error('Unknown conversation');
        state.handleSelectChat(id);
      },
      setDraft: (text: string) => readComposer(key()).setInput(text),
      attachFiles: (files: readonly File[]) => attachFiles(files, key()),
      removeFile: (index: number) => removeFile(index, key()),
      retryFile: (index: number) => {
        const file = readComposer(key()).uploadedFiles[index];
        if (file) retryFile(file, key());
      },
      regenerate: (id: string) => { void state.regenerate(messageIndex(id)); },
      editAndResend: (id: string, text: string) => { void state.editAndResendFollow(messageIndex(id), text); },
      confirm: async (id: string, decision: FileDecision, optionId?: string) => {
        const chatId = useChatStore.getState().currentChatId, epoch = useComposerStore.getState().epoch;
        const result = await confirmFileWrite(chatId, id, decision, optionId);
        if (useComposerStore.getState().epoch === epoch) {
          useUIStore.getState().resolvePendingConfirm(chatId, id);
          if (useUIStore.getState().pendingDesignPick[chatId]?.confirmId === id) useUIStore.getState().setPendingDesignPick(chatId, null);
          for (const cascaded of result.cascaded || []) useUIStore.getState().resolvePendingConfirm(chatId, cascaded);
        }
        return result;
      },
      answer: async (id: string, answers: QuestionAnswer[]) => {
        const chatId = useChatStore.getState().currentChatId, epoch = useComposerStore.getState().epoch;
        const result = await answerUserQuestion(chatId, id, answers);
        if ((result.ok || result.stale) && useComposerStore.getState().epoch === epoch) useUIStore.getState().resolvePendingUserQuestion(chatId, id);
        return result;
      },
    });
  }, [state, attachFiles, removeFile, retryFile]);
  return useMemo(() => Object.freeze({ view, actions }), [view, actions]);
}
