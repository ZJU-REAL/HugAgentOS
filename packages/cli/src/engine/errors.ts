/** Errors the engine layer raises; `exitCode` is what main.ts exits with when one reaches the top. */

export const EXIT = { ok: 0, failed: 1, usage: 2, setup: 3, interrupted: 130 } as const;

export class CliError extends Error {
  constructor(message: string, public readonly exitCode: number = EXIT.failed) {
    super(message);
    this.name = 'CliError';
  }
}

/** An HTTP call to HugAgentOS failed; `detail` is the server's error body, kept for display. */
export class HugAgentOSHttpError extends CliError {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: number,
    public readonly detail?: unknown,
  ) {
    super(message, EXIT.failed);
    this.name = 'HugAgentOSHttpError';
  }
}

/** The local engine or cloud login needs a one-time setup step before anything works. */
export class SetupRequiredError extends CliError {
  constructor(message: string) {
    super(message, EXIT.setup);
    this.name = 'SetupRequiredError';
  }
}

/** The engine for this mode cannot do what was asked (device mode lacks most of the API). */
export class UnsupportedError extends CliError {
  constructor(message: string) {
    super(message, EXIT.failed);
    this.name = 'UnsupportedError';
  }
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && (error.name === 'AbortError' || error.name === 'TimeoutError');
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
