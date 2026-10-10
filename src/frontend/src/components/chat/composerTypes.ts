import type React from 'react';

export interface InputAreaProps {
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  send: () => void;
  abort?: () => void;
  activateQueuedMessage?: (chatId?: string) => Promise<void>;
  discardQueuedMessage?: (chatId?: string) => Promise<void>;
  continueLoop?: (chatId?: string) => void;
  handleFileSelect: (e: React.ChangeEvent<HTMLInputElement>, ref: React.RefObject<HTMLInputElement | null>, draftKey?: string) => void;
  removeFile: (index: number, draftKey?: string) => void;
  placeholder?: string;
  mobilePlaceholder?: string;
  rows?: number;
  disableMention?: boolean;

}


export type ComposerOptions = {
  disableMention: boolean;
  abort?: InputAreaProps['abort'];
};
