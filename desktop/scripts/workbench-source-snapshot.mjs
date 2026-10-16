import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream, lstatSync, readlinkSync, realpathSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";

const hash = bytes => createHash("sha256").update(bytes).digest("hex");
export function gitOutput(root, args) {
  const result = spawnSync("git", args, { cwd: root, encoding: "utf8", shell: false });
  if (result.error || result.status !== 0) throw new Error("Cannot inspect source checkout.");
  return result.stdout;
}

export function excludedSourcePath(path) {
  const parts = path.split("/");
  const name = parts.at(-1);
  return ["storage", "data"].includes(parts[0]) || parts.some(part => [".git", ".scratch", ".venv", "venv", "node_modules", "target",
    "dist", "generated", ".tauri", ".config", "__pycache__"].includes(part)) ||
    (name === ".env" || (name.startsWith(".env.") && !name.endsWith(".example"))) ||
    /\.(?:key|p12|pfx|keychain-db|pem)$/.test(name) || ["auth.json", "server.json"].includes(name);
}

export async function captureSource(checkout) {
  const root = realpathSync(checkout);
  if (realpathSync(gitOutput(root, ["rev-parse", "--show-toplevel"]).trim()) !== root) {
    throw new Error("Snapshot checkout must be the repository root.");
  }
  const baselineCommit = gitOutput(root, ["rev-parse", "HEAD"]).trim();
  const status = gitOutput(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]);
  const paths = [...new Set(gitOutput(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    .split("\0").filter(Boolean))].sort();
  const files = [], excludedPaths = [];
  for (const path of paths) {
    if (isAbsolute(path) || path.split("/").includes("..")) throw new Error("Unsafe Git source path.");
    if (excludedSourcePath(path)) { excludedPaths.push(path); continue; }
    const absolute = join(root, path);
    let stat;
    try { stat = lstatSync(absolute); } catch (error) {
      if (error.code !== "ENOENT") throw error;
      files.push({ path, deleted: true });
      continue;
    }
    // Never read a source file through an external directory symlink.
    const parent = realpathSync(resolve(absolute, ".."));
    const rel = relative(root, parent);
    if (rel.startsWith("..") || isAbsolute(rel)) throw new Error("Source path escapes checkout.");
    if (stat.isSymbolicLink()) {
      files.push({ path, kind: "symlink", target: readlinkSync(absolute), mode: "120000" });
    } else if (stat.isFile()) {
      const digest = createHash("sha256");
      for await (const chunk of createReadStream(absolute)) digest.update(chunk);
      files.push({ path, kind: "file", sha256: digest.digest("hex"), size: stat.size, mode: stat.mode & 0o111 ? "100755" : "100644" });
    } else {
      throw new Error("Unsupported source path kind: " + path);
    }
  }
  return { schemaVersion: 1, baselineCommit, sourceSha256: hash(JSON.stringify(files)),
    dirty: Boolean(status), dirtyStatusSha256: hash(status), files, excludedPaths,
    scope: "Git tracked and nonignored untracked source; secrets/data/build outputs excluded" };
}
