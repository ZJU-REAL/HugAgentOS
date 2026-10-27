/** ACP owns stdin/stdout; diagnostics and startup progress must use stderr. */
import { Readable, Writable } from 'node:stream';
import { ndJsonStream } from '@agentclientprotocol/sdk';
import { createAcpAgent, type AcpOptions } from '../acp/agent.js';

export async function runAcp(options: AcpOptions): Promise<number> {
  const runtime = createAcpAgent(options);
  const connection = runtime.app.connect(ndJsonStream(
    Writable.toWeb(process.stdout), Readable.toWeb(process.stdin) as unknown as ReadableStream<Uint8Array>,
  ));
  let exitCode = 0;
  const interrupt = () => { exitCode = 130; connection.close(); };
  process.on('SIGINT', interrupt);
  process.on('SIGTERM', interrupt);
  try { await connection.closed; }
  finally {
    process.off('SIGINT', interrupt);
    process.off('SIGTERM', interrupt);
    await runtime.close();
  }
  return exitCode;
}
