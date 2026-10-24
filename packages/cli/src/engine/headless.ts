/**
 * One non-interactive turn, shared by the text and JSON output modes: picks the session, applies the
 * non-interactive approval policy (DESIGN.md §8.4: deny file_confirm unless --auto-approve, skip
 * every user_question) and turns Ctrl+C into a run cancel.
 */
import type { ChatEvent, Engine, FileDecision, QuestionAnswer } from './engine.js';
import { CliError, EXIT, errorMessage } from './errors.js';

export type TurnStatus = 'completed' | 'failed' | 'cancelled';

/** What main.ts hands the text and JSON modes. The engine may not be connected yet. */
export interface HeadlessOptions {
  engine: Engine;
  prompt: string;
  chatId?: string;
  continueLast?: boolean;
  model?: { providerId: string; modelName: string };
  autoApprove: boolean;
  stdout?: NodeJS.WritableStream;
  stderr?: NodeJS.WritableStream;
}

export interface TurnResult {
  status: TurnStatus;
  chatId: string;
  runId?: string;
  error?: string;
}

export interface TurnOptions {
  autoApprove: boolean;
  model?: { providerId: string; modelName: string };
  signal?: AbortSignal;
  onEvent(event: ChatEvent): void;
  /** One line about what the policy answered on the user's behalf. */
  onNote?(note: string): void;
  onRunId?(runId: string): void;
}

/** How one headless turn ended; exactly one of `result` and `error` is set. */
export interface HeadlessOutcome {
  chatId?: string;
  result?: TurnResult;
  /** Thrown while connecting, choosing the session or running the turn. */
  error?: unknown;
  interrupted: boolean;
}

/** Connect, choose the session and run one turn with Ctrl+C wired to cancel: the spine of `-p` and `--output json`. */
export async function runHeadless(
  options: HeadlessOptions,
  hooks: Pick<TurnOptions, 'onEvent' | 'onNote'>,
): Promise<HeadlessOutcome> {
  let chatId: string | undefined;
  const interrupt = new Interrupt(options.engine, () => chatId);
  try {
    await options.engine.connect();
    chatId = await chooseChat(options.engine, options.chatId, !!options.continueLast);
    const result = await runTurn(options.engine, chatId, options.prompt, {
      autoApprove: options.autoApprove,
      model: options.model,
      signal: interrupt.signal,
      onRunId: (runId) => { interrupt.runId = runId; },
      ...hooks,
    });
    return { chatId, result, interrupted: interrupt.interrupted };
  } catch (error) {
    return { chatId, error, interrupted: interrupt.interrupted };
  } finally {
    await interrupt.dispose();
  }
}

/** Exit code per DESIGN.md §8.4: 0 completed, 130 interrupted, the CliError's own code, else 1. */
export function headlessExitCode(outcome: HeadlessOutcome): number {
  if (outcome.result) {
    if (outcome.result.status === 'completed') return EXIT.ok;
    return outcome.result.status === 'cancelled' && outcome.interrupted ? EXIT.interrupted : EXIT.failed;
  }
  if (outcome.interrupted) return EXIT.interrupted;
  return outcome.error instanceof CliError ? outcome.error.exitCode : EXIT.failed;
}

/** -r <id> wins, then -c (latest session of this engine), else a new session. */
export async function chooseChat(engine: Engine, chatId: string | undefined, continueLast: boolean): Promise<string> {
  if (chatId) return chatId;
  if (continueLast) {
    const [latest] = await engine.listSessions(1);
    if (latest) return latest.chatId;
  }
  return (await engine.newSession()).chatId;
}

export function eventError(event: ChatEvent): string {
  const value = event.error ?? event.message ?? event.content;
  if (typeof value === 'string' && value) return value;
  if (value && typeof value === 'object') {
    const nested = (value as Record<string, unknown>).message;
    return typeof nested === 'string' ? nested : JSON.stringify(value);
  }
  return 'Run failed';
}

export async function runTurn(engine: Engine, chatId: string, message: string, options: TurnOptions): Promise<TurnResult> {
  const result: TurnResult = { status: 'completed', chatId };
  let failure: string | undefined;
  let endStatus: string | undefined;
  try {
    for await (const event of engine.send(chatId, message, { model: options.model, signal: options.signal })) {
      if (event.type === 'run_started' && typeof event.run_id === 'string' && !result.runId) {
        result.runId = event.run_id;
        options.onRunId?.(event.run_id);
      }
      options.onEvent(event);
      if (event.type === 'error') failure ??= eventError(event);
      else if (event.type === 'end' && typeof event.status === 'string') endStatus = event.status;
      else if (event.type === 'file_confirm' && typeof event.confirm_id === 'string' && !event.expired) {
        const decision: FileDecision = options.autoApprove ? 'allow' : 'deny';
        await engine.confirm(chatId, event.confirm_id, decision).then(
          () => options.onNote?.(`${decision === 'allow' ? 'approved' : 'denied'} ${describeConfirm(event)}` +
            (decision === 'deny' ? ' (pass --auto-approve to allow)' : '')),
          (error: unknown) => options.onNote?.(`could not answer approval ${event.confirm_id}: ${errorMessage(error)}`),
        );
      } else if (event.type === 'user_question' && typeof event.request_id === 'string') {
        const questions = Array.isArray(event.questions) ? event.questions : [];
        const answers: QuestionAnswer[] = questions
          .map((question) => (question && typeof question === 'object' ? (question as Record<string, unknown>).id : undefined))
          .filter((id): id is string => typeof id === 'string')
          .map((id) => ({ id, selected: [], skipped: true }));
        if (answers.length) {
          await engine.answer(chatId, event.request_id, answers).then(
            () => options.onNote?.(`skipped ${answers.length} question(s) from the agent (non-interactive)`),
            (error: unknown) => options.onNote?.(`could not skip question ${event.request_id}: ${errorMessage(error)}`),
          );
        }
      }
    }
  } catch (error) {
    if (options.signal?.aborted) return { ...result, status: 'cancelled' };
    return { ...result, status: 'failed', error: errorMessage(error) };
  }
  if (options.signal?.aborted || endStatus === 'cancelled') return { ...result, status: 'cancelled' };
  if (failure || endStatus === 'failed' || endStatus === 'needs_attention') {
    return { ...result, status: 'failed', error: failure ?? `Run ${endStatus}` };
  }
  return result;
}

function describeConfirm(event: ChatEvent): string {
  for (const key of ['title', 'summary', 'description', 'path', 'file_path', 'command', 'tool_name', 'action']) {
    const value = event[key];
    if (typeof value === 'string' && value.trim()) return value.trim().slice(0, 160);
  }
  return 'a pending action';
}

/**
 * Ctrl+C during a turn: abort following, cancel the run on the server, then let the mode exit 130.
 * A second Ctrl+C exits at once.
 */
export class Interrupt {
  readonly controller = new AbortController();
  runId?: string;
  private cancelling?: Promise<void>;

  constructor(private readonly engine: Engine, private readonly chatId: () => string | undefined) {
    process.on('SIGINT', this.handle);
  }

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  get interrupted(): boolean {
    return this.controller.signal.aborted;
  }

  private readonly handle = () => {
    if (this.cancelling) process.exit(130);
    this.controller.abort();
    this.cancelling = this.cancelRun();
  };

  private async cancelRun(): Promise<void> {
    const work = (async () => {
      let runId = this.runId;
      const chatId = this.chatId();
      if (!runId && chatId) runId = (await this.engine.activeRun(chatId).catch(() => null))?.runId;
      if (runId) await this.engine.cancel(runId);
    })().catch(() => {});
    await Promise.race([work, new Promise((resolve) => setTimeout(resolve, 5000).unref())]);
  }

  /** Wait for the cancel request to go out, and stop listening for Ctrl+C. */
  async dispose(): Promise<void> {
    process.off('SIGINT', this.handle);
    await this.cancelling;
  }
}
