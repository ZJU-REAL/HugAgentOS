/**
 * `--output rpc`: embed hugagent in another program (DESIGN.md §8.4).
 *   stdin  {"id","method","params"}            one command per line; methods mirror Engine
 *   stdout {"id","result"} | {"id","error"}    one reply per command
 *          {"event":{…},"request":"<id>"}      events of a running send/resume, before its reply
 * Commands run concurrently, so `cancel`, `steer` and `confirm` work while a `send` is streaming.
 * A send replies when its run ends: {"status","chatId","runId"}. No approval policy is applied:
 * the client answers file_confirm / user_question itself.
 */
import { createInterface } from 'node:readline';
import type { ChatEvent, Engine, FileDecision, QuestionAnswer } from '../engine/engine.js';
import { CliError, EXIT, HugAgentOSHttpError, errorMessage } from '../engine/errors.js';

export interface RpcOptions {
  engine: Engine;
  stdin?: NodeJS.ReadableStream;
  stdout?: NodeJS.WritableStream;
}

type Params = Record<string, unknown>;
type Id = string | number | null;

class ParamError extends Error {}

function str(params: Params, key: string, required = true): string | undefined {
  const value = params[key];
  if (value === undefined || value === null) {
    if (required) throw new ParamError(`params.${key} is required`);
    return undefined;
  }
  if (typeof value !== 'string' || (required && !value)) throw new ParamError(`params.${key} must be a string`);
  return value;
}

function num(params: Params, key: string): number | undefined {
  const value = params[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'number' || !Number.isFinite(value)) throw new ParamError(`params.${key} must be a number`);
  return value;
}

function errorBody(error: unknown): Record<string, unknown> {
  const body: Record<string, unknown> = { message: errorMessage(error) };
  if (error instanceof ParamError) body.code = 'invalid_params';
  if (error instanceof HugAgentOSHttpError) {
    body.status = error.status;
    if (error.code !== undefined) body.code = error.code;
  }
  if (error instanceof CliError) body.exitCode = error.exitCode;
  return body;
}

const RPC_METHODS = [
  'info', 'connect', 'close', 'newSession', 'listSessions', 'history', 'fork', 'send', 'resume', 'activeRun',
  'cancel', 'steer', 'confirm', 'answer', 'compact', 'listModels', 'getPermissionMode', 'setPermissionMode',
] as const;

export async function runRpc(options: RpcOptions): Promise<number> {
  const { engine } = options;
  const stdin = options.stdin ?? process.stdin;
  const stdout = options.stdout ?? process.stdout;
  const write = (value: unknown) => stdout.write(JSON.stringify(value) + '\n');
  const streams = new Map<Id, { controller: AbortController; runId?: string }>();
  const pending = new Set<Promise<void>>();

  const stream = async (id: Id, source: AsyncIterable<ChatEvent>, chatId?: string) => {
    let runId: string | undefined;
    let status = 'completed';
    for await (const event of source) {
      if (event.type === 'run_started' && typeof event.run_id === 'string' && !runId) {
        runId = event.run_id;
        const entry = streams.get(id);
        if (entry) entry.runId = runId;
      }
      if (event.type === 'error') status = 'failed';
      if (event.type === 'end' && typeof event.status === 'string' && event.status !== 'completed') {
        status = event.status === 'cancelled' ? 'cancelled' : 'failed';
      }
      write({ event, request: id });
    }
    return { status, chatId: chatId ?? null, runId: runId ?? null };
  };

  const call = async (id: Id, method: string, params: Params): Promise<unknown> => {
    if (method === 'info') return engine.info;
    if (method === 'close') return engine.close();
    await engine.connect();
    switch (method) {
      case 'connect': return engine.info;
      case 'newSession': return engine.newSession(str(params, 'title', false));
      case 'listSessions': return engine.listSessions(num(params, 'limit'));
      case 'history': return engine.history(str(params, 'chatId')!, num(params, 'limit'));
      case 'fork': return engine.fork(str(params, 'chatId')!, str(params, 'messageId', false));
      case 'send': {
        const chatId = str(params, 'chatId')!;
        const controller = new AbortController();
        streams.set(id, { controller });
        try {
          return await stream(id, engine.send(chatId, str(params, 'message')!, {
            model: params.model as { providerId: string; modelName: string } | undefined,
            attachments: params.attachments as { file_id: string; name: string; mime_type: string }[] | undefined,
            signal: controller.signal,
          }), chatId);
        } finally {
          streams.delete(id);
        }
      }
      case 'resume': {
        const controller = new AbortController();
        streams.set(id, { controller, runId: str(params, 'runId')! });
        try {
          return await stream(id, engine.resume(str(params, 'runId')!, num(params, 'fromOffset'), controller.signal));
        } finally {
          streams.delete(id);
        }
      }
      case 'activeRun': return engine.activeRun(str(params, 'chatId')!);
      case 'cancel': return engine.cancel(str(params, 'runId')!);
      case 'steer': return engine.steer(str(params, 'runId')!, str(params, 'message')!,
        str(params, 'mode', false) as 'steer' | 'followUp' | 'nextRun' | undefined);
      case 'confirm': return engine.confirm(str(params, 'chatId')!, str(params, 'confirmId')!,
        str(params, 'decision')! as FileDecision, str(params, 'optionId', false));
      case 'answer': {
        if (!Array.isArray(params.answers)) throw new ParamError('params.answers must be an array');
        return engine.answer(str(params, 'chatId')!, str(params, 'requestId')!, params.answers as QuestionAnswer[]);
      }
      case 'compact': return engine.compact(str(params, 'chatId')!);
      case 'listModels': return engine.listModels();
      case 'getPermissionMode': return engine.getPermissionMode();
      case 'setPermissionMode': {
        const mode = str(params, 'mode')!;
        if (mode !== 'ask' && mode !== 'auto' && mode !== 'full') throw new ParamError('params.mode must be ask, auto or full');
        return engine.setPermissionMode(mode);
      }
      default:
        throw new ParamError(`Unknown method ${method}; expected one of ${RPC_METHODS.join(', ')}`);
    }
  };

  const handle = (line: string) => {
    if (!line.trim()) return;
    let request: unknown;
    try {
      request = JSON.parse(line);
    } catch {
      write({ id: null, error: { message: 'Invalid JSON', code: 'parse_error' } });
      return;
    }
    const record = request && typeof request === 'object' && !Array.isArray(request) ? request as Record<string, unknown> : {};
    const id = (typeof record.id === 'string' || typeof record.id === 'number' ? record.id : null) as Id;
    const method = record.method;
    const params = record.params && typeof record.params === 'object' && !Array.isArray(record.params) ? record.params as Params : {};
    if (typeof method !== 'string') {
      write({ id, error: { message: 'method must be a string', code: 'invalid_request' } });
      return;
    }
    const work = call(id, method, params).then(
      (result) => { write({ id, result: result ?? null }); },
      (error: unknown) => { write({ id, error: errorBody(error) }); },
    );
    pending.add(work);
    void work.finally(() => pending.delete(work));
  };

  // Ctrl+C: stop reading, cancel every run being followed, exit 130.
  let interrupted = false;
  let cancelling: Promise<unknown> | undefined;
  const lines = createInterface({ input: stdin, crlfDelay: Infinity });
  const onSigint = () => {
    if (interrupted) process.exit(EXIT.interrupted);
    interrupted = true;
    const runs = [...streams.values()];
    for (const entry of runs) entry.controller.abort();
    cancelling = Promise.race([
      Promise.allSettled(runs.flatMap((entry) => (entry.runId ? [engine.cancel(entry.runId)] : []))),
      new Promise((resolve) => setTimeout(resolve, 5000).unref()),
    ]);
    lines.close();
  };
  process.on('SIGINT', onSigint);
  try {
    for await (const line of lines) handle(line);
    // stdin closed: finish what was asked, then leave.
    while (pending.size) await Promise.allSettled([...pending]);
    await cancelling;
  } finally {
    process.off('SIGINT', onSigint);
    await engine.close().catch(() => {});
  }
  return interrupted ? EXIT.interrupted : EXIT.ok;
}
