import { createContext, useContext, type ComponentType, type ReactNode } from 'react';
import type { ConfirmationResult, FileDecision, QuestionAnswer } from './types.js';

export const CHAT_UI_API_VERSION = 1 as const;
export interface MessageView {
  readonly id: string;
  readonly role: 'user' | 'assistant';
  readonly content: string;
  readonly streaming: boolean;
  readonly thinking?: readonly string[];
  readonly error?: string;
  readonly attachments?: readonly { readonly id?: string; readonly name: string; readonly url?: string; readonly mimeType?: string }[];
  readonly citations?: readonly { readonly id: string; readonly title: string; readonly url: string; readonly snippet: string }[];
  readonly tools?: readonly { readonly id?: string; readonly name: string; readonly status?: string }[];
}
export interface ConversationView {
  readonly id: string;
  readonly title: string;
  readonly messages: readonly MessageView[];
  readonly sending: boolean;
}
export interface AttachmentView {
  readonly index: number; readonly name: string; readonly size: number;
  readonly uploading: boolean; readonly error?: string;
}
export interface ConfirmationView {
  readonly id: string; readonly operation: string; readonly path: string; readonly message?: string;
}
export interface QuestionView {
  readonly requestId: string;
  readonly questions: readonly {
    readonly id: string; readonly question: string; readonly multiSelect: boolean;
    readonly options: readonly { readonly id: string; readonly label: string }[];
  }[];
}
export interface ChatView {
  readonly conversation: ConversationView;
  readonly conversations: readonly { readonly id: string; readonly title: string }[];
  readonly productName: string;
  readonly draft: string;
  readonly attachments: readonly AttachmentView[];
  readonly confirmations: readonly ConfirmationView[];
  readonly questions: readonly QuestionView[];
  readonly designPick?: {
    readonly id: string; readonly question: string;
    readonly options: readonly { readonly id: string; readonly title: string; readonly brief?: string; readonly imageFileId: string }[];
  } | null;
}
export interface ChatActions {
  send(text?: string): void;
  cancel(): void;
  newChat(): void;
  selectChat(id: string): void;
  setDraft(text: string): void;
  attachFiles(files: readonly File[]): void;
  removeFile(index: number): void;
  retryFile(index: number): void;
  regenerate(messageId: string): void;
  editAndResend(messageId: string, text: string): void;
  confirm(id: string, decision: FileDecision, optionId?: string): Promise<ConfirmationResult>;
  answer(requestId: string, answers: QuestionAnswer[]): Promise<ConfirmationResult>;
}
export interface ChatRuntime { readonly view: ChatView; readonly actions: ChatActions }
export interface ChatSlotProps { children: ReactNode }
export interface MessageSlotProps extends ChatSlotProps { message: MessageView }
export interface ChatSlots {
  welcome?: ComponentType<ChatSlotProps>;
  composer?: ComponentType<ChatSlotProps>;
  message?: ComponentType<MessageSlotProps>;
  sidebar?: ComponentType<ChatSlotProps>;
  conversation?: ComponentType<ChatSlotProps>;
}
export interface ChatCustomization { apiVersion: typeof CHAT_UI_API_VERSION; id: string; slots?: ChatSlots }
/** Type-level authoring helper. Runtime validation runs inside the host error boundary. */
export function defineChatCustomization(customization: ChatCustomization): ChatCustomization { return customization; }
export function validateChatCustomization(value: unknown): asserts value is ChatCustomization {
  if (!value || typeof value !== 'object') throw new Error('Invalid chat customization');
  const item = value as ChatCustomization;
  if (item.apiVersion !== CHAT_UI_API_VERSION) throw new Error('Unsupported chat UI API version');
  if (typeof item.id !== 'string' || !/^[a-z][a-z0-9-]{0,63}$/.test(item.id)) throw new Error('Invalid customization id');
  if (item.slots !== undefined) {
    if (!item.slots || typeof item.slots !== 'object') throw new Error('Invalid customization slots');
    for (const [name, component] of Object.entries(item.slots)) {
      if (!['welcome', 'composer', 'message', 'sidebar', 'conversation'].includes(name)) throw new Error('Unknown chat slot: ' + name);
      if (typeof component !== 'function') throw new Error('Chat slot must be a component function: ' + name);
    }
  }
}
const Context = createContext<ChatRuntime | null>(null);
export function ChatRuntimeProvider({ value, children }: { value: ChatRuntime; children: ReactNode }) {
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useOptionalChatRuntime(): ChatRuntime | null { return useContext(Context); }
export function useChatRuntime(): ChatRuntime {
  const value = useContext(Context);
  if (!value) throw new Error('useChatRuntime requires a ChatRuntimeProvider');
  return value;
}
