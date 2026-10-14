import { useEffect, useRef } from 'react';
import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { Markdown } from '@tiptap/markdown';
import { TableKit } from '@tiptap/extension-table';
import { TaskList, TaskItem } from '@tiptap/extension-list';
import { editorMarkdownParser, MarkdownLiterals } from '../chat/composerMarkdown';
import { t } from '../../i18n';

export function MarkdownFileEditor({ value, disabled, onChange }: {
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Editor | null>(null);
  const initialValue = useRef(value);
  const onChangeRef = useRef(onChange);
  useEffect(() => { onChangeRef.current = onChange; }, [onChange]);

  useEffect(() => {
    if (!host.current) return;
    const editor = new Editor({
      element: host.current,
      content: initialValue.current,
      contentType: 'markdown',
      extensions: [
        StarterKit.configure({ underline: false, trailingNode: false, link: { openOnClick: false } }),
        TableKit, TaskList, TaskItem.configure({ nested: true }), ...MarkdownLiterals,
        Markdown.configure({ marked: editorMarkdownParser(), markedOptions: { gfm: true, breaks: true } }),
      ],
      editorProps: { attributes: {
        class: 'jx-fileMarkdownEditor jx-md', role: 'textbox',
        'aria-label': t('文件内容'), 'aria-multiline': 'true',
      } },
      onUpdate: () => onChangeRef.current(editor.getMarkdown()),
    });
    editorRef.current = editor;
    return () => { editorRef.current = null; editor.destroy(); };
  }, []);

  useEffect(() => {
    const editor = editorRef.current;
    if (editor && editor.getMarkdown().replace(/\r\n/g, '\n') !== value.replace(/\r\n/g, '\n')) {
      editor.commands.setContent(value, { contentType: 'markdown', emitUpdate: false });
    }
  }, [value]);
  useEffect(() => { editorRef.current?.setEditable(!disabled, false); }, [disabled]);

  return <div ref={host} className="jx-fileMarkdownHost" />;
}
