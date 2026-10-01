import { useLayoutEffect, useRef } from 'react';
import { handleComposerNewline } from './composerEditingKeys';
import { createRichEditor, disposeRichEditor } from './composerRichText';
import type { Editor } from '@tiptap/core';
import '../../styles/composer-rich.css';

interface RichTextFieldProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCancel?: () => void;
  active?: boolean;
  className?: string;
  label: string;
}

/** Editing a saved or queued message uses the same Markdown semantics as the composer. */
export function RichTextField({ value, onChange, onSubmit, onCancel, active = true, className, label }: RichTextFieldProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Editor | null>(null);
  const callbacks = useRef({ onChange, onSubmit, onCancel });
  const lastValue = useRef(value);
  const composing = useRef(false);
  useLayoutEffect(() => { callbacks.current = { onChange, onSubmit, onCancel }; });
  useLayoutEffect(() => {
    if (!hostRef.current) return;
    const editor = createRichEditor(hostRef.current, () => {
      lastValue.current = editor.getMarkdown();
      callbacks.current.onChange(lastValue.current);
    });
    editor.chain().setContent(lastValue.current, { contentType: 'markdown', emitUpdate: false }).setMeta('addToHistory', false).run();
    editorRef.current = editor;
    return () => { editorRef.current = null; disposeRichEditor(editor); };
  }, []);
  useLayoutEffect(() => {
    if (value === lastValue.current) return;
    lastValue.current = value;
    editorRef.current?.chain().setContent(value, { contentType: 'markdown', emitUpdate: false }).setMeta('addToHistory', false).run();
  }, [value]);
  useLayoutEffect(() => {
    if (!active) return;
    const timer = window.setTimeout(() => editorRef.current?.commands.focus('end'), 250);
    return () => window.clearTimeout(timer);
  }, [active]);
  return <div ref={hostRef} className={className} aria-label={label}
    onCompositionStart={() => { composing.current = true; }}
    onCompositionEnd={() => { composing.current = false; }}
    onKeyDownCapture={event => {
      if (composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
      const editor = editorRef.current;
      if (event.key === 'Enter' && event.shiftKey && editor) {
        event.preventDefault();
        handleComposerNewline(editor);
        return;
      }
      if (event.key === 'Escape' && callbacks.current.onCancel) {
        event.preventDefault();
        callbacks.current.onCancel();
      } else if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        callbacks.current.onSubmit();
      }
    }} />;
}
