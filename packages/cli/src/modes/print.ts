/** `hugagent -p`: the answer streams to stdout; tool lines, approvals and errors go to stderr. */
import type { ChatEvent } from '../engine/engine.js';
import { errorMessage } from '../engine/errors.js';
import { type HeadlessOptions, eventError, headlessExitCode, runHeadless } from '../engine/headless.js';
import { oneLine, summarizeArgs } from '../tui/format.js';

const CONTENT = new Set(['content', 'ai_message', 'text', 'delta']);
const TOOL_START = new Set(['tool_call_start', 'tool_use', 'tool_start']);
const TOOL_END = new Set(['tool_result', 'tool_end']);

export function contentDelta(event: ChatEvent): string {
  for (const key of ['delta', 'content', 'text']) {
    const value = event[key];
    if (typeof value === 'string') return value;
  }
  return '';
}

export async function runPrint(options: HeadlessOptions): Promise<number> {
  const stdout = options.stdout ?? process.stdout;
  const stderr = options.stderr ?? process.stderr;
  const sameTerminal = (stdout as NodeJS.WriteStream).isTTY === true && (stderr as NodeJS.WriteStream).isTTY === true;
  let midLine = false;
  const out = (text: string) => {
    if (!text) return;
    stdout.write(text);
    midLine = !text.endsWith('\n');
  };
  const err = (line: string) => {
    // Keep a side note from landing in the middle of an answer line on a shared terminal.
    if (sameTerminal && midLine) out('\n');
    stderr.write(line + '\n');
  };
  const tools = new Map<string, { name: string; printed: boolean }>();
  const toolName = (event: ChatEvent) => String(event.name ?? event.tool_name ?? tools.get(String(event.id))?.name ?? 'tool');

  let sawError = false;
  const outcome = await runHeadless(options, {
    onNote: (note) => err('· ' + note),
    onEvent: (event) => {
      const id = String(event.id ?? '');
      if (CONTENT.has(event.type)) out(contentDelta(event));
      else if (event.type === 'content_replace') {
        err('· answer revised:');
        out((midLine ? '\n' : '') + contentDelta(event));
      } else if (TOOL_START.has(event.type)) {
        tools.set(id, { name: toolName(event), printed: false });
      } else if (event.type === 'tool_call') {
        const tool = tools.get(id) ?? { name: toolName(event), printed: false };
        tool.name = toolName(event);
        if (!tool.printed) {
          const args = summarizeArgs(event.input ?? event.args ?? event.tool_args ?? event.arguments, 100);
          err(`→ ${tool.name}${args ? ' ' + args : ''}`);
          tool.printed = true;
        }
        tools.set(id, tool);
      } else if (TOOL_END.has(event.type)) {
        const tool = tools.get(id) ?? { name: toolName(event), printed: false };
        if (!tool.printed) err(`→ ${tool.name}`);
        if (event.error) err(`  ✗ ${tool.name}: ${oneLine(typeof event.error === 'string' ? event.error : JSON.stringify(event.error), 200)}`);
        tools.delete(id);
      } else if (event.type === 'error') {
        sawError = true;
        err('error: ' + eventError(event));
      }
    },
  });
  if (midLine) out('\n');
  const { result } = outcome;
  if (result?.status === 'cancelled') err('· cancelled');
  // An `error` event was already printed as it arrived; a thrown error was not.
  else if (result?.status === 'failed' && !sawError && result.error) err('error: ' + result.error);
  else if (!result && !outcome.interrupted) err('error: ' + errorMessage(outcome.error));
  return headlessExitCode(outcome);
}
