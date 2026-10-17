import type { MessageView } from '@hugagent/chat-sdk/react';
import type { ChatMessage } from '../types';

/** Copy scalar fields at the boundary; never expose mutable store objects. */
export function toMessageView(message: ChatMessage): MessageView {
  return Object.freeze({
    id: message.uid,
    role: message.role,
    content: message.content || '',
    streaming: !!message.isStreaming,
    error: message.error,
    attachments: Object.freeze((message.attachments || []).map(file => Object.freeze({
      id: file.file_id, name: file.name, url: file.download_url, mimeType: file.mime_type,
    }))),
    citations: Object.freeze((message.citations || []).map(cite => Object.freeze({
      id: cite.id, title: cite.title, url: cite.url, snippet: cite.snippet,
    }))),
    thinking: Object.freeze((message.thinking || []).map(block => block.content)),
    tools: Object.freeze((message.toolCalls || []).map(tool => Object.freeze({ id: tool.id, name: tool.displayName || tool.name, status: tool.status }))),
  });
}
