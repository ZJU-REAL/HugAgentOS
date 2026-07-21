import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { validateDesktopReleaseTag } from "./desktop-version.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const releaseTag = process.argv[2];

try {
  const { version, expectedTag } = validateDesktopReleaseTag(
    desktopDir,
    releaseTag,
  );
  console.log(
    `[desktop] Release version validated: ${version} (${expectedTag})`,
  );
} catch (error) {
  console.error(
    `[desktop] ${error instanceof Error ? error.message : String(error)}`,
  );
  process.exitCode = 1;
}
