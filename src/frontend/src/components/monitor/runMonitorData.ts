import type { ChatMessage, ToolCall } from '../../types';
import type { CanvasArtifact } from '../../stores/canvasStore';
import { artifactOrigin } from '../../utils/artifactAccess';
import { extractArtifactOutputs } from '../../utils/fileParser';

export interface MonitorSubagent {
  tool: ToolCall;
  messageUid: string;
  isStreaming: boolean;
}

export interface MonitorFiles {
  input: CanvasArtifact[];
  output: CanvasArtifact[];
}

export interface MonitorToolStats {
  calls: number;
  failed: number;
}

/** Every call_subagent in the chat, in the order the agent made them. */
export function collectSubagents(messages: ChatMessage[] | undefined): MonitorSubagent[] {
  const result: MonitorSubagent[] = [];
  for (const m of messages || []) {
    if (m.role !== 'assistant') continue;
    for (const tool of m.toolCalls || []) {
      if (tool.name === 'call_subagent' && tool.id) {
        result.push({ tool, messageUid: m.uid, isStreaming: !!m.isStreaming });
      }
    }
  }
  return result;
}

function toArtifact(raw: Record<string, unknown>, chatId?: string): CanvasArtifact {
  const name = String(raw.name || raw.filename || '').split(/[\\/]/).pop() || String(raw.file_id);
  const artifact = {
    ...raw,
    file_id: String(raw.file_id),
    name,
    url: typeof raw.url === 'string' ? raw.url : '',
    chat_id: typeof raw.chat_id === 'string' ? raw.chat_id : chatId,
  } as CanvasArtifact;
  return { ...artifact, origin: artifactOrigin(artifact) };
}

/**
 * Files that went into and came out of the chat.
 *
 * Input is what the user attached. Output is every file a tool or sub-agent
 * wrote, including intermediates the agent did not pin to the conversation:
 * the monitor shows the whole run, not only the delivered cards.
 */
export function collectFiles(messages: ChatMessage[] | undefined, chatId?: string): MonitorFiles {
  const input = new Map<string, CanvasArtifact>();
  const output = new Map<string, CanvasArtifact>();
  const collect = (value: unknown) => {
    for (const raw of extractArtifactOutputs(value)) {
      const artifact = toArtifact(raw, chatId);
      output.delete(artifact.file_id);
      output.set(artifact.file_id, artifact);
    }
  };
  for (const m of messages || []) {
    if (m.role === 'user') {
      for (const att of m.attachments || []) {
        if (!att.file_id) continue;
        input.set(att.file_id, toArtifact({
          file_id: att.file_id, origin: att.origin, name: att.name,
          url: att.download_url || `/files/${att.file_id}`, mime_type: att.mime_type,
        }, chatId));
      }
      continue;
    }
    for (const tool of m.toolCalls || []) {
      if (tool.status != null && tool.status !== 'success') continue;
      // pin_to_workspace echoes a file that another tool already produced.
      if (tool.name !== 'pin_to_workspace') collect(tool.output);
      for (const step of tool.subSteps || []) {
        if (step.kind === 'tool' && step.status === 'success') collect(step.output);
      }
    }
  }
  return { input: [...input.values()], output: [...output.values()] };
}

/** Tool calls made in the chat, sub-agent steps included. */
export function countToolCalls(messages: ChatMessage[] | undefined): MonitorToolStats {
  let calls = 0;
  let failed = 0;
  for (const m of messages || []) {
    for (const tool of m.toolCalls || []) {
      calls += 1;
      if (tool.status === 'error') failed += 1;
      for (const step of tool.subSteps || []) {
        if (step.kind !== 'tool') continue;
        calls += 1;
        if (step.status === 'error') failed += 1;
      }
    }
  }
  return { calls, failed };
}
