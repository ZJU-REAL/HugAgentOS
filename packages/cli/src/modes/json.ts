/**
 * `--output json`: every SSE event as one JSON line on stdout, unchanged, then
 * {"type":"cli_result","status":"completed|failed|cancelled","chat_id","run_id"} (DESIGN.md §8.4).
 * Policy notes go to stderr so stdout stays machine-readable.
 */
import { errorMessage } from '../engine/errors.js';
import { type HeadlessOptions, headlessExitCode, runHeadless } from '../engine/headless.js';

export async function runJson(options: HeadlessOptions): Promise<number> {
  const stdout = options.stdout ?? process.stdout;
  const stderr = options.stderr ?? process.stderr;
  const line = (value: unknown) => stdout.write(JSON.stringify(value) + '\n');
  const outcome = await runHeadless(options, {
    onEvent: line,
    onNote: (note) => stderr.write('hugagent: ' + note + '\n'),
  });
  const thrown = outcome.error === undefined ? undefined : errorMessage(outcome.error);
  if (thrown !== undefined) stderr.write('hugagent: ' + thrown + '\n');
  const status = outcome.result?.status ?? (outcome.interrupted ? 'cancelled' : 'failed');
  const error = outcome.result?.error ?? thrown;
  line({
    type: 'cli_result',
    status,
    chat_id: outcome.chatId ?? null,
    run_id: outcome.result?.runId ?? null,
    ...(status !== 'completed' && error ? { error } : {}),
  });
  return headlessExitCode(outcome);
}
