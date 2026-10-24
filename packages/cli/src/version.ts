import { createRequire } from 'node:module';

/** This package's version, read once from package.json. */
export const CLI_VERSION: string =
  (createRequire(import.meta.url)('../package.json') as { version?: string }).version ?? '0.0.0';
