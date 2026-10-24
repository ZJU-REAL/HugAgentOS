import type { RunOptions } from '../engine/engine.js';
import { errorMessage } from '../engine/errors.js';
import { InteractiveApp } from '../tui/app.js';

/** pi-style TUI. Returns the process exit code. */
export async function runInteractive(options: RunOptions): Promise<number> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    process.stderr.write('hugagent: interactive mode needs a terminal; use -p for pipes and scripts\n');
    return 2;
  }
  const app = new InteractiveApp(options);

  // Engine failures surface as transcript lines; anything that escapes still must not leave
  // the terminal in raw mode.
  const onRejection = (reason: unknown) => {
    app.transcript.error(`内部错误：${errorMessage(reason)}`);
    app.tui.requestRender();
  };
  const onException = (error: Error) => {
    app.emergencyStop();
    process.stderr.write(`hugagent: ${error.stack ?? error.message}\n`);
    process.exit(1);
  };
  const onSignal = () => { void app.shutdown(130); };
  process.on('unhandledRejection', onRejection);
  process.on('uncaughtException', onException);
  process.on('SIGTERM', onSignal);
  process.on('SIGHUP', onSignal);
  try {
    return await app.start();
  } finally {
    process.off('unhandledRejection', onRejection);
    process.off('uncaughtException', onException);
    process.off('SIGTERM', onSignal);
    process.off('SIGHUP', onSignal);
  }
}
