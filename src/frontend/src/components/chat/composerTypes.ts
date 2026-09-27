import type React from 'react';

export interface InputAreaProps {
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  send: () => void;
  abort?: () => void;
  activateQueuedMessage?: (chatId?: string) => Promise<void>;
  discardQueuedMessage?: (chatId?: string) => Promise<void>;
  continueLoop?: (chatId?: string) => void;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>, ref: React.RefObject<HTMLInputElement | null>) => void;
  removeFile: (index: number) => void;
  placeholder?: string;
  mobilePlaceholder?: string;
  rows?: number;
  disableMention?: boolean;
  /** New-chat composer on the project page: hides the "Project" selector dropdown
   *  (the chat is fixed to the current project) and the autonomous-loop entry; mode
   *  items in the "+" menu are marked "selected" per activeMode. All other abilities
   *  (attachment upload / skills / plugins / @sub-agents / import from My Space) are
   *  identical to the main composer. */
  projectComposer?: boolean;
  /** Always show the send button, ignoring the current chat's streaming state. Used by
   *  the project-page composer: it is a "new-chat starting point" and should not reflect
   *  the state of some chat that is currently streaming. */
  forceSendMode?: boolean;
  /** Custom "enter plan/batch mode" behavior. The project page passes this in: defer
   *  chat creation until send, no navigation; when omitted, falls back to the default
   *  enterChatMode (switches the current chat in place). */
  onEnterMode?: (mode: 'plan' | 'batch' | 'workflow') => void;
  /** Currently selected mode (projectComposer project page only; drives the "selected" marker and the indicator pill). */
  activeMode?: 'plan' | 'batch' | 'workflow' | null;
}


export type ComposerOptions = {
  projectComposer: boolean;
  forceSendMode: boolean;
  disableMention: boolean;
  activeMode: 'plan' | 'batch' | 'workflow' | null;
  onEnterMode?: InputAreaProps['onEnterMode'];
  abort?: InputAreaProps['abort'];
};
