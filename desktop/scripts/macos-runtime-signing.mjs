import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, realpathSync, writeFileSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";

export function macRuntimeSigningTargets(root) {
  const files = [], bundles = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(path);
        if (/\.(app|framework|appex|xpc|mdimporter|qlgenerator)$/.test(path)) bundles.push(path);
      } else if (!entry.isSymbolicLink()) files.push(path);
    }
  };
  visit(root);
  // Signing a framework executable also seals its bundle. Sign its helpers first.
  files.sort((a, b) => b.split(sep).length - a.split(sep).length || a.localeCompare(b));
  return { files, bundles };
}

export function isBrowserRuntimeCode(root, path) {
  const name = relative(root, path).split(sep).join("/");
  return name.startsWith("native/browser/") || name.endsWith("/site-packages/playwright/driver/node");
}

export function refreshSignedBrowserManifest(root) {
  const manifestPath = join(root, "browser-runtime.json");
  if (!existsSync(manifestPath)) return;
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const executable = realpathSync(resolve(root, manifest.executable));
  const name = relative(realpathSync(root), executable);
  if (isAbsolute(name) || name === ".." || name.startsWith(`..${sep}`)) {
    throw new Error("Signed browser executable escapes the private runtime");
  }
  manifest.executable_sha256 = createHash("sha256").update(readFileSync(executable)).digest("hex");
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
}
