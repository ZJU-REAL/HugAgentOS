#!/usr/bin/env node
/** hugagent entry: parse arguments, build the engine for --mode, run one output mode. */
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { configPath, loadConfig, parseMode, saveConfig, validateServerUrl } from './config.js';
import type { Mode } from './engine/engine.js';
import { CliError, EXIT, HugAgentOSHttpError, errorMessage } from './engine/errors.js';
import { buildEngine, engineFactory } from './engine/index.js';
import { HugAgentOSHttp } from './engine/http.js';
import { resolveServerCommand } from './engine/server-command.js';
import { CLI_VERSION } from './version.js';

const HELP = `hugagent — HugAgentOS in your terminal

Usage
  hugagent [options] [prompt]          interactive session (prompt is sent first)
  hugagent -p [options] [prompt]       run one turn and exit; reads stdin when no prompt
  hugagent --output rpc [options]      JSON-lines embedding protocol on stdin/stdout
  hugagent --output acp --mode local   native ACP v1 on stdin/stdout
  hugagent login [--server URL]        save a cloud server and API key
  hugagent logout                      forget the saved API key
  hugagent serve|onboard|doctor …      forwarded to hugagent-server

Options
  -p, --print [prompt]                 non-interactive; "-" or no prompt reads stdin
      --output text|json|rpc|acp       output format (default text)
      --mode local|cloud|device:<id>   where the agent runs (default: config defaultMode, else local)
  -c, --continue                       continue the latest session
  -r, --resume [chat_id]               resume a session (pick one when no id is given)
      --session <chat_id>              same as --resume <chat_id>
      --model <provider_id>:<model>    model for this run
      --server <url>                   cloud server (cloud and device modes)
      --permission-mode ask|auto|full  ACP initial session policy (default ask)
      --auto-approve                   non-interactive: allow file/command approvals (default deny)
  -h, --help                           show this help
  -v, --version                        print the version

Exit codes: 0 completed, 1 failed, 2 usage error, 3 setup required, 130 interrupted.
Environment: HUGAGENT_HOME, HUGAGENT_SERVER, HUGAGENT_API_KEY, HUGAGENT_ALLOW_HTTP=1,
  HUGAGENT_ENGINE_CMD (JSON array), HUGAGENT_REPO, HUGAGENT_PYTHON.
`;

type Output = 'text' | 'json' | 'rpc' | 'acp';

interface ParsedArgs {
  command: 'run' | 'help' | 'version' | 'login' | 'logout' | 'server';
  /** serve | onboard | doctor, with the arguments forwarded untouched. */
  serverCommand?: string;
  serverArgs?: string[];
  print: boolean;
  /** -p value or positional words; '-' means stdin. */
  prompt?: string;
  output: Output;
  mode?: Mode;
  continueLast: boolean;
  resume: boolean;
  chatId?: string;
  model?: { providerId: string; modelName: string };
  server?: string;
  autoApprove: boolean;
  permissionMode?: 'ask'|'auto'|'full';
}

const SERVER_COMMANDS = new Set(['serve', 'onboard', 'doctor']);
/** An optional -r value is taken as a chat id only when it looks like one, not like a prompt word. */
const CHAT_ID = /^(?=.*\d)[A-Za-z0-9_-]{6,100}$/;

function usage(message: string): never {
  throw new CliError(message + '\nRun `hugagent --help` for usage.', EXIT.usage);
}

function parseModel(value: string): { providerId: string; modelName: string } {
  const split = value.indexOf(':');
  if (split <= 0 || split === value.length - 1) usage(`--model expects <provider_id>:<model_name>, got "${value}"`);
  return { providerId: value.slice(0, split), modelName: value.slice(split + 1) };
}

function parseArgs(argv: string[]): ParsedArgs {
  const parsed: ParsedArgs = {
    command: 'run', print: false, output: 'text', continueLast: false, resume: false, autoApprove: false,
  };
  if (argv[0] && SERVER_COMMANDS.has(argv[0])) {
    return { ...parsed, command: 'server', serverCommand: argv[0], serverArgs: argv.slice(1) };
  }
  let args = argv;
  if (argv[0] === 'login' || argv[0] === 'logout') {
    parsed.command = argv[0];
    args = argv.slice(1);
  }
  const words: string[] = [];
  let printValue: string | undefined;
  for (let index = 0; index < args.length; index++) {
    let arg = args[index];
    let inline: string | undefined;
    if (arg === '--') {
      words.push(...args.slice(index + 1));
      break;
    }
    if (arg.startsWith('--') && arg.includes('=')) {
      inline = arg.slice(arg.indexOf('=') + 1);
      arg = arg.slice(0, arg.indexOf('='));
    }
    const next = args[index + 1];
    const takesValue = (name: string): string => {
      if (inline !== undefined) return inline;
      if (next === undefined || (next.startsWith('-') && next !== '-')) usage(`${name} needs a value`);
      index += 1;
      return next;
    };
    const optional = (accept: (value: string) => boolean): string | undefined => {
      if (inline !== undefined) return inline;
      if (next !== undefined && (!next.startsWith('-') || next === '-') && accept(next)) {
        index += 1;
        return next;
      }
      return undefined;
    };
    switch (arg) {
      case '-h': case '--help': parsed.command = 'help'; break;
      case '-v': case '--version': parsed.command = 'version'; break;
      case '-p': case '--print':
        parsed.print = true;
        printValue = optional(() => true) ?? printValue;
        break;
      case '--output': {
        const value = takesValue('--output');
        if (value !== 'text' && value !== 'json' && value !== 'rpc' && value !== 'acp') usage(`--output must be text, json, rpc or acp, got "${value}"`);
        parsed.output = value;
        break;
      }
      case '--permission-mode': {
        const value=takesValue('--permission-mode');
        if(value!=='ask' && value!=='auto' && value!=='full') usage('--permission-mode must be ask, auto or full');
        parsed.permissionMode=value;break;
      }
      case '--mode': {
        const value = takesValue('--mode');
        parsed.mode = parseMode(value) ?? usage(`--mode must be local, cloud or device:<id>, got "${value}"`);
        break;
      }
      case '-c': case '--continue': parsed.continueLast = true; break;
      case '-r': case '--resume':
        parsed.resume = true;
        parsed.chatId = optional((value) => CHAT_ID.test(value)) ?? parsed.chatId;
        break;
      case '--session': parsed.resume = true; parsed.chatId = takesValue('--session'); break;
      case '--model': parsed.model = parseModel(takesValue('--model')); break;
      case '--server': parsed.server = takesValue('--server'); break;
      case '--auto-approve': parsed.autoApprove = true; break;
      default:
        if (arg.startsWith('-') && arg !== '-') usage(`Unknown option ${arg}`);
        words.push(arg);
    }
  }
  if (parsed.command === 'login' || parsed.command === 'logout') {
    if (words.length) usage(`Unexpected argument for ${parsed.command}: ${words[0]}`);
    return parsed;
  }
  const parts = printValue !== undefined ? [printValue, ...words] : words;
  if (parts.length) parsed.prompt = parts.join(' ');
  if (parsed.continueLast && parsed.resume) usage('Use either --continue or --resume, not both');
  return parsed;
}


async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(typeof chunk === 'string' ? Buffer.from(chunk) : chunk as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

function ask(question: string, hidden = false): Promise<string> {
  const rl = createInterface({ input: process.stdin, output: process.stderr, terminal: true });
  if (hidden) {
    const internal = rl as unknown as { _writeToOutput: (text: string) => void };
    internal._writeToOutput = (text: string) => {
      if (text.includes(question)) process.stderr.write(question);
    };
  }
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      rl.close();
      if (hidden) process.stderr.write('\n');
      resolve(answer.trim());
    });
  });
}

async function login(args: ParsedArgs): Promise<number> {
  const config = loadConfig();
  const interactive = process.stdin.isTTY === true;
  let raw = args.server || process.env.HUGAGENT_SERVER?.trim() || '';
  if (!raw && interactive) raw = await ask(`Server URL${config.server ? ` [${config.server}]` : ''}: `) || config.server || '';
  if (!raw) raw = config.server ?? '';
  if (!raw) usage('hugagent login needs --server <url>');
  const server = validateServerUrl(raw);
  let apiKey = process.env.HUGAGENT_API_KEY?.trim() || '';
  if (!apiKey) {
    apiKey = interactive ? await ask('API key (Settings → API keys): ', true) : (await readStdin()).trim();
  }
  if (!apiKey) usage('No API key given');
  if (/[\r\n]/.test(apiKey)) usage('API key must be a single line');
  try {
    await new HugAgentOSHttp({ baseUrl: server, token: apiKey }).getToolApproval();
  } catch (error) {
    if (error instanceof HugAgentOSHttpError && (error.status === 401 || error.status === 403)) {
      throw new CliError(`${server} rejected this API key (${error.message})`);
    }
    throw new CliError(`Could not verify the key with ${server}: ${errorMessage(error)}`);
  }
  saveConfig({ ...config, server, apiKey });
  process.stderr.write(`Logged in to ${server} (saved to ${configPath()})\n`);
  return EXIT.ok;
}

function logout(): number {
  const config = loadConfig();
  if (!config.apiKey) {
    process.stderr.write('Not logged in\n');
    return EXIT.ok;
  }
  const { apiKey: _removed, ...rest } = config;
  saveConfig(rest);
  process.stderr.write(`Logged out${config.server ? ' of ' + config.server : ''}\n`);
  return EXIT.ok;
}

/** `hugagent serve|onboard|doctor`: hand the terminal to hugagent-server and pass its exit code on. */
function forwardToServer(command: string, args: string[]): Promise<number> {
  const base = resolveServerCommand();
  return new Promise((resolve, reject) => {
    const child = spawn(base[0], [...base.slice(1), command, ...args], { stdio: 'inherit' });
    // The terminal delivers Ctrl+C to the child too; let it decide how to stop.
    const ignore = () => {};
    process.on('SIGINT', ignore);
    child.on('error', (error) => {
      process.off('SIGINT', ignore);
      reject(new CliError(`Cannot run ${base[0]}: ${error.message}`));
    });
    child.on('exit', (code, signal) => {
      process.off('SIGINT', ignore);
      resolve(code ?? (signal === 'SIGINT' ? EXIT.interrupted : EXIT.failed));
    });
  });
}

async function main(argv: string[]): Promise<number> {
  const args = parseArgs(argv);
  switch (args.command) {
    case 'help': process.stdout.write(HELP); return EXIT.ok;
    case 'version': process.stdout.write(CLI_VERSION + '\n'); return EXIT.ok;
    case 'login': return login(args);
    case 'logout': return logout();
    case 'server': return forwardToServer(args.serverCommand!, args.serverArgs ?? []);
    default: break;
  }

  const config = loadConfig();
  let mode: Mode = 'local';
  if (args.mode) mode = args.mode;
  else if (typeof config.defaultMode === 'string') {
    mode = parseMode(config.defaultMode) ?? usage(`defaultMode in ${configPath()} must be local, cloud or device:<id>`);
  }
  const cwd = process.cwd();
  const status = (message: string) => process.stderr.write(message + '\n');

  if (args.output === 'acp') {
    if (args.print || args.prompt !== undefined || args.resume || args.continueLast || args.autoApprove)
      usage('--output acp takes sessions and permissions from its client');
    return (await import('./modes/acp.js')).runAcp({ mode, server: args.server, model: args.model, permissionMode:args.permissionMode });
  }

  if(args.permissionMode) usage('--permission-mode requires --output acp');
  if (args.output === 'rpc') {
    if (args.print || args.prompt !== undefined) usage('--output rpc takes commands on stdin, not a prompt');
    return (await import('./modes/rpc.js')).runRpc({ engine: buildEngine(mode, { cwd }, { server: args.server }) });
  }

  const interactive = !args.print && args.output !== 'json' &&
    process.stdin.isTTY === true && process.stdout.isTTY === true;
  if (!interactive) {
    if (args.resume && !args.chatId) usage('--resume needs a chat id in non-interactive mode (or use --continue)');
    let prompt = args.prompt;
    if (prompt === undefined || prompt === '-') {
      if (process.stdin.isTTY) usage('No prompt: pass one after -p or pipe it on stdin');
      prompt = await readStdin();
    }
    if (!prompt.trim()) usage('The prompt is empty');
    let engine;
    try {
      engine = buildEngine(mode, { cwd }, { server: args.server, onStatus: status });
    } catch (error) {
      // JSON consumers always get a closing cli_result, even when no engine could be built.
      if (args.output === 'json') {
        process.stdout.write(JSON.stringify({ type: 'cli_result', status: 'failed', chat_id: null, run_id: null, error: errorMessage(error) }) + '\n');
      }
      throw error;
    }
    const options = {
      engine, prompt, chatId: args.chatId, continueLast: args.continueLast, model: args.model, autoApprove: args.autoApprove,
    };
    return args.output === 'json'
      ? (await import('./modes/json.js')).runJson(options)
      : (await import('./modes/print.js')).runPrint(options);
  }

  // Load the TUI while the engine starts; a cold local engine takes seconds.
  const tui = import('./modes/interactive.js');
  const createEngine = engineFactory({ server: args.server });
  const engine = await engineFactory({ server: args.server, onStatus: status })(mode, { cwd });
  let chatId = args.chatId;
  if (args.continueLast) chatId = (await engine.listSessions(1))[0]?.chatId;
  const { runInteractive } = await tui;
  return runInteractive({
    engine,
    createEngine,
    cwd,
    chatId,
    model: args.model,
    prompt: args.prompt,
    pickSession: args.resume && !args.chatId,
  });
}

/** Exit only after piped stdout has drained (pipes are asynchronous on macOS). */
function exit(code: number): void {
  process.stdout.write('', () => process.exit(code));
}

main(process.argv.slice(2)).then(exit, (error: unknown) => {
  process.stderr.write('hugagent: ' + errorMessage(error) + '\n');
  exit(error instanceof CliError ? error.exitCode : EXIT.failed);
});
