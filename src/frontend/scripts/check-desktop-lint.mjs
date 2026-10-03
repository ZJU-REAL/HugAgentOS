import { ESLint } from "eslint";
import { readFile } from "node:fs/promises";
import { relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const baseline = JSON.parse(await readFile(new URL("desktop-lint-baseline.json", import.meta.url), "utf8"));
const eslint = new ESLint({ cwd: root });
const results = await eslint.lintFiles(["."]);
const failures = [];
for (const result of results) {
  const file = relative(root, result.filePath).replaceAll("\\", "/");
  const counts = {};
  for (const message of result.messages) {
    const rule = `${message.severity}:${message.ruleId ?? "parse"}`;
    counts[rule] = (counts[rule] ?? 0) + 1;
  }
  const strict = file.startsWith("src/components/desktop/")
    || ["src/stores/desktopCapabilityState.ts", "src/stores/deploymentModeStore.ts", "src/utils/syntaxHighlight.ts"].includes(file);
  for (const [rule, count] of Object.entries(counts)) {
    const allowed = strict ? 0 : baseline[file]?.[rule] ?? 0;
    if (count > allowed) failures.push(`${file}: ${rule} ${count} > ${allowed}`);
  }
}
if (failures.length) {
  console.error(failures.join("\n"));
  process.exitCode = 1;
} else console.log("Desktop lint clean; no increases over the recorded shared-frontend debt.");
