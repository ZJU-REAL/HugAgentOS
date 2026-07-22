import { appendFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { resolveDesktopReleaseTag } from "./desktop-version.mjs";

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const releaseTag = process.argv[2];

try {
  const { version, expectedTag } = resolveDesktopReleaseTag(
    desktopDir,
    releaseTag,
  );
  console.log(
    `[desktop] Release version validated: ${version} (${expectedTag})`,
  );
  if (process.env.GITHUB_OUTPUT) {
    appendFileSync(
      process.env.GITHUB_OUTPUT,
      `version=${version}\nrelease_tag=${expectedTag}\n`,
      "utf8",
    );
  }
} catch (error) {
  console.error(
    `[desktop] ${error instanceof Error ? error.message : String(error)}`,
  );
  process.exitCode = 1;
}
