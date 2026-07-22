import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { setDesktopVersion } from "./desktop-version.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const version = process.argv[2];

try {
  const updated = setDesktopVersion(desktopDir, version);
  console.log(`[desktop] Version synchronized: ${updated}`);
} catch (error) {
  console.error(
    `[desktop] ${error instanceof Error ? error.message : String(error)}`,
  );
  process.exitCode = 1;
}
